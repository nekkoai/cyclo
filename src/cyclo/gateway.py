from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import CycloError
from .state import StateStore
from .credential_gateway import auth as gateway_auth
from .credential_gateway import docker as gateway_docker
from .credential_gateway import gateway as gateway_runtime


@dataclass(frozen=True)
class GatewayConfig:
    name: str
    gateway_image: str
    gateway_container: str
    gateway_network: str
    store_volume: str
    host_models_json: Path
    client_registry_dir: Path


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

    def _set_restart_policy(self) -> str:
        container_id = self.gateway.gateway_container_id(self.container_name)
        restart_rc, _restart_output = self.runner_docker.run_command_capture(
            ["docker", "update", "--restart", "unless-stopped", container_id]
        )
        if restart_rc != 0:
            raise CycloError("failed to set the gateway container restart policy")
        return container_id

    def catalog(self) -> dict[str, dict]:
        """Read the concrete gateway catalogue without provisioning anything."""

        token_path = self.store.gateway_registry / "gateway-token"
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CycloError(f"cannot read gateway token {token_path}: {exc}") from exc
        if not token:
            raise CycloError(f"gateway token is empty: {token_path}")
        port = self.gateway.published_port(self.container_name)
        return self.gateway.fetch_provider_catalog(port, token)

    def validate_running(self) -> tuple[str, str, int]:
        """Validate the current gateway boundary without provisioning it."""

        token_path = self.store.gateway_registry / "gateway-token"
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CycloError(f"cannot read gateway token {token_path}: {exc}") from exc
        if not token:
            raise CycloError(f"gateway token is empty: {token_path}")
        return self.gateway.validate_running_gateway(self.config(), token)

    def validate_login(self) -> None:
        """Fail closed if a running writer on this store is incompatible."""

        self.gateway.validate_login_store_gateways(
            self.store_volume,
            configured_container=self.container_name,
            validate_config=self.validate_running,
        )

    def restart(self, *, build: bool = False) -> dict[str, dict]:
        """Explicitly replace the gateway without rewriting its client registry."""

        admin_token = self.gateway.shared_token(self.store.gateway_registry)
        catalog = self.gateway.start_gateway(
            self.config(),
            admin_token,
            build=build,
            force_restart=True,
        )
        self._set_restart_policy()
        self._last_catalog = catalog
        return catalog

    def usage(self) -> dict[str, object]:
        return self.gateway.fetch_usage(self.store.gateway_registry)
