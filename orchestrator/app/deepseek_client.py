"""DeepSeek V4 finalizer with retries and a cross-process token budget."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import get_settings
from .locks import file_lock

log = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {408, 409, 425, 429}


class BudgetExceeded(RuntimeError):
    """Raised before a request that would exceed the configured monthly cap."""


class RemoteProtocolError(RuntimeError):
    """Raised when the remote finalizer returns an unusable response."""


class BudgetLedgerError(RuntimeError):
    """Raised when the token ledger cannot be read and spend cannot be bounded."""


def _as_count(value: object) -> int:
    """Coerce a persisted non-negative count, tolerating floats and junk."""

    try:
        return max(0, int(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_seconds(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


class TokenBudget:
    """Track committed and in-flight monthly token use across processes."""

    def __init__(
        self,
        path: str,
        monthly_limit: int,
        *,
        reservation_ttl_s: int = 3600,
    ) -> None:
        self.path = Path(path).expanduser()
        self.limit = monthly_limit
        self.reservation_ttl_s = max(60, reservation_ttl_s)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @staticmethod
    def _month_key() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _load_unlocked(self) -> dict:
        """Read the ledger, failing closed when it cannot be trusted.

        Returning an empty ledger on a read error would silently reset the
        month to zero and then persist that reset on the next write, turning a
        transient I/O problem into an unbounded spend.
        """

        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise BudgetLedgerError(
                f"token budget ledger at {self.path} is unreadable: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise BudgetLedgerError(
                f"token budget ledger at {self.path} is not a JSON object"
            )

        normalized: dict[str, dict] = {}
        for month, value in raw.items():
            if isinstance(value, (int, float)):
                normalized[str(month)] = {
                    "used": _as_count(value),
                    "reservations": {},
                }
                continue
            if not isinstance(value, dict):
                continue
            reservations = value.get("reservations", {})
            if not isinstance(reservations, dict):
                reservations = {}
            normalized[str(month)] = {
                "used": _as_count(value.get("used", 0)),
                "reservations": {
                    str(reservation_id): {
                        "tokens": _as_count(record.get("tokens", 0)),
                        "created_at": _as_seconds(record.get("created_at", 0)),
                    }
                    for reservation_id, record in reservations.items()
                    if isinstance(record, dict)
                },
            }
        return normalized

    def _write_unlocked(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        payload = json.dumps(data, indent=2, sort_keys=True)
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.path)

    def _prune_unlocked(self, month_state: dict) -> None:
        cutoff = time.time() - self.reservation_ttl_s
        reservations = month_state.setdefault("reservations", {})
        stale = [
            reservation_id
            for reservation_id, record in reservations.items()
            if float(record.get("created_at", 0)) < cutoff
        ]
        for reservation_id in stale:
            reservations.pop(reservation_id, None)

    def reserve(self, estimated_tokens: int) -> str:
        """Atomically reserve a worst-case request budget before network I/O.

        The returned handle carries the month it was booked against, so a
        request that straddles midnight UTC on the first settles against the
        month it reserved from instead of orphaning tokens in the old month.
        """

        estimated = max(1, int(estimated_tokens))
        reservation_id = uuid.uuid4().hex
        with file_lock(self.lock_path, timeout_s=30):
            data = self._load_unlocked()
            key = self._month_key()
            month = data.setdefault(key, {"used": 0, "reservations": {}})
            self._prune_unlocked(month)
            reserved = sum(
                int(record.get("tokens", 0))
                for record in month.get("reservations", {}).values()
            )
            used = int(month.get("used", 0))
            if used + reserved + estimated > self.limit:
                raise BudgetExceeded(
                    "remote finalizer monthly token budget would be exceeded "
                    f"({used} used + {reserved} reserved + {estimated} requested "
                    f"> {self.limit} limit)"
                )
            month.setdefault("reservations", {})[reservation_id] = {
                "tokens": estimated,
                "created_at": time.time(),
            }
            self._write_unlocked(data)
        return f"{key}:{reservation_id}"

    @staticmethod
    def _split_handle(handle: str) -> tuple[str, str]:
        key, separator, reservation_id = handle.partition(":")
        if not separator:
            return TokenBudget._month_key(), handle
        return key, reservation_id

    def settle(self, handle: str, actual_tokens: int) -> None:
        """Convert one reservation into committed usage in its own month."""

        actual = max(0, int(actual_tokens))
        key, reservation_id = self._split_handle(handle)
        with file_lock(self.lock_path, timeout_s=30):
            data = self._load_unlocked()
            month = data.setdefault(key, {"used": 0, "reservations": {}})
            month.setdefault("reservations", {}).pop(reservation_id, None)
            month["used"] = _as_count(month.get("used", 0)) + actual
            self._write_unlocked(data)

    def release(self, handle: str) -> None:
        """Release an in-flight reservation that provably cost nothing."""

        key, reservation_id = self._split_handle(handle)
        with file_lock(self.lock_path, timeout_s=30):
            data = self._load_unlocked()
            month = data.setdefault(key, {"used": 0, "reservations": {}})
            month.setdefault("reservations", {}).pop(reservation_id, None)
            self._write_unlocked(data)

    def used(self) -> int:
        """Return committed usage for the current UTC month."""

        with file_lock(self.lock_path, timeout_s=30):
            data = self._load_unlocked()
            month = data.get(self._month_key(), {})
            return int(month.get("used", 0)) if isinstance(month, dict) else 0

    def reserved(self) -> int:
        """Return active reservations for the current UTC month."""

        with file_lock(self.lock_path, timeout_s=30):
            data = self._load_unlocked()
            month = data.get(self._month_key(), {})
            if not isinstance(month, dict):
                return 0
            self._prune_unlocked(month)
            return sum(
                int(record.get("tokens", 0))
                for record in month.get("reservations", {}).values()
            )

    # Compatibility helpers used by tests and operators.
    def check(self, estimated: int = 0) -> None:
        reservation = self.reserve(max(1, estimated))
        self.release(reservation)

    def record(self, tokens: int) -> None:
        if tokens <= 0:
            return
        reservation = self.reserve(tokens)
        self.settle(reservation, tokens)


class DeepSeekClient:
    """Streaming client used for one optional remote synthesis call."""

    def __init__(self) -> None:
        settings = get_settings()
        self.settings = settings
        self.budget = TokenBudget(
            settings.deepseek_budget_file,
            settings.deepseek_monthly_token_budget,
            reservation_ttl_s=max(3600, settings.deepseek_request_timeout_s * 2),
        )

    async def finalize(self, system_prompt: str, user_prompt: str) -> dict:
        """Run a budget-reserved DeepSeek V4 finalization request."""

        settings = self.settings
        if not settings.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        input_chars = len(system_prompt) + len(user_prompt)
        if input_chars > settings.max_agent_input_chars:
            raise ValueError(
                f"remote finalizer input exceeds MAX_AGENT_INPUT_CHARS "
                f"({input_chars} > {settings.max_agent_input_chars})"
            )

        input_estimate = max(1, input_chars // 4)
        request_estimate = input_estimate + settings.deepseek_max_tokens
        payload: dict = {
            "model": settings.deepseek_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": settings.deepseek_max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "thinking": {"type": settings.deepseek_thinking},
        }
        if settings.deepseek_thinking == "enabled":
            payload["reasoning_effort"] = settings.deepseek_reasoning_effort
        else:
            payload["temperature"] = 0.1

        headers = {
            "Authorization": f"Bearer {settings.deepseek_api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        url = f"{settings.deepseek_base_url.rstrip('/')}/chat/completions"

        response = await self._with_retries(
            url,
            headers,
            payload,
            request_estimate=request_estimate,
            input_estimate=input_estimate,
        )
        return response

    async def _with_retries(
        self,
        url: str,
        headers: dict,
        payload: dict,
        *,
        request_estimate: int,
        input_estimate: int,
    ) -> dict:
        """Attempt the request, reserving and settling budget per attempt.

        Every attempt is a real generation the provider bills for, so one
        reservation cannot cover the whole retry sequence: four attempts under
        a single reservation can overshoot the monthly cap several times over.
        A failed attempt settles at the input estimate rather than releasing to
        zero, because the prompt was sent and charged even when no usable
        response came back.
        """

        last_error: Exception | None = None
        attempts = self.settings.deepseek_retries + 1
        for attempt in range(attempts):
            handle = await asyncio.to_thread(self.budget.reserve, request_estimate)
            try:
                response = await self._stream_once(url, headers, payload)
            except asyncio.CancelledError:
                await asyncio.to_thread(self.budget.release, handle)
                raise
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                # The request reached the provider, so the prompt was charged.
                await asyncio.to_thread(self.budget.settle, handle, input_estimate)
                retryable = status in RETRYABLE_STATUS_CODES or status >= 500
                if not retryable:
                    raise RuntimeError(
                        f"remote finalizer rejected request with HTTP {status}"
                    ) from exc
            except httpx.TransportError as exc:
                last_error = exc
                # A transport failure may or may not have reached the provider.
                await asyncio.to_thread(self.budget.release, handle)
            except RemoteProtocolError as exc:
                last_error = exc
                await asyncio.to_thread(self.budget.settle, handle, input_estimate)
            else:
                usage = response.get("usage", {})
                spent = int(
                    usage.get("total_tokens")
                    or input_estimate + max(1, len(response["content"]) // 4)
                )
                await asyncio.to_thread(self.budget.settle, handle, spent)
                response["charged_tokens"] = spent
                response["estimated_usage"] = not bool(usage.get("total_tokens"))
                return response

            if attempt < attempts - 1:
                delay = 2**attempt + random.uniform(0, 0.5)
                log.warning(
                    "remote finalizer attempt %d failed: %s; retrying in %.2fs",
                    attempt + 1,
                    last_error,
                    delay,
                )
                await asyncio.sleep(delay)

        raise RuntimeError(f"remote finalization failed after retries: {last_error}")

    async def _stream_once(
        self,
        url: str,
        headers: dict,
        payload: dict,
    ) -> dict:
        chunks: list[str] = []
        usage: dict = {}
        finish_reason = ""
        malformed_events = 0
        output_chars = 0
        started = time.monotonic()
        timeout = httpx.Timeout(
            self.settings.deepseek_request_timeout_s,
            connect=15,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                url,
                headers=headers,
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError as exc:
                        malformed_events += 1
                        if malformed_events > 3:
                            raise RemoteProtocolError(
                                "remote finalizer returned malformed stream events"
                            ) from exc
                        continue
                    if not isinstance(event, dict):
                        continue
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    for choice in event.get("choices", []):
                        if not isinstance(choice, dict):
                            continue
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
                        delta = choice.get("delta", {})
                        content = delta.get("content") if isinstance(delta, dict) else None
                        if isinstance(content, str):
                            output_chars += len(content)
                            if output_chars > self.settings.max_agent_output_chars:
                                raise RemoteProtocolError(
                                    "remote finalizer output exceeds "
                                    "MAX_AGENT_OUTPUT_CHARS"
                                )
                            chunks.append(content)

        content = "".join(chunks).strip()
        if not content:
            raise RemoteProtocolError("remote finalizer returned empty content")
        if finish_reason in {"length", "max_tokens"}:
            raise RemoteProtocolError("remote finalizer output was truncated")
        return {
            "content": content,
            "usage": usage,
            "finish_reason": finish_reason,
            "latency_s": round(time.monotonic() - started, 3),
        }
