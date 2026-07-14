from __future__ import annotations

import argparse
import os
import sys

from ..errors import CycloError
from . import docker, gateway


OAUTH_PROVIDERS = {"openai-codex", "anthropic", "github-copilot"}
PROVIDER_ENV_VARS = {
    "xai": "XAI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "google": "GEMINI_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "zai": "ZAI_API_KEY",
    "moonshotai": "MOONSHOT_API_KEY",
    "huggingface": "HF_TOKEN",
}

GATEWAY_CONTAINER_HARDENING = [
    "--security-opt",
    "no-new-privileges",
    "--cap-drop",
    "ALL",
    "--pids-limit",
    "256",
    "--read-only",
    "--tmpfs",
    "/tmp:rw,noexec,nosuid,nodev,size=64m",
]


def login_command(
    image: str,
    store_volume: str,
    provider: str,
    *,
    account: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    api_key_stdin: bool = False,
    stdin_is_tty: bool = False,
) -> list[str]:
    """Build the isolated one-shot credential provisioning command."""

    oauth = api_key is None and api_key_env is None and not api_key_stdin
    command = ["docker", "run", "--rm", *GATEWAY_CONTAINER_HARDENING]
    if oauth:
        command += ["-i", "-t"]
    elif api_key_stdin:
        command.append("-i")
        if stdin_is_tty:
            command.append("-t")
    if api_key_env is not None:
        # Only the environment variable name is present in argv. Docker reads
        # its value directly from the host process environment.
        command += ["-e", api_key_env]
    command += [
        "--mount",
        f"type=volume,src={store_volume},dst={gateway.GATEWAY_STORE_PATH}",
        image,
        "login.mjs",
        provider,
    ]
    if account is not None:
        command += ["--as", account]
    if api_key is not None:
        command += ["--api-key", api_key]
    elif api_key_env is not None:
        command += ["--api-key-env", api_key_env]
    elif api_key_stdin:
        command.append("--api-key-stdin")
    return command


def status_command(image: str, store_volume: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        *GATEWAY_CONTAINER_HARDENING,
        "--network",
        "none",
        "--mount",
        f"type=volume,src={store_volume},dst={gateway.GATEWAY_STORE_PATH},readonly",
        image,
        "providers.mjs",
    ]


def login_env_var(
    provider: str,
    *,
    api_key: str | None,
    api_key_env: str | None,
    api_key_stdin: bool,
    environ: dict[str, str],
) -> str | None:
    if api_key is not None or api_key_stdin:
        return None
    if api_key_env is not None:
        name = api_key_env or PROVIDER_ENV_VARS.get(provider)
        if not name:
            raise CycloError(
                f"no conventional API-key env var known for {provider!r}; "
                "pass --api-key-env VAR"
            )
    elif provider not in OAUTH_PROVIDERS and provider in PROVIDER_ENV_VARS:
        name = PROVIDER_ENV_VARS[provider]
    else:
        return None
    if not environ.get(name):
        raise CycloError(
            f"${name} is not set; export it, or use --api-key-stdin or --api-key"
        )
    return name


def cmd_login(args: argparse.Namespace) -> int:
    gateway.ensure_gateway_image(args.image, build=args.build)
    env_var = login_env_var(
        args.provider,
        api_key=args.api_key,
        api_key_env=args.api_key_env,
        api_key_stdin=args.api_key_stdin,
        environ=os.environ,
    )
    return docker.run_command(
        login_command(
            args.image,
            args.store_volume,
            args.provider,
            account=args.account,
            api_key=args.api_key,
            api_key_env=env_var,
            api_key_stdin=args.api_key_stdin,
            stdin_is_tty=sys.stdin.isatty(),
        )
    )


def cmd_status(args: argparse.Namespace) -> int:
    gateway.ensure_gateway_image(args.image, build=args.build)
    print(f"gateway store volume: {args.store_volume}")
    return docker.run_command(status_command(args.image, args.store_volume))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cyclo gateway", description="manage Cyclo's isolated credential gateway store"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--image", default=gateway.DEFAULT_GATEWAY_IMAGE)
    common.add_argument(
        "--store-volume",
        default=gateway.DEFAULT_STORE_VOLUME,
        help="Docker volume containing gateway credentials",
    )
    common.add_argument("--build", action="store_true", help="rebuild the gateway image first")

    login = sub.add_parser("login", parents=[common], help="provision one provider credential")
    login.add_argument("provider")
    login.add_argument(
        "--as",
        dest="account",
        metavar="ACCOUNT",
        help="store the credential under this account alias",
    )
    key_source = login.add_mutually_exclusive_group()
    key_source.add_argument(
        "--api-key",
        help="API key argument (visible in shell history/process listings; prefer env or stdin)",
    )
    key_source.add_argument(
        "--api-key-env",
        nargs="?",
        const="",
        metavar="VAR",
        help="read from VAR; without VAR use the provider's conventional variable",
    )
    key_source.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="read the API key from standard input",
    )
    login.set_defaults(func=cmd_login)

    status = sub.add_parser("status", parents=[common], help="list provisioned accounts")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return int(args.func(args))
    except CycloError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
