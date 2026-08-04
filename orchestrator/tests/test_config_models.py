"""Configuration and request validation tests."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.models import CriticReview, TaskRequest


def test_invalid_scoring_weights_fail_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("W_CORRECTNESS", "1")
    monkeypatch.setenv("W_SECURITY", "1")
    monkeypatch.setenv("W_STYLE", "1")
    monkeypatch.setenv("W_TESTS", "1")
    get_settings.cache_clear()

    with pytest.raises(ValidationError, match="scoring weights"):
        get_settings()


def test_retired_deepseek_alias_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    get_settings.cache_clear()

    with pytest.raises(ValidationError, match="retired alias"):
        get_settings()


def test_required_api_token_must_be_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUIRE_API_TOKEN", "true")
    monkeypatch.delenv("SWARM_API_TOKEN", raising=False)
    get_settings.cache_clear()

    with pytest.raises(ValidationError, match="SWARM_API_TOKEN"):
        get_settings()


def test_apply_with_tests_requires_sandbox_or_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_GIT_APPLY", "true")
    monkeypatch.setenv("REQUIRE_TESTS", "true")
    monkeypatch.delenv("TEST_RUNNER_COMMAND", raising=False)
    monkeypatch.setenv("ALLOW_UNSANDBOXED_TESTS", "false")
    get_settings.cache_clear()

    with pytest.raises(ValidationError, match="TEST_RUNNER_COMMAND"):
        get_settings()


def test_task_request_rejects_unsafe_context_path() -> None:
    with pytest.raises(ValidationError, match="unsafe repository-relative path"):
        TaskRequest(task="Fix parser", context_paths=[""])
    with pytest.raises(ValidationError, match="unsafe repository-relative path"):
        TaskRequest(task="Fix parser", context_paths=["../secrets.txt"])


def test_task_request_normalizes_and_bounds_controls() -> None:
    request = TaskRequest(
        task="Fix parser",
        context_paths=["src/parser.py", "src/parser.py"],
        constraints=[" preserve API ", "preserve API"],
        expected_head="ABCDEF1",
    )

    assert request.context_paths == ["src/parser.py"]
    assert request.constraints == ["preserve API"]
    assert request.expected_head == "abcdef1"

    with pytest.raises(ValidationError, match="only valid when apply=true"):
        TaskRequest(task="Fix parser", allow_high_risk_paths=True)


def test_critic_review_is_strict_and_deduplicates_evidence() -> None:
    review = CriticReview(
        correctness=9,
        security=8,
        style=7,
        tests=8,
        confidence=9,
        blockers=[],
        evidence=["line 12 validates input", "line 12 validates input"],
        one_fix="",
    )
    assert review.evidence == ["line 12 validates input"]

    with pytest.raises(ValidationError):
        CriticReview(
            correctness=11,
            security=8,
            style=7,
            tests=8,
            confidence=9,
            blockers=[],
            evidence=[],
            one_fix="",
        )
