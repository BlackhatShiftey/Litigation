from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest import mock

import providers


FAKE_CLI = r'''import json
import os
import sys

capture = os.environ["LITIGATION_TEST_CAPTURE"]
payload = {
    "argv": sys.argv[1:],
    "stdin": sys.stdin.read(),
    "env": {
        name: (name in os.environ)
        for name in (
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GEMINI_CLI_SYSTEM_SETTINGS_PATH",
            "ZAI_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLINE_DATA_DIR",
            "CLINE_PROVIDER_SETTINGS_PATH",
            "CLINE_GLOBAL_SETTINGS_PATH",
            "CLINE_MCP_SETTINGS_PATH",
            "CLINE_TOOL_APPROVAL_MODE",
            "XAI_API_KEY",
            "GROK_API_KEY",
            "OPENAI_API_KEY",
        )
    },
}
with open(capture, "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
mode = os.environ.get("LITIGATION_TEST_MODE", "plain")
if mode == "fail":
    print("secret-token-from-stderr", file=sys.stderr)
    raise SystemExit(7)
if mode == "invalid-json":
    print("not-json")
elif mode == "antigravity-json":
    print(json.dumps({"status": "SUCCESS", "response": "antigravity reply"}))
elif mode == "zero-timeout":
    print(json.dumps({"status": "SUCCESS", "response": "partial"}))
    print("print timeout; turn still in progress; partial output", file=sys.stderr)
elif mode == "cline-ndjson":
    print(json.dumps({"type": "agent_event", "event": {
        "type": "content_start", "contentType": "text", "text": "cline "}}))
    print(json.dumps({"type": "agent_event", "event": {
        "type": "content_start", "contentType": "text", "text": "reply"}}))
    print(json.dumps({"type": "agent_event", "event": {
        "type": "done", "reason": "completed", "iterations": 1}}))
else:
    print("plain reply")
'''


SLEEPING_CLI = r'''import os
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
with open(os.environ["LITIGATION_TEST_CAPTURE"], "w", encoding="utf-8") as handle:
    handle.write(f"{os.getpgrp()} {child.pid}")
    handle.flush()
time.sleep(30)
'''


class ProviderCliTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="litigation-provider-test-")
        self.root = Path(self.tempdir.name)
        self.fake_cli = self.root / "fake_cli.py"
        self.fake_cli.write_text(FAKE_CLI, encoding="utf-8")
        self.capture = self.root / "capture.json"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _fake_agent(self, provider_id: str) -> dict[str, object]:
        agent = providers.get_agent(provider_id)
        assert agent is not None
        agent["cli"] = [sys.executable, str(self.fake_cli), *agent["cli"][1:]]
        env = dict(agent.get("cli_env", {}))
        env["LITIGATION_TEST_CAPTURE"] = str(self.capture)
        agent["cli_env"] = env
        return agent

    async def test_claude_safe_args_env_filter_and_model_override(self) -> None:
        agent = self._fake_agent("claude")
        with mock.patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "do-not-forward",
                "CLAUDE_CODE_USE_BEDROCK": "do-not-forward",
                "CLAUDE_CODE_OAUTH_TOKEN": "do-not-forward",
            },
        ):
            reply = await providers.complete(
                agent, [{"role": "user", "content": "hello"}], model="opus"
            )
        self.assertEqual(reply, "plain reply")
        captured = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertEqual(captured["stdin"], "hello")
        self.assertFalse(captured["env"]["ANTHROPIC_API_KEY"])
        self.assertFalse(captured["env"]["CLAUDE_CODE_USE_BEDROCK"])
        self.assertFalse(captured["env"]["CLAUDE_CODE_OAUTH_TOKEN"])
        argv = captured["argv"]
        self.assertIn("--safe-mode", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertNotIn("--dangerously-skip-permissions", argv)

    async def test_grok_prompt_and_tool_denials(self) -> None:
        agent = self._fake_agent("grok")
        with mock.patch.dict(os.environ, {
            "XAI_API_KEY": "do-not-forward",
            "GROK_API_KEY": "do-not-forward",
            "OPENAI_API_KEY": "do-not-forward",
        }):
            reply = await providers.complete(
                agent, [{"role": "user", "content": "grok prompt"}], model="grok-test"
            )
        self.assertEqual(reply, "plain reply")
        captured = json.loads(self.capture.read_text(encoding="utf-8"))
        argv = captured["argv"]
        self.assertFalse(captured["env"]["XAI_API_KEY"])
        self.assertFalse(captured["env"]["GROK_API_KEY"])
        self.assertFalse(captured["env"]["OPENAI_API_KEY"])
        self.assertEqual(argv[-1], "grok prompt")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--deny") + 1], "*")
        self.assertIn("--no-subagents", argv)
        self.assertIn("--disable-web-search", argv)
        self.assertNotIn("--always-approve", argv)

    async def test_gemini_routes_to_antigravity_with_prompt_last(self) -> None:
        agent = self._fake_agent("gemini")
        agent["cli_env"] = {
            **agent["cli_env"],
            "LITIGATION_TEST_MODE": "antigravity-json",
        }
        reply = await providers.complete(
            agent, [{"role": "user", "content": "/unsafe-looking"}]
        )
        self.assertEqual(reply, "antigravity reply")
        captured = json.loads(self.capture.read_text(encoding="utf-8"))
        argv = captured["argv"]
        self.assertIn("--mode", argv)
        self.assertIn("--sandbox", argv)
        self.assertNotIn("--disable-slash-commands", argv)
        self.assertLess(argv.index("--model"), argv.index("-p"))
        self.assertEqual(argv[-2], "-p")
        self.assertEqual(argv[-1], "User message:\n/unsafe-looking")

    async def test_failure_does_not_expose_cli_output_or_stderr(self) -> None:
        agent = self._fake_agent("claude")
        agent["cli_env"] = {
            **agent["cli_env"],
            "LITIGATION_TEST_MODE": "fail",
        }
        with self.assertRaises(providers.ProviderError) as caught:
            await providers.complete(agent, [{"role": "user", "content": "hello"}])
        message = str(caught.exception)
        self.assertIn("exit 7", message)
        self.assertNotIn("secret-token", message)

    async def test_structured_output_parse_failure_is_generic(self) -> None:
        agent = self._fake_agent("gemini")
        agent["cli_env"] = {
            **agent["cli_env"],
            "LITIGATION_TEST_MODE": "invalid-json",
        }
        with self.assertRaisesRegex(providers.ProviderError, "returned invalid JSON"):
            await providers.complete(agent, [{"role": "user", "content": "hello"}])

    async def test_zero_exit_partial_output_is_rejected(self) -> None:
        agent = self._fake_agent("gemini")
        agent["cli_env"] = {
            **agent["cli_env"],
            "LITIGATION_TEST_MODE": "zero-timeout",
        }
        with self.assertRaisesRegex(providers.ProviderError, "timed out"):
            await providers.complete(agent, [{"role": "user", "content": "hello"}])

    async def test_glm_maps_only_zai_key_into_claude_environment(self) -> None:
        agent = self._fake_agent("glm")
        with mock.patch.dict(os.environ, {"ZAI_API_KEY": "do-not-print"}):
            reply = await providers.complete(
                agent, [{"role": "user", "content": "hello"}], model="glm-5.3"
            )
        self.assertEqual(reply, "plain reply")
        captured = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertFalse(captured["env"]["ZAI_API_KEY"])
        self.assertTrue(captured["env"]["ANTHROPIC_AUTH_TOKEN"])
        self.assertTrue(captured["env"]["ANTHROPIC_BASE_URL"])

    async def test_cline_uses_isolated_tool_disabled_configuration(self) -> None:
        agent = self._fake_agent("cline")
        agent["cli_state_root"] = str(self.root / "cline-state")
        agent["cli_env"] = {
            **agent["cli_env"],
            "LITIGATION_TEST_MODE": "cline-ndjson",
        }
        with mock.patch.dict(os.environ, {"CLINE_TOOL_APPROVAL_MODE": "unsafe"}):
            reply = await providers.complete(
                agent, [{"role": "user", "content": "read @secret"}]
            )
        self.assertEqual(reply, "cline reply")
        captured = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertFalse(captured["env"]["CLINE_TOOL_APPROVAL_MODE"])
        for name in (
            "CLINE_DATA_DIR", "CLINE_PROVIDER_SETTINGS_PATH",
            "CLINE_GLOBAL_SETTINGS_PATH", "CLINE_MCP_SETTINGS_PATH",
        ):
            self.assertTrue(captured["env"][name], name)
        argv = captured["argv"]
        self.assertEqual(argv[argv.index("--provider") + 1], "cline-pass")
        self.assertEqual(argv[argv.index("--auto-approve") + 1], "false")
        self.assertIn(r"\u0040secret", argv[-1])

    def test_cline_parser_rejects_noncompleted_terminal_event(self) -> None:
        agent = {"label": "Cline", "cli_output": "cline-ndjson"}
        raw = b"\n".join((
            b'{"type":"agent_event","event":{"type":"content_start","contentType":"text","text":"partial"}}',
            b'{"type":"agent_event","event":{"type":"done","reason":"aborted"}}',
        ))
        with self.assertRaisesRegex(providers.ProviderError, "did not complete"):
            providers._parse_cli_output(agent, raw)

    async def test_cline_rejects_non_subscription_model_override(self) -> None:
        agent = self._fake_agent("cline")
        agent["cli_state_root"] = str(self.root / "cline-state")
        with self.assertRaisesRegex(providers.ProviderError, "cline-pass/"):
            await providers.complete(
                agent, [{"role": "user", "content": "hello"}], model="anthropic/paid"
            )

    async def test_timeout_terminates_process_group(self) -> None:
        sleeping = self.root / "sleeping_cli.py"
        sleeping.write_text(SLEEPING_CLI, encoding="utf-8")
        capture = self.root / "sleeping-pids.txt"
        agent = {
            "id": "fake",
            "label": "Fake",
            "api": "cli",
            "cli": [sys.executable, str(sleeping)],
            "cli_stdin": True,
            "cli_timeout": 0.2,
            "cli_env": {"LITIGATION_TEST_CAPTURE": str(capture)},
            "model": "",
        }
        with self.assertRaisesRegex(providers.ProviderError, "timed out"):
            await providers.complete(agent, [{"role": "user", "content": "hello"}])
        process_group, child_pid = map(int, capture.read_text(encoding="utf-8").split())
        for _ in range(20):
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            self.fail(f"process group {process_group} survived timeout; child={child_pid}")


class ProviderRegistryTests(unittest.TestCase):
    def test_antigravity_long_turn_timeout_stays_inside_agent_wall_budget(self) -> None:
        for agent_id in ("antigravity", "gemini"):
            agent = providers.get_agent(agent_id)
            assert agent is not None
            marker = agent["cli"].index("--print-timeout")
            self.assertEqual(agent["cli"][marker + 1], "180s")
            self.assertEqual(agent["cli_timeout"], 240)

    def test_cline_controller_prompt_requires_literal_json_without_native_tools(self) -> None:
        transformed = providers._transform_cli_prompt(
            {"cli_prompt_transform": "cline-safe"},
            "You are a coding agent controlled by Litigation. Use read_files and return an action for user@example.com",
        )
        decoded = json.loads(transformed)
        self.assertIn("Do not invoke any native Cline tool", decoded)
        self.assertIn("external Litigation controller", decoded)
        self.assertIn("read_files", decoded)
        self.assertNotIn("@example.com", transformed)

    def test_cline_ordinary_chat_does_not_force_controller_json(self) -> None:
        transformed = providers._transform_cli_prompt(
            {"cli_prompt_transform": "cline-safe"},
            "Tell me why the sky is blue.",
        )
        decoded = json.loads(transformed)
        self.assertIn("Do not invoke any native Cline tool", decoded)
        self.assertNotIn("Emit exactly one JSON object", decoded)
        self.assertNotIn("external Litigation controller", decoded)
        self.assertIn("Tell me why the sky is blue", decoded)

    def test_cli_installation_is_not_reported_as_verified_auth(self) -> None:
        with mock.patch("providers.shutil.which", return_value="/opt/fake/claude"):
            agent = providers.get_agent("claude")
        assert agent is not None
        self.assertTrue(agent["installed"])
        self.assertEqual(agent["setup_status"], "installed_unverified")
        self.assertEqual(agent["auth_status"], "unverified")
        self.assertEqual(agent["billing"], "subscription")


if __name__ == "__main__":
    unittest.main()
