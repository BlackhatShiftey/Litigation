from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from project_store import ProjectStore


class FakeGit:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.heads: dict[str, str] = {}
        self.files = {
            "README.md": {"path": "README.md", "sha256": "abc", "size": 12, "content": "project text"}
        }
        self.proposals: dict[str, dict[str, Any]] = {}
        self.apply_calls = 0

    async def create_project(self, project_id: str, source_path: str | None = None) -> dict[str, Any]:
        self.heads[project_id] = "base-commit"
        return {"head": "base-commit", "source_description": source_path or "", "excluded": []}

    async def head(self, project_id: str) -> str:
        return self.heads[project_id]

    async def list_files(self, project_id: str, ref: str | None = None) -> list[dict[str, Any]]:
        return [{key: value for key, value in item.items() if key != "content"} for item in self.files.values()]

    async def read_files(self, project_id: str, paths: list[str], ref: str) -> list[dict[str, Any]]:
        return [dict(self.files[path]) for path in paths]

    async def create_proposal(
        self, project_id: str, handoff_id: str, base_commit: str,
        files: list[dict[str, Any]], summary: str,
    ) -> dict[str, Any]:
        proposal = {
            "id": "proposal-" + handoff_id,
            "base_commit": base_commit,
            "branch": "codex/" + handoff_id,
            "commit_sha": "proposal-commit",
            "summary": summary,
            "diff_text": "diff",
            "changed_paths": [item["path"] for item in files],
        }
        self.proposals[handoff_id] = proposal
        return proposal

    async def apply_proposal(
        self, project_id: str, proposal: dict[str, Any], expected_main: str
    ) -> dict[str, str]:
        self.apply_calls += 1
        if self.heads[project_id] != expected_main:
            raise RuntimeError("stale")
        self.heads[project_id] = proposal["commit_sha"]
        return {"previous_head": expected_main, "head": proposal["commit_sha"]}


class FakeProviders:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, str]], dict[str, Any]]] = []
        self.block: asyncio.Event | None = None
        self.fail_agents: set[str] = set()
        self.responses: dict[str, str] = {}

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return {"id": agent_id, "model": agent_id + "-model", "available": True}

    async def complete(self, agent: dict[str, Any], messages: list[dict[str, str]], **kwargs: Any) -> str:
        self.calls.append((agent["id"], messages, kwargs))
        if self.block is not None:
            await self.block.wait()
        if agent["id"] in self.fail_agents:
            raise RuntimeError("secret provider diagnostic")
        return self.responses.get(agent["id"], "response from " + agent["id"])


async def wait_terminal(store: ProjectStore, handoff_id: str, timeout: float = 3.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        item = store.get_handoff(handoff_id)
        if item and item["status"] in {"completed", "needs_revision", "failed", "interrupted", "cancelled"}:
            return item
        await asyncio.sleep(0.02)
    raise AssertionError("handoff did not reach a terminal state")


class ProjectRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Import after local_git exists in the integrated worker patch.
        from project_runtime import ProjectRuntime

        self.tempdir = tempfile.TemporaryDirectory(prefix="litigation-runtime-test-")
        self.root = Path(self.tempdir.name)
        self.store = ProjectStore(str(self.root / "projects.sqlite3"))
        self.git = FakeGit(self.root)
        self.providers = FakeProviders()
        self.runtime = ProjectRuntime(
            self.store, self.git, state_root=self.root,
            provider_module=self.providers, poll_interval=0.01,
        )
        await self.runtime.start()
        self.project = await self.runtime.create_project("Test")

    async def asyncTearDown(self) -> None:
        await self.runtime.stop()
        self.tempdir.cleanup()

    async def test_context_is_snapshotted_and_reaches_provider(self) -> None:
        issue = self.store.create_issue(self.project["id"], "Bug", "exact issue body")
        first = await self.runtime.create_handoff(
            self.project["id"], request_id="first", agent_id="claude", model=None,
            mode="analysis", instructions="first analysis", acceptance=[], file_paths=[],
            issue_ids=[], parent_handoff_id=None, source_output_ids=[],
        )
        await wait_terminal(self.store, first["id"])
        output = self.store.list_outputs(first["id"])[0]
        second = await self.runtime.create_handoff(
            self.project["id"], request_id="second", agent_id="grok", model=None,
            mode="review", instructions="review context", acceptance=["be precise"],
            file_paths=["README.md"], issue_ids=[issue["id"]],
            parent_handoff_id=first["id"], source_output_ids=[output["id"]],
        )
        await wait_terminal(self.store, second["id"])
        stored = self.store.get_handoff(second["id"])
        self.assertEqual(stored["model"], "grok-model")
        self.assertEqual(stored["context"]["file_context"][0]["content"], "project text")
        prompt = self.providers.calls[-1][1][1]["content"]
        self.assertIn("exact issue body", prompt)
        self.assertIn("response from claude", prompt)
        self.assertIn("project text", prompt)
        self.assertFalse(self.providers.calls[-1][2]["coding"])

    async def test_cancel_waits_and_preserves_terminal_record(self) -> None:
        self.providers.block = asyncio.Event()
        handoff = await self.runtime.create_handoff(
            self.project["id"], request_id="cancel", agent_id="claude", model=None,
            mode="analysis", instructions="wait", acceptance=[], file_paths=[], issue_ids=[],
            parent_handoff_id=None, source_output_ids=[],
        )
        while self.store.get_handoff(handoff["id"])["status"] != "running":
            await asyncio.sleep(0.01)
        result = await self.runtime.cancel_handoff(handoff["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.store.list_outputs(handoff["id"])[0]["final_state"], "cancelled")

    async def test_debate_persists_peer_failure_and_one_rebuttal(self) -> None:
        self.providers.fail_agents.add("grok")
        debate = await self.runtime.create_debate(
            self.project["id"], request_id="debate-one", question="A or B?",
            agent_ids=["claude", "grok"], source_output_ids=[], issue_ids=[],
        )
        for handoff_id in debate["handoff_ids"]:
            await wait_terminal(self.store, handoff_id)
        deadline = asyncio.get_running_loop().time() + 3
        while len(self.store.list_handoffs(self.project["id"])) < 3:
            if asyncio.get_running_loop().time() > deadline:
                self.fail("rebuttal was not durably scheduled")
            await asyncio.sleep(0.02)
        handoffs = self.store.list_handoffs(self.project["id"])
        self.assertEqual(len(handoffs), 3)
        self.assertEqual(sum(item["status"] == "failed" for item in handoffs), 1)
        rebuttal = next(item for item in handoffs if ":rebuttal:" in item["request_id"])
        self.assertEqual(rebuttal["agent_id"], "claude")
        self.assertTrue(rebuttal["source_output_ids"])

    async def test_event_paging_covers_more_than_five_hundred(self) -> None:
        for index in range(510):
            self.store.append_project_event(
                self.project["id"], "test", str(index), "note", {"index": index}
            )
        events = self.runtime._all_events(self.project["id"])
        self.assertGreaterEqual(len(events), 511)
        self.assertEqual(events[-1]["payload"]["index"], 509)

    async def test_provider_failure_is_sanitized(self) -> None:
        self.providers.fail_agents.add("glm")
        handoff = await self.runtime.create_handoff(
            self.project["id"], request_id="failure", agent_id="glm", model=None,
            mode="analysis", instructions="fail", acceptance=[], file_paths=[], issue_ids=[],
            parent_handoff_id=None, source_output_ids=[],
        )
        await wait_terminal(self.store, handoff["id"])
        events = self.runtime._all_events(self.project["id"])
        serialized = json.dumps(events)
        self.assertNotIn("secret provider diagnostic", serialized)
        self.assertIn("subscription provider request failed", serialized)

    async def test_request_id_returns_original_before_resnapshot(self) -> None:
        first = await self.runtime.create_handoff(
            self.project["id"], request_id="stable-request", agent_id="claude", model=None,
            mode="analysis", instructions="same", acceptance=[], file_paths=[], issue_ids=[],
            parent_handoff_id=None, source_output_ids=[],
        )
        self.git.heads[self.project["id"]] = "changed-main"
        original_get_agent = self.providers.get_agent
        self.providers.get_agent = lambda agent_id: {
            **original_get_agent(agent_id), "model": "changed-default"
        }
        repeated = await self.runtime.create_handoff(
            self.project["id"], request_id="stable-request", agent_id="claude", model=None,
            mode="analysis", instructions="same", acceptance=[], file_paths=[], issue_ids=[],
            parent_handoff_id=None, source_output_ids=[],
        )
        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual(repeated["base_commit"], "base-commit")
        with self.assertRaisesRegex(Exception, "different handoff"):
            await self.runtime.create_handoff(
                self.project["id"], request_id="stable-request", agent_id="claude", model=None,
                mode="analysis", instructions="changed", acceptance=[], file_paths=[], issue_ids=[],
                parent_handoff_id=None, source_output_ids=[],
            )

    async def test_invalid_code_output_is_preserved_for_revision(self) -> None:
        self.providers.responses["claude"] = "not a code envelope"
        handoff = await self.runtime.create_handoff(
            self.project["id"], request_id="invalid-code", agent_id="claude", model=None,
            mode="code", instructions="change it", acceptance=[], file_paths=[], issue_ids=[],
            parent_handoff_id=None, source_output_ids=[],
        )
        terminal = await wait_terminal(self.store, handoff["id"])
        self.assertEqual(terminal["status"], "needs_revision")
        outputs = self.store.list_outputs(handoff["id"])
        self.assertEqual(outputs[0]["raw_text"], "not a code envelope")
        self.assertIsNone(outputs[0]["parsed"])

    async def test_code_output_becomes_proposal_then_explicit_apply(self) -> None:
        self.providers.responses["claude"] = json.dumps({
            "version": 1,
            "summary": "Add a file",
            "files": [{
                "path": "new.txt", "operation": "write",
                "base_sha256": None, "content": "new content\n",
            }],
            "acceptance_notes": ["file supplied"],
            "checks": [{"name": "unit tests", "status": "not_run"}],
        })
        handoff = await self.runtime.create_handoff(
            self.project["id"], request_id="valid-code", agent_id="claude", model=None,
            mode="code", instructions="add it", acceptance=[], file_paths=[], issue_ids=[],
            parent_handoff_id=None, source_output_ids=[],
        )
        terminal = await wait_terminal(self.store, handoff["id"])
        self.assertEqual(terminal["status"], "completed")
        output = self.store.list_outputs(handoff["id"])[0]
        proposal_events = [
            event for event in self.runtime._all_events(self.project["id"])
            if event["kind"] == "proposal_created"
        ]
        proposal = self.store.get_proposal(proposal_events[0]["entity_id"])
        self.assertEqual(proposal["output_id"], output["id"])
        applied = await self.runtime.apply_proposal(proposal["id"], "base-commit")
        self.assertEqual(applied["result"]["head"], "proposal-commit")
        self.assertEqual(
            self.store.get_proposal(proposal["id"])["apply_status"], "proposal_applied"
        )
        self.assertFalse(self.providers.calls[-1][2]["coding"])

    async def test_restart_interrupts_running_without_replay_and_resumes_queued(self) -> None:
        project_id = self.project["id"]
        running = self.store.create_handoff(
            project_id, "claude", "claude-model", "analysis", "running", [], [], [],
            None, [], "base-commit", "restart-running",
        )
        queued = self.store.create_handoff(
            project_id, "grok", "grok-model", "analysis", "queued", [], [], [],
            None, [], "base-commit", "restart-queued",
        )
        self.assertTrue(self.store.claim_handoff(running["id"]))
        # Simulate the process boundary without invoking runtime.stop(), which
        # intentionally marks its own active tasks before closing.
        await self.runtime.stop()
        reopened = ProjectStore(str(self.root / "projects.sqlite3"))
        from project_runtime import ProjectRuntime
        providers_after = FakeProviders()
        restarted = ProjectRuntime(
            reopened, self.git, state_root=self.root,
            provider_module=providers_after, poll_interval=0.01,
        )
        await restarted.start()
        try:
            self.assertEqual(reopened.get_handoff(running["id"])["status"], "interrupted")
            await wait_terminal(reopened, queued["id"])
            self.assertEqual([call[0] for call in providers_after.calls], ["grok"])
        finally:
            await restarted.stop()
        # Prevent tearDown from closing the already-closed original store twice.
        self.runtime = _StoppedRuntime()

    async def test_apply_recovery_uses_recorded_commit_without_replay(self) -> None:
        project_id = self.project["id"]
        handoff = self.store.create_handoff(
            project_id, "claude", "claude-model", "code", "code", [], [], [],
            None, [], "base-commit", "apply-recovery",
        )
        self.assertTrue(self.store.claim_handoff(handoff["id"]))
        proposal = {
            "id": "recovery-proposal", "base_commit": "base-commit",
            "branch": "codex/recovery", "commit_sha": "proposal-commit",
            "summary": "change", "diff_text": "diff", "changed_paths": ["README.md"],
        }
        bundle = self.store.finish_handoff(
            handoff["id"], "raw", {"version": 1}, "completed", proposal
        )
        proposal_id = bundle["proposal"]["id"]
        self.store.append_project_event(
            project_id, "proposal", proposal_id, "proposal_apply_started",
            {"expected_main": "base-commit", "commit_sha": "proposal-commit"},
        )
        self.git.heads[project_id] = "proposal-commit"
        await self.runtime.stop()
        reopened = ProjectStore(str(self.root / "projects.sqlite3"))
        from project_runtime import ProjectRuntime
        restarted = ProjectRuntime(
            reopened, self.git, state_root=self.root,
            provider_module=FakeProviders(), poll_interval=0.01,
        )
        await restarted.start()
        try:
            self.assertEqual(
                reopened.get_proposal(proposal_id)["apply_status"], "proposal_reconciled"
            )
            self.assertEqual(self.git.apply_calls, 0)
        finally:
            await restarted.stop()
        self.runtime = _StoppedRuntime()


class _StoppedRuntime:
    async def stop(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
