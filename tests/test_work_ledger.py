from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest

from project_runtime import ProjectRuntime, ProjectRuntimeError
from project_store import ProjectStore, ProjectStoreError


class FakeGit:
    def __init__(self) -> None:
        self.head_sha = "a" * 40

    async def head(self, _project_id: str) -> str:
        return self.head_sha

    async def read_files(self, _project_id: str, _paths: list[str], _ref: str) -> list[dict]:
        return []

    async def list_files(self, _project_id: str, _ref: str | None = None) -> list[dict]:
        return []


class FakeProviders:
    def get_agent(self, agent_id: str) -> dict:
        return {"id": agent_id, "model": agent_id + "-model", "available": True}


class WorkLedgerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="litigation-work-ledger-")
        self.path = str(Path(self.temp.name) / "projects.sqlite3")
        self.store = ProjectStore(self.path)
        self.store.create_project("p1", "Project", "", "a" * 40)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def handoff(self, request: str, agent: str = "claude") -> dict:
        handoff = self.store.create_handoff(
            "p1", agent, "model", "agent", "Do work", [], [], [], None, [],
            "a" * 40, request,
        )
        self.assertTrue(self.store.claim_handoff(handoff["id"]))
        return self.store.get_handoff(handoff["id"])

    def work(self, request: str, paths: list[str]) -> dict:
        return self.store.create_work_item(
            "p1", request, "Task", "Implement task", ["tests pass"], paths, [],
        )

    def test_two_connections_claim_once_and_replay_is_idempotent(self) -> None:
        work = self.work("work-1", ["src"])
        handoff = self.handoff("handoff-1")
        stores = [ProjectStore(self.path), ProjectStore(self.path)]
        barrier = threading.Barrier(2)

        def claim(index: int) -> str:
            barrier.wait()
            try:
                stores[index].claim_work_item(
                    work["id"], f"attempt-{index}", "claude", handoff["id"]
                )
                return "won"
            except ProjectStoreError:
                return "lost"

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(claim, (0, 1)))
            self.assertEqual(sorted(results), ["lost", "won"])
            current = self.store.get_work_item(work["id"])
            replay = self.store.claim_work_item(
                work["id"], current["attempt_id"], "claude", handoff["id"]
            )
            self.assertEqual(replay["attempt_id"], current["attempt_id"])
        finally:
            for store in stores:
                store.close()

    def test_component_path_locks_release_only_after_terminal(self) -> None:
        first, second, separate = (
            self.work("w1", ["src"]), self.work("w2", ["src/pkg"]),
            self.work("w3", ["src2"]),
        )
        h1, h2, h3 = self.handoff("h1"), self.handoff("h2", "grok"), self.handoff("h3", "glm")
        self.store.claim_work_item(first["id"], "a1", "claude", h1["id"])
        with self.assertRaisesRegex(ProjectStoreError, "overlap"):
            self.store.claim_work_item(second["id"], "a2", "grok", h2["id"])
        self.store.claim_work_item(separate["id"], "a3", "glm", h3["id"])
        self.store.finish_handoff(h1["id"], "done", {"checks": []}, "completed")
        claimed = self.store.claim_work_item(second["id"], "a2", "grok", h2["id"])
        self.assertEqual(claimed["status"], "running")

    def test_claim_survives_restart_and_recovery_is_explicit(self) -> None:
        work, handoff = self.work("work", []), self.handoff("handoff")
        self.store.claim_work_item(work["id"], "attempt", "claude", handoff["id"])
        self.store.close()
        self.store = ProjectStore(self.path)
        self.store.recover_interrupted()
        self.assertEqual(self.store.get_work_item(work["id"])["status"], "blocked")
        recovered = self.store.recover_work_item(work["id"], "attempt", "operator retry")
        self.assertEqual(recovered["status"], "queued")

    def test_queued_link_cancel_blocks_work_without_orphan_lock(self) -> None:
        work = self.work("work", ["src"])
        handoff = self.store.create_handoff(
            "p1", "claude", "model", "agent", "Do work", [], [], [], None, [],
            "a" * 40, "pull", work_item_id=work["id"], work_attempt_id="attempt",
        )
        self.assertEqual(self.store.get_work_item(work["id"])["status"], "claimed")
        self.store.append_task_event(handoff["id"], "cancel_requested")
        self.assertEqual(self.store.get_work_item(work["id"])["status"], "blocked")

    def test_provenance_detects_missing_parent_and_immutability_trigger(self) -> None:
        handoff = self.store.create_handoff(
            "p1", "claude", "model", "analysis", "Review", [], [], [], None, [],
            "a" * 40, "handoff",
        )
        context = dict(handoff["context"])
        context["parent_handoff_id"] = "missing"
        with self.store._conn:
            self.store._conn.execute("drop trigger immutable_handoffs_update")
            self.store._conn.execute(
                "update handoffs set context_json=? where id=?",
                (json.dumps(context), handoff["id"]),
            )
        result = self.store.verify_local_provenance("p1")
        self.assertFalse(result["ok"])
        self.assertEqual({item["check"] for item in result["issues"]},
                         {"immutability_trigger", "parent_handoff"})


class WorkLedgerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="litigation-work-runtime-")
        root = Path(self.temp.name)
        self.store = ProjectStore(str(root / "projects.sqlite3"))
        self.store.create_project("p1", "Project", "", "a" * 40)
        self.runtime = ProjectRuntime(
            self.store, FakeGit(), state_root=root / "state", provider_module=FakeProviders(),
        )

    async def asyncTearDown(self) -> None:
        self.store.close()
        if self.runtime._owns_rules_store:
            self.runtime.rules_store.close()
        self.temp.cleanup()

    async def test_pull_is_atomic_and_exact_replay_returns_same_handoff(self) -> None:
        work = self.store.create_work_item("p1", "work", "Task", "Do it", [], ["src"], [])
        first = await self.runtime.pull_work_item(
            work["id"], request_id="pull", agent_id="claude"
        )
        replay = await self.runtime.pull_work_item(
            work["id"], request_id="pull", agent_id="claude"
        )
        self.assertEqual(first, replay)
        self.assertEqual(len(self.store.list_handoffs("p1")), 1)
        with self.assertRaises(ProjectRuntimeError):
            await self.runtime.pull_work_item(
                work["id"], request_id="pull", agent_id="grok"
            )

    async def test_two_concurrent_pulls_create_only_one_handoff(self) -> None:
        work = self.store.create_work_item("p1", "work", "Task", "Do it", [], [], [])
        results = await asyncio.gather(
            self.runtime.pull_work_item(work["id"], request_id="pull-a", agent_id="claude"),
            self.runtime.pull_work_item(work["id"], request_id="pull-b", agent_id="grok"),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
        self.assertEqual(len(self.store.list_handoffs("p1")), 1)


if __name__ == "__main__":
    unittest.main()
