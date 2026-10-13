from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock

import model_dispatch


async def _consume(iterator):
    return [item async for item in iterator]


class ModelDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        model_dispatch.reset_for_tests()

    async def test_same_key_serializes_while_models_run_independently(self) -> None:
        release = asyncio.Event()
        active: dict[tuple[str, str], int] = {}
        peaks: dict[tuple[str, str], int] = {}

        def operation(key):
            async def run(emit):
                active[key] = active.get(key, 0) + 1
                peaks[key] = max(peaks.get(key, 0), active[key])
                try:
                    await release.wait()
                    await emit(str(key))
                finally:
                    active[key] -= 1
            return run

        a = asyncio.create_task(_consume(model_dispatch.stream(("openai", "m1"), operation(("openai", "m1")))))
        b = asyncio.create_task(_consume(model_dispatch.stream(("openai", "m1"), operation(("openai", "m1")))))
        c = asyncio.create_task(_consume(model_dispatch.stream(("openai", "m2"), operation(("openai", "m2")))))
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(active[("openai", "m1")], 1)
        self.assertEqual(active[("openai", "m2")], 1)
        release.set()
        await asyncio.gather(a, b, c)
        self.assertEqual(peaks[("openai", "m1")], 1)

    async def test_queue_overload_timeout_and_cancel_release_reservations(self) -> None:
        release = asyncio.Event()

        async def blocked(_emit):
            await release.wait()

        env = {
            "LITIGATION_DISPATCH_CONCURRENCY": "1",
            "LITIGATION_DISPATCH_MAX_PENDING": "1",
            "LITIGATION_DISPATCH_QUEUE_WAIT": "0.05",
        }
        with mock.patch.dict(os.environ, env):
            model_dispatch.reset_for_tests()
            first = asyncio.create_task(_consume(model_dispatch.stream(("p", "m"), blocked)))
            for _ in range(4):
                await asyncio.sleep(0)
            queued = asyncio.create_task(_consume(model_dispatch.stream(("p", "m"), blocked)))
            for _ in range(4):
                await asyncio.sleep(0)
            with self.assertRaisesRegex(model_dispatch.DispatchError, "queue is full"):
                await _consume(model_dispatch.stream(("p", "m"), blocked))
            with self.assertRaisesRegex(model_dispatch.DispatchError, "waiting"):
                await queued
            status = model_dispatch.status()[0]
            self.assertEqual(status["pending"], 0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(model_dispatch.status()[0]["active"], 0)

    async def test_generator_close_cancels_producer_before_slot_release(self) -> None:
        stopped = asyncio.Event()

        async def operation(emit):
            try:
                await emit("first")
                await asyncio.Event().wait()
            finally:
                stopped.set()

        stream = model_dispatch.stream(("p", "m"), operation)
        self.assertEqual(await anext(stream), "first")
        await stream.aclose()
        self.assertTrue(stopped.is_set())
        self.assertEqual(model_dispatch.status()[0]["active"], 0)

    async def test_fifo_order_and_cancelled_waiter_cleanup(self) -> None:
        order: list[int] = []

        def operation(number):
            async def run(_emit):
                order.append(number)
                await asyncio.sleep(0.01)
            return run

        tasks = [asyncio.create_task(_consume(
            model_dispatch.stream(("p", "fifo"), operation(i))
        )) for i in range(4)]
        await asyncio.sleep(0)
        tasks[2].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(order, [0, 1, 3])
        row = model_dispatch.status()[0]
        self.assertEqual((row["active"], row["pending"]), (0, 0))

    async def test_capacity_override_allows_two_active(self) -> None:
        release = asyncio.Event()
        active = 0
        peak = 0

        async def operation(_emit):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await release.wait()
            finally:
                active -= 1

        with mock.patch.dict(os.environ, {"LITIGATION_DISPATCH_CONCURRENCY": "2"}):
            model_dispatch.reset_for_tests()
            tasks = [asyncio.create_task(_consume(
                model_dispatch.stream(("p", "two"), operation)
            )) for _ in range(3)]
            for _ in range(8):
                await asyncio.sleep(0)
            self.assertEqual(peak, 2)
            release.set()
            await asyncio.gather(*tasks)

    async def test_overall_deadline_is_not_extended_by_heartbeats(self) -> None:
        async def operation(emit):
            while True:
                await emit("heartbeat")
                await asyncio.sleep(0.03)

        with mock.patch.dict(os.environ, {"LITIGATION_DISPATCH_OVERALL_TIMEOUT": "1"}):
            model_dispatch.reset_for_tests()
            with self.assertRaisesRegex(model_dispatch.DispatchError, "deadline"):
                await _consume(model_dispatch.stream(("p", "heartbeat"), operation))
            self.assertEqual(model_dispatch.status()[0]["active"], 0)

    async def test_lane_limit_does_not_evict_scheduled_or_active_lane(self) -> None:
        release = asyncio.Event()
        live = 0
        active_by_name: dict[str, int] = {}
        peak_zero = 0

        def operation(name):
            async def run(_emit):
                nonlocal live, peak_zero
                live += 1
                active_by_name[name] = active_by_name.get(name, 0) + 1
                if name == "0":
                    peak_zero = max(peak_zero, active_by_name[name])
                try:
                    await release.wait()
                finally:
                    live -= 1
                    active_by_name[name] -= 1
            return run

        with mock.patch.object(model_dispatch, "_MAX_LANES", 2):
            tasks = [asyncio.create_task(
                _consume(model_dispatch.stream(("p", str(i)), operation(str(i)))),
                name="live-zero" if i == 0 else f"live-{i}",
            ) for i in range(3)]
            tasks.append(asyncio.create_task(
                _consume(model_dispatch.stream(("p", "0"), operation("0"))),
                name="live-zero",
            ))
            for _ in range(12):
                await asyncio.sleep(0)
            self.assertLessEqual(live, 2)
            self.assertLessEqual(len(model_dispatch.status()), 2)
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.assertEqual(peak_zero, 1)

    async def test_nonfinite_override_is_rejected(self) -> None:
        raw = '{"p:m":{"capacity":1e999,"queue_wait":NaN}}'
        with mock.patch.dict(os.environ, {"LITIGATION_DISPATCH_MODEL_OVERRIDES": raw}):
            model_dispatch.reset_for_tests()

            async def operation(emit):
                await emit("ok")

            self.assertEqual(await _consume(model_dispatch.stream(("p", "m"), operation)), ["ok"])
            row = model_dispatch.status()[0]
            self.assertEqual(row["capacity"], 1)
            self.assertEqual(row["queue_wait_seconds"], 45.0)


if __name__ == "__main__":
    unittest.main()
