from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import stat
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

from .credential_gateway import auth as gateway_auth
from .credential_gateway import gateway as gateway_runtime
from .errors import CycloError
from .provider_runtime import ProviderIdentity, ProviderRuntime
from .runtime_container import (
    RuntimeContainer,
    provider_runtime_base_url,
)
from .state import Instance, validate_instance_id
from .team import Team


class ProviderService(RuntimeContainer):
    """Orchestrate runtime capabilities, provider readiness, and team projection."""

    def _team_local_addresses(
        self,
        instances: Iterable[Instance],
    ) -> dict[str, tuple[str, ...]]:
        """Return the runtime interface addresses for each private team network."""

        info = self._owned_container()
        if info is None:
            return {}
        settings = info.get("NetworkSettings")
        networks = settings.get("Networks") if isinstance(settings, Mapping) else None
        if not isinstance(networks, Mapping):
            raise CycloError("cannot inspect provider-runtime Docker networks")
        result: dict[str, tuple[str, ...]] = {}
        for instance in instances:
            network = networks.get(instance.network_name)
            if not isinstance(network, Mapping):
                result[instance.id] = ()
                continue
            addresses: list[str] = []
            for key in ("IPAddress", "GlobalIPv6Address"):
                raw = network.get(key)
                if not isinstance(raw, str) or not raw:
                    continue
                try:
                    selected = str(ipaddress.ip_address(raw))
                except ValueError as exc:
                    raise CycloError(
                        f"invalid provider-runtime address on {instance.network_name}: "
                        f"{raw!r}"
                    ) from exc
                if selected not in addresses:
                    addresses.append(selected)
            result[instance.id] = tuple(addresses)
        return result

    def _existing_provider_clients(self) -> list[dict[str, object]]:
        return [
            record
            for record in self._read_client_registry(
                self.state_root / "clients.json", "provider runtime"
            )
            if record.get("kind") == "provider"
        ]

    def merged_provider_clients(
        self, records: Iterable[Mapping[str, object]]
    ) -> tuple[dict[str, object], ...]:
        existing = self._existing_provider_clients()
        by_prefix = {
            record["provider_prefix"]: dict(record)
            for record in existing
            if isinstance(record.get("provider_prefix"), str)
        }
        order = [str(record["provider_prefix"]) for record in existing]
        for record in records:
            selected = dict(record)
            prefix = selected.get("provider_prefix")
            if not isinstance(prefix, str) or not prefix:
                raise CycloError("invalid provider-runtime provider client prefix")
            if prefix not in by_prefix:
                order.append(prefix)
            by_prefix[prefix] = selected
        return tuple(by_prefix[prefix] for prefix in order)

    def provider_client_prefixes(self) -> tuple[str, ...]:
        return tuple(
            str(record["provider_prefix"])
            for record in self._existing_provider_clients()
            if isinstance(record.get("provider_prefix"), str)
        )

    def provider_clients(self) -> tuple[dict[str, object], ...]:
        return tuple(self._existing_provider_clients())

    @staticmethod
    def _read_client_registry(
        path: Path, label: str
    ) -> list[dict[str, object]]:
        descriptor = -1
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise CycloError(
                    f"{label} client registry is not a regular file: {path}"
                )
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
            ):
                raise CycloError(
                    f"{label} client registry changed while reading: {path}"
                )
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                raw = stream.read(4 * 1024 * 1024 + 1)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise CycloError(
                f"cannot read {label} client registry {path}: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(raw) > 4 * 1024 * 1024:
            raise CycloError(f"{label} client registry is too large: {path}")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CycloError(
                f"cannot read {label} client registry {path}: {exc}"
            ) from exc
        if (
            not isinstance(document, dict)
            or document.get("version") != 1
            or not isinstance(document.get("clients"), list)
        ):
            raise CycloError(f"invalid {label} client registry: {path}")
        records: list[dict[str, object]] = []
        seen: set[str] = set()
        for raw in document["clients"]:
            if not isinstance(raw, dict):
                raise CycloError(f"invalid {label} client registry: {path}")
            client_id = raw.get("client_id")
            if not isinstance(client_id, str) or not client_id or client_id in seen:
                raise CycloError(f"invalid {label} client registry: {path}")
            seen.add(client_id)
            records.append(dict(raw))
        return records

    @staticmethod
    def _write_client_registry(
        path: Path,
        records: Iterable[Mapping[str, object]],
        *,
        public_hashes: bool = False,
    ) -> None:
        directory_mode = 0o755 if public_hashes else 0o700
        file_mode = 0o644 if public_hashes else 0o600
        path.parent.mkdir(parents=True, mode=directory_mode, exist_ok=True)
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise CycloError(
                f"refusing unsafe client registry directory: {path.parent}"
            )
        os.chmod(path.parent, directory_mode)
        document = {
            "version": 1,
            "clients": sorted(
                (dict(record) for record in records),
                key=lambda record: str(record.get("client_id", "")),
            ),
        }
        temporary = path.with_name(
            f".{path.name}.tmp.{os.getpid()}.{os.urandom(6).hex()}"
        )
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                file_mode,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
                stream.flush()
                # Apply the final mode before publication. After os.replace(),
                # no fallible operation may turn a successful authority change
                # into an unacknowledged error.
                os.fchmod(stream.fileno(), file_mode)
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise CycloError(f"cannot publish client registry {path}: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _concrete_model_resolver(self):
        definitions = {
            definition.prefix: definition for definition in self.host_config.load()
        }

        def resolve(requested: Iterable[str]) -> tuple[str, ...]:
            resolved: list[str] = []
            visiting: set[str] = set()

            def expand(model: str) -> None:
                prefix, separator, model_id = model.partition("/")
                if not separator or not prefix or not model_id:
                    raise CycloError(f"invalid runtime model scope: {model!r}")
                definition = definitions.get(prefix)
                if definition is None:
                    if model not in resolved:
                        resolved.append(model)
                    return
                if prefix in visiting:
                    raise CycloError(
                        f"cyclic host provider dependency while resolving {model!r}"
                    )
                visiting.add(prefix)
                try:
                    for provider_input in definition.inputs:
                        expand(provider_input)
                finally:
                    visiting.remove(prefix)

            for model in requested:
                if not isinstance(model, str):
                    raise CycloError("invalid non-string runtime model scope")
                expand(model)
            return tuple(resolved)

        return resolve

    def _gateway_client_record(
        self,
        record: Mapping[str, object],
        resolve_models,
    ) -> dict[str, object]:
        models = record.get("models")
        if not isinstance(models, list) or any(
            not isinstance(model, str) for model in models
        ):
            raise CycloError("invalid provider-runtime client model scopes")
        concrete = resolve_models(models)
        selected = dict(record)
        selected.pop("local_addresses", None)
        selected["models"] = list(concrete)
        selected["providers"] = list(
            dict.fromkeys(model.partition("/")[0] for model in concrete)
        )
        return selected

    @staticmethod
    def _bridge_client_records(
        old_runtime: Iterable[Mapping[str, object]],
        new_runtime: Iterable[Mapping[str, object]],
        old_gateway: Iterable[Mapping[str, object]],
        new_gateway: Iterable[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        """Keep only capabilities safe on both sides of a registry transition."""

        old_gateway_by_id = {
            str(record.get("client_id")): dict(record) for record in old_gateway
        }
        new_gateway_by_id = {
            str(record.get("client_id")): dict(record) for record in new_gateway
        }
        new_runtime_by_id = {
            str(record.get("client_id")): dict(record) for record in new_runtime
        }
        bridge: list[dict[str, object]] = []
        for raw in old_runtime:
            record = dict(raw)
            client_id = str(record.get("client_id"))
            if new_runtime_by_id.get(client_id) != record:
                continue
            # Provider-component capabilities terminate at the runtime. A team
            # capability is safe to retain only if its concrete gateway grant is
            # also byte-for-byte structurally unchanged.
            if record.get("kind") == "team" and (
                old_gateway_by_id.get(client_id)
                != new_gateway_by_id.get(client_id)
            ):
                continue
            bridge.append(record)
        return bridge

    @staticmethod
    def _validate_combined_clients(
        teams: Iterable[Mapping[str, object]],
        providers: Iterable[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        combined = [dict(record) for record in (*tuple(teams), *tuple(providers))]
        seen: set[str] = set()
        for record in combined:
            client_id = record.get("client_id")
            token_hash = record.get("token_sha256")
            if (
                not isinstance(client_id, str)
                or not client_id
                or client_id in seen
                or not isinstance(token_hash, str)
                or len(token_hash) != 64
                or any(character not in "0123456789abcdef" for character in token_hash)
                or not isinstance(record.get("providers"), list)
                or not isinstance(record.get("models"), list)
            ):
                raise CycloError("invalid provider-runtime client record")
            seen.add(client_id)
        return combined

    def update_clients(
        self,
        instances: Iterable[Instance],
        *,
        provider_clients: Iterable[Mapping[str, object]] | None = None,
        apply_runtime: bool = True,
    ) -> dict[str, str]:
        selected_instances = tuple(instances)
        local_addresses = (
            self._team_local_addresses(selected_instances)
            if apply_runtime
            else {}
        )
        tokens: dict[str, str] = {}
        document: list[dict[str, object]] = []
        active_instances = sorted(
            (instance for instance in selected_instances if instance.active),
            key=lambda item: item.id,
        )
        for instance in active_instances:
            # The raw capability remains only in private runtime state. Its hash
            # is published to both boundaries because direct leaf requests keep
            # the original team bearer when runtime forwards them to gateway.
            identifier = validate_instance_id(instance.id)
            token = self._stable_token(
                self.state_root / "client-tokens" / f"{identifier}.token"
            )
            tokens[instance.id] = token
            document.append(
                {
                    "client_id": instance.id,
                    "kind": "team",
                    "provider_prefix": None,
                    "team_id": instance.team_name,
                    "binding_generation": instance.generation or None,
                    "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    "providers": list(instance.providers),
                    "models": list(instance.models),
                    "local_addresses": list(
                        local_addresses.get(instance.id, ())
                    ),
                    "expires_at": None,
                    "enabled": True,
                    "revoked": False,
                }
            )
        retained_runtime = (
            self._existing_provider_clients()
            if provider_clients is None
            else [dict(record) for record in provider_clients]
        )
        for record in retained_runtime:
            if record.get("kind") != "provider":
                raise CycloError("invalid provider-runtime provider client record")
        runtime_records = self._validate_combined_clients(
            document, retained_runtime
        )
        gateway_path = gateway_runtime.host_client_registry_path(
            self.store.gateway_registry
        )
        resolve_models = self._concrete_model_resolver()
        gateway_teams = [
            self._gateway_client_record(record, resolve_models)
            for record in document
        ]
        gateway_records = self._validate_combined_clients(
            gateway_teams, ()
        )
        runtime_path = self.state_root / "clients.json"
        old_runtime = self._read_client_registry(
            runtime_path, "provider runtime"
        )
        old_gateway = self._read_client_registry(
            gateway_path, "credential gateway"
        )
        bridge_records = self._bridge_client_records(
            old_runtime,
            runtime_records,
            old_gateway,
            gateway_records,
        )
        # Three-phase publication fails closed at every interruption point:
        # revoke old/changed runtime grants, publish the final gateway grants,
        # then let the runtime accept new/changed capabilities.
        if apply_runtime:
            with self.capability_update_guard():
                self._write_client_registry(runtime_path, bridge_records)
                self.reload_control(require_current=False)
        else:
            self._write_client_registry(runtime_path, bridge_records)
        self._write_client_registry(
            gateway_path, gateway_records, public_hashes=True
        )
        if apply_runtime:
            with self.capability_update_guard():
                self._write_client_registry(runtime_path, runtime_records)
                self.reload_control()
        else:
            self._write_client_registry(runtime_path, runtime_records)
        return tokens

    def remove_provider_clients(self, prefixes: Iterable[str]) -> None:
        removed = set(prefixes)
        runtime_path = self.state_root / "clients.json"
        runtime_records = [
            record
            for record in self._read_client_registry(
                runtime_path, "provider runtime"
            )
            if not (
                record.get("kind") == "provider"
                and record.get("provider_prefix") in removed
            )
        ]
        checked_runtime = self._validate_combined_clients(
            (record for record in runtime_records if record.get("kind") != "provider"),
            (record for record in runtime_records if record.get("kind") == "provider"),
        )
        with self.capability_update_guard():
            self._write_client_registry(runtime_path, checked_runtime)
            self.reload_control(require_current=False)

    def rotate_client_token(self, identifier: str) -> None:
        identifier = validate_instance_id(identifier)
        path = self.state_root / "client-tokens" / f"{identifier}.token"
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise CycloError(
                f"cannot rotate provider-runtime capability for {identifier}: {exc}"
            ) from exc

    def catalog(
        self,
        *,
        refresh: bool = False,
    ) -> dict[str, dict]:
        if refresh:
            self.refresh_catalog_control()
        data = self._request("/providers")
        if not isinstance(data, dict):
            raise CycloError("provider runtime catalog was not a JSON object")
        return data  # type: ignore[return-value]

    @staticmethod
    def _instance_catalog(
        instance: Instance,
        catalog: Mapping[str, object],
    ) -> dict[str, dict]:
        allowed_providers = set(instance.providers)
        allowed_models = set(instance.models)
        selected: dict[str, dict] = {}
        for prefix, raw in catalog.items():
            if "*" not in allowed_providers and prefix not in allowed_providers:
                continue
            if not isinstance(raw, Mapping) or not isinstance(raw.get("models"), list):
                continue
            models = [
                model
                for model in raw["models"]
                if isinstance(model, Mapping)
                and isinstance(model.get("id"), str)
                and (
                    "*" in allowed_models
                    or f"{prefix}/{model['id']}" in allowed_models
                )
            ]
            if not models:
                continue
            selected[prefix] = {**raw, "models": models}
        return selected

    @staticmethod
    def _registered_timestamp(value: object) -> float | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            selected = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if selected.tzinfo is None:
            return None
        return selected.timestamp()

    @staticmethod
    def _provider_diagnostics(
        runtime: ProviderRuntime, identity: ProviderIdentity
    ) -> str:
        try:
            status = runtime.status(identity)
            state = (
                "running"
                if status.container_running
                else "stopped"
                if status.container_exists
                else "absent"
            )
            status_detail = f"container={state}"
        except CycloError as exc:
            status_detail = f"container status unavailable: {exc}"
        try:
            logs = runtime.logs_tail(identity, lines=40).strip()
            log_detail = (
                f"; provider logs:\n{logs[-8192:]}"
                if logs
                else "; provider logs are empty"
            )
        except CycloError as exc:
            log_detail = f"; provider logs unavailable: {exc}"
        return status_detail + log_detail

    def wait_provider(
        self,
        prefix: str,
        generation: str,
        *,
        runtime: ProviderRuntime,
        identity: ProviderIdentity,
        registered_after: float | None = None,
        timeout: float = 45.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        last_problem = "no catalog entry"
        while time.monotonic() < deadline:
            try:
                container_running = runtime.container_running(identity)
            except CycloError as exc:
                last_problem = str(exc)
            else:
                if not container_running:
                    detail = self._provider_diagnostics(runtime, identity)
                    raise CycloError(
                        f"provider {prefix!r} exited before registration; {detail}"
                    )
            try:
                entry = self.catalog().get(prefix)
                if not isinstance(entry, Mapping):
                    last_problem = "no catalog entry"
                elif entry.get("kind") != "component":
                    last_problem = "catalog prefix is not a provider component"
                elif entry.get("generation") != generation:
                    last_problem = "catalog generation does not match the launched provider"
                elif registered_after is not None:
                    registered_at = self._registered_timestamp(
                        entry.get("registered_at")
                    )
                    if registered_at is None:
                        last_problem = "catalog registration has no valid timestamp"
                    elif registered_at < registered_after:
                        last_problem = "catalog registration predates this launch"
                    else:
                        return
                else:
                    return
            except CycloError as exc:
                last_problem = str(exc)
            time.sleep(0.2)
        diagnostics = self._provider_diagnostics(runtime, identity)
        raise CycloError(
            f"provider {prefix!r} did not become ready: {last_problem}; "
            f"{diagnostics}"
        )

    def prepare_instance(
        self,
        instance: Instance,
        team: Team,
        active_instances: Iterable[Instance],
    ) -> str:
        tokens = self.update_clients(active_instances)
        token = tokens.get(instance.id)
        if not token:
            raise CycloError(
                f"provider runtime did not issue a token for Cyclo instance {instance.id}"
            )
        catalog = self._instance_catalog(instance, self.catalog())
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
                "packages": list(gateway_auth.PI_PACKAGES),
            }
            models = gateway_auth.projected_models_json(
                catalog,
                provider_runtime_base_url(self.container_name),
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
        return token

    @staticmethod
    def _validate_models(team: Team, catalog: dict[str, dict]) -> None:
        for agent in team.agents:
            provider = catalog.get(agent.provider)
            models = provider.get("models") if isinstance(provider, dict) else None
            available = {
                model["id"]
                for model in models or []
                if isinstance(model, dict)
                and isinstance(model.get("id"), str)
                and model["id"]
            }
            if agent.model_id not in available:
                raise CycloError(
                    f"agent {agent.name} requests unavailable runtime model {agent.model!r}"
                )
