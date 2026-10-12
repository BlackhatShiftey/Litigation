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

# test_server_models.py may already have imported server under its own temp
# environment; these lines follow the same isolation pattern regardless.
os.environ.setdefault("LITIGATION_SEAM_SYNC_ENABLED", "0")

import server  # noqa: E402


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
         "env_key": "", "base_url": "http://localhost:11434/v1", "model": model},
        {"id": "zcode", "label": "ZCode", "color": "#f59e0b", "api": "cli",
         "env_key": "", "billing": "api_or_local", "model": model},
    ]


class _RouteTestBase(unittest.TestCase):
    def setUp(self) -> None:
        _SETUP.unlink(missing_ok=True)
        _KEYS.unlink(missing_ok=True)
        self._saved_env = {name: os.environ.get(name)
                           for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY",
                                        "ZAI_API_KEY")}
        for name in self._saved_env:
            os.environ.pop(name, None)
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

    def test_missing_preference_reads_as_unset(self) -> None:
        self.assertEqual(self.store.auth_mode(), "unset")

    def test_mode_round_trip_and_record_shape(self) -> None:
        import json
        record = self.store.set_auth_mode("subscription")
        self.assertEqual(record["version"], 1)
        self.assertEqual(record["surface"], "litigation")
        self.assertTrue(record["chosen_at"].endswith(("+00:00", "Z")))
        self.assertEqual(self.store.auth_mode(), "subscription")
        on_disk = json.loads(_SETUP.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["auth_mode"], "subscription")

    def test_invalid_mode_is_rejected(self) -> None:
        with self.assertRaises(SetupStoreError):
            self.store.set_auth_mode("bogus")

    def test_corrupt_preference_is_visible_not_silent(self) -> None:
        _SETUP.parent.mkdir(parents=True, exist_ok=True)
        _SETUP.write_text("{not json", encoding="utf-8")
        with self.assertRaises(SetupStoreError):
            self.store.auth_mode()

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

    def test_load_keys_into_overrides(self) -> None:
        try:
            env: dict[str, str] = {"OPENAI_API_KEY": "old"}
            self.store.write_keys({"OPENAI_API_KEY": "new"})
            loaded = self.store.load_keys_into(env)
            self.assertEqual(loaded, ["OPENAI_API_KEY"])
            self.assertEqual(env["OPENAI_API_KEY"], "new")
        finally:
            os.environ.pop("OPENAI_API_KEY", None)


class SetupApiTests(_RouteTestBase):
    def test_get_setup_reports_unset_and_lists_keys_without_values(self) -> None:
        body = self.client.get("/api/setup").json()
        self.assertEqual(body["auth_mode"], "unset")
        self.assertFalse(body["configured"])
        self.assertEqual(body["modes"], ["subscription", "api", "mixed"])
        by_name = {k["env_key"]: k for k in body["api_keys"]}
        self.assertIn("OPENAI_API_KEY", by_name)
        self.assertIn("OPENROUTER_API_KEY", by_name)
        self.assertFalse(by_name["OPENAI_API_KEY"]["set"])
        credentials = {c["env_key"] for c in body["subscription_credentials"]}
        self.assertEqual(credentials, {"ZAI_API_KEY"})
        self.assertNotIn("cli", str(body))
        self.assertNotIn("required_env", str(body))

    def test_mode_post_persists(self) -> None:
        response = self.client.post("/api/setup/mode", json={"mode": "api"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["auth_mode"], "api")
        self.assertEqual(self.store.auth_mode(), "api")

    def test_mode_post_rejects_unknown_mode(self) -> None:
        response = self.client.post("/api/setup/mode", json={"mode": "bogus"})
        self.assertEqual(response.status_code, 422)

    def test_corrupt_preference_reports_store_error_without_crashing(self) -> None:
        _SETUP.parent.mkdir(parents=True, exist_ok=True)
        _SETUP.write_text("{broken", encoding="utf-8")
        body = self.client.get("/api/setup").json()
        self.assertEqual(body["auth_mode"], "unset")
        self.assertIn("store_error", body)
        fixed = self.client.post("/api/setup/mode", json={"mode": "mixed"})
        self.assertEqual(fixed.status_code, 200)
        self.assertEqual(self.store.auth_mode(), "mixed")

    def test_key_write_applies_immediately_and_never_echoes_value(self) -> None:
        try:
            response = self.client.post("/api/setup/keys", json={
                "keys": {"OPENAI_API_KEY": "sk-live-secret-value"}})
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body, {"keys": {"OPENAI_API_KEY": True}})
            self.assertNotIn("sk-live-secret-value", response.text)
            self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-live-secret-value")
            listed = self.client.get("/api/setup").json()
            by_name = {k["env_key"]: k for k in listed["api_keys"]}
            self.assertTrue(by_name["OPENAI_API_KEY"]["set"])
            self.assertNotIn("sk-live-secret-value", str(listed))
            # The stored file survives a fresh store instance (server restart).
            reopened = SetupStore(setup_path=_SETUP, keys_path=_KEYS)
            self.assertEqual(reopened.read_keys()["OPENAI_API_KEY"],
                             "sk-live-secret-value")
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

    def test_zai_credential_is_accepted_subscription_side(self) -> None:
        try:
            response = self.client.post("/api/setup/keys", json={
                "keys": {"ZAI_API_KEY": "zai-plan-secret"}})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"keys": {"ZAI_API_KEY": True}})
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
            self.assertEqual(removed.json(),
                             {"env_key": "OPENAI_API_KEY", "set": False})
            self.assertEqual(self.store.read_keys(), {})
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

    def test_delete_unknown_key_is_404(self) -> None:
        self.assertEqual(
            self.client.delete("/api/setup/keys/NOT_A_THING").status_code, 404)

    def test_setup_page_serves(self) -> None:
        response = self.client.get("/setup")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Subscriptions or API keys", response.text)


class RoomModeEnforcementTests(_RouteTestBase):
    """New rooms honor the chosen mode; flips apply to the next creation."""

    def setUp(self) -> None:
        super().setUp()
        self.server_client = TestClient(server.app)

    def _registry(self) -> list[dict[str, object]]:
        return _fake_registry()

    def _create(self, *participant_ids: str):
        return self.server_client.post("/api/rooms", json={
            "participants": [{"id": pid} for pid in participant_ids],
            "crosstalk": False,
        })

    def test_unset_mode_allows_everything(self) -> None:
        with mock.patch.object(server, "SETUP_STORE", self.store), \
             mock.patch("server.providers.registry", side_effect=self._registry):
            response = self._create("gpt", "claude", "ollama", "zcode")
        self.assertEqual(response.status_code, 200)

    def test_subscription_mode_blocks_api_billed_only(self) -> None:
        with mock.patch.object(server, "SETUP_STORE", self.store), \
             mock.patch("server.providers.registry", side_effect=self._registry):
            self.store.set_auth_mode("subscription")
            blocked = self._create("gpt")
            allowed = self._create("claude", "ollama", "zcode")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("subscription usage", blocked.json()["detail"])
        self.assertIn("flip the auth mode", blocked.json()["detail"].lower())
        self.assertEqual(allowed.status_code, 200)

    def test_api_mode_blocks_subscription_routes_only(self) -> None:
        with mock.patch.object(server, "SETUP_STORE", self.store), \
             mock.patch("server.providers.registry", side_effect=self._registry):
            self.store.set_auth_mode("api")
            blocked = self._create("claude")
            blocked_glm = self._create("glm")
            allowed = self._create("gpt", "ollama", "zcode")
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(blocked_glm.status_code, 400)
        self.assertIn("API-key usage", blocked.json()["detail"])
        self.assertEqual(allowed.status_code, 200)

    def test_flip_mid_session_changes_next_creation(self) -> None:
        with mock.patch.object(server, "SETUP_STORE", self.store), \
             mock.patch("server.providers.registry", side_effect=self._registry):
            self.store.set_auth_mode("subscription")
            self.assertEqual(self._create("gpt").status_code, 400)
            self.store.set_auth_mode("mixed")
            self.assertEqual(self._create("gpt").status_code, 200)

    def test_mixed_mode_restores_today_behavior(self) -> None:
        with mock.patch.object(server, "SETUP_STORE", self.store), \
             mock.patch("server.providers.registry", side_effect=self._registry):
            self.store.set_auth_mode("mixed")
            response = self._create("gpt", "claude", "ollama")
        self.assertEqual(response.status_code, 200)

    def test_billing_class_classification(self) -> None:
        registry = {entry["id"]: entry for entry in _fake_registry()}
        self.assertEqual(server._billing_class(registry["claude"]), "subscription")
        self.assertEqual(server._billing_class(registry["glm"]), "subscription")
        self.assertEqual(server._billing_class(registry["gpt"]), "api")
        self.assertEqual(server._billing_class(registry["ollama"]), "other")
        self.assertEqual(server._billing_class(registry["zcode"]), "other")


if __name__ == "__main__":
    unittest.main()
