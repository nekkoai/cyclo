from __future__ import annotations

import base64
import json
import math
import os
import secrets
from pathlib import Path


PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
PI_PACKAGES = ["npm:pi-web-access", "npm:pi-lens", "npm:pi-simplify"]
JWT_CLAIM_PATH = "https://api.openai.com/auth"
GATEWAY_PORT = 8787

# Pi model records are provider metadata, not a general extension mechanism.
# This allowlist is the boundary that prevents host-side URLs, headers, and
# credentials from being projected into a team container.
SAFE_COST_FIELDS = {"input", "output", "cacheRead", "cacheWrite"}
SAFE_INPUT_TYPES = {"text", "image"}
SAFE_COMPAT_BOOLEAN_FIELDS = {
    "requiresAssistantAfterToolResult",
    "requiresReasoningContentOnAssistantMessages",
    "requiresThinkingAsText",
    "requiresToolResultName",
    "sendSessionAffinityHeaders",
    "sendSessionIdHeader",
    "supportsDeveloperRole",
    "supportsEagerToolInputStreaming",
    "supportsLongCacheRetention",
    "supportsReasoningEffort",
    "supportsStore",
    "supportsStrictMode",
    "supportsUsageInStreaming",
    "zaiToolStream",
}
SAFE_MAX_TOKENS_FIELDS = {"max_completion_tokens", "max_tokens"}
SAFE_THINKING_FORMATS = {
    "openai",
    "openrouter",
    "deepseek",
    "zai",
    "qwen",
    "qwen-chat-template",
}
SAFE_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh"}


def expand_pi_agent_dir(value: str, home: Path) -> Path:
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value).expanduser()


def resolve_host_pi_agent_dir(home: Path, env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    configured = env.get(PI_AGENT_DIR_ENV)
    if configured:
        return expand_pi_agent_dir(configured, home).resolve()
    return (home / ".pi" / "agent").resolve()


def _base64url_json(value: object) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_proxy_token() -> str:
    """Create an opaque gateway capability compatible with Pi provider probes.

    The token only identifies a gateway client.  It is not a provider credential
    and is accepted solely through a hash in the gateway client registry.
    """

    header = {"alg": "none", "typ": "JWT"}
    payload = {
        JWT_CLAIM_PATH: {"chatgpt_account_id": "host-auth"},
        "aud": "cyclo-gateway",
        "jti": secrets.token_urlsafe(32),
    }
    return f"{_base64url_json(header)}.{_base64url_json(payload)}.sk-ant-oat-gateway"


def gateway_base_url(container: str) -> str:
    return f"http://{container}:{GATEWAY_PORT}"


def sanitize_model(model: object) -> dict[str, object] | None:
    if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"]:
        return None
    clean: dict[str, object] = {"id": model["id"]}
    for key in ("name", "provider", "api"):
        value = model.get(key)
        if isinstance(value, str) and value:
            clean[key] = value
    if isinstance(model.get("reasoning"), bool):
        clean["reasoning"] = model["reasoning"]
    input_types = model.get("input")
    if isinstance(input_types, list):
        clean["input"] = [
            value for value in input_types if isinstance(value, str) and value in SAFE_INPUT_TYPES
        ]
    for key in ("contextWindow", "maxTokens"):
        value = model.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            clean[key] = value
    cost = model.get("cost")
    if isinstance(cost, dict):
        safe_cost = {
            key: value
            for key, value in cost.items()
            if key in SAFE_COST_FIELDS
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        }
        if safe_cost:
            clean["cost"] = safe_cost
    compat = model.get("compat")
    if isinstance(compat, dict):
        safe_compat = {
            key: value
            for key, value in compat.items()
            if key in SAFE_COMPAT_BOOLEAN_FIELDS and isinstance(value, bool)
        }
        max_tokens_field = compat.get("maxTokensField")
        if isinstance(max_tokens_field, str) and max_tokens_field in SAFE_MAX_TOKENS_FIELDS:
            safe_compat["maxTokensField"] = max_tokens_field
        thinking_format = compat.get("thinkingFormat")
        if isinstance(thinking_format, str) and thinking_format in SAFE_THINKING_FORMATS:
            safe_compat["thinkingFormat"] = thinking_format
        if compat.get("cacheControlFormat") == "anthropic":
            safe_compat["cacheControlFormat"] = "anthropic"
        if safe_compat:
            clean["compat"] = safe_compat
    thinking_levels = model.get("thinkingLevelMap")
    if isinstance(thinking_levels, dict):
        safe_thinking_levels = {
            key: value
            for key, value in thinking_levels.items()
            if key in SAFE_THINKING_LEVELS and (isinstance(value, str) or value is None)
        }
        if safe_thinking_levels:
            clean["thinkingLevelMap"] = safe_thinking_levels
    return clean


def projected_models_json(
    catalog: dict[str, dict], base_url: str, token: str
) -> dict[str, object]:
    """Project non-secret model metadata and route every provider via Cyclo."""

    providers: dict[str, object] = {}
    for name, info in catalog.items():
        gateway_url = f"{base_url}/p/{name}"
        models: list[dict[str, object]] = []
        raw_models = info.get("models", []) if isinstance(info, dict) else []
        if isinstance(raw_models, list):
            for model in raw_models:
                projected = sanitize_model(model)
                if projected is None:
                    continue
                projected["baseUrl"] = gateway_url
                models.append(projected)
        provider_api = info.get("api") if isinstance(info, dict) else None
        if not isinstance(provider_api, str) or not provider_api:
            provider_api = "openai-completions"
        providers[name] = {
            "baseUrl": gateway_url,
            "api": provider_api,
            "apiKey": token,
            "models": models,
        }
    return {"providers": providers}
