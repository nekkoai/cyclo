from __future__ import annotations

import ast
import re
from pathlib import Path


SOURCE_ROOT = Path(__file__).parents[1] / "src" / "cyclo"


def test_controller_has_no_external_agent_runtime_discovery() -> None:
    """Release code must never fall back to sibling/home-directory checkouts."""

    forbidden_text = {
        "home checkout path": re.compile(
            r"(?:~|\$\{?HOME\}?|/home/[^/\s'\"]+)/(?:agentws|multiagent)(?:[/\s'\"]|$)",
            re.IGNORECASE,
        ),
        "checkout root override": re.compile(
            r"CYCLO_(?:AGENTWS|MULTIAGENT)_ROOT|--(?:agentws|multiagent)-root",
            re.IGNORECASE,
        ),
        "checkout discovery helper": re.compile(
            r"discover_(?:agentws|multiagent)_root|load_multiagent_modules",
            re.IGNORECASE,
        ),
        "PATH lookup for old gateway CLI": re.compile(
            r"(?:shutil\.)?which\([^)]*['\"]multiagent['\"]",
            re.IGNORECASE,
        ),
    }
    failures: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(SOURCE_ROOT)
        text = path.read_text(encoding="utf-8")
        for label, pattern in forbidden_text.items():
            if match := pattern.search(text):
                line = text.count("\n", 0, match.start()) + 1
                failures.append(f"{relative}:{line}: {label}: {match.group(0)!r}")

        tree = ast.parse(text, filename=str(relative))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for module in imported:
                if module.split(".", 1)[0] in {"agentws", "multiagent"}:
                    failures.append(
                        f"{relative}:{node.lineno}: external runtime import: {module!r}"
                    )

    assert failures == []


def test_team_runtime_uses_the_fixed_private_pi_home() -> None:
    from cyclo.team.component import PI_ROOT

    assert PI_ROOT == "/home/cyclo/.pi"
