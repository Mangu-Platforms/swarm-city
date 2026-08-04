"""Bounded, persistent lifecycle management for coding swarm tasks."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from .config import Settings, get_settings
from .metrics import ACTIVE_TASKS, QUEUED_TASKS, TASK_DURATION, TASKS_TOTAL
from .pipeline import Pipeline

TERMINAL_STATUSES = {"done", "error", "cancelled"}
log = logging.getLogger(__name__)


class QueueFullError(RuntimeError):
    """Raised when the configured queued-task capacity is exhausted."""


class IdempotencyConflict(RuntimeError):
    """Raised when a key is reused with a different request body."""


class _TaskState(dict):
    """Dictionary that persists itself after state mutations."""

    def __init__(self, *args, persist_callback=None, **kwargs) -> None:
        self._persist_callback = persist_callback
        self._suspend_persist = True
        super().__init__(*args, **kwargs)
        self._suspend_persist = False

    def _changed(self) -> None:
        if not self._suspend_persist and self._persist_callback is not None:
            self._persist_callback(self)

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self._changed()

    def update(self, *args, **kwargs) -> None:
        self._suspend_persist = True
        try:
            super().update(*args, **kwargs)
        finally:
            self._suspend_persist = False
        self._changed()


class TaskManager:
    """Queue tasks, limit concurrency, persist state, and enforce backpressure."""

    def __init__(
        self,
        pipeline: Pipeline,
        settings: Settings | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.settings = settings or get_settings()
        self.store_dir = Path(self.settings.task_store_dir).expanduser()
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.tasks: OrderedDict[str, _TaskState] = OrderedDict()
        self.handles: dict[str, asyncio.Task[None]] = {}
        self.idempotency: dict[str, tuple[str, str]] = {}
        self._slots = asyncio.Semaphore(self.settings.max_active_tasks)
        self._load_retained_states()
        self._refresh_metrics()

    def submit(self, payload: dict, *, idempotency_key: str | None = None) -> dict:
        """Create a queued task, enforcing capacity and idempotency."""

        fingerprint = self._fingerprint(payload)
        if idempotency_key:
            existing = self.idempotency.get(idempotency_key)
            if existing:
                task_id, previous_fingerprint = existing
                if previous_fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        "idempotency key was already used for a different request"
                    )
                state = self.tasks.get(task_id)
                if state is not None:
                    return self._submission_response(state, reused=True)

        queued_count = sum(
            state.get("status") == "queued" for state in self.tasks.values()
        )
        if queued_count >= self.settings.max_queued_tasks:
            raise QueueFullError(
                f"task queue is full ({queued_count}/{self.settings.max_queued_tasks})"
            )

        task_id = uuid.uuid4().hex[:16]
        now = time.time()
        state = _TaskState(
            {
                "task_id": task_id,
                "status": "queued",
                "phase": "queued",
                "created_at": now,
                "updated_at": now,
                "request_fingerprint": fingerprint,
                "idempotency_key": idempotency_key,
                "request": {
                    "task": payload["task"],
                    "mode": str(
                        getattr(
                            payload.get("mode", "auto"),
                            "value",
                            payload.get("mode", "auto"),
                        )
                    ),
                    "language": payload.get("language"),
                    "apply": bool(payload.get("apply")),
                    "expected_head": payload.get("expected_head"),
                    "allow_high_risk_paths": bool(
                        payload.get("allow_high_risk_paths")
                    ),
                    "context_paths": payload.get("context_paths", []),
                    "inline_context_file_count": len(
                        payload.get("context_files", {})
                    ),
                    "auto_context": payload.get("auto_context"),
                },
            },
            persist_callback=self._persist_state,
        )
        self.tasks[task_id] = state
        if idempotency_key:
            self.idempotency[idempotency_key] = (task_id, fingerprint)
        self._persist_state(state)
        TASKS_TOTAL.labels(status="submitted").inc()

        execution_payload = {**payload, "task_id": task_id}
        handle = asyncio.create_task(
            self._execute(task_id, execution_payload),
            name=f"swarm-task-{task_id}",
        )
        self.handles[task_id] = handle
        handle.add_done_callback(
            lambda completed, task_id=task_id: self._handle_completion(
                task_id, completed
            )
        )
        self._trim()
        self._refresh_metrics()
        return self._submission_response(state, reused=False)

    async def _execute(self, task_id: str, payload: dict) -> None:
        state = self.tasks[task_id]
        acquired = False
        started = state["created_at"]
        try:
            await self._slots.acquire()
            acquired = True
            state.update(
                status="running",
                phase="starting",
                started_at=time.time(),
                updated_at=time.time(),
            )
            self._refresh_metrics()
            result = await self.pipeline.run(payload, state)
            state.update(
                status="done",
                phase="done",
                result=result,
                finished_at=time.time(),
                updated_at=time.time(),
            )
            TASKS_TOTAL.labels(status="done").inc()
        except asyncio.CancelledError:
            log.info("task %s cancelled during phase %s", task_id, state.get("phase"))
            state.update(
                status="cancelled",
                phase="cancelled",
                finished_at=time.time(),
                updated_at=time.time(),
            )
            TASKS_TOTAL.labels(status="cancelled").inc()
            raise
        except TimeoutError:
            log.warning("task %s exceeded its timeout", task_id)
            state.update(
                status="error",
                phase="timed_out",
                error=(
                    "task exceeded TASK_TIMEOUT_S "
                    f"({self.settings.task_timeout_s} seconds)"
                ),
                finished_at=time.time(),
                updated_at=time.time(),
            )
            TASKS_TOTAL.labels(status="error").inc()
        except Exception as exc:  # noqa: BLE001 - task errors become API state
            log.exception("task %s failed during phase %s", task_id, state.get("phase"))
            state.update(
                status="error",
                phase="error",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=time.time(),
                updated_at=time.time(),
            )
            TASKS_TOTAL.labels(status="error").inc()
        finally:
            if acquired:
                self._slots.release()
            TASK_DURATION.observe(time.time() - started)
            self._trim()
            self._refresh_metrics()

    def _handle_completion(
        self,
        task_id: str,
        completed: asyncio.Task[None],
    ) -> None:
        """Finalize tasks cancelled before their coroutine begins executing."""

        self.handles.pop(task_id, None)
        state = self.tasks.get(task_id)
        if state is None:
            return
        if completed.cancelled() and state.get("status") not in TERMINAL_STATUSES:
            state.update(
                status="cancelled",
                phase="cancelled",
                finished_at=time.time(),
                updated_at=time.time(),
            )
            TASKS_TOTAL.labels(status="cancelled").inc()
        self._trim()
        self._refresh_metrics()

    def get(self, task_id: str) -> dict:
        """Return a detached copy of one retained task state."""

        try:
            return copy.deepcopy(dict(self.tasks[task_id]))
        except KeyError as exc:
            raise KeyError(f"unknown task_id: {task_id}") from exc

    def list(self, *, status: str | None = None, limit: int = 20) -> list[dict]:
        """Return newest task summaries without large result payloads."""

        summaries: list[dict] = []
        for state in reversed(self.tasks.values()):
            if status and state["status"] != status:
                continue
            summaries.append(
                copy.deepcopy(
                    {
                        key: value
                        for key, value in state.items()
                        if key not in {"result", "request_fingerprint"}
                    }
                )
            )
            if len(summaries) >= limit:
                break
        return summaries

    def cancel(self, task_id: str) -> bool:
        """Request cancellation for a queued or running task."""

        state = self.tasks.get(task_id)
        if state is None:
            raise KeyError(f"unknown task_id: {task_id}")
        if state["status"] in TERMINAL_STATUSES:
            return False
        handle = self.handles.get(task_id)
        if handle is None:
            return False
        state.update(phase="cancellation_requested", updated_at=time.time())
        return handle.cancel()

    async def close(self) -> None:
        """Cancel and collect all live task handles."""

        handles = list(self.handles.values())
        for handle in handles:
            handle.cancel()
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)
        self._refresh_metrics()

    def _submission_response(self, state: dict, *, reused: bool) -> dict:
        return {
            "task_id": state["task_id"],
            "status": state["status"],
            "status_url": f"/tasks/{state['task_id']}",
            "idempotent_reuse": reused,
        }

    @staticmethod
    def _fingerprint(payload: dict) -> str:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: getattr(value, "value", str(value)),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _state_path(self, task_id: str) -> Path:
        return self.store_dir / f"{task_id}.json"

    def _persist_state(self, state: dict) -> None:
        task_id = str(state.get("task_id", ""))
        if not re_full_task_id(task_id):
            return
        path = self._state_path(task_id)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        payload = json.dumps(dict(state), indent=2, sort_keys=True, default=str)
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(path)

    def _load_retained_states(self) -> None:
        loaded: list[dict] = []
        for path in self.store_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict) or not re_full_task_id(str(raw.get("task_id", ""))):
                continue
            if raw.get("status") not in TERMINAL_STATUSES:
                raw.update(
                    status="error",
                    phase="interrupted_by_restart",
                    error="orchestrator restarted before task completion",
                    finished_at=time.time(),
                    updated_at=time.time(),
                )
            loaded.append(raw)

        loaded.sort(key=lambda item: float(item.get("created_at", 0)))
        for raw in loaded[-self.settings.task_retention :]:
            state = _TaskState(raw, persist_callback=self._persist_state)
            task_id = state["task_id"]
            self.tasks[task_id] = state
            key = state.get("idempotency_key")
            fingerprint = state.get("request_fingerprint")
            if key and fingerprint:
                self.idempotency[str(key)] = (task_id, str(fingerprint))
            self._persist_state(state)
        self._trim()

    def _trim(self) -> None:
        while len(self.tasks) > self.settings.task_retention:
            removable = next(
                (
                    task_id
                    for task_id, state in self.tasks.items()
                    if state["status"] in TERMINAL_STATUSES
                ),
                None,
            )
            if removable is None:
                return
            state = self.tasks.pop(removable)
            key = state.get("idempotency_key")
            if key:
                self.idempotency.pop(str(key), None)
            try:
                self._state_path(removable).unlink()
            except FileNotFoundError:
                pass

    def _refresh_metrics(self) -> None:
        queued = sum(state.get("status") == "queued" for state in self.tasks.values())
        active = sum(state.get("status") == "running" for state in self.tasks.values())
        QUEUED_TASKS.set(queued)
        ACTIVE_TASKS.set(active)


def re_full_task_id(value: str) -> bool:
    return len(value) == 16 and all(character in "0123456789abcdef" for character in value)
