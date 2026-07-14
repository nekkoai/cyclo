from __future__ import annotations

import json
import subprocess
import sys

from ..errors import CycloError


def docker_image_inspect_command(image: str) -> list[str]:
    return ["docker", "image", "inspect", image]


def docker_image_exists(image: str) -> bool:
    try:
        proc = subprocess.run(
            docker_image_inspect_command(image),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CycloError("required command not found: docker") from exc
    return proc.returncode == 0


def docker_container_exists(name: str) -> bool:
    try:
        proc = subprocess.run(
            ["docker", "inspect", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CycloError("required command not found: docker") from exc
    return proc.returncode == 0


def docker_label_template(label: str) -> str:
    return "{{ index .Config.Labels " + json.dumps(label) + " }}"


def _docker_inspect_label(command: list[str]) -> str:
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CycloError("required command not found: docker") from exc
    if proc.returncode != 0:
        return ""
    value = proc.stdout.strip()
    return "" if value == "<no value>" else value


def docker_image_label(image: str, label: str) -> str:
    return _docker_inspect_label(
        ["docker", "image", "inspect", "--format", docker_label_template(label), image]
    )


def docker_container_label(name: str, label: str) -> str:
    return _docker_inspect_label(
        ["docker", "inspect", "--format", docker_label_template(label), name]
    )


def docker_container_running(name: str) -> bool:
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CycloError("required command not found: docker") from exc
    return proc.returncode == 0 and proc.stdout.strip().lower() == "true"


def run_command(command: list[str]) -> int:
    try:
        return subprocess.call(command)
    except FileNotFoundError as exc:
        raise CycloError(f"required command not found: {command[0]}") from exc


def run_command_capture(command: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CycloError(f"required command not found: {command[0]}") from exc
    if proc.stdout:
        print(proc.stdout, end="")
    return proc.returncode, proc.stdout.strip()


def docker_call_ignore_missing(command: list[str]) -> int:
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CycloError("required command not found: docker") from exc
    detail = (proc.stderr or "").lower()
    missing = any(
        marker in detail
        for marker in ("no such container", "no such network", "no such object")
    )
    if proc.returncode != 0 and missing:
        return 0
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode
