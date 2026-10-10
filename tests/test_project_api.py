from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from project_store import ProjectStore
from tests.test_project_runtime import FakeGit, FakeProviders


class ProjectApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from project_routes import create_project_router
        from project_runtime import ProjectRuntime

        self.tempdir = tempfile.TemporaryDirectory(prefix="litigation-api-test-")
        self.root = Path(self.tempdir.name)
        self.store = ProjectStore(str(self.root / "projects.sqlite3"))
        self.runtime = ProjectRuntime(
            self.store, FakeGit(self.root), state_root=self.root,
            provider_module=FakeProviders(), poll_interval=0.01,
        )
        app = FastAPI()
        app.include_router(create_project_router(self.runtime))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.store.close()
        self.tempdir.cleanup()

    def test_create_issue_handoff_and_export_contract(self) -> None:
        project_response = self.client.post("/api/projects", json={"name": "API project"})
        self.assertEqual(project_response.status_code, 201)
        project = project_response.json()
        self.assertEqual(project["import_report"]["included_count"], 1)
        detail = self.client.get(f"/api/projects/{project['id']}").json()
        self.assertEqual(detail["import_report"], project["import_report"])
        issue = self.client.post(
            f"/api/projects/{project['id']}/issues",
            json={"title": "Track it", "body": "details"},
        ).json()
        response = self.client.post(
            f"/api/projects/{project['id']}/handoffs",
            json={
                "request_id": "api-one", "agent_id": "claude", "mode": "review",
                "instructions": "review", "acceptance": [], "file_paths": ["README.md"],
                "issue_ids": [issue["id"]], "source_output_ids": [],
            },
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["state"], "queued")
        work = self.client.post(
            f"/api/projects/{project['id']}/work-items",
            json={"request_id": "work-one", "title": "Shared review",
                  "instructions": "Review the implementation", "acceptance": ["Report evidence"],
                  "owned_paths": ["README.md"], "issue_ids": [issue["id"]]},
        ).json()
        running = self.store.get_handoff(response.json()["id"])
        self.assertTrue(self.store.claim_handoff(running["id"]))
        self.store.claim_work_item(work["id"], "attempt-one", "claude", running["id"])
        handoffs = self.client.get(f"/api/projects/{project['id']}/handoffs").json()["items"]
        self.assertEqual(len(handoffs), 1)
        self.assertNotIn("file_context", handoffs[0])
        exported = self.client.get(f"/api/projects/{project['id']}/export")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("attachment", exported.headers["content-disposition"])
        self.assertEqual(exported.json()["format"], "litigation-project-export-v1")
        self.assertIn("excluded", exported.json()["repository_content"])
        self.assertEqual(exported.json()["work_items"][0]["instructions"], "Review the implementation")
        work_events = [event for event in exported.json()["events"]
                       if event["entity_type"] == "work_item"]
        self.assertEqual([event["kind"] for event in work_events], ["queued", "claimed", "running"])

    def test_cross_project_parent_is_rejected(self) -> None:
        one = self.client.post("/api/projects", json={"name": "One"}).json()
        two = self.client.post("/api/projects", json={"name": "Two"}).json()
        first = self.client.post(f"/api/projects/{one['id']}/handoffs", json={
            "request_id": "one", "agent_id": "claude", "mode": "analysis",
            "instructions": "one",
        }).json()
        response = self.client.post(f"/api/projects/{two['id']}/handoffs", json={
            "request_id": "two", "agent_id": "grok", "mode": "analysis",
            "instructions": "two", "parent_handoff_id": first["id"],
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("does not belong", response.json()["detail"])

    def test_export_rejects_secret_shaped_material(self) -> None:
        project = self.client.post("/api/projects", json={"name": "Secret check"}).json()
        self.client.post(f"/api/projects/{project['id']}/issues", json={
            "title": "Do not share", "body": 'api_key="' + "".join(("abcdefghijk", "lmnopqrstuv")) + '"',
        })
        response = self.client.get(f"/api/projects/{project['id']}/export")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("".join(("abcdefghijk", "lmnopqrstuv")), response.text)


if __name__ == "__main__":
    unittest.main()
