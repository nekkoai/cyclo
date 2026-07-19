from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable

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

STORE_CONTENTS = "credentials, subscriptions, and retained usage history"
STORE_VOLUME_HELP = (
    f"Docker volume containing gateway {STORE_CONTENTS}; "
    "destroy-store deletes all three irreversibly"
)

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


def validate_route_name(value: object, *, label: str) -> str:
    """Validate a provider/account name accepted by gateway HTTP routes."""

    if (
        not isinstance(value, str)
        or value.startswith("-")
        or not gateway.PROVIDER_RE.fullmatch(value)
        or value in gateway.RESERVED_PROVIDER_NAMES
    ):
        raise CycloError(
            f"invalid gateway {label} {value!r}; use lowercase letters, numbers, "
            "underscore, or hyphen"
        )
    return value


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

    provider = validate_route_name(provider, label="provider name")
    if account is not None:
        account = validate_route_name(account, label="account name")
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
        "--pull=never",
        "--rm",
        *GATEWAY_CONTAINER_HARDENING,
        "--network",
        "none",
        "--mount",
        f"type=volume,src={store_volume},dst={gateway.GATEWAY_STORE_PATH},readonly",
        image,
        "providers.mjs",
    ]


def providers_command(image: str) -> list[str]:
    """Build provider discovery without access to the gateway store."""

    return [
        "docker",
        "run",
        "--rm",
        *GATEWAY_CONTAINER_HARDENING,
        "--network",
        "none",
        image,
        "supported-providers.mjs",
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
    provider = validate_route_name(args.provider, label="provider name")
    account = (
        validate_route_name(args.account, label="account name")
        if args.account is not None
        else None
    )
    env_var = login_env_var(
        provider,
        api_key=args.api_key,
        api_key_env=args.api_key_env,
        api_key_stdin=args.api_key_stdin,
        environ=os.environ,
    )
    gateway.ensure_gateway_image(args.image, build=args.build)
    if args.login_guard is not None:
        args.login_guard(args)
    command = login_command(
        args.image,
        args.store_volume,
        provider,
        account=account,
        api_key=args.api_key,
        api_key_env=env_var,
        api_key_stdin=args.api_key_stdin,
        stdin_is_tty=sys.stdin.isatty(),
    )
    return docker.run_command(command)


def cmd_status(args: argparse.Namespace) -> int:
    gateway.require_gateway_image_current(args.image)
    print(f"gateway store volume: {args.store_volume}")
    return docker.run_command(status_command(args.image, args.store_volume))


def cmd_providers(args: argparse.Namespace) -> int:
    gateway.ensure_gateway_image(args.image, build=args.build)
    return docker.run_command(providers_command(args.image))


def cmd_destroy_store(args: argparse.Namespace) -> int:
    if args.confirm != args.store_volume:
        raise CycloError(
            "--confirm must exactly match the selected gateway store volume "
            f"({args.store_volume})"
        )
    removed = gateway.destroy_store_volume(args.store_volume)
    if removed:
        print(f"destroyed gateway store volume: {args.store_volume}")
    else:
        print(f"gateway store volume is already absent: {args.store_volume}")
    return 0


def build_parser(
    restart_handler: Callable[[argparse.Namespace], int] | None = None,
    login_guard: Callable[[argparse.Namespace], None] | None = None,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cyclo gateway",
        description=(
            "discover built-in providers and manage Cyclo's isolated gateway "
            f"store for {STORE_CONTENTS}"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    image_common = argparse.ArgumentParser(add_help=False)
    image_common.add_argument("--image", default=gateway.DEFAULT_GATEWAY_IMAGE)
    image_common.add_argument(
        "--build", action="store_true", help="rebuild the gateway image first"
    )

    store_common = argparse.ArgumentParser(add_help=False, parents=[image_common])
    store_common.add_argument(
        "--store-volume",
        default=gateway.DEFAULT_STORE_VOLUME,
        help=STORE_VOLUME_HELP,
    )

    providers = sub.add_parser(
        "providers",
        parents=[image_common],
        help="explain built-in AI providers and show login commands",
        description=(
            "Providers are upstream AI services or subscription accounts used by "
            "Cyclo's isolated gateway. This command explains each built-in "
            "provider, shows its default authentication and login command, and "
            "does not read or mount the gateway credential store."
        ),
    )
    providers.set_defaults(func=cmd_providers)

    login = sub.add_parser(
        "login",
        parents=[store_common],
        help="provision one provider credential",
        description=(
            "Provision one provider credential with a short-lived login container; "
            "this does not start the long-running gateway. On success, the top-level "
            "Cyclo command refreshes a running provider runtime. The credential may "
            "already be committed if that follow-up refresh reports an error."
        ),
    )
    login.add_argument("provider", metavar="PROVIDER")
    login.add_argument(
        "--as",
        dest="account",
        metavar="ACCOUNT",
        help=(
            "catalogue provider/account name (default: PROVIDER); use lowercase "
            "letters, numbers, underscore, or hyphen"
        ),
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
    login.set_defaults(func=cmd_login, login_guard=login_guard)

    status = sub.add_parser(
        "status",
        help="list provisioned accounts without building or pulling an image",
    )
    status.add_argument("--image", default=gateway.DEFAULT_GATEWAY_IMAGE)
    status.add_argument(
        "--store-volume",
        default=gateway.DEFAULT_STORE_VOLUME,
        help=STORE_VOLUME_HELP,
    )
    status.set_defaults(func=cmd_status)

    if restart_handler is not None:
        restart = sub.add_parser(
            "restart",
            parents=[store_common],
            help="recreate only the credential gateway and preserve its store",
            description=(
                "Recreate Cyclo's credential gateway container without deleting "
                "its credential store. The separate provider runtime reconnects "
                "through the gateway's stable private network name. On success, "
                "the top-level Cyclo command refreshes a running provider runtime; "
                "the gateway replacement may already be committed if that follow-up "
                "refresh reports an error."
            ),
        )
        restart.set_defaults(func=restart_handler)

    destroy = sub.add_parser(
        "destroy-store",
        help=f"irreversibly delete all {STORE_CONTENTS} in the selected volume",
        description=(
            f"Irreversibly delete all {STORE_CONTENTS} in the selected gateway "
            "store volume. This operation cannot be undone."
        ),
    )
    # The outer ``cyclo`` command injects its selected image alongside the
    # volume for every gateway action.  Accept it for composability, but keep
    # irrelevant image/build controls out of destructive-command help.
    destroy.add_argument(
        "--image", default=gateway.DEFAULT_GATEWAY_IMAGE, help=argparse.SUPPRESS
    )
    destroy.add_argument(
        "--store-volume",
        default=gateway.DEFAULT_STORE_VOLUME,
        help=STORE_VOLUME_HELP,
    )
    destroy.add_argument(
        "--confirm",
        required=True,
        metavar="VOLUME",
        help="must exactly match --store-volume to authorize irreversible deletion",
    )
    destroy.set_defaults(func=cmd_destroy_store)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    restart_handler: Callable[[argparse.Namespace], int] | None = None,
    login_guard: Callable[[argparse.Namespace], None] | None = None,
) -> int:
    args = build_parser(restart_handler, login_guard).parse_args(
        sys.argv[1:] if argv is None else argv
    )
    try:
        return int(args.func(args))
    except CycloError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
