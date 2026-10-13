"""Per-model admission control for Litigation provider calls.

This is an in-process worker-slot scheduler.  It deliberately does not imply
that a provider has multiple remote replicas: each canonical provider/model
pair receives an independent bounded FIFO lane around the one provider route.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable


class DispatchError(RuntimeError):
    """Safe provider-facing error with a stable machine-readable category."""

    def __init__(self, message: str, *, code: str = "transient", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class DispatchConfig:
    capacity: int = 1
    max_pending: int = 8
    queue_wait: float = 45.0
    overall_timeout: float = 720.0


@dataclass
class _Lane:
    key: tuple[str, str]
    config: DispatchConfig
    semaphore: asyncio.Semaphore = field(init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: int = 0
    pending: int = 0
    last_used: float = field(default_factory=time.monotonic)
    counters: dict[str, int] = field(default_factory=lambda: {
        "admitted": 0, "completed": 0, "failed": 0, "cancelled": 0,
        "overloaded": 0, "queue_timeouts": 0, "retries": 0,
        "transport_fallbacks": 0, "context_recoveries": 0,
    })

    def __post_init__(self) -> None:
        self.semaphore = asyncio.Semaphore(self.config.capacity)


_LOOP_LANES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, OrderedDict[tuple[str, str], _Lane]]" = weakref.WeakKeyDictionary()
_MAX_LANES = 256
_MAX_COUNTER = 2_147_483_647


def _bump(lane: _Lane, name: str) -> None:
    lane.counters[name] = min(_MAX_COUNTER, lane.counters[name] + 1)


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(high, max(low, value))


def _bounded_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(high, max(low, value))


def _config_for(key: tuple[str, str]) -> DispatchConfig:
    values: dict[str, Any] = {
        "capacity": _bounded_int("LITIGATION_DISPATCH_CONCURRENCY", 1, 1, 32),
        "max_pending": _bounded_int("LITIGATION_DISPATCH_MAX_PENDING", 8, 0, 256),
        "queue_wait": _bounded_float("LITIGATION_DISPATCH_QUEUE_WAIT", 45.0, 0.05, 600.0),
        "overall_timeout": _bounded_float("LITIGATION_DISPATCH_OVERALL_TIMEOUT", 720.0, 1.0, 890.0),
    }
    raw = os.environ.get("LITIGATION_DISPATCH_MODEL_OVERRIDES", "").strip()
    if raw:
        try:
            overrides = json.loads(raw)
        except json.JSONDecodeError:
            overrides = {}
        if isinstance(overrides, dict):
            override = overrides.get(f"{key[0]}:{key[1]}")
            if isinstance(override, dict):
                for name in tuple(values):
                    if name in override:
                        candidate = override[name]
                        if isinstance(candidate, int) and not isinstance(candidate, bool):
                            values[name] = candidate
                        elif isinstance(candidate, float) and math.isfinite(candidate):
                            values[name] = candidate
    try:
        return DispatchConfig(
            capacity=min(32, max(1, int(values["capacity"]))),
            max_pending=min(256, max(0, int(values["max_pending"]))),
            queue_wait=min(600.0, max(0.05, float(values["queue_wait"]))),
            overall_timeout=min(890.0, max(1.0, float(values["overall_timeout"]))),
        )
    except (TypeError, ValueError, OverflowError):
        return DispatchConfig()


def _lanes() -> OrderedDict[tuple[str, str], _Lane]:
    loop = asyncio.get_running_loop()
    for existing in tuple(_LOOP_LANES):
        if existing.is_closed():
            del _LOOP_LANES[existing]
    lanes = _LOOP_LANES.get(loop)
    if lanes is None:
        lanes = OrderedDict()
        _LOOP_LANES[loop] = lanes
    return lanes


def _lane(key: tuple[str, str]) -> _Lane:
    lanes = _lanes()
    lane = lanes.get(key)
    if lane is not None:
        lanes.move_to_end(key)
        return lane
    if len(lanes) >= _MAX_LANES:
        for old_key, old_lane in tuple(lanes.items()):
            if old_lane.active == 0 and old_lane.pending == 0:
                del lanes[old_key]
                break
        else:
            raise DispatchError("provider dispatcher has too many active model lanes", code="overloaded")
    lane = _Lane(key, _config_for(key))
    lanes[key] = lane
    return lane


class _Lease:
    def __init__(self, lane: _Lane, queued_notice: Callable[[], Awaitable[None]] | None = None):
        self.lane = lane
        self.queued_notice = queued_notice
        self.acquired = False

    async def __aenter__(self) -> _Lane:
        lane = self.lane
        async with lane.lock:
            if lane.active + lane.pending >= lane.config.capacity + lane.config.max_pending:
                _bump(lane, "overloaded")
                raise DispatchError("selected model is busy; pending queue is full", code="overloaded")
            lane.pending += 1
            lane.last_used = time.monotonic()
        try:
            if self.queued_notice is not None and lane.active >= lane.config.capacity:
                await self.queued_notice()
            await asyncio.wait_for(lane.semaphore.acquire(), timeout=lane.config.queue_wait)
        except asyncio.TimeoutError as exc:
            async with lane.lock:
                lane.pending -= 1
                _bump(lane, "queue_timeouts")
            raise DispatchError("timed out waiting for the selected model", code="queue_timeout") from exc
        except BaseException:
            async with lane.lock:
                lane.pending -= 1
                _bump(lane, "cancelled")
            raise
        async with lane.lock:
            lane.pending -= 1
            lane.active += 1
            _bump(lane, "admitted")
        self.acquired = True
        return lane

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if not self.acquired:
            return
        lane = self.lane
        async with lane.lock:
            lane.active -= 1
            lane.last_used = time.monotonic()
            if exc_type is asyncio.CancelledError:
                _bump(lane, "cancelled")
            elif exc_type is None:
                _bump(lane, "completed")
            else:
                _bump(lane, "failed")
        lane.semaphore.release()


def record(key: tuple[str, str], counter: str) -> None:
    lane = _lane(key)
    if counter in lane.counters:
        _bump(lane, counter)


async def stream(
    key: tuple[str, str],
    operation: Callable[[Callable[[str], Awaitable[None]]], Awaitable[None]],
    queued_notice: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[str]:
    """Run ``operation`` in a lane-owned task and safely forward its deltas.

    The producer owns the lease and wall-clock timeout.  Closing or cancelling
    the consumer cancels and awaits that producer, which in turn closes the
    provider iterator/process before the slot is released.
    """
    output: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=64)

    async def emit(delta: str) -> None:
        await output.put(("delta", delta))

    async def produce() -> None:
        try:
            # Resolve and immediately reserve inside the producer.  A lane may
            # only be evicted while active+pending is zero, so no scheduled
            # producer can retain an unregistered lane reference.
            lane = _lane(key)
            async with _Lease(lane, queued_notice):
                try:
                    async with asyncio.timeout(lane.config.overall_timeout):
                        await operation(emit)
                except TimeoutError as exc:
                    raise DispatchError(
                        "selected model exceeded the overall response deadline",
                        code="timeout", retryable=True,
                    ) from exc
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # forwarded in-process; no diagnostics emitted here
            await output.put(("error", exc))
        else:
            await output.put(("done", None))

    task = asyncio.create_task(produce(), name=f"litigation-dispatch:{key[0]}:{key[1]}")
    try:
        while True:
            kind, value = await output.get()
            if kind == "delta":
                yield value
            elif kind == "error":
                raise value
            else:
                return
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def status() -> list[dict[str, Any]]:
    """Return safe status for the current event loop only."""
    rows: list[dict[str, Any]] = []
    for (provider, model), lane in _lanes().items():
        rows.append({
            "provider": provider,
            "model": model,
            "capacity": lane.config.capacity,
            "active": lane.active,
            "pending": lane.pending,
            "max_pending": lane.config.max_pending,
            "queue_wait_seconds": lane.config.queue_wait,
            "overall_timeout_seconds": lane.config.overall_timeout,
            "counters": dict(lane.counters),
        })
    return rows


def reset_for_tests() -> None:
    """Clear the current loop's inactive lanes (test helper)."""
    lanes = _lanes()
    if any(l.active or l.pending for l in lanes.values()):
        raise RuntimeError("cannot reset active dispatcher lanes")
    lanes.clear()
