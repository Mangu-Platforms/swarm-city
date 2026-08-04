"""Configuration and request validation tests."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents import AgentRegistry
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


def test_authentication_is_required_by_default(monkeypatch) -> None:
    """The documented posture is fail-closed; the code default must match."""

    monkeypatch.delenv("REQUIRE_API_TOKEN", raising=False)
    monkeypatch.delenv("SWARM_API_TOKEN", raising=False)
    get_settings.cache_clear()

    with pytest.raises(ValidationError, match="REQUIRE_API_TOKEN"):
        get_settings()


def test_protect_metrics_without_a_token_is_refused(monkeypatch) -> None:
    monkeypatch.setenv("REQUIRE_API_TOKEN", "false")
    monkeypatch.setenv("SWARM_API_TOKEN", "")
    monkeypatch.setenv("PROTECT_METRICS", "true")
    get_settings.cache_clear()

    with pytest.raises(ValidationError, match="PROTECT_METRICS"):
        get_settings()


def test_jointly_unworkable_settings_are_refused(monkeypatch) -> None:
    for variables, expected in (
        ({"AGENT_TIMEOUT_S": "3600", "TASK_TIMEOUT_S": "60"}, "AGENT_TIMEOUT_S"),
        ({"LOCAL_MAX_TOKENS": "900000", "LOCAL_CONTEXT_TOKENS": "2048"}, "LOCAL_MAX_TOKENS"),
        ({"MAX_INLINE_CONTEXT_CHARS": "10000000"}, "MAX_INLINE_CONTEXT_CHARS"),
        ({"TASK_RETENTION": "2", "MAX_QUEUED_TASKS": "20"}, "TASK_RETENTION"),
    ):
        for name, value in variables.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        with pytest.raises(ValidationError, match=expected):
            get_settings()
        for name in variables:
            monkeypatch.delenv(name, raising=False)
        get_settings.cache_clear()


def test_unsatisfiable_review_gates_are_refused_at_startup(monkeypatch) -> None:
    """A gate the roster can never meet must fail loudly, not block every task."""

    monkeypatch.setenv("MINIMUM_SECURITY_REVIEWS", "9")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="MINIMUM_SECURITY_REVIEWS"):
        AgentRegistry()


def test_one_model_for_both_draft_roles_is_supported(monkeypatch) -> None:
    """Pointing both draft overrides at one tag is the normal low-memory setup."""

    monkeypatch.setenv("SWARM_CODER_MODEL", "qwen3-coder:30b")
    monkeypatch.setenv("SWARM_REASONER_MODEL", "qwen3-coder:30b")
    get_settings.cache_clear()

    registry = AgentRegistry()

    drafters = registry.drafters(None)
    assert len({agent.agent_id for agent in drafters}) == len(drafters)
    assert {agent.model for agent in drafters} == {"qwen3-coder:30b"}


def test_inline_context_content_is_preserved_verbatim() -> None:
    """Stripping inline context breaks indentation, so patches fail to apply."""

    source = "def outer():\n    inner = 1\n    return inner\n"
    request = TaskRequest(
        task="  refactor outer  ",
        context_files={"app/module.py": source, "app/indented.py": "    x = 1\n"},
    )

    assert request.context_files["app/module.py"] == source
    assert request.context_files["app/indented.py"] == "    x = 1\n"
    assert request.task == "refactor outer"


def test_an_overlong_review_is_truncated_rather_than_discarded() -> None:
    """Rejecting the review would throw away the reviewer that found the most."""

    review = CriticReview(
        correctness=5,
        security=5,
        style=5,
        tests=5,
        confidence=5,
        blockers=[f"blocker {index}" for index in range(20)],
        evidence=["e" * 4000],
    )

    assert len(review.blockers) == 12
    assert review.blockers[0] == "blocker 0"
    assert len(review.evidence[0]) == 1000
