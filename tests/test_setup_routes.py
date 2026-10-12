from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from setup_routes import create_setup_router
from setup_store import SetupStore, SetupStoreError

# Isolated storage paths so these tests never touch the real user files.
_TEMP = tempfile.TemporaryDirectory(prefix="litigation-setup-test-")
_SETUP = Path(_TEMP.name) / "setup.json"
_KEYS = Path(_TEMP.name) / "keys.env"

os.environ.setdefault("LITIGATION_SEAM_SYNC_ENABLED", "0")


def _fake_registry() -> list[dict[str, object]]:
    model = "test-model"
    return [
        {"id": "gpt", "label": "GPT", "color": "#10a37f", "api": "openai",
         "env_key": "OPENAI_API_KEY", "billing": "api_or_local", "model": model},
        {"id": "openrouter", "label": "OpenRouter", "color": "#8b5cf6",
         "api": "openai", "env_key": "OPENROUTER_API_KEY",
         "billing": "api_or_local", "model": model},
        {"id": "claude", "label": "Claude", "color": "#d97757", "api": "cli",
         "env_key": "", "billing": "subscription", "model": model},
        {"id": "glm", "label": "GLM", "color": "#38bdf8", "api": "cli",
         "env_key": "", "billing": "subscription", "model": model,
         "required_env": ["ZAI_API_KEY"]},
        {"id": "ollama", "label": "Ollama", "color": "#a78bfa", "api": "openai",
         "env_key": "", "base_url": "http://127.0.0.1:1/v1",
         "default_model": "llama3.2", "model_env": "OLLAMA_MODEL", "model": model},
        {"id": "lmstudio", "label": "LM Studio", "color": "#e879f9",
         "api": "openai", "env_key": "",
         "base_url": "http://127.0.0.1:2/v1", "default_model": "qwen",
         "model_env": "LMSTUDIO_MODEL", "model": model},
    ]


class _RouteTestBase(unittest.TestCase):
    def setUp(self) -> None:
        _SETUP.unlink(missing_ok=True)
        _KEYS.unlink(missing_ok=True)
        self._saved_env = {name: os.environ.get(name)
                           for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY",
                                        "ZAI_API_KEY", "OLLAMA_MODEL",
                                        "LITIGATION_SUBSCRIPTION_ONLY")}
        for name in self._saved_env:
            os.environ.pop(name, None)
        os.environ["LITIGATION_SUBSCRIPTION_ONLY"] = "0"
        self.store = SetupStore(setup_path=_SETUP, keys_path=_KEYS)
        app = FastAPI()
        app.include_router(create_setup_router(self.store))
        self.registry_patch = mock.patch(
            "setup_routes.providers.registry", side_effect=_fake_registry)
        self.registry_patch.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.registry_patch.stop()
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class SetupStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        _SETUP.unlink(missing_ok=True)
        _KEYS.unlink(missing_ok=True)
        self.store = SetupStore(setup_path=_SETUP, keys_path=_KEYS)

    def test_fresh_store_is_unconfigured(self) -> None:
        self.assertFalse(self.store.is_configured())
        self.assertEqual(self.store.model_overrides(), {})

    def test_mark_configured_is_idempotent(self) -> None:
        import json
        first = self.store.mark_configured()
        self.assertIn("configured_at", first)
        second = self.store.mark_configured()
        self.assertEqual(second["configured_at"], first["configured_at"])
        on_disk = json.loads(_SETUP.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["surface"], "litigation")
        self.assertEqual(on_disk["version"], 1)

    def test_corrupt_settings_are_visible_not_silent(self) -> None:
        _SETUP.parent.mkdir(parents=True, exist_ok=True)
        _SETUP.write_text("{not json", encoding="utf-8")
        with self.assertRaises(SetupStoreError):
            self.store.model_overrides()

    def test_model_overrides_round_trip_and_clear(self) -> None:
        try:
            self.store.set_model_override("OLLAMA_MODEL", "llama3.1")
            self.assertEqual(self.store.model_overrides(),
                             {"OLLAMA_MODEL": "llama3.1"})
            self.store.set_model_override("OLLAMA_MODEL", "")
            self.assertEqual(self.store.model_overrides(), {})
            with self.assertRaises(SetupStoreError):
                self.store.set_model_override("OLLAMA_MODEL", "multi\nline")
            with self.assertRaises(SetupStoreError):
                self.store.set_model_override("bad-name", "x")
        finally:
            os.environ.pop("OLLAMA_MODEL", None)

    def test_keys_write_read_clear_and_environ_apply(self) -> None:
        try:
            state = self.store.write_keys({"OPENAI_API_KEY": "sk-test-value"})
            self.assertEqual(state, {"OPENAI_API_KEY": True})
            self.assertEqual(self.store.read_keys(), {"OPENAI_API_KEY": "sk-test-value"})
            self.store.apply_to_environ({"OPENAI_API_KEY": "sk-test-value"})
            self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-test-value")
            cleared = self.store.write_keys({"OPENAI_API_KEY": None})
            self.assertEqual(cleared, {"OPENAI_API_KEY": False})
            self.assertEqual(self.store.read_keys(), {})
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

    def test_key_file_is_created_private(self) -> None:
        try:
            self.store.write_keys({"ZAI_API_KEY": "zai-secret"})
            mode = stat.S_IMODE(_KEYS.stat().st_mode)
            self.assertEqual(mode, 0o600)
        finally:
            os.environ.pop("ZAI_API_KEY", None)

    def test_multiline_value_is_rejected(self) -> None:
        with self.assertRaises(SetupStoreError):
            self.store.write_keys({"OPENAI_API_KEY": "line1\nFORGED_KEY=x"})

    def test_load_keys_and_overrides_into(self) -> None:
        try:
            env: dict[str, str] = {"OPENAI_API_KEY": "old"}
            self.store.write_keys({"OPENAI_API_KEY": "new"})
            self.store.set_model_override("OLLAMA_MODEL", "llama3.1")
            self.store.load_keys_into(env)
            self.store.load_overrides_into(env)
            self.assertEqual(env["OPENAI_API_KEY"], "new")
            self.assertEqual(env["OLLAMA_MODEL"], "llama3.1")
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("OLLAMA_MODEL", None)


class SetupApiTests(_RouteTestBase):
    def test_get_setup_reports_first_run_and_families(self) -> None:
        body = self.client.get("/api/setup").json()
        self.assertTrue(body["first_run"])
        self.assertFalse(body["configured"])
        self.assertEqual(body["subscription_ids"], ["claude", "glm"])
        by_name = {k["env_key"]: k for k in body["api_keys"]}
        self.assertIn("OPENAI_API_KEY", by_name)
        self.assertFalse(by_name["OPENAI_API_KEY"]["set"])
        credentials = {c["env_key"] for c in body["subscription_credentials"]}
        self.assertEqual(credentials, {"ZAI_API_KEY"})
        locals_by_id = {s["id"]: s for s in body["local_servers"]}
        self.assertEqual(set(locals_by_id), {"ollama", "lmstudio"})
        # base_url points at closed ports, so reachability is False and safe
        self.assertFalse(locals_by_id["ollama"]["reachable"])
        self.assertEqual(locals_by_id["ollama"]["model"], "llama3.2")
        self.assertNotIn("cli", str(body))

    def test_key_write_marks_configured_and_never_echoes_value(self) -> None:
        try:
            response = self.client.post("/api/setup/keys", json={
                "keys": {"OPENAI_API_KEY": "sk-live-secret-value"}})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"keys": {"OPENAI_API_KEY": True}})
            self.assertNotIn("sk-live-secret-value", response.text)
            self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-live-secret-value")
            listed = self.client.get("/api/setup").json()
            self.assertFalse(listed["first_run"])  # saving configures the app
            self.assertNotIn("sk-live-secret-value", str(listed))
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

    def test_zai_credential_is_accepted_subscription_side(self) -> None:
        try:
            response = self.client.post("/api/setup/keys", json={
                "keys": {"ZAI_API_KEY": "zai-plan-secret"}})
            self.assertEqual(response.status_code, 200)
            listed = self.client.get("/api/setup").json()
            credentials = {c["env_key"]: c for c in listed["subscription_credentials"]}
            self.assertTrue(credentials["ZAI_API_KEY"]["set"])
            self.assertNotIn("zai-plan-secret", str(listed))
        finally:
            os.environ.pop("ZAI_API_KEY", None)

    def test_unknown_key_name_is_rejected(self) -> None:
        response = self.client.post("/api/setup/keys", json={
            "keys": {"TOTALLY_MADE_UP": "x"}})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(_KEYS.exists())

    def test_empty_value_clears_and_delete_removes(self) -> None:
        try:
            self.client.post("/api/setup/keys", json={
                "keys": {"OPENAI_API_KEY": "sk-to-clear"}})
            cleared = self.client.post("/api/setup/keys", json={
                "keys": {"OPENAI_API_KEY": ""}})
            self.assertEqual(cleared.json(), {"keys": {"OPENAI_API_KEY": False}})
            self.assertNotIn("OPENAI_API_KEY", os.environ)
            self.client.post("/api/setup/keys", json={
                "keys": {"OPENAI_API_KEY": "sk-again"}})
            removed = self.client.delete("/api/setup/keys/OPENAI_API_KEY")
            self.assertEqual(removed.status_code, 200)
            self.assertEqual(self.store.read_keys(), {})
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

    def test_delete_unknown_key_is_404(self) -> None:
        self.assertEqual(
            self.client.delete("/api/setup/keys/NOT_A_THING").status_code, 404)

    def test_dismiss_stops_first_run(self) -> None:
        self.assertEqual(
            self.client.post("/api/setup/dismiss").json(), {"configured": True})
        body = self.client.get("/api/setup").json()
        self.assertFalse(body["first_run"])
        self.assertTrue(body["configured"])

    def test_local_model_override_round_trip(self) -> None:
        try:
            response = self.client.post("/api/setup/local/ollama",
                                        json={"model": "llama3.1"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(),
                             {"id": "ollama", "model_env": "OLLAMA_MODEL",
                              "model": "llama3.1"})
            self.assertEqual(os.environ["OLLAMA_MODEL"], "llama3.1")
            listed = self.client.get("/api/setup").json()
            locals_by_id = {s["id"]: s for s in listed["local_servers"]}
            self.assertEqual(locals_by_id["ollama"]["model"], "llama3.1")
            cleared = self.client.post("/api/setup/local/ollama",
                                       json={"model": ""})
            self.assertEqual(cleared.json()["model"], "")
            self.assertNotIn("OLLAMA_MODEL", os.environ)
        finally:
            os.environ.pop("OLLAMA_MODEL", None)

    def test_local_override_rejects_unknown_and_keyed_providers(self) -> None:
        self.assertEqual(
            self.client.post("/api/setup/local/gpt", json={"model": "x"}).status_code, 404)
        self.assertEqual(
            self.client.post("/api/setup/local/nope", json={"model": "x"}).status_code, 404)

    def test_corrupt_settings_reports_store_error_without_crashing(self) -> None:
        _SETUP.parent.mkdir(parents=True, exist_ok=True)
        _SETUP.write_text("{broken", encoding="utf-8")
        body = self.client.get("/api/setup").json()
        self.assertTrue(body["first_run"])
        self.assertIn("store_error", body)
        fixed = self.client.post("/api/setup/dismiss")
        self.assertEqual(fixed.status_code, 200)
        self.assertTrue(self.store.is_configured())

    def test_setup_page_serves(self) -> None:
        response = self.client.get("/setup")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Subscription accounts", response.text)

    def test_subscription_only_hides_and_rejects_paid_and_local_setup(self) -> None:
        with mock.patch.dict(os.environ, {"LITIGATION_SUBSCRIPTION_ONLY": "1"}), \
             mock.patch("setup_routes._reachable") as reachable:
            body = self.client.get("/api/setup").json()
            paid = self.client.post("/api/setup/keys", json={
                "keys": {"OPENAI_API_KEY": "must-not-be-written"}})
            local = self.client.post("/api/setup/local/ollama", json={
                "model": "llama3.1"})
        self.assertTrue(body["subscription_only"])
        self.assertEqual(body["api_keys"], [])
        self.assertEqual(body["local_servers"], [])
        reachable.assert_not_called()
        self.assertEqual(paid.status_code, 400)
        self.assertEqual(local.status_code, 404)
        self.assertFalse(_KEYS.exists())


class RoomCreationTests(_RouteTestBase):
    """No global billing mode exists anymore: rooms mix families freely."""

    def test_room_creation_mixes_families_without_restriction(self) -> None:
        import server
        server_client = TestClient(server.app)
        with mock.patch("server.providers.registry", side_effect=_fake_registry):
            response = server_client.post("/api/rooms", json={
                "participants": [{"id": "gpt"}, {"id": "claude"},
                                 {"id": "ollama"}],
                "crosstalk": False,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["participants"]), 3)


class RoomParticipantUpdateTests(_RouteTestBase):
    """Counsel can be added to a room after the conversation started."""

    def setUp(self) -> None:
        super().setUp()
        import server
        self.server_client = TestClient(server.app)
        patcher = mock.patch("server.providers.registry",
                             side_effect=_fake_registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        created = self.server_client.post("/api/rooms", json={
            "participants": [{"id": "claude"}], "crosstalk": False})
        self.assertEqual(created.status_code, 200)
        self.room_id = created.json()["id"]

    def test_add_models_mid_conversation(self) -> None:
        response = self.server_client.patch(f"/api/rooms/{self.room_id}", json={
            "participants": [
                {"id": "claude"},          # keeps its existing model
                {"id": "gpt"},
                {"id": "ollama", "model": "llama3.1"},
            ]})
        self.assertEqual(response.status_code, 200)
        by_id = {p["id"]: p for p in response.json()["participants"]}
        self.assertEqual(set(by_id), {"claude", "gpt", "ollama"})
        self.assertEqual(by_id["ollama"]["model"], "llama3.1")
        self.assertEqual(by_id["claude"]["model"], "test-model")
        # the roster survives a fresh read
        reread = self.server_client.get(f"/api/rooms/{self.room_id}").json()
        self.assertEqual(len(reread["participants"]), 3)

    def test_unknown_participant_is_rejected_without_changing_the_room(self) -> None:
        before = self.server_client.get(f"/api/rooms/{self.room_id}").json()
        response = self.server_client.patch(f"/api/rooms/{self.room_id}", json={
            "participants": [{"id": "claude"}, {"id": "not-a-model"}]})
        self.assertEqual(response.status_code, 400)
        after = self.server_client.get(f"/api/rooms/{self.room_id}").json()
        self.assertEqual(after["participants"], before["participants"])

    def test_empty_roster_is_rejected(self) -> None:
        response = self.server_client.patch(f"/api/rooms/{self.room_id}",
                                            json={"participants": []})
        self.assertEqual(response.status_code, 400)

    def test_missing_participant_model_falls_back_to_current(self) -> None:
        patched = self.server_client.patch(f"/api/rooms/{self.room_id}", json={
            "participants": [{"id": "claude", "model": "custom-x"}]})
        self.assertEqual(patched.json()["participants"][0]["model"], "custom-x")
        again = self.server_client.patch(f"/api/rooms/{self.room_id}", json={
            "participants": [{"id": "claude"}]})
        self.assertEqual(again.json()["participants"][0]["model"], "custom-x")

    def test_unknown_room_is_404(self) -> None:
        self.assertEqual(
            self.server_client.patch("/api/rooms/missing", json={
                "participants": [{"id": "claude"}]}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
