"""Task queue, idempotency, persistence, and cancellation tests."""
from __future__ import annotations

import asyncio

import pytest

from app.config import get_settings
from app.task_manager import IdempotencyConflict, QueueFullError, TaskManager


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
