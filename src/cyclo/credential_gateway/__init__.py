"""Cyclo-owned credential gateway runtime.

This package is deliberately self-contained.  Team containers receive only a
scoped capability; provider credentials remain in the gateway's Docker volume.
"""

from .auth import PI_PACKAGES, gateway_base_url, projected_models_json, resolve_host_pi_agent_dir
from .gateway import (
    DEFAULT_GATEWAY_IMAGE,
    DEFAULT_STORE_VOLUME,
    ensure_client_registry_mount,
    fetch_provider_catalog,
    fetch_usage,
    gateway_container_name,
    gateway_network_name,
    host_client_registry_dir,
    owned_network_id,
    published_port,
    shared_token,
    start_gateway,
    validate_running_gateway,
)

__all__ = [
    "DEFAULT_GATEWAY_IMAGE",
    "DEFAULT_STORE_VOLUME",
    "PI_PACKAGES",
    "ensure_client_registry_mount",
    "fetch_provider_catalog",
    "fetch_usage",
    "gateway_base_url",
    "gateway_container_name",
    "gateway_network_name",
    "host_client_registry_dir",
    "owned_network_id",
    "projected_models_json",
    "published_port",
    "resolve_host_pi_agent_dir",
    "shared_token",
    "start_gateway",
    "validate_running_gateway",
]
