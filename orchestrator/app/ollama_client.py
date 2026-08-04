"""Resilient async client for Ollama-compatible chat endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

import httpx
from pydantic import BaseModel

from .agents import Agent
from .config import get_settings

log = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {408, 409, 425, 429}


class OllamaProtocolError(RuntimeError):
    """Raised when an Ollama response is structurally invalid or truncated."""


def _message_chars(messages: list[dict[str, str]]) -> int:
    return sum(len(str(message.get("content", ""))) for message in messages)


async def chat(
    agent: Agent,
    messages: list[dict[str, str]],
    json_mode: bool = False,
    client: httpx.AsyncClient | None = None,
    response_model: type[BaseModel] | None = None,
) -> str:
    """Send one bounded chat request and return non-empty assistant text."""

    settings = get_settings()
    input_chars = _message_chars(messages)
    if input_chars > settings.max_agent_input_chars:
        raise ValueError(
            f"agent input exceeds MAX_AGENT_INPUT_CHARS "
            f"({input_chars} > {settings.max_agent_input_chars})"
        )

    payload: dict[str, Any] = {
        "model": agent.model,
        "messages": messages,
        "stream": False,
        "keep_alive": settings.ollama_keep_alive,
        "options": {
            "temperature": agent.temperature,
            "num_predict": settings.local_max_tokens,
            "num_ctx": settings.local_context_tokens,
        },
    }
    if response_model is not None:
        payload["format"] = response_model.model_json_schema()
    elif json_mode:
        payload["format"] = "json"

    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(settings.agent_timeout_s, connect=15),
        headers=settings.ollama_headers,
    )
    last_error: Exception | None = None

    try:
        for attempt in range(settings.agent_retries + 1):
            try:
                response = await active_client.post(
                    f"{agent.endpoint}/api/chat",
                    json=payload,
                    headers=settings.ollama_headers,
                )
                response.raise_for_status()
                # Decode the body only after bounding it. response.json() would
                # otherwise buffer and parse an arbitrarily large body — on
                # every retry — before the output limit below is consulted.
                body = response.content
                if len(body) > settings.max_agent_output_chars * 4:
                    raise OllamaProtocolError(
                        f"agent {agent.agent_id} response exceeds the "
                        "MAX_AGENT_OUTPUT_CHARS transfer ceiling"
                    )
                try:
                    data = json.loads(body)
                except (ValueError, RecursionError) as exc:
                    raise OllamaProtocolError(
                        f"agent {agent.agent_id} returned invalid JSON"
                    ) from exc
                if not isinstance(data, dict):
                    raise OllamaProtocolError(
                        f"agent {agent.agent_id} returned a non-object response"
                    )
                if data.get("done") is False:
                    raise OllamaProtocolError(
                        f"agent {agent.agent_id} returned an incomplete response"
                    )
                done_reason = str(data.get("done_reason", "")).lower()
                if done_reason in {"length", "max_tokens"}:
                    raise OllamaProtocolError(
                        f"agent {agent.agent_id} output was truncated by token limit"
                    )
                message = data.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, str) or not content.strip():
                    raise OllamaProtocolError(
                        f"agent {agent.agent_id} returned no assistant content"
                    )
                content = content.strip()
                if len(content) > settings.max_agent_output_chars:
                    raise OllamaProtocolError(
                        f"agent {agent.agent_id} output exceeds "
                        f"MAX_AGENT_OUTPUT_CHARS ({settings.max_agent_output_chars})"
                    )
                if response_model is not None:
                    parsed = extract_json(content)
                    try:
                        response_model.model_validate(parsed)
                    except Exception as exc:  # noqa: BLE001 - normalized protocol error
                        raise OllamaProtocolError(
                            f"agent {agent.agent_id} returned invalid structured output"
                        ) from exc
                return content
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                retryable = status in RETRYABLE_STATUS_CODES or status >= 500
                if not retryable:
                    raise RuntimeError(
                        f"agent {agent.agent_id} rejected request with HTTP {status}"
                    ) from exc
            except (
                httpx.TransportError,
                json.JSONDecodeError,
                OllamaProtocolError,
            ) as exc:
                last_error = exc

            if attempt < settings.agent_retries:
                delay = 1.25 * (2**attempt) + random.uniform(0, 0.25)
                log.warning(
                    "agent %s attempt %d failed: %s; retrying in %.2fs",
                    agent.agent_id,
                    attempt + 1,
                    last_error,
                    delay,
                )
                await asyncio.sleep(delay)

        raise RuntimeError(
            f"agent {agent.agent_id} failed after "
            f"{settings.agent_retries + 1} attempts: {last_error}"
        )
    finally:
        if owns_client:
            await active_client.aclose()


async def list_models(
    endpoint: str,
    client: httpx.AsyncClient | None = None,
) -> list[str]:
    """List model tags loaded or available at one endpoint."""

    settings = get_settings()
    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(settings.readiness_timeout_s, connect=3),
        headers=settings.ollama_headers,
    )
    try:
        response = await active_client.get(
            f"{endpoint.rstrip('/')}/api/tags",
            headers=settings.ollama_headers,
        )
        response.raise_for_status()
        data = response.json()
    finally:
        if owns_client:
            await active_client.aclose()

    models = data.get("models", []) if isinstance(data, dict) else []
    return sorted(
        model["name"]
        for model in models
        if isinstance(model, dict) and isinstance(model.get("name"), str)
    )


def extract_json(text: str) -> dict:
    """Extract the first JSON object from plain or fenced model output."""

    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            stripped = "\n".join(lines[1:-1]).strip()

    # Deeply nested input makes json raise RecursionError, which is not a
    # JSONDecodeError and is not in any caller's retry tuple. One badly behaved
    # model response would otherwise abort the whole task instead of counting
    # as an invalid review.
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, RecursionError):
        pass

    decoder = json.JSONDecoder()
    for index, character in enumerate(stripped):
        if character != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(stripped[index:])
        except (ValueError, RecursionError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}
