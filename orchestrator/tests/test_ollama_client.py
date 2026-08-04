"""Ollama protocol, structured output, and size-bound tests."""

from __future__ import annotations

import json

import httpx
import pytest

from app.agents import Agent
from app.config import get_settings
from app.models import CriticReview
from app.ollama_client import chat, extract_json, list_models


def _review_json() -> dict:
    return {
        "correctness": 9,
        "security": 9,
        "style": 8,
        "tests": 8,
        "confidence": 9,
        "blockers": [],
        "evidence": ["input is validated"],
        "one_fix": "",
    }


def test_extract_json_from_fence_and_surrounding_text() -> None:
    assert extract_json('```json\n{"correctness": 8}\n```') == {"correctness": 8}
    assert extract_json('review: {"security": 9} trailing') == {"security": 9}
    assert extract_json("not json") == {}


@pytest.mark.asyncio
async def test_chat_sends_json_schema_and_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_API_KEY", "secret-token")
    get_settings.cache_clear()
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={"done": True, "message": {"content": json.dumps(_review_json())}},
        )

    agent = Agent("critic-1", "critic", "reviewer", "http://ollama.test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        content = await chat(
            agent,
            [{"role": "user", "content": "review this"}],
            response_model=CriticReview,
            client=client,
        )

    assert CriticReview.model_validate_json(content).correctness == 9
    assert captured["payload"]["format"]["title"] == "CriticReview"
    assert (
        captured["payload"]["options"]["num_ctx"] == get_settings().local_context_tokens
    )
    assert captured["authorization"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_chat_rejects_invalid_or_truncated_structured_output() -> None:
    responses = iter(
        [
            {"done": True, "message": {"content": '{"correctness": 9}'}},
            {
                "done": True,
                "done_reason": "length",
                "message": {"content": json.dumps(_review_json())},
            },
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=next(responses))

    agent = Agent("critic-1", "critic", "reviewer", "http://ollama.test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="invalid structured output"):
            await chat(
                agent,
                [{"role": "user", "content": "review"}],
                response_model=CriticReview,
                client=client,
            )
        with pytest.raises(RuntimeError, match="truncated"):
            await chat(
                agent,
                [{"role": "user", "content": "review"}],
                response_model=CriticReview,
                client=client,
            )


@pytest.mark.asyncio
async def test_chat_enforces_input_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_AGENT_INPUT_CHARS", "10")
    get_settings.cache_clear()
    agent = Agent("draft-1", "draft", "coder", "http://ollama.test")

    with pytest.raises(ValueError, match="MAX_AGENT_INPUT_CHARS"):
        await chat(agent, [{"role": "user", "content": "x" * 11}])


@pytest.mark.asyncio
async def test_list_models_returns_exact_tags() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={"models": [{"name": "coder:latest"}, {"name": "reviewer:7b"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        models = await list_models("http://ollama.test", client=client)
    assert models == ["coder:latest", "reviewer:7b"]
