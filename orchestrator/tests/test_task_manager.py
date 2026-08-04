"""Task queue, idempotency, persistence, and cancellation tests."""
from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import pytest

from app.config import get_settings
from app.task_manager import IdempotencyConflict, QueueFullError, TaskManager


class _StubPipeline:
    async def run(self, payload, progress):
        del payload, progress
        return {"release_gate": {"status": "blocked", "reasons": []}}


class ControlledPipeline:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = 0

    async def run(self, payload, progress):
        del payload
        self.started += 1
        progress["phase"] = "controlled"
        await self.release.wait()
        return {"final": "done"}


class InstantPipeline:
    async def run(self, payload, progress):
        del payload
        progress["phase"] = "instant"
        return {"final": "done"}


def payload(task: str = "test task") -> dict:
    return {
        "task": task,
        "mode": "auto",
        "language": "python",
        "apply": False,
    }


async def _wait_for(manager: TaskManager, task_id: str, status: str) -> dict:
    for _ in range(100):
        state = manager.get(task_id)
        if state["status"] == status:
            return state
        await asyncio.sleep(0.01)
    raise AssertionError(f"task {task_id} did not reach {status}")


@pytest.mark.asyncio
async def test_manager_queues_and_cancels_tasks() -> None:
    pipeline = ControlledPipeline()
    manager = TaskManager(pipeline, get_settings())

    first = manager.submit(payload("first"))
    await _wait_for(manager, first["task_id"], "running")
    second = manager.submit(payload("second"))

    assert manager.get(second["task_id"])["status"] == "queued"
    assert manager.cancel(second["task_id"]) is True
    await _wait_for(manager, second["task_id"], "cancelled")

    pipeline.release.set()
    await _wait_for(manager, first["task_id"], "done")
    await manager.close()


@pytest.mark.asyncio
async def test_manager_enforces_queue_capacity_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_QUEUED_TASKS", "1")
    get_settings.cache_clear()
    pipeline = ControlledPipeline()
    manager = TaskManager(pipeline, get_settings())

    first = manager.submit(payload("first"), idempotency_key="same-key")
    reused = manager.submit(payload("first"), idempotency_key="same-key")
    assert reused["task_id"] == first["task_id"]
    assert reused["idempotent_reuse"] is True
    with pytest.raises(IdempotencyConflict):
        manager.submit(payload("different"), idempotency_key="same-key")

    await _wait_for(manager, first["task_id"], "running")
    manager.submit(payload("queued"))
    with pytest.raises(QueueFullError):
        manager.submit(payload("overflow"))

    pipeline.release.set()
    await manager.close()


@pytest.mark.asyncio
async def test_terminal_state_is_persisted_and_loaded_after_restart() -> None:
    manager = TaskManager(InstantPipeline(), get_settings())
    submitted = manager.submit(payload("persist me"), idempotency_key="persist-key")
    await _wait_for(manager, submitted["task_id"], "done")
    await manager.close()

    restarted = TaskManager(InstantPipeline(), get_settings())
    state = restarted.get(submitted["task_id"])
    assert state["status"] == "done"
    reused = restarted.submit(payload("persist me"), idempotency_key="persist-key")
    assert reused["task_id"] == submitted["task_id"]
    assert reused["idempotent_reuse"] is True
    await restarted.close()


async def test_corrupt_state_files_are_discarded_instead_of_blocking_startup(
    tmp_path: Path,
) -> None:
    """A bad file in the data volume must not put the container in a crash loop."""

    settings = get_settings()
    store = Path(settings.task_store_dir)
    store.mkdir(parents=True, exist_ok=True)
    (store / "0123456789abcdef.json").write_bytes(b"\xff\xfe not utf-8")
    (store / "fedcba9876543210.json").write_text(
        json.dumps({"task_id": "fedcba9876543210", "status": "done", "created_at": None}),
        encoding="utf-8",
    )
    (store / "abcdef0123456789.json").write_text(
        json.dumps({"task_id": "abcdef0123456789", "status": "done", "created_at": "yesterday"}),
        encoding="utf-8",
    )
    (store / "backup.json").write_text("{}", encoding="utf-8")
    (store / "leftover.tmp").write_text("partial", encoding="utf-8")

    manager = TaskManager(_StubPipeline(), settings)

    assert set(manager.tasks) == {"fedcba9876543210", "abcdef0123456789"}
    assert not (store / "leftover.tmp").exists()
    assert not (store / "backup.json").exists()


async def test_state_files_outside_retention_are_removed_from_disk(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """TASK_RETENTION bounds the audit store, not just process memory."""

    monkeypatch.setenv("TASK_RETENTION", "25")
    get_settings.cache_clear()
    settings = get_settings()
    store = Path(settings.task_store_dir)
    store.mkdir(parents=True, exist_ok=True)
    for index in range(40):
        task_id = f"{index:016x}"
        (store / f"{task_id}.json").write_text(
            json.dumps({"task_id": task_id, "status": "done", "created_at": float(index)}),
            encoding="utf-8",
        )

    manager = TaskManager(_StubPipeline(), settings)

    assert len(manager.tasks) == 25
    assert len(list(store.glob("*.json"))) == 25


async def test_persisted_state_is_private_to_the_owner(tmp_path: Path) -> None:
    settings = get_settings()
    manager = TaskManager(_StubPipeline(), settings)
    submission = manager.submit({"task": "persist permissions"})
    await asyncio.sleep(0)

    path = Path(settings.task_store_dir) / f"{submission['task_id']}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(settings.task_store_dir).stat().st_mode) == 0o700


async def test_idempotency_key_of_a_failed_task_is_replayable(tmp_path: Path) -> None:
    """A retry after a failure must run the work, not replay the failure."""

    settings = get_settings()
    manager = TaskManager(_StubPipeline(), settings)
    first = manager.submit({"task": "retryable work"}, idempotency_key="retry-me")
    manager.tasks[first["task_id"]].update(
        status="error",
        phase="interrupted_by_restart",
        error="orchestrator restarted before task completion",
    )

    second = manager.submit({"task": "retryable work"}, idempotency_key="retry-me")

    assert second["task_id"] != first["task_id"]
    assert second.get("idempotent_reuse") is not True
