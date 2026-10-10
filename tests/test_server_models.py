from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient


_TEMP_DIR = tempfile.TemporaryDirectory(prefix="litigation-server-test-")
os.environ["LITIGATION_DB"] = str(Path(_TEMP_DIR.name) / "litigation.db")
os.environ["LITIGATION_PROJECTS_DIR"] = str(Path(_TEMP_DIR.name) / "projects")
os.environ["LITIGATION_PROJECTS_DB"] = str(Path(_TEMP_DIR.name) / "projects.sqlite3")
os.environ["LITIGATION_RULES_DB"] = str(Path(_TEMP_DIR.name) / "rules.sqlite3")
os.environ["LITIGATION_SEAM_STATE_DIR"] = str(Path(_TEMP_DIR.name) / "seam")
os.environ["LITIGATION_SEAM_SYNC_ENABLED"] = "0"

import server  # noqa: E402  (the isolated DB path must be set first)


def _entry(model_id: str, *, available: bool = True) -> dict[str, object]:
    subscription = model_id in server.CHECKABLE_SUBSCRIPTIONS
    return {
        "id": model_id,
        "label": model_id.title(),
        "color": "#123456",
        "model": "test-model",
        "api": "cli" if subscription else "openai",
        "env_key": "" if subscription else "TEST_API_KEY",
        "installed": True if subscription else None,
        "configured": True,
        "available": available,
        "setup_status": "installed_unverified" if subscription else "configured_unverified",
        "auth_status": "unverified",
        "transport": "test-cli" if subscription else "openai",
        "billing": "subscription" if subscription else "api_or_local",
        "setup_hint": "Sign in, then try again.",
        "cli": ["secret-command", "--secret-argument"],
    }


class ModelApiTests(unittest.TestCase):
    def setUp(self) -> None:
        server.MODEL_CHECKS.clear()
        self.client = TestClient(server.app)

    def test_unknown_and_non_subscription_checks_are_rejected(self) -> None:
        entries = [_entry("claude"), _entry("gpt")]
        with mock.patch("server.providers.registry", return_value=entries), \
             mock.patch("server.providers.complete") as complete:
            unknown = self.client.post("/api/models/missing/check")
            api_provider = self.client.post("/api/models/gpt/check")
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(api_provider.status_code, 400)
        complete.assert_not_called()

    def test_exact_reply_marks_check_verified_and_get_merges_status(self) -> None:
        claude = _entry("claude")
        with mock.patch("server.providers.registry", return_value=[claude]), \
             mock.patch("server.providers.get_agent", return_value=claude), \
             mock.patch("server.providers.complete", return_value="LITIGATION_OK") as complete:
            checked = self.client.post("/api/models/claude/check")
            listed = self.client.get("/api/models")
        self.assertEqual(checked.status_code, 200)
        self.assertTrue(checked.json()["ok"])
        self.assertEqual(checked.json()["connection_status"], "verified")
        self.assertTrue(checked.json()["checked_at"].endswith("Z"))
        self.assertEqual(listed.json()[0]["auth_status"], "verified")
        self.assertEqual(listed.json()[0]["connection_status"], "verified")
        self.assertNotIn("cli", listed.json()[0])
        self.assertNotIn("env_key", listed.json()[0])
        kwargs = complete.call_args.kwargs
        self.assertFalse(kwargs["coding"])
        self.assertEqual(
            complete.call_args.args[1],
            [{
                "role": "user",
                "content": (
                    "This is a connection test. Please reply with exactly "
                    "LITIGATION_OK. Do not use tools or read files."
                ),
            }],
        )

    def test_failure_is_flagged_without_raw_exception_or_output(self) -> None:
        grok = _entry("grok")
        with mock.patch("server.providers.get_agent", return_value=grok), \
             mock.patch("server.providers.complete", side_effect=RuntimeError("secret raw output")):
            response = self.client.post("/api/models/grok/check")
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["ok"])
        self.assertEqual(body["connection_status"], "error")
        self.assertNotIn("secret raw output", str(body))
        self.assertNotIn("secret raw output", str(server.MODEL_CHECKS))

    def test_unexpected_reply_is_not_verified_or_returned(self) -> None:
        gemini = _entry("gemini")
        with mock.patch("server.providers.get_agent", return_value=gemini), \
             mock.patch("server.providers.complete", return_value="private provider response"):
            response = self.client.post("/api/models/gemini/check")
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["connection_status"], "error")
        self.assertNotIn("private provider response", str(body))
        self.assertNotIn("private provider response", str(server.MODEL_CHECKS))


class RoomRulesTests(unittest.TestCase):
    def test_room_system_uses_global_and_each_canonical_model_section(self) -> None:
        expected = {
            "claude": "# Claude rules", "openai": "# OpenAI rules",
            "glm": "# GLM rules", "gemini": "# Gemini rules",
            "cline": "# Cline rules", "grok": "# Grok rules",
            "codex": "# OpenAI rules", "gpt": "# OpenAI rules",
            "astra": "# OpenAI rules", "chatgpt": "# OpenAI rules",
            "antigravity": "# Gemini rules", "agy": "# Gemini rules",
        }
        for identity, model_heading in expected.items():
            with self.subTest(identity=identity):
                prompt = server._room_system(identity, [{"id": identity, "label": identity}])
                self.assertIn("# Global rules", prompt)
                self.assertIn(model_heading, prompt)
                self.assertIn("Active Litigation rules for this response", prompt)

    def test_unknown_provider_receives_global_rules_only(self) -> None:
        prompt = server._room_system("local-extra", [{"id": "local-extra", "label": "Extra"}])
        self.assertIn("# Global rules", prompt)
        for heading in ("# Claude rules", "# OpenAI rules", "# GLM rules",
                        "# Gemini rules", "# Cline rules", "# Grok rules"):
            self.assertNotIn(heading, prompt)

    def test_non_scope_rules_error_is_not_silently_discarded(self) -> None:
        broken = mock.Mock()
        broken.get_effective.side_effect = server.RulesStoreError("database is not open")
        with mock.patch.object(server, "RULES_STORE", broken):
            with self.assertRaisesRegex(server.RulesStoreError, "database is not open"):
                server._room_system("claude", [{"id": "claude", "label": "Claude"}])
        broken.get_active.assert_not_called()

    def test_build_messages_includes_effective_rules(self) -> None:
        messages = server._build_messages(
            "grok", [{"sender": "user", "content": "hello"}],
            [{"id": "grok", "label": "Grok"}],
        )
        self.assertIn("# Global rules", messages[0]["content"])
        self.assertIn("# Grok rules", messages[0]["content"])
        self.assertEqual(messages[1], {"role": "user", "content": "hello"})

class CodingReviewRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_only_subscription_reviews_without_coding_flags(self) -> None:
        agent = _entry("claude")
        agent["supports_coding"] = False
        captured: dict[str, object] = {}

        async def fake_stream(_agent, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            yield "review"

        room = {
            "id": "room-test",
            "participants": [{"id": "claude", "label": "Claude", "model": "test-model"}],
            "crosstalk": False,
        }
        with mock.patch("server.providers.get_agent", return_value=agent), \
             mock.patch("server.providers.stream_completion", side_effect=fake_stream), \
             mock.patch("server.DB.add_message", return_value={"id": "message-test"}), \
             mock.patch("server._head_ref") as head_ref:
            events = [event async for event in server._run_turn(
                room,
                room["participants"],
                [{"sender": "user", "content": "review the patch"}],
                coding=True,
                workspace="/tmp/review-only",
            )]

        self.assertTrue(events)
        head_ref.assert_not_called()
        self.assertFalse(captured["kwargs"]["coding"])
        self.assertIsNone(captured["kwargs"]["cwd"])
        self.assertIn("reviewer", captured["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
