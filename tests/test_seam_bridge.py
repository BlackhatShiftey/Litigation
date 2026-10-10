from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from seam_bridge import PROTOCOL, SeamBridge, SeamBridgeError, SeamSecretRejected
from seam_sync import SeamSyncCoordinator
from deploy.seam import remote_helper


class FakeRemoteBridge(SeamBridge):
    def __init__(self, state_dir: Path) -> None:
        super().__init__(state_dir, ssh_target="test@example", timeout=1)
        self.fail = False
        self.calls: list[dict[str, Any]] = []

    def _remote(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        if self.fail:
            raise SeamBridgeError("offline")
        if payload["operation"] == "status":
            return {"protocol": PROTOCOL, "ok": True, "reachable": True, "installed": True}
        return {
            "protocol": PROTOCOL, "ok": True, "operation": payload["operation"],
            "namespace": payload.get("namespace"), "seam": {"nodes": [], "edges": []},
        }


class FakeStore:
    def __init__(self) -> None:
        self.project = {
            "id": "project-one", "name": "One", "description": "Test",
            "source_description": "empty",
        }
        self.events = [
            {"seq": 1, "project_id": "project-one", "entity_type": "project",
             "entity_id": "project-one", "kind": "created", "payload": {}, "created_at": 1.0},
            {"seq": 2, "project_id": "project-one", "entity_type": "handoff",
             "entity_id": "task-one", "kind": "completed",
             "payload": {"output_id": "output-one"}, "created_at": 2.0},
        ]

    def list_projects(self) -> list[dict[str, Any]]:
        return [self.project]

    def list_events(self, project_id: str, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return [event for event in self.events if event["seq"] > after][:limit]

    def get_handoff(self, handoff_id: str) -> dict[str, Any]:
        return {"id": handoff_id, "agent_id": "claude", "model": "subscription",
                "mode": "code", "instructions": "Implement it", "acceptance": ["tests pass"]}

    def list_outputs(self, handoff_id: str) -> list[dict[str, Any]]:
        return [{"id": "output-one", "final_state": "completed", "raw_text": "All tests pass."}]

    def list_issues(self, project_id: str) -> list[dict[str, Any]]:
        return []

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        return None


class SeamBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="litigation-seam-test-")
        self.root = Path(self.temp.name)
        self.bridge = FakeRemoteBridge(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_failed_delivery_is_retained_then_retried_once(self) -> None:
        self.bridge.fail = True
        with self.assertRaises(SeamBridgeError):
            self.bridge.publish_event(
                "project-one", "event.1", "completed", "task-one", 1, "Durable output", {},
            )
        self.assertEqual(self.bridge.outbox_counts(), {
            "total": 1, "delivered": 0, "pending": 1, "rejected": 0,
        })
        self.bridge.fail = False
        result = self.bridge.retry_pending()
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(self.bridge.retry_pending()["attempted"], 0)
        with sqlite3.connect(self.bridge.db_path) as conn:
            self.assertEqual(conn.execute("select count(*) from delivery_attempts").fetchone()[0], 2)

    def test_reused_event_id_with_different_content_is_rejected(self) -> None:
        args = ("project-one", "event.1", "completed", "task-one", 1)
        self.bridge.enqueue_event(*args, "one", {})
        with self.assertRaises(SeamBridgeError):
            self.bridge.enqueue_event(*args, "two", {})

    def test_secret_rejection_stores_only_hash_and_safe_metadata(self) -> None:
        secret = 'api_key="' + "".join(("abcdefghijk", "lmnopqrstuv")) + '"'
        with self.assertRaises(SeamSecretRejected):
            self.bridge.enqueue_event(
                "project-one", "event.2", "completed", "task-one", 2, secret, {},
            )
        raw = self.bridge.db_path.read_bytes()
        self.assertNotIn(secret.encode(), raw)
        self.assertEqual(self.bridge.outbox_counts()["rejected"], 1)

    def test_replace_cannot_overwrite_append_only_outbox(self) -> None:
        queued = self.bridge.enqueue_event(
            "project-one", "event.3", "completed", "task-one", 3, "immutable", {},
        )
        with sqlite3.connect(self.bridge.db_path) as conn:
            conn.execute("PRAGMA recursive_triggers=ON")
            row = conn.execute(
                "select * from outbox_items where item_id=?", (queued["item_id"],)
            ).fetchone()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "insert or replace into outbox_items values (?,?,?,?,?,?,?)",
                    (*row[:5], '{\"changed\":true}', row[6]),
                )

    def test_search_and_graph_are_project_namespace_scoped(self) -> None:
        self.bridge.search("project-one", "tests", 5)
        self.bridge.graph("project-one", "tests", 5)
        self.assertEqual(self.bridge.calls[-2]["namespace"], "litigation.project.project-one")
        self.assertEqual(self.bridge.calls[-1]["operation"], "graph")


class RemoteHelperTests(unittest.TestCase):
    def test_json_contract_rejects_unscoped_namespace_and_missing_provenance(self) -> None:
        base = {
            "protocol": PROTOCOL, "operation": "ingest",
            "idempotency_key": "litigation:key", "namespace": "other.project",
            "source_ref": "litigation:source", "agent_id": "litigation", "text": "body",
        }
        with self.assertRaisesRegex(ValueError, "namespace"):
            remote_helper._validate_request(base)
        base["namespace"] = "litigation.project.one"
        with self.assertRaisesRegex(ValueError, "provenance"):
            remote_helper._validate_request(base)

    def test_json_contract_binds_namespace_source_and_key_to_event(self) -> None:
        project_id = "project-one"
        event_id = "event.1"
        digest = __import__("hashlib").sha256(f"{project_id}:{event_id}".encode()).hexdigest()
        request = {
            "protocol": PROTOCOL, "operation": "ingest",
            "idempotency_key": f"litigation:{digest}",
            "namespace": "litigation.project.project-one",
            "source_ref": "litigation:project-one:event.1",
            "agent_id": "litigation", "text": "body",
            "provenance": {
                "project_id": project_id, "event_id": event_id, "event_kind": "completed",
                "entity_id": "task-one", "sequence": 1,
            },
        }
        remote_helper._validate_request(request)
        request["source_ref"] = "litigation:project-one:event.2"
        with self.assertRaisesRegex(ValueError, "source_ref"):
            remote_helper._validate_request(request)

    def test_remote_receipt_replace_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="litigation-seam-remote-test-") as temp:
            with mock.patch.dict(os.environ, {"LITIGATION_SEAM_REMOTE_ROOT": temp}):
                conn = remote_helper._db(Path(temp))
                try:
                    conn.execute(
                        "insert into requests values (?,?,?,?)", ("key", "hash", "{}", "now")
                    )
                    conn.commit()
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(
                            "insert or replace into requests values (?,?,?,?)",
                            ("key", "changed", "{}", "later"),
                        )
                finally:
                    conn.close()


class SeamSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="litigation-seam-sync-test-")
        self.root = Path(self.temp.name)
        self.bridge = FakeRemoteBridge(self.root)
        self.store = FakeStore()
        self.sync = SeamSyncCoordinator(
            self.store, self.bridge, poll_interval=0.01, enabled=True,
        )

    async def asyncTearDown(self) -> None:
        await self.sync.stop()
        self.temp.cleanup()

    async def test_restart_scan_is_idempotent_and_delivers_terminal_output(self) -> None:
        first = await self.sync.sync_once()
        self.assertEqual(first["items_queued"], 2)
        second = SeamSyncCoordinator(
            self.store, self.bridge, poll_interval=0.01, enabled=True,
        )
        again = await second.sync_once()
        self.assertEqual(again["events_scanned"], 0)
        self.assertEqual(self.bridge.outbox_counts()["total"], 2)
        payloads = [call for call in self.bridge.calls if call.get("operation") == "ingest"]
        self.assertTrue(any("All tests pass" in call["text"] for call in payloads))

    async def test_disabled_sync_never_scans_or_calls_remote(self) -> None:
        disabled = SeamSyncCoordinator(self.store, self.bridge, enabled=False)
        result = await disabled.sync_once()
        self.assertFalse(result["enabled"])
        self.assertEqual(result["events_scanned"], 0)
        self.assertEqual(self.bridge.calls, [])

    async def test_secret_event_advances_with_content_free_rejection(self) -> None:
        self.store.events = [{
            "seq": 3, "project_id": "project-one", "entity_type": "handoff",
            "entity_id": "task-one", "kind": "completed",
            "payload": {"output_id": "output-one"}, "created_at": 3.0,
        }]
        self.store.list_outputs = lambda _handoff: [{
            "id": "output-one", "final_state": "completed",
            "raw_text": 'password="' + "".join(("abcdefghijk", "lmnopqrstuv")) + '"',
        }]
        result = await self.sync.sync_once()
        self.assertEqual(result["items_rejected"], 1)
        self.assertEqual(self.sync._cursor("project-one"), 3)
        self.assertEqual(self.bridge.outbox_counts()["rejected"], 1)

    def test_proposal_render_does_not_depend_on_mutable_apply_projection(self) -> None:
        event = {"seq": 4, "project_id": "project-one", "entity_type": "proposal",
                 "entity_id": "proposal-one", "kind": "proposal_created",
                 "payload": {"commit_sha": "abc"}, "created_at": 4.0}
        projection = {"id": "proposal-one", "summary": "Change", "branch": "codex/one",
                      "commit_sha": "abc", "changed_paths": ["app.py"],
                      "apply_status": "pending"}
        self.store.get_proposal = lambda _id: projection
        before = self.sync._render_event(self.store.project, event)
        projection["apply_status"] = "proposal_applied"
        after = self.sync._render_event(self.store.project, event)
        self.assertEqual(before, after)

    def test_issue_render_does_not_depend_on_mutable_status_projection(self) -> None:
        event = {"seq": 5, "project_id": "project-one", "entity_type": "issue",
                 "entity_id": "issue-one", "kind": "open", "payload": {}, "created_at": 5.0}
        issue = {"id": "issue-one", "title": "Track", "body": "Immutable", "status": "open"}
        self.store.list_issues = lambda _project: [issue]
        before = self.sync._render_event(self.store.project, event)
        issue["status"] = "closed"
        after = self.sync._render_event(self.store.project, event)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
