from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def team_repo(tmp_path: Path) -> Path:
    root = tmp_path / "review-team"
    (root / "roles").mkdir(parents=True)
    (root / "roles" / "planner.md").write_text("Plan the work.\n", encoding="utf-8")
    (root / "roles" / "reviewer.md").write_text("Review the work.\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("Use the queue.\n", encoding="utf-8")
    (root / "team").write_text(
        "planner-1 planner pi openai-codex/gpt-test\n"
        "reviewer-1 reviewer pi-interactive anthropic/claude-test\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


@pytest.fixture
def project_repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root
