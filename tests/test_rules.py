from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rules_routes import create_rules_router
from rules_store import RevisionConflict, RulesStore, RulesStoreError, canonical_model_scope


class RulesStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="litigation-rules-test-")
        self.root = Path(self.tempdir.name)
        self.store = RulesStore(str(self.root / "rules.sqlite3"))

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_seeded_index_and_effective_precedence(self) -> None:
        scopes = self.store.list_scopes()
        self.assertEqual([item["id"] for item in scopes], [
            "global", "claude", "openai", "glm", "gemini", "cline", "grok",
        ])
        snapshot = self.store.get_effective("antigravity/gemini-3.8-flash-medium")
        self.assertEqual(snapshot["model_scope"], "gemini")
        self.assertLess(
            snapshot["combined_text"].index("# Global rules"),
            snapshot["combined_text"].index("# Gemini rules"),
        )
        self.assertEqual(len(snapshot["snapshot_hash"]), 64)
        self.assertEqual(canonical_model_scope("gpt-6-astra"), "openai")
        self.assertEqual(canonical_model_scope("codex"), "openai")

    def test_publish_is_cas_and_preserves_history(self) -> None:
        first = self.store.get_active("claude")
        second = self.store.publish(
            "claude", first["content"] + "\n- Include a minimal reproduction.\n",
            "Require reproductions", expected_revision=first["revision"],
        )
        self.assertEqual(second["revision"], 2)
        with self.assertRaises(RevisionConflict):
            self.store.publish(
                "claude", first["content"] + "\n- Stale edit.\n",
                "Stale", expected_revision=1,
            )
        history = self.store.list_revisions("claude")
        self.assertEqual([item["revision"] for item in history], [2, 1])
        self.assertEqual(history[1]["content"], first["content"])

    def test_agent_proposal_is_scoped_evidenced_and_inert_until_accepted(self) -> None:
        active = self.store.get_active("glm")
        candidate = active["content"] + "\n- Attach failure output to the handoff.\n"
        with self.assertRaises(RulesStoreError):
            self.store.propose("global", candidate, "unsafe", "handoff-1", proposer="agent", proposer_scope="glm")
        with self.assertRaises(RulesStoreError):
            self.store.propose("glm", candidate, "wrong model", "handoff-1", proposer="agent", proposer_scope="claude")
        with self.assertRaises(RulesStoreError):
            self.store.propose("glm", candidate, "no evidence", None, proposer="agent", proposer_scope="glm")
        proposal = self.store.propose(
            "glm", candidate, "Capture failures", "handoff-1",
            proposer="agent", proposer_scope="glm",
        )
        self.assertEqual(self.store.get_active("glm")["revision"], 1)
        self.assertEqual(proposal["status"], "proposed")
        accepted = self.store.accept_proposal(proposal["id"], expected_revision=1)
        self.assertEqual(accepted["revision"]["revision"], 2)
        self.assertEqual(accepted["proposal"]["status"], "accepted")

    def test_all_ledger_tables_reject_update_delete_and_replace(self) -> None:
        proposal = self.store.propose(
            "cline", self.store.get_active("cline")["content"] + "\n- Preserve receipts.\n",
            "Receipt rule", None,
        )
        tables_and_keys = {
            "rule_scopes": ("id", "cline"),
            "rule_revisions": ("id", self.store.get_active("cline")["id"]),
            "rule_proposals": ("id", proposal["id"]),
            "rule_events": ("seq", self.store._latest_event("proposal", proposal["id"])["seq"]),
        }
        for table, (column, value) in tables_and_keys.items():
            with self.subTest(table=table, operation="update"), self.assertRaises(sqlite3.IntegrityError):
                self.store._conn.execute(f"update {table} set {column}={column} where {column}=?", (value,))
            with self.subTest(table=table, operation="delete"), self.assertRaises(sqlite3.IntegrityError):
                self.store._conn.execute(f"delete from {table} where {column}=?", (value,))
            row = self.store._conn.execute(f"select * from {table} where {column}=?", (value,)).fetchone()
            columns = [item[1] for item in self.store._conn.execute(f"pragma table_info({table})")]
            placeholders = ",".join("?" for _ in columns)
            with self.subTest(table=table, operation="replace"), self.assertRaises(sqlite3.IntegrityError):
                self.store._conn.execute(
                    f"insert or replace into {table} ({','.join(columns)}) values ({placeholders})",
                    tuple(row[column_name] for column_name in columns),
                )
            self.store._conn.rollback()

    def test_accept_reject_decision_is_atomic_across_store_instances(self) -> None:
        second = RulesStore(str(self.root / "rules.sqlite3"))
        try:
            active = self.store.get_active("claude")
            proposal = self.store.propose(
                "claude", active["content"] + "\n- Preserve concurrency evidence.\n",
                "Concurrent decision", None,
            )
            barrier = threading.Barrier(2)

            def accept() -> str:
                barrier.wait()
                self.store.accept_proposal(proposal["id"], 1)
                return "accepted"

            def reject() -> str:
                barrier.wait()
                second.reject_proposal(proposal["id"], "not ready")
                return "rejected"

            outcomes: list[str] = []
            failures = 0
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(accept), pool.submit(reject)]
                for future in futures:
                    try:
                        outcomes.append(future.result(timeout=5))
                    except RulesStoreError:
                        failures += 1
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(failures, 1)
            events = self.store._conn.execute(
                "select kind from rule_events where entity_type='proposal' and entity_id=? order by seq",
                (proposal["id"],),
            ).fetchall()
            self.assertEqual(events[0]["kind"], "proposed")
            self.assertEqual([row["kind"] for row in events[1:]], outcomes)
        finally:
            second.close()


class RulesApiAndUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="litigation-rules-api-test-")
        self.root = Path(self.tempdir.name)
        self.store = RulesStore(str(self.root / "rules.sqlite3"))
        app = FastAPI()
        app.include_router(create_rules_router(self.store))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.store.close()
        self.tempdir.cleanup()

    def test_human_api_does_not_accept_agent_identity_claims(self) -> None:
        active = self.client.get("/api/rules/global").json()
        response = self.client.post("/api/rules/proposals", json={
            "scope": "global", "content": active["content"] + "\n- Review changes.\n",
            "summary": "Review", "proposer": "agent", "proposer_scope": "glm",
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["proposer"], "user")

    def test_api_publish_conflict_and_proposal_acceptance(self) -> None:
        active = self.client.get("/api/rules/grok").json()
        content = active["content"] + "\n- Link debate evidence.\n"
        proposal = self.client.post("/api/rules/proposals", json={
            "scope": "grok", "content": content, "summary": "Link evidence",
        }).json()
        accepted = self.client.post(
            f"/api/rules/proposals/{proposal['id']}/accept", json={"expected_revision": 1},
        )
        self.assertEqual(accepted.status_code, 200)
        stale = self.client.post("/api/rules/grok/revisions", json={
            "content": content + "\n- Stale.\n", "summary": "Stale", "expected_revision": 1,
        })
        self.assertEqual(stale.status_code, 409)

    def test_revision_history_is_cursor_paginated_without_omissions(self) -> None:
        for revision in range(2, 108):
            active = self.store.get_active("gemini")
            self.store.publish(
                "gemini", active["content"] + f"\n- Revision marker {revision}.\n",
                f"Revision {revision}", expected_revision=revision - 1,
            )
        first = self.client.get("/api/rules/gemini/history?limit=40").json()
        second = self.client.get(
            f"/api/rules/gemini/history?limit=40&before_revision={first['next_before_revision']}"
        ).json()
        third = self.client.get(
            f"/api/rules/gemini/history?limit=40&before_revision={second['next_before_revision']}"
        ).json()
        revisions = [item["revision"] for page in (first, second, third) for item in page["items"]]
        self.assertEqual(revisions, list(range(107, 0, -1)))
        self.assertIsNone(third["next_before_revision"])

    def test_ui_uses_safe_text_rendering_and_responsive_hidden_styles(self) -> None:
        root = Path(__file__).parents[1]
        script = (root / "rules.js").read_text(encoding="utf-8")
        styles = (root / "rules.css").read_text(encoding="utf-8")
        page = (root / "rules.html").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", script)
        self.assertIn("textContent", script)
        self.assertIn("[hidden] { display: none !important; }", styles)
        self.assertIn("@media (max-width: 800px)", styles)
        self.assertIn('aria-label="Rule sections"', page)


if __name__ == "__main__":
    unittest.main()
