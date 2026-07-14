from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import CycloError
from .state import Instance, StateStore
from .team import Team
from .vendor_gateway import auth as gateway_auth
from .vendor_gateway import commands as gateway_commands
from .vendor_gateway import docker as gateway_docker
from .vendor_gateway import gateway as gateway_runtime
from .vendor_gateway import source as gateway_source


@dataclass(frozen=True)
class GatewayClient:
    project_id: str
    name: str
    generation: str


@dataclass(frozen=True)
class GatewayConfig:
    name: str
    gateway_image: str
    gateway_container: str
    gateway_network: str
    store_volume: str
    host_models_json: Path
    client_registry_dir: Path


@dataclass(frozen=True)
class GatewaySession:
    container: str
    network: str
    catalog: dict[str, dict]


class CredentialGateway:
    def __init__(
        self,
        store: StateStore,
        *,
        gateway_image: str = "cyclo-gateway:local",
        store_volume: str = "cyclo-gateway-store",
    ) -> None:
        self.store = store
        self.gateway_image = gateway_image
        self.store_volume = store_volume
        # These are Cyclo-owned modules and packaged build contexts.
        self.gateway = gateway_runtime
        self.auth = gateway_auth
        self.runner_docker = gateway_docker
        self.commands = gateway_commands
        self.source = gateway_source
        self._last_catalog: dict[str, dict] = {}

    @property
    def container_name(self) -> str:
        return self.gateway.gateway_container_name(self.store.gateway_registry)

    @property
    def network_name(self) -> str:
        return self.gateway.gateway_network_name(self.store.gateway_registry)

    @property
    def container_id(self) -> str:
        return self.gateway.gateway_container_id(self.container_name)

    @property
    def host_pi_agent_dir(self) -> Path:
        return self.auth.resolve_host_pi_agent_dir(Path.home())

    def config(self) -> GatewayConfig:
        return GatewayConfig(
            name="cyclo",
            gateway_image=self.gateway_image,
            gateway_container=self.container_name,
            gateway_network=self.network_name,
            store_volume=self.store_volume,
            host_models_json=self.host_pi_agent_dir / "models.json",
            client_registry_dir=self.gateway.host_client_registry_dir(self.store.gateway_registry),
        )

    def ensure_runtime_image(self, image: str, *, build: bool = False) -> None:
        fingerprint = self.source.source_fingerprint()
        current = (
            self.runner_docker.docker_image_exists(image)
            and self.runner_docker.docker_image_label(image, self.commands.SOURCE_FINGERPRINT_LABEL)
            == fingerprint
        )
        if build or not current:
            command = self.commands.docker_build_command(image, fingerprint)
            if self.runner_docker.run_command(command) != 0:
                raise CycloError(f"failed to build Cyclo runtime image: {image}")

    def _clients(
        self, instances: Iterable[Instance]
    ) -> tuple[
        list[GatewayClient],
        dict[str, tuple[str, ...]],
        dict[str, tuple[str, ...]],
    ]:
        clients: list[GatewayClient] = []
        provider_scopes: dict[str, tuple[str, ...]] = {}
        model_scopes: dict[str, tuple[str, ...]] = {}
        for instance in instances:
            if not instance.active:
                continue
            clients.append(
                GatewayClient(
                    project_id=instance.id,
                    name=instance.team_name,
                    generation=instance.generation,
                )
            )
            provider_scopes[instance.id] = tuple(instance.providers)
            model_scopes[instance.id] = tuple(instance.models)
        return clients, provider_scopes, model_scopes

    def reconcile(self, instances: Iterable[Instance], *, build: bool = False) -> dict[str, str]:
        clients, provider_scopes, model_scopes = self._clients(instances)
        tokens = self.gateway.prepare_client_registry(
            self.store.gateway_registry,
            clients,
            allowed_providers=provider_scopes,
            allowed_models=model_scopes,
        )
        admin_token = self.gateway.shared_token(self.store.gateway_registry)
        self._last_catalog = self.gateway.start_gateway(self.config(), admin_token, build=build)
        container_id = self.gateway.gateway_container_id(self.container_name)
        restart_rc, _restart_output = self.runner_docker.run_command_capture(
            ["docker", "update", "--restart", "unless-stopped", container_id]
        )
        if restart_rc != 0:
            raise CycloError("failed to set the gateway container restart policy")
        return tokens

    def catalog(self, instances: Iterable[Instance], *, build: bool = False) -> dict[str, dict]:
        self.reconcile(instances, build=build)
        return self._last_catalog

    def prepare_instance(
        self,
        instance: Instance,
        team: Team,
        project: Path,
        active_instances: Iterable[Instance],
        *,
        build_gateway: bool = False,
    ) -> GatewaySession:
        tokens = self.reconcile(active_instances, build=build_gateway)
        token = tokens.get(instance.id)
        if not token:
            raise CycloError(f"gateway did not issue a token for Cyclo instance {instance.id}")
        port = self.gateway.published_port(self.container_name)
        catalog = self.gateway.fetch_provider_catalog(port, token)
        self._validate_models(team, catalog)
        pi_root = self.store.pi_root(instance.id)
        temporary = self.store.new_tree(pi_root)
        try:
            agent_dir = temporary / "agent"
            agent_dir.mkdir(mode=0o700)
            first = team.agents[0]
            settings = {
                "defaultProvider": first.provider,
                "defaultModel": first.model_id,
                "defaultThinkingLevel": "xhigh",
                "packages": list(self.auth.PI_PACKAGES),
            }
            models = self.auth.projected_models_json(
                catalog,
                self.auth.gateway_base_url(self.container_name),
                token,
            )
            (agent_dir / "settings.json").write_text(
                json.dumps(settings, indent=2) + "\n", encoding="utf-8"
            )
            (agent_dir / "models.json").write_text(
                json.dumps(models, indent=2) + "\n", encoding="utf-8"
            )
            os.chmod(agent_dir / "settings.json", 0o600)
            os.chmod(agent_dir / "models.json", 0o600)
            self.store.replace_tree(temporary, pi_root)
        except Exception:
            self.store._remove_tree(temporary)
            raise
        return GatewaySession(container=self.container_name, network=self.network_name, catalog=catalog)

    @staticmethod
    def _validate_models(team: Team, catalog: dict[str, dict]) -> None:
        for agent in team.agents:
            provider = catalog.get(agent.provider)
            if not isinstance(provider, dict):
                raise CycloError(
                    f"agent {agent.name} requests unprovisioned gateway provider {agent.provider!r}"
                )
            models = provider.get("models")
            if not isinstance(models, list) or not models:
                raise CycloError(
                    f"gateway provider {agent.provider!r} returned no usable model catalogue"
                )
            available = {
                model["id"]
                for model in models
                if isinstance(model, dict)
                and isinstance(model.get("id"), str)
                and model["id"]
            }
            if agent.model_id not in available:
                raise CycloError(
                    f"agent {agent.name} requests model {agent.model!r}, which is not in the gateway catalog"
                )

    def usage(self) -> dict[str, object]:
        return self.gateway.fetch_usage(self.store.gateway_registry)

    def rotate_client_token(self, identifier: str) -> None:
        path = self.gateway.client_token_path(self.store.gateway_registry, identifier)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise CycloError(f"cannot rotate gateway capability for {identifier}: {exc}") from exc
