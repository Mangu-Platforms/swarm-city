"""End-to-end swarm orchestration tests without model servers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents import Agent
from app.config import get_settings
from app.pipeline import Pipeline


SAFE_PATCH = """```diff
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old
+new
```
RATIONALE: focused change"""

UNSAFE_PATCH = """```diff
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old
+unsafe
```
SUMMARY: unsafe synthesis"""


class FakeRegistry:
    def __init__(self) -> None:
        self._drafts = [
            Agent("draft-1", "draft", "coder-a", "http://unused"),
            Agent("draft-2", "draft", "coder-b", "http://unused"),
        ]
        self._quality = [
            Agent("critic-1", "critic", "reviewer", "http://unused")
        ]
        self._security = [
            Agent("security-1", "security", "security-reviewer", "http://unused")
        ]
        self._tests = [
            Agent("tests-1", "test_gen", "tester", "http://unused")
        ]
        self._finalizers = [
            Agent("final-1", "finalizer", "coder-final", "http://unused")
        ]

    def drafters(self, language_tag=None):
        del language_tag
        return list(self._drafts)

    def quality_critics(self):
        return list(self._quality)

    def security_critics(self):
        return list(self._security)

    def critics(self):
        return [*self._quality, *self._security]

    def test_generators(self):
        return list(self._tests)

    def local_finalizers(self):
        return list(self._finalizers)


def _review(*, blocker: str | None = None) -> str:
    return json.dumps(
        {
            "correctness": 9,
            "security": 9 if blocker is None else 2,
            "style": 8,
            "tests": 8,
            "confidence": 9,
            "blockers": [blocker] if blocker else [],
            "evidence": ["reviewed exact patch and repository context"],
            "one_fix": "replace unsafe behavior" if blocker else "",
        }
    )


def _task() -> dict:
    return {
        "task": "Replace old behavior",
        "context_files": {},
        "context_paths": ["app.py"],
        "constraints": ["keep the public interface"],
        "language": "python",
        "mode": "fix",
        "auto_context": False,
        "seed": 7,
        "apply": False,
    }


@pytest.mark.asyncio
async def test_pipeline_returns_release_ready_local_patch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("old\n", encoding="utf-8")
    get_settings.cache_clear()

    async def fake_chat(
        agent,
        messages,
        json_mode=False,
        client=None,
        response_model=None,
    ):
        del json_mode, client, response_model
        if agent.role in {"critic", "security"}:
            return _review()
        if agent.role == "test_gen":
            return """```diff
diff --git a/tests/test_app.py b/tests/test_app.py
new file mode 100644
--- /dev/null
+++ b/tests/test_app.py
@@ -0,0 +1 @@
+def test_app(): assert True
```"""
        if agent.role == "finalizer":
            return SAFE_PATCH
        replacement = "new" if agent.agent_id == "draft-1" else "better"
        return SAFE_PATCH.replace("+new", f"+{replacement}")

    monkeypatch.setattr("app.pipeline.chat", fake_chat)
    pipeline = Pipeline(FakeRegistry())
    progress: dict = {}
    result = await pipeline.run(_task(), progress)

    assert result["patch"]["valid"] is True
    assert result["finalized_by"] == "local:coder-final"
    assert result["drafts"]["completed"] == 2
    assert result["ranking"][0]["review_count"] == 2
    assert result["ranking"][0]["security_review_count"] == 1
    assert result["release_gate"]["status"] == "ready"
    assert result["final_review_attempts"][0]["score"]["eligible"] is True
    assert progress["phase"] == "done"


@pytest.mark.asyncio
async def test_pipeline_rejects_bad_synthesis_and_falls_back_to_re_reviewed_builder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("old\n", encoding="utf-8")
    get_settings.cache_clear()

    async def fake_chat(
        agent,
        messages,
        json_mode=False,
        client=None,
        response_model=None,
    ):
        del json_mode, client, response_model
        user = messages[-1]["content"]
        if agent.role in {"critic", "security"}:
            blocker = "unsafe synthesized behavior" if "+unsafe" in user else None
            return _review(blocker=blocker)
        if agent.role == "finalizer":
            return UNSAFE_PATCH
        if agent.role == "test_gen":
            return "not a patch"
        replacement = "new" if agent.agent_id == "draft-1" else "better"
        return SAFE_PATCH.replace("+new", f"+{replacement}")

    monkeypatch.setattr("app.pipeline.chat", fake_chat)
    result = await Pipeline(FakeRegistry()).run(_task(), {})

    assert "+unsafe" not in result["final"]
    assert "eligible-candidate-fallback" in result["finalized_by"]
    assert result["release_gate"]["status"] == "ready"
    assert len(result["final_review_attempts"]) == 3
    assert any("re-reviewed eligible builder" in warning for warning in result["warnings"])


@pytest.mark.asyncio
async def test_pipeline_blocks_when_no_builder_produces_a_valid_patch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("old\n", encoding="utf-8")
    get_settings.cache_clear()

    async def fake_chat(
        agent,
        messages,
        json_mode=False,
        client=None,
        response_model=None,
    ):
        del agent, messages, json_mode, client, response_model
        return "I would change app.py, but this is not a unified diff."

    monkeypatch.setattr("app.pipeline.chat", fake_chat)
    result = await Pipeline(FakeRegistry()).run(_task(), {})

    assert result["final"] == ""
    assert result["release_gate"]["status"] == "blocked"
    assert result["patch"]["valid"] is False
    assert "no builder produced" in result["release_gate"]["reasons"][0]
