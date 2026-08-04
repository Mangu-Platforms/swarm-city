"""FastAPI control plane for the repository-aware coding swarm."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .agents import AgentRegistry
from .config import get_settings
from .metrics import QUEUE_REJECTIONS
from .models import ContextPreviewRequest, TaskRequest
from .ollama_client import list_models
from .pipeline import Pipeline
from .task_manager import (
    TERMINAL_STATUSES,
    IdempotencyConflict,
    QueueFullError,
    TaskManager,
)

settings = get_settings()
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("orchestrator")
_TASK_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


class RequestBodyLimitMiddleware:
    """Reject content-length and streamed bodies beyond the configured maximum."""

    def __init__(self, app, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > self.max_body_size:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                await self._reject(scope, receive, send)
                return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_size:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope, receive, send) -> None:
        response = JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content={"detail": "request body exceeds MAX_REQUEST_BODY_BYTES"},
        )
        await response(scope, receive, send)


class _BodyTooLarge(Exception):
    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize shared orchestration state and cancel tasks on shutdown."""

    registry = AgentRegistry()
    pipeline = Pipeline(registry, settings)
    manager = TaskManager(pipeline, settings)
    app.state.registry = registry
    app.state.pipeline = pipeline
    app.state.task_manager = manager
    log.info("orchestrator ready with %d agents", len(registry.agents))
    try:
        yield
    finally:
        await manager.close()


app = FastAPI(
    title="LLM Swarm Coding Orchestrator",
    version="3.0.0",
    description=(
        "Repository-aware coding swarm with independent builders, schema-bound "
        "quality and security review, final release gates, durable queue state, "
        "and isolated git worktree verification."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_body_size=settings.max_request_body_bytes,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    return response


def _manager(request: Request) -> TaskManager:
    manager = getattr(request.app.state, "task_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="orchestrator is not ready",
        )
    return manager


def _registry(request: Request) -> AgentRegistry:
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="orchestrator is not ready",
        )
    return registry


def _token_matches(candidate: str | None, expected: str) -> bool:
    if candidate is None:
        return False
    return secrets.compare_digest(candidate.encode(), expected.encode())


async def require_api_token(
    authorization: Annotated[str | None, Header()] = None,
    x_swarm_token: Annotated[str | None, Header()] = None,
) -> None:
    """Enforce bearer or X-Swarm-Token authentication when configured."""

    expected = settings.swarm_api_token
    if not expected and not settings.require_api_token:
        return
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API token authentication is required but not configured",
        )
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    if not _token_matches(bearer, expected) and not _token_matches(
        x_swarm_token,
        expected,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_metrics_token(
    authorization: Annotated[str | None, Header()] = None,
    x_swarm_token: Annotated[str | None, Header()] = None,
) -> None:
    if not settings.protect_metrics:
        return
    await require_api_token(authorization, x_swarm_token)


def _validate_task_id(task_id: str) -> None:
    if not _TASK_ID_RE.fullmatch(task_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown task_id",
        )


def _validate_idempotency_key(key: str | None) -> str | None:
    if key is None:
        return None
    normalized = key.strip()
    if (
        not normalized
        or len(normalized) > settings.idempotency_key_max_chars
        or not _IDEMPOTENCY_RE.fullmatch(normalized)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Idempotency-Key must contain only letters, digits, dot, underscore, "
                "colon, or hyphen and fit IDEMPOTENCY_KEY_MAX_CHARS"
            ),
        )
    return normalized


@app.post(
    "/tasks",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_token)],
)
@app.post(
    "/task",
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
    dependencies=[Depends(require_api_token)],
)
async def submit_task(
    request: Request,
    payload: TaskRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    """Queue a coding task and return its durable status URL."""

    if payload.apply and not settings.enable_git_apply:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="apply=true requires ENABLE_GIT_APPLY=true",
        )
    key = _validate_idempotency_key(idempotency_key)
    try:
        return _manager(request).submit(
            payload.model_dump(mode="json"),
            idempotency_key=key,
        )
    except QueueFullError as exc:
        QUEUE_REJECTIONS.inc()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": "10"},
        ) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@app.get("/tasks", dependencies=[Depends(require_api_token)])
async def list_tasks(
    request: Request,
    task_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """List recent task summaries."""

    allowed = TERMINAL_STATUSES | {"queued", "running"}
    if task_status is not None and task_status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"status must be one of: {', '.join(sorted(allowed))}",
        )
    return {"tasks": _manager(request).list(status=task_status, limit=limit)}


@app.get("/tasks/{task_id}", dependencies=[Depends(require_api_token)])
@app.get(
    "/status/{task_id}",
    include_in_schema=False,
    dependencies=[Depends(require_api_token)],
)
async def get_task(request: Request, task_id: str) -> dict:
    """Return live progress or the completed task result."""

    _validate_task_id(task_id)
    try:
        return _manager(request).get(task_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@app.delete(
    "/tasks/{task_id}",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_token)],
)
async def cancel_task(request: Request, task_id: str) -> dict:
    """Cancel a queued or running task, including git workers."""

    _validate_task_id(task_id)
    manager = _manager(request)
    try:
        cancelled = manager.cancel(task_id)
        state = manager.get(task_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return {
        "task_id": task_id,
        "cancel_requested": cancelled,
        "status": state["status"],
        "phase": state["phase"],
    }


@app.post("/context/preview", dependencies=[Depends(require_api_token)])
async def preview_context(
    request: Request,
    payload: ContextPreviewRequest,
) -> dict:
    """Preview repository selection and redaction metadata without model calls."""

    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="orchestrator is not ready",
        )
    bundle = await asyncio.to_thread(
        pipeline.context_builder.build,
        payload.task,
        language=payload.language,
        context_paths=payload.context_paths,
        auto_context=payload.auto_context,
    )
    return {
        **bundle.metadata(),
        "repository_map": bundle.repository_map,
        "selected_file_sizes": {
            path: len(content) for path, content in bundle.files.items()
        },
    }


@app.get("/model-list", dependencies=[Depends(require_api_token)])
async def model_list(request: Request) -> dict:
    """Return logical agents and exact model availability by endpoint."""

    registry = _registry(request)
    endpoint_results = await _model_checks(registry)
    return {"agents": registry.summary(), "endpoints": endpoint_results}


async def _model_checks(registry: AgentRegistry) -> dict:
    required = registry.required_models_by_endpoint()

    async def fetch(endpoint: str, expected: set[str]) -> tuple[str, dict]:
        try:
            available = set(await list_models(endpoint))
        except Exception as exc:  # noqa: BLE001 - readiness diagnostics
            return endpoint, {
                "reachable": False,
                "required": sorted(expected),
                "available": [],
                "missing": sorted(expected),
                "error": f"{type(exc).__name__}: {exc}",
            }
        return endpoint, {
            "reachable": True,
            "required": sorted(expected),
            "available": sorted(available),
            "missing": sorted(expected - available),
            "error": None,
        }

    results = await asyncio.gather(
        *(fetch(endpoint, expected) for endpoint, expected in required.items())
    )
    return dict(results)


@app.get("/healthz")
async def healthz(request: Request) -> dict:
    """Liveness probe that does not depend on model availability."""

    registry = getattr(request.app.state, "registry", None)
    manager = getattr(request.app.state, "task_manager", None)
    return {
        "ok": registry is not None and manager is not None,
        "version": app.version,
        "agents": len(registry.agents) if registry else 0,
    }


@app.get("/readyz")
async def readyz(request: Request, response: Response) -> dict:
    """Readiness probe that verifies the configured model roster."""

    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"ready": False, "reason": "registry not initialized"}

    endpoints = await _model_checks(registry)
    reachable = [entry for entry in endpoints.values() if entry["reachable"]]
    missing = {
        endpoint: entry["missing"]
        for endpoint, entry in endpoints.items()
        if entry["missing"]
    }
    if settings.readiness_require_all_models:
        ready = len(reachable) == len(endpoints) and not missing
    else:
        ready = bool(reachable) and any(not entry["missing"] for entry in reachable)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "ready": ready,
        "version": app.version,
        "agents": len(registry.agents),
        "configured_endpoints": len(endpoints),
        "reachable_endpoints": len(reachable),
        "missing_models": missing,
        "endpoints": endpoints,
    }


@app.get("/version")
async def version() -> dict:
    return {
        "name": "llm-swarm",
        "version": app.version,
        "pipeline_schema": "3.0",
    }


@app.get("/metrics", dependencies=[Depends(require_metrics_token)])
async def metrics() -> PlainTextResponse:
    """Expose Prometheus metrics, optionally behind the API token."""

    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
