"""Shared test configuration."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    state_root = tmp_path.parent / f".swarm-state-{tmp_path.name}"
    monkeypatch.setenv("DATA_DIR", str(state_root))
    monkeypatch.setenv("TASK_STORE_DIR", str(state_root / "tasks"))
    monkeypatch.setenv("WORKTREE_ROOT", str(state_root / "worktrees"))
    monkeypatch.setenv("GIT_LOCK_FILE", str(state_root / "locks" / "git.lock"))
    monkeypatch.setenv("DEEPSEEK_BUDGET_FILE", str(state_root / "budget.json"))
    monkeypatch.setenv("REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTS_CONFIG", str(Path(__file__).parents[1] / "agents.yaml"))
    # Authentication is required by default, so every test that builds Settings
    # needs a token. Tests that exercise the auth dependency override both.
    monkeypatch.setenv("SWARM_API_TOKEN", "test-swarm-token")
    monkeypatch.setenv("SKIP_FINALIZE", "true")
    monkeypatch.setenv("N_DRAFT", "2")
    monkeypatch.setenv("M_CRITICS", "1")
    monkeypatch.setenv("TOP_K", "1")
    monkeypatch.setenv("MINIMUM_CRITIC_REVIEWS", "2")
    monkeypatch.setenv("MINIMUM_SECURITY_REVIEWS", "1")
    monkeypatch.setenv("FINAL_REVIEW_COUNT", "1")
    monkeypatch.setenv("MAX_REPAIR_ROUNDS", "1")
    monkeypatch.setenv("MAX_ACTIVE_TASKS", "1")
    monkeypatch.setenv("MAX_QUEUED_TASKS", "2")
    monkeypatch.setenv("AGENT_RETRIES", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
