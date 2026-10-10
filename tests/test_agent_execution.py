from __future__ import annotations

import json
import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_runner import AgentRunError, AgentRunner, MAX_ROUNDS
from local_git import LocalGit
from test_sandbox import TestSandbox
from project_store import ProjectStore
from project_runtime import ProjectRuntime
from rules_store import RulesStore


class ScriptedProvider:
    def __init__(self, actions: list[dict[str, Any]]) -> None:
        self.actions = list(actions)

    async def complete(self, agent: dict[str, Any], messages: list[dict[str, str]], **kwargs: Any) -> str:
        return json.dumps(self.actions.pop(0))

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return {"id": agent_id, "model": agent_id + "-model", "available": True}


class MultiProvider:
    def __init__(self, actions: dict[str, list[dict[str, Any]]]) -> None:
        self.actions = actions

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return {"id": agent_id, "model": agent_id + "-model", "available": True}

    async def complete(self, agent: dict[str, Any], messages: list[dict[str, str]], **kwargs: Any) -> str:
        return json.dumps(self.actions[agent["id"]].pop(0))


class RawProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    async def complete(self, agent: dict[str, Any], messages: list[dict[str, str]], **kwargs: Any) -> str:
        return self.responses.pop(0)


class AgentExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="litigation-agent-test-")
        self.root = Path(self.temp.name)
        self.git = LocalGit(self.root)
        created = await self.git.create_project("project1")
        self.base = created["head"]

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_controller_writes_tests_commits_and_records_receipts(self) -> None:
        source = "def add(a, b):\n    return a + b\n"
        test = "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n"
        provider = ScriptedProvider([
            {"action": "write_file", "path": "calc.py", "base_sha256": None, "content": source},
            {"action": "write_file", "path": "tests/__init__.py", "base_sha256": None, "content": ""},
            {"action": "write_file", "path": "tests/test_calc.py", "base_sha256": None, "content": test},
            {"action": "run_tests", "profile": "python-unittest"},
            {"action": "finish", "summary": "Add tested calculator"},
        ])
        events: list[dict[str, Any]] = []
        runner = AgentRunner(self.git, TestSandbox(15), provider, lambda kind, payload: events.append(payload))
        handoff = {"id": "handoff1", "project_id": "project1", "model": "", "instructions": "add",
                   "acceptance": ["tests pass"], "context": {}}
        result = await runner.run(
            handoff=handoff, agent={"id": "claude", "available": True},
            rules_snapshot={"combined_text": "follow project rules"},
            working_base=self.base, integration_base=self.base,
        )
        self.assertEqual(result.parsed["checks"][-1]["status"], "passed")
        self.assertEqual(result.proposal["branch"], "codex/handoff1")
        self.assertIn("calc.py", result.proposal["changed_paths"])
        self.assertEqual(events[0]["request"]["accepted_content"], source)
        self.assertEqual(events[-2]["result"]["tree_sha256"],
                         await self.git.agent_tree_fingerprint("project1", "handoff1"))

    async def test_malformed_turn_is_receipted_then_repaired(self) -> None:
        test = "import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n"
        responses = [
            "I will inspect the project first.",
            json.dumps({"action": "write_file", "path": "tests/__init__.py", "base_sha256": None, "content": ""}),
            json.dumps({"action": "write_file", "path": "tests/test_ok.py", "base_sha256": None, "content": test}),
            json.dumps({"action": "run_tests", "profile": "python-unittest"}),
            json.dumps({"action": "finish", "summary": "Recover from malformed protocol turn"}),
        ]
        events: list[dict[str, Any]] = []
        runner = AgentRunner(self.git, TestSandbox(15), RawProvider(responses),
                             lambda kind, payload: events.append(payload))
        result = await runner.run(
            handoff={"id": "malformed1", "project_id": "project1", "model": "", "instructions": "test",
                     "acceptance": [], "context": {}},
            agent={"id": "glm", "available": True}, rules_snapshot={"combined_text": "rules"},
            working_base=self.base, integration_base=self.base,
        )
        self.assertEqual(events[0]["action"], "protocol_error")
        self.assertEqual(events[0]["result"]["error"], "agent action is not valid JSON")
        self.assertNotIn("I will inspect", json.dumps(events[0]))
        self.assertEqual(result.parsed["checks"][0]["status"], "passed")

    async def test_malformed_turns_exhaust_round_bound_without_tools(self) -> None:
        events: list[dict[str, Any]] = []
        runner = AgentRunner(
            self.git, TestSandbox(15), RawProvider(["not json"] * MAX_ROUNDS),
            lambda kind, payload: events.append(payload),
        )
        with self.assertRaisesRegex(AgentRunError, "round limit"):
            await runner.run(
                handoff={"id": "malformed2", "project_id": "project1", "model": "", "instructions": "test",
                         "acceptance": [], "context": {}},
                agent={"id": "glm", "available": True}, rules_snapshot={"combined_text": "rules"},
                working_base=self.base, integration_base=self.base,
            )
        self.assertEqual(len(events), MAX_ROUNDS)
        self.assertEqual({item["action"] for item in events}, {"protocol_error"})

    async def test_write_after_pass_requires_a_new_test_run(self) -> None:
        provider = ScriptedProvider([
            {"action": "write_file", "path": "tests/__init__.py", "base_sha256": None, "content": ""},
            {"action": "write_file", "path": "tests/test_ok.py", "base_sha256": None,
             "content": "import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n"},
            {"action": "run_tests", "profile": "python-unittest"},
            {"action": "write_file", "path": "late.py", "base_sha256": None, "content": "x = 1\n"},
            {"action": "finish", "summary": "too early"},
            {"action": "run_tests", "profile": "python-unittest"},
            {"action": "finish", "summary": "tested current tree"},
        ])
        runner = AgentRunner(self.git, TestSandbox(15), provider, lambda *_: None)
        result = await runner.run(
            handoff={"id": "handoff2", "project_id": "project1", "model": "", "instructions": "work",
                     "acceptance": [], "context": {}},
            agent={"id": "claude", "available": True}, rules_snapshot={"combined_text": "rules"},
            working_base=self.base, integration_base=self.base,
        )
        self.assertEqual(len(result.parsed["checks"]), 2)
        self.assertEqual(result.proposal["summary"], "tested current tree")

    async def test_runtime_fail_repair_pass_and_peer_lineage_are_durable(self) -> None:
        wrong = "def add(a, b):\n    return a - b\n"
        fixed = "def add(a, b):\n    return a + b\n"
        test = "import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n"
        provider = MultiProvider({
            "claude": [
                {"action": "write_file", "path": "calc.py", "base_sha256": None, "content": wrong},
                {"action": "write_file", "path": "tests/__init__.py", "base_sha256": None, "content": ""},
                {"action": "write_file", "path": "tests/test_calc.py", "base_sha256": None, "content": test},
                {"action": "run_tests", "profile": "python-unittest"},
                {"action": "write_file", "path": "calc.py",
                 "base_sha256": hashlib.sha256(wrong.encode()).hexdigest(), "content": fixed},
                {"action": "run_tests", "profile": "python-unittest"},
                {"action": "delegate", "agent_id": "glm", "instructions": "review the tested change",
                 "acceptance": ["confirm tests"]},
                {"action": "finish", "summary": "Repair calculator and tests"},
            ],
            "glm": [
                {"action": "run_tests", "profile": "python-unittest"},
                {"action": "finish", "summary": "Peer verified inherited branch"},
            ],
        })
        store = ProjectStore(str(self.root / "runtime.sqlite3"))
        rules = RulesStore(str(self.root / "runtime-rules.sqlite3"))
        runtime = ProjectRuntime(store, self.git, state_root=self.root / "state", provider_module=provider,
                                 rules_store=rules, poll_interval=0.01)
        await runtime.start()
        try:
            project = store.create_project("project1", "Runtime", "", self.base, "")
            parent = await runtime.create_handoff(
                project["id"], request_id="agent-parent", agent_id="claude", model=None, mode="agent",
                instructions="fix and test", acceptance=["tests pass"], file_paths=[], issue_ids=[],
                parent_handoff_id=None, source_output_ids=[],
            )
            deadline = asyncio.get_running_loop().time() + 10
            child = None
            while asyncio.get_running_loop().time() < deadline:
                handoffs = store.list_handoffs(project["id"])
                if len(handoffs) == 2 and all(item["status"] == "completed" for item in handoffs):
                    child = next(item for item in handoffs if item["id"] != parent["id"])
                    break
                await asyncio.sleep(.05)
            self.assertIsNotNone(child)
            parent_output = store.list_outputs(parent["id"])[0]
            checks = parent_output["parsed"]["checks"]
            self.assertEqual([item["status"] for item in checks], ["failed", "passed"])
            proposal = store.get_proposal(parent_output["proposal_id"])
            self.assertEqual(child["context"]["working_base"], proposal["commit_sha"])
            self.assertEqual(child["context"]["integration_base"], self.base)
            self.assertEqual(child["context"]["lineage_depth"], 1)
            kinds = [event["kind"] for event in store.list_events(project["id"], limit=500)]
            self.assertIn("peer_handoff_queued", kinds)
            self.assertGreaterEqual(kinds.count("agent_tool_result"), 10)
        finally:
            await runtime.stop()
            rules.close()

    async def test_child_cumulative_proposal_fast_forwards_main(self) -> None:
        await self.git.prepare_agent_workspace("project1", "parent", self.base)
        await self.git.agent_write_file("project1", "parent", "a.py", "a = 1\n", None)
        parent = await self.git.finish_agent_workspace("project1", "parent", self.base, self.base, "parent")
        await self.git.prepare_agent_workspace("project1", "child", parent["commit_sha"])
        await self.git.agent_write_file("project1", "child", "b.py", "b = 2\n", None)
        child = await self.git.finish_agent_workspace(
            "project1", "child", self.base, parent["commit_sha"], "child"
        )
        self.assertEqual(set(child["changed_paths"]), {"a.py", "b.py"})
        result = await self.git.apply_proposal("project1", child, self.base)
        self.assertEqual(result["head"], child["commit_sha"])

    async def test_sandbox_blocks_network_and_host_home(self) -> None:
        workspace = self.root / "sandbox"
        (workspace / "tests").mkdir(parents=True)
        (workspace / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (workspace / "tests" / "test_isolation.py").write_text(
            "import pathlib,socket,unittest\n"
            "class T(unittest.TestCase):\n"
            " def test_isolation(self):\n"
            "  self.assertFalse(pathlib.Path('/home/terrabyte').exists())\n"
            "  with self.assertRaises(OSError): socket.socket().connect(('127.0.0.1',9))\n",
            encoding="utf-8",
        )
        receipt = await TestSandbox(10).run(workspace, "python-unittest")
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["network"], "disabled")
        self.assertEqual(receipt["workspace"], "read-only")

    async def test_sandbox_output_is_bounded(self) -> None:
        workspace = self.root / "sandbox-output"
        (workspace / "tests").mkdir(parents=True)
        (workspace / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (workspace / "tests" / "test_output.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n def test_output(self): print('x'*200000)\n",
            encoding="utf-8",
        )
        receipt = await TestSandbox(10).run(workspace, "python-unittest")
        self.assertEqual(receipt["status"], "output_limit")
        self.assertTrue(receipt["truncated"])
        self.assertLessEqual(len(receipt["output"].encode()), 128 * 1024)

    async def test_empty_test_discovery_does_not_qualify(self) -> None:
        workspace = self.root / "sandbox-empty"
        (workspace / "tests").mkdir(parents=True)
        (workspace / "tests" / "__init__.py").write_text("", encoding="utf-8")
        receipt = await TestSandbox(10).run(workspace, "python-unittest")
        self.assertEqual(receipt["status"], "no_tests")
        self.assertEqual(receipt["test_count"], 0)

    async def test_restart_reconciles_one_durable_peer_child(self) -> None:
        db = self.root / "reconcile.sqlite3"
        store = ProjectStore(str(db))
        store.create_project("project1", "P", "", self.base, "")
        parent = store.create_handoff(
            "project1", "claude", "claude-model", "agent", "work", [], [], [], None, [],
            self.base, "reconcile-parent", {},
            {"working_base": self.base, "integration_base": self.base, "lineage_depth": 0,
             "rules_snapshot": {"model_scope": "claude"}},
        )
        self.assertTrue(store.claim_handoff(parent["id"]))
        parsed = {"version": 2, "kind": "agent_execution", "summary": "done", "receipts": [],
                  "checks": [], "rule_proposals": [],
                  "delegations": [{"agent_id": "glm", "instructions": "peer review", "acceptance": ["review"]}]}
        store.finish_handoff(parent["id"], json.dumps(parsed), parsed, "completed", None)
        store.close()
        reopened = ProjectStore(str(db))
        rules = RulesStore(str(self.root / "reconcile-rules.sqlite3"))
        runtime = ProjectRuntime(reopened, self.git, state_root=self.root / "reconcile-state",
                                 provider_module=MultiProvider({"glm": []}), rules_store=rules,
                                 max_concurrency=0, poll_interval=.01)
        await runtime.start()
        try:
            deadline = asyncio.get_running_loop().time() + 2
            while len(reopened.list_handoffs("project1")) < 2 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.02)
            children = [item for item in reopened.list_handoffs("project1") if item["id"] != parent["id"]]
            self.assertEqual(len(children), 1)
            await runtime._reconcile_agent_outputs()
            self.assertEqual(len(reopened.list_handoffs("project1")), 2)
        finally:
            await runtime.stop()
            rules.close()

    async def test_sandbox_cancellation_stops_promptly(self) -> None:
        workspace = self.root / "sandbox-cancel"
        (workspace / "tests").mkdir(parents=True)
        (workspace / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (workspace / "tests" / "test_slow.py").write_text(
            "import time,unittest\nclass T(unittest.TestCase):\n def test_slow(self): time.sleep(30)\n",
            encoding="utf-8",
        )
        task = asyncio.create_task(TestSandbox(60).run(workspace, "python-unittest"))
        await asyncio.sleep(.15)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)

    async def test_sandbox_bounds_temporary_file_growth(self) -> None:
        workspace = self.root / "sandbox-tmp-limit"
        (workspace / "tests").mkdir(parents=True)
        (workspace / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (workspace / "tests" / "test_fill.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            " def test_fill(self):\n  open('/tmp/fill','wb').write(b'x'*(40*1024*1024))\n",
            encoding="utf-8",
        )
        receipt = await TestSandbox(10).run(workspace, "python-unittest")
        self.assertEqual(receipt["status"], "failed")
        self.assertNotEqual(receipt["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
