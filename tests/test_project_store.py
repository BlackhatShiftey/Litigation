from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import tempfile
import threading
import unittest

from project_store import ProjectStore, ProjectStoreError


class ProjectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="litigation-store-test-")
        self.db_path = f"{self.tempdir.name}/projects.sqlite3"
        self.store = ProjectStore(self.db_path)
        self.store.create_project("project1", "Project", "Description", "a" * 40)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def handoff(self, request_id: str = "request-1", **overrides: object) -> dict:
        fields = {
            "project_id": "project1",
            "agent_id": "claude",
            "model": "sonnet",
            "mode": "analysis",
            "instructions": "Review the selected context.",
            "acceptance": ["Return a durable output"],
            "file_context": [{"path": "a.txt", "sha256": "b" * 64, "content": "hello"}],
            "issue_ids": [],
            "parent_handoff_id": None,
            "source_output_ids": [],
            "base_commit": "a" * 40,
            "request_id": request_id,
        }
        fields.update(overrides)
        return self.store.create_handoff(**fields)  # type: ignore[arg-type]

    def test_records_and_events_are_immutable_including_replace(self) -> None:
        handoff = self.handoff()
        self.assertTrue(self.store.claim_handoff(handoff["id"]))
        finished = self.store.finish_handoff(
            handoff["id"], "raw", {"answer": 1}, "completed"
        )
        output_id = finished["output"]["id"]
        event_seq = self.store.list_events("project1")[0]["seq"]
        attempts = (
            ("update projects set name='changed' where id='project1'", ()),
            ("delete from handoffs where id=?", (handoff["id"],)),
            (
                "insert or replace into outputs "
                "(id,handoff_id,raw_text,parsed_json,final_state,created_at) "
                "values (?,?,?,?,?,?)",
                (output_id, handoff["id"], "changed", None, "failed", 0.0),
            ),
            (
                "insert or replace into project_events "
                "(seq,project_id,entity_type,entity_id,kind,payload_json,created_at) "
                "values (?,?,?,?,?,?,?)",
                (event_seq, "project1", "project", "project1", "changed", "{}", 0.0),
            ),
        )
        for sql, values in attempts:
            with self.subTest(sql=sql), self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                with self.store._conn:
                    self.store._conn.execute(sql, values)
        self.assertEqual(self.store.get_output(output_id)["raw_text"], "raw")

    def test_schema_creation_preserves_preexisting_room_data(self) -> None:
        other_path = f"{self.tempdir.name}/combined.sqlite3"
        connection = sqlite3.connect(other_path)
        connection.execute("create table rooms (id text primary key, title text not null)")
        connection.execute("insert into rooms values ('r1','Existing')")
        connection.commit()
        connection.close()
        store = ProjectStore(other_path)
        try:
            row = store._conn.execute("select * from rooms where id='r1'").fetchone()
            self.assertEqual(dict(row), {"id": "r1", "title": "Existing"})
        finally:
            store.close()

    def test_request_id_is_idempotent_only_for_identical_payload(self) -> None:
        first = self.handoff("same-request")
        second = self.handoff("same-request")
        self.assertEqual(first["id"], second["id"])
        with self.assertRaisesRegex(ProjectStoreError, "different handoff"):
            self.handoff("same-request", project_id="different")
        with self.assertRaisesRegex(ProjectStoreError, "different handoff"):
            self.handoff("same-request", instructions="different")

    def test_submitted_payload_is_stable_idempotency_identity(self) -> None:
        submitted = {
            "project_id": "project1", "agent_id": "claude", "model": None,
            "mode": "analysis", "instructions": "Review", "acceptance": [],
            "file_paths": ["a.txt"], "issue_ids": [], "parent_handoff_id": None,
            "source_output_ids": [],
        }
        first = self.handoff(
            "stable-request", model="resolved-one", base_commit="a" * 40,
            submitted_payload=submitted,
        )
        replay = self.handoff(
            "stable-request", model="resolved-two", base_commit="b" * 40,
            file_context=[{"path": "a.txt", "content": "new snapshot"}],
            submitted_payload=dict(submitted),
        )
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(
            self.store.get_handoff_by_request_id("stable-request")["id"], first["id"]
        )
        with self.assertRaisesRegex(ProjectStoreError, "different handoff"):
            self.handoff(
                "stable-request", submitted_payload={**submitted, "instructions": "Different"}
            )

    def test_claim_is_atomic_across_store_instances(self) -> None:
        handoff = self.handoff()
        stores = [ProjectStore(self.db_path), ProjectStore(self.db_path)]
        barrier = threading.Barrier(2)

        def claim(store: ProjectStore) -> bool:
            barrier.wait()
            return store.claim_handoff(handoff["id"])

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(claim, stores))
            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(self.store.get_handoff(handoff["id"])["status"], "running")
        finally:
            for store in stores:
                store.close()

    def test_cancel_finish_race_preserves_output(self) -> None:
        handoff = self.handoff()
        self.store.claim_handoff(handoff["id"])
        stores = [ProjectStore(self.db_path), ProjectStore(self.db_path)]
        barrier = threading.Barrier(2)

        def cancel() -> object:
            barrier.wait()
            return stores[0].append_task_event(handoff["id"], "cancel_requested")

        def finish() -> object:
            barrier.wait()
            return stores[1].finish_handoff(
                handoff["id"], "finished response", {"ok": True}, "completed"
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(cancel), pool.submit(finish)]
                for future in futures:
                    future.result()
            current = self.store.get_handoff(handoff["id"])
            self.assertIn(current["status"], {"completed", "cancelled"})
            outputs = self.store.list_outputs(handoff["id"])
            self.assertEqual(len(outputs), 1)
            self.assertEqual(outputs[0]["raw_text"], "finished response")
            if current["status"] == "cancelled":
                self.assertEqual(outputs[0]["final_state"], "cancelled")
        finally:
            for store in stores:
                store.close()

    def test_queued_cancel_and_repeated_terminal_are_idempotent(self) -> None:
        handoff = self.handoff()
        event = self.store.append_task_event(handoff["id"], "cancel_requested")
        self.assertEqual(event["kind"], "cancelled")
        before = len(self.store.list_events("project1"))
        repeated = self.store.append_task_event(handoff["id"], "cancel_requested")
        self.assertEqual(repeated["kind"], "cancelled")
        self.assertEqual(len(self.store.list_events("project1")), before)

    def test_context_snapshots_issues_parent_and_explicit_outputs(self) -> None:
        issue = self.store.create_issue("project1", "Issue", "Original body")
        self.store.append_issue_event(issue["id"], "comment", "Original comment")
        parent = self.handoff("parent", issue_ids=[issue["id"]])
        self.store.claim_handoff(parent["id"])
        parent_result = self.store.finish_handoff(
            parent["id"], "parent raw output", {"result": "parent"}, "completed"
        )
        child = self.handoff(
            "child",
            issue_ids=[issue["id"]],
            parent_handoff_id=parent["id"],
            source_output_ids=[parent_result["output"]["id"]],
        )
        context = child["context"]
        self.assertEqual(context["issue_context"][0]["body"], "Original body")
        self.assertEqual(context["issue_context"][0]["events"][-1]["payload"]["text"], "Original comment")
        self.assertEqual(context["source_outputs"][0]["raw_text"], "parent raw output")
        self.assertNotIn("context", context["parent_handoff"])
        self.store.append_issue_event(issue["id"], "closed")
        self.assertEqual(
            self.store.get_handoff(child["id"])["context"]["issue_context"][0]["status"],
            "open",
        )

    def test_context_limit_rejects_without_partial_handoff(self) -> None:
        with self.assertRaisesRegex(ProjectStoreError, "1 MiB"):
            self.handoff("too-large", file_context=[{"content": "x" * (1024 * 1024)}])
        self.assertEqual(self.store.list_handoffs("project1"), [])

    def test_finish_is_idempotent_and_proposal_state_is_event_derived(self) -> None:
        handoff = self.handoff()
        self.store.claim_handoff(handoff["id"])
        proposal = {
            "id": "p1", "base_commit": "a" * 40, "branch": f"codex/{handoff['id']}",
            "commit_sha": "b" * 40, "summary": "Change", "diff_text": "diff",
            "changed_paths": ["a.txt"],
        }
        first = self.store.finish_handoff(
            handoff["id"], "raw", {"ok": True}, "completed", proposal
        )
        second = self.store.finish_handoff(
            handoff["id"], "other", {"ok": False}, "failed", None
        )
        self.assertEqual(first["output"]["id"], second["output"]["id"])
        self.assertEqual(first["output"]["proposal_id"], "p1")
        self.assertEqual(self.store.list_outputs(handoff["id"])[0]["raw_text"], "raw")
        self.store.append_project_event(
            "project1", "proposal", "p1", "proposal_apply_failed", {"category": "stale_base"}
        )
        self.assertEqual(self.store.get_proposal("p1")["apply_status"], "proposal_apply_failed")

    def test_recover_interrupted_projects_running_states(self) -> None:
        running = self.handoff("running")
        cancelling = self.handoff("cancelling")
        self.store.claim_handoff(running["id"])
        self.store.claim_handoff(cancelling["id"])
        self.store.append_task_event(cancelling["id"], "cancel_requested")
        self.store.recover_interrupted()
        self.assertEqual(self.store.get_handoff(running["id"])["status"], "interrupted")
        self.assertEqual(self.store.get_handoff(cancelling["id"])["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
