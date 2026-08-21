"""API boundary, authentication, and readiness tests."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient


def _main_module():
    return importlib.import_module("app.main")


@pytest.mark.asyncio
async def test_api_token_accepts_bearer_or_swarm_header_and_rejects_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _main_module()
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(
            swarm_api_token="correct-token",
            require_api_token=True,
            protect_metrics=True,
        ),
    )

    await main.require_api_token("Bearer correct-token", None)
    await main.require_api_token(None, "correct-token")
    await main.require_metrics_token(None, "correct-token")

    with pytest.raises(HTTPException) as exc_info:
        await main.require_api_token("Bearer wrong-token", None)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_required_auth_without_a_configured_secret_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _main_module()
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(
            swarm_api_token="",
            require_api_token=True,
            protect_metrics=True,
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await main.require_api_token(None, None)
    assert exc_info.value.status_code == 503


def test_request_body_limit_rejects_declared_and_streamed_oversize_bodies() -> None:
    main = _main_module()
    mini = FastAPI()
    mini.add_middleware(main.RequestBodyLimitMiddleware, max_body_size=5)

    @mini.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    with TestClient(mini) as client:
        declared = client.post("/echo", content=b"123456")
        assert declared.status_code == 413
        assert "MAX_REQUEST_BODY_BYTES" in declared.json()["detail"]

        accepted = client.post("/echo", content=b"12345")
        assert accepted.status_code == 200
        assert accepted.json() == {"size": 5}


def test_idempotency_key_validation_is_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _main_module()
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(idempotency_key_max_chars=12),
    )

    assert main._validate_idempotency_key(" task:123 ") == "task:123"
    for invalid in ("", "space key", "x" * 13, "bad/key"):
        with pytest.raises(HTTPException) as exc_info:
            main._validate_idempotency_key(invalid)
        assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_model_checks_report_exact_missing_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _main_module()

    class Registry:
        @staticmethod
        def required_models_by_endpoint() -> dict[str, set[str]]:
            return {
                "http://one": {"coder", "reviewer"},
                "http://two": {"security"},
            }

    async def fake_list_models(endpoint: str) -> list[str]:
        if endpoint == "http://one":
            return ["coder"]
        raise RuntimeError("unreachable")

    monkeypatch.setattr(main, "list_models", fake_list_models)
    checks = await main._model_checks(Registry())

    assert checks["http://one"]["missing"] == ["reviewer"]
    assert checks["http://one"]["reachable"] is True
    assert checks["http://two"]["reachable"] is False
    assert checks["http://two"]["missing"] == ["security"]
