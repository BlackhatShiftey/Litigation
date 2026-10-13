from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import unittest
from unittest import mock

import providers


FAKE_CLI = r'''import json
import os
import stat
import sys

capture = os.environ["LITIGATION_TEST_CAPTURE"]
stdin_text = sys.stdin.read()
payload = {
    "argv": sys.argv[1:],
    "stdin": stdin_text,
    "stdin_is_regular": stat.S_ISREG(os.fstat(0).st_mode),
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
elif mode == "antigravity-stream-json":
    print(json.dumps({"event": "init", "private": "not returned"}))
    print(json.dumps({"event": "step_update", "private": "not returned"}))
    print(json.dumps({"event": "result", "result": {
        "status": "SUCCESS", "response": "antigravity stream reply",
        "private": "not returned"}}))
elif mode == "zero-timeout":
    print(json.dumps({"status": "SUCCESS", "response": "partial"}))
    print("print timeout; turn still in progress; partial output", file=sys.stderr)
elif mode == "cline-ndjson":
    primer = "Respond to the complete piped input. Follow its instructions and do not call native tools."
    if os.environ.get("LITIGATION_TEST_REQUIRE_CLINE_PRIMER") == "1":
        if not payload["stdin_is_regular"] or primer not in payload["argv"]:
            raise SystemExit(9)
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
        self.subscription_patch = mock.patch.dict(
            os.environ, {"LITIGATION_SUBSCRIPTION_ONLY": "0"}
        )
        self.subscription_patch.start()
        self.tempdir = tempfile.TemporaryDirectory(prefix="litigation-provider-test-")
        self.root = Path(self.tempdir.name)
        self.fake_cli = self.root / "fake_cli.py"
        self.fake_cli.write_text(FAKE_CLI, encoding="utf-8")
        self.capture = self.root / "capture.json"

    def tearDown(self) -> None:
        self.tempdir.cleanup()
        self.subscription_patch.stop()

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

    async def test_grok_60k_unicode_uses_retained_private_prompt_file(self) -> None:
        agent = self._fake_agent("grok")
        retained = self.root / "retained"
        prompt = ("x" * 60_000) + "🙂終"
        with mock.patch.dict(os.environ, {
            "LITIGATION_PROVIDER_STATE_ROOT": str(retained),
            "LITIGATION_CLI_ARGV_THRESHOLD_BYTES": "49152",
        }):
            reply = await providers.complete(agent, [{"role": "user", "content": prompt}])
        self.assertEqual(reply, "plain reply")
        captured = json.loads(self.capture.read_text(encoding="utf-8"))
        argv = captured["argv"]
        self.assertIn("--prompt-file", argv)
        path = Path(argv[argv.index("--prompt-file") + 1])
        self.assertEqual(path.read_text(encoding="utf-8"), prompt)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(captured["stdin"], "")

    async def test_cline_60k_unicode_uses_stdin_without_losing_transform(self) -> None:
        agent = self._fake_agent("cline")
        agent["cli_state_root"] = str(self.root / "cline-state")
        retained = self.root / "retained"
        agent["cli_env"] = {
            **agent["cli_env"],
            "LITIGATION_TEST_MODE": "cline-ndjson",
            "LITIGATION_TEST_REQUIRE_CLINE_PRIMER": "1",
        }
        prompt = ("文🙂" * 10_000) + "tail"
        with mock.patch.dict(os.environ, {
            "LITIGATION_PROVIDER_STATE_ROOT": str(retained),
        }):
            reply = await providers.complete(agent, [{"role": "user", "content": prompt}])
        self.assertEqual(reply, "cline reply")
        captured = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertNotIn(prompt, captured["argv"])
        self.assertTrue(captured["stdin_is_regular"])
        self.assertEqual(captured["argv"][-1], agent["cli_large_primer"])
        expected = providers._transform_cli_prompt(agent, prompt)
        self.assertEqual(captured["stdin"], expected)
        files = list((retained / "cline").glob("prompt-*.txt"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_text(encoding="utf-8"), expected)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        open_targets = []
        for fd in Path("/proc/self/fd").iterdir():
            try:
                open_targets.append(fd.resolve())
            except FileNotFoundError:
                continue
        self.assertNotIn(files[0].resolve(), open_targets)

    async def test_cline_large_stdin_file_is_closed_after_timeout(self) -> None:
        sleeping = self.root / "sleeping_cline.py"
        sleeping.write_text(SLEEPING_CLI, encoding="utf-8")
        retained = self.root / "retained"
        capture = self.root / "cline-sleeping-pids.txt"
        agent = {
            "id": "cline", "canonical_id": "cline", "label": "Cline",
            "api": "cli", "cli": [sys.executable, str(sleeping)],
            "cli_large_transport": "stdin-file",
            "cli_large_primer": (
                "Respond to the complete piped input. Follow its instructions "
                "and do not call native tools."
            ),
            "cli_prompt_transform": "cline-safe", "cli_timeout": 0.2,
            "cli_env": {"LITIGATION_TEST_CAPTURE": str(capture)}, "model": "",
        }
        with mock.patch.dict(os.environ, {
            "LITIGATION_PROVIDER_STATE_ROOT": str(retained),
        }):
            with self.assertRaisesRegex(providers.ProviderError, "timed out"):
                await providers.complete(
                    agent, [{"role": "user", "content": "🙂" * 5_000}],
                )
        files = list((retained / "cline").glob("prompt-*.txt"))
        self.assertEqual(len(files), 2)  # the bounded timeout retry retains its input too
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in files))
        open_targets = []
        for fd in Path("/proc/self/fd").iterdir():
            try:
                open_targets.append(fd.resolve())
            except FileNotFoundError:
                continue
        self.assertTrue(all(path.resolve() not in open_targets for path in files))

    async def test_antigravity_60k_uses_stream_json_stdin_and_terminal_result(self) -> None:
        agent = self._fake_agent("gemini")
        agent["cli_env"] = {
            **agent["cli_env"], "LITIGATION_TEST_MODE": "antigravity-stream-json",
        }
        prompt = ("g" * 60_000) + "🙂"
        reply = await providers.complete(agent, [{"role": "user", "content": prompt}])
        self.assertEqual(reply, "antigravity stream reply")
        captured = json.loads(self.capture.read_text(encoding="utf-8"))
        argv = captured["argv"]
        self.assertEqual(argv[argv.index("--input-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        envelope = json.loads(captured["stdin"])
        self.assertEqual(envelope["event"], "user")
        self.assertEqual(envelope["message"], {
            "role": "user", "content": "User message:\n" + prompt,
        })
        self.assertEqual(
            len(captured["stdin"].encode("utf-8")),
            providers._serialized_request_size(
                agent, [{"role": "user", "content": prompt}],
                str(agent["model"]), 2048, 0.7,
            ),
        )

    def test_antigravity_stream_parser_rejects_failure_missing_and_malformed_terminal(self) -> None:
        agent = {"label": "Gemini", "cli_output": "antigravity-stream-json"}
        cases = (
            (b'{"event":"result","result":{"status":"FAILURE","response":"private"}}',
             "did not complete"),
            (b'{"event":"init"}\n{"event":"step_update"}', "successful terminal"),
            (b'{"event":"result","result":{"status":"SUCCESS"}}', "no response text"),
            (b'{"event":"unexpected"}', "malformed"),
        )
        for raw, pattern in cases:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(
                providers.ProviderError, pattern,
            ):
                providers._parse_cli_output(agent, raw)

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
    def test_subscription_only_registry_is_a_builtin_allowlist(self) -> None:
        spoof = json.dumps([{
            "id": "spoof", "label": "Spoof", "api": "cli",
            "billing": "subscription", "cli": ["sh"],
        }])
        with mock.patch.dict(os.environ, {
            "LITIGATION_SUBSCRIPTION_ONLY": "1",
            "LITIGATION_EXTRA_MODELS": spoof,
        }):
            entries = providers.registry()
        self.assertEqual(
            {entry["id"] for entry in entries},
            {"claude", "grok", "gemini", "glm", "cline", "openai", "astra"},
        )
        self.assertTrue(all(entry["api"] == "cli" for entry in entries))
        self.assertTrue(all(entry["billing"] == "subscription" for entry in entries))

    def test_explicit_zero_restores_legacy_mixed_registry(self) -> None:
        with mock.patch.dict(os.environ, {"LITIGATION_SUBSCRIPTION_ONLY": "0"}):
            by_id = {entry["id"]: entry for entry in providers.registry()}
        self.assertEqual(by_id["gpt"]["api"], "openai")
        self.assertIn("ollama", by_id)

    def test_safe_legacy_aliases_resolve_to_subscription_clis(self) -> None:
        with mock.patch.dict(os.environ, {"LITIGATION_SUBSCRIPTION_ONLY": "1"}):
            gpt = providers.resolve_agent("gpt", "gpt-4o-mini")
            agy = providers.resolve_agent("agy", "gemini-safe-model")
            astra = providers.resolve_agent("astra", "gpt-6-astra")
        self.assertEqual(gpt["api"], "cli")
        self.assertEqual(gpt["canonical_id"], "openai")
        self.assertEqual(gpt["model"], "")
        self.assertEqual(agy["canonical_id"], "gemini")
        self.assertEqual(agy["model"], "gemini-safe-model")
        self.assertEqual(astra["canonical_id"], "openai")
        self.assertEqual(astra["model"], "gpt-6-astra")

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


class SubscriptionDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_http_descriptor_is_denied_before_network(self) -> None:
        paid = {
            "id": "openrouter", "label": "OpenRouter", "api": "openai",
            "billing": "api_or_local", "base_url": "https://example.invalid/v1",
            "env_key": "OPENROUTER_API_KEY", "model": "paid-model",
        }
        with mock.patch.dict(os.environ, {"LITIGATION_SUBSCRIPTION_ONLY": "1"}), \
             mock.patch("providers._stream_openai") as stream_openai:
            with self.assertRaisesRegex(providers.ProviderError, "subscription-only"):
                await providers.complete(
                    paid, [{"role": "user", "content": "do not dispatch"}]
                )
        stream_openai.assert_not_called()

    def test_pre_resolved_paid_gpt_descriptor_becomes_chatgpt_cli(self) -> None:
        legacy = {
            "id": "gpt", "label": "GPT", "api": "openai",
            "billing": "api_or_local", "model": "gpt-4o-mini",
        }
        with mock.patch.dict(os.environ, {"LITIGATION_SUBSCRIPTION_ONLY": "1"}):
            resolved, model = providers._resolve_dispatch_agent(legacy, None)
        self.assertEqual(resolved["api"], "cli")
        self.assertEqual(resolved["canonical_id"], "openai")
        self.assertEqual(model, "")


class ProviderRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.subscription_patch = mock.patch.dict(os.environ, {
            "LITIGATION_SUBSCRIPTION_ONLY": "0",
            "LITIGATION_PROVIDER_PAYLOAD_BYTES": "262144",
        })
        self.subscription_patch.start()

    def tearDown(self) -> None:
        self.subscription_patch.stop()

    async def test_timeout_retries_once_same_model_before_output(self) -> None:
        calls = 0
        routed: list[tuple[str, str]] = []

        async def fake_raw(agent, _messages, model, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            routed.append((agent["id"], model))
            if calls == 1:
                raise providers.ProviderError("timeout", code="timeout", retryable=True)
            yield "ok"

        agent = {"id": "fake", "api": "openai", "base_url": "http://local", "model": "m"}
        with mock.patch("providers._stream_completion_raw", side_effect=fake_raw):
            self.assertEqual(await providers.complete(agent, [{"role": "user", "content": "hi"}]), "ok")
        self.assertEqual(calls, 2)
        self.assertEqual(routed, [("fake", "m"), ("fake", "m")])

    async def test_stream_close_waits_for_raw_provider_cleanup_and_releases_lane(self) -> None:
        stopped = asyncio.Event()

        async def fake_raw(*_args, **_kwargs):
            try:
                yield "first"
                await asyncio.Event().wait()
            finally:
                stopped.set()

        agent = {"id": "cleanup", "api": "openai", "base_url": "http://local", "model": "m"}
        with mock.patch("providers._stream_completion_raw", side_effect=fake_raw):
            stream = providers.stream_completion(agent, [{"role": "user", "content": "hi"}])
            self.assertEqual(await anext(stream), "first")
            await stream.aclose()
        self.assertTrue(stopped.is_set())
        lane = next(row for row in providers.model_dispatch.status()
                    if row["provider"] == "cleanup" and row["model"] == "m")
        self.assertEqual(lane["active"], 0)

    async def test_auth_partial_and_coding_failures_are_not_replayed(self) -> None:
        for mode in ("auth", "partial", "coding"):
            calls = 0

            async def fake_raw(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                if mode == "partial":
                    yield "partial"
                raise providers.ProviderError(
                    "failed", code="auth" if mode == "auth" else "timeout", retryable=True,
                )

            agent = {"id": "fake", "api": "openai", "base_url": "http://local", "model": "m"}
            with mock.patch("providers._stream_completion_raw", side_effect=fake_raw):
                with self.assertRaises(providers.ProviderError):
                    await providers.complete(
                        agent, [{"role": "user", "content": "hi"}], coding=mode == "coding",
                    )
            self.assertEqual(calls, 1, mode)

    async def test_real_60k_payload_rejection_recovers_with_smaller_same_model_calls(self) -> None:
        sizes: list[int] = []
        contents: list[str] = []
        request_parts: list[str] = []

        async def fake_raw(_agent, messages, _model, *_args, **_kwargs):
            serialized = providers._serialize_prompt(messages)
            size = len(serialized.encode("utf-8"))
            sizes.append(size)
            contents.append(serialized)
            if size >= 60_000:
                raise providers.ProviderError("too large", code="payload_too_large")
            if "Summarize request chunk" in serialized:
                request_parts.append(messages[-1]["content"].split("\n\n", 1)[1])
                yield "bounded summary"
            else:
                yield "final from summaries"

        prompt = "a" * 60_000 + "🙂終"
        notices: list[dict] = []

        async def notice(value):
            notices.append(value)

        agent = {"id": "fake", "api": "cli", "model": "exact-model",
                 "cli": ["unused"], "cli_stdin": True}
        with mock.patch("providers._stream_completion_raw", side_effect=fake_raw):
            result = await providers.complete(
                agent, [{"role": "system", "content": "keep-policy"},
                        {"role": "user", "content": prompt}],
                notice=notice,
            )
        self.assertEqual(result, "final from summaries")
        self.assertGreater(len(sizes), 2)
        self.assertEqual(sizes[0], len(("[instructions] keep-policy\n\n" + prompt).encode("utf-8")))
        self.assertTrue(all(size < 60_000 for size in sizes[1:]))
        self.assertTrue(all("[instructions] keep-policy" in text for text in contents))
        self.assertEqual("".join(request_parts), prompt)
        self.assertIn("context_recovery", {item["kind"] for item in notices})

    async def test_recovery_chunks_fit_actual_serialized_transformed_budget(self) -> None:
        cases = (
            ({"id": "http", "api": "openai"}, ('"\\' * 10_000) + "end"),
            ({
                "id": "cline", "api": "cli", "cli_prompt_transform": "cline-safe",
                "cli_large_transport": "stdin-file",
                "cli_large_primer": (
                    "Respond to the complete piped input. Follow its instructions "
                    "and do not call native tools."
                ),
            }, "🙂" * 3_500),
        )
        for agent, prompt in cases:
            seen_sizes: list[int] = []
            seen_parts: list[str] = []

            async def fake_collect(current_agent, messages, model, max_tokens,
                                   temperature, _cwd, _coding):
                seen_sizes.append(providers._serialized_request_size(
                    current_agent, messages, model, max_tokens, temperature,
                ))
                seen_parts.append(messages[-1]["content"].split("\n\n", 1)[1])
                return "summary"

            with self.subTest(agent=agent["id"]), \
                 mock.patch("providers._collect_raw", side_effect=fake_collect):
                await providers._summarize_large_latest(
                    agent,
                    [{"role": "system", "content": "policy"},
                     {"role": "user", "content": prompt}],
                    "exact", 512, 0.1, None, 8192,
                )
            self.assertTrue(seen_sizes)
            self.assertTrue(all(size <= 8192 for size in seen_sizes))
            self.assertEqual("".join(seen_parts), prompt)

    async def test_controller_payload_is_never_shortened(self) -> None:
        calls = 0

        async def fake_raw(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise providers.ProviderError("too large", code="payload_too_large")
            yield  # pragma: no cover

        agent = {"id": "fake", "api": "cli", "model": "m", "cli": ["unused"]}
        messages = [{"role": "system", "content": "You are a coding agent controlled by Litigation."},
                    {"role": "user", "content": "x" * 60_000}]
        with mock.patch("providers._stream_completion_raw", side_effect=fake_raw):
            with self.assertRaisesRegex(providers.ProviderError, "protected"):
                await providers.complete(agent, messages)
        self.assertEqual(calls, 1)

    async def test_preflight_drops_old_history_before_considering_latest_summary(self) -> None:
        observed: list[list[dict[str, str]]] = []

        async def fake_raw(_agent, messages, *_args, **_kwargs):
            observed.append(messages)
            yield "ok"

        agent = {"id": "fake", "api": "cli", "model": "m", "cli": ["unused"]}
        messages = [
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "old" * 4_000},
            {"role": "assistant", "content": "older answer"},
            {"role": "user", "content": "tiny latest"},
        ]
        with mock.patch.dict(os.environ, {"LITIGATION_PROVIDER_PAYLOAD_BYTES": "8192"}), \
             mock.patch("providers._stream_completion_raw", side_effect=fake_raw):
            self.assertEqual(await providers.complete(agent, messages), "ok")
        sent = observed[0]
        self.assertTrue(any(m["content"] == "policy" for m in sent))
        self.assertTrue(any(m["content"] == "tiny latest" for m in sent))
        self.assertFalse(any(m["content"].startswith("oldold") for m in sent))
        self.assertFalse(any("Summarize request chunk" in m["content"] for m in sent))

    def test_http_failure_is_typed_and_never_contains_body(self) -> None:
        error = providers._http_failure("m", 413)
        self.assertEqual(error.code, "payload_too_large")
        self.assertNotIn("body", str(error))


class ProviderHttpTests(unittest.IsolatedAsyncioTestCase):
    async def _client_for(self, response: providers.httpx.Response):
        async def handler(_request):
            return response
        return providers.httpx.AsyncClient(transport=providers.httpx.MockTransport(handler))

    async def test_http400_context_error_is_payload_too_large_without_body_leak(self) -> None:
        response = providers.httpx.Response(
            400,
            content=b'{"error":{"code":"context_length_exceeded","private":"secret-body"}}',
        )
        client = await self._client_for(response)
        with mock.patch("providers.httpx.AsyncClient", return_value=client):
            stream = providers._stream_openai(
                "http://provider", "secret-key", "m",
                [{"role": "user", "content": "hello"}], 10, 0.1,
            )
            with self.assertRaises(providers.ProviderError) as caught:
                await anext(stream)
        self.assertEqual(caught.exception.code, "payload_too_large")
        self.assertNotIn("secret-body", str(caught.exception))

    async def test_incomplete_and_empty_http_streams_fail(self) -> None:
        bodies = (
            b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
            b'',
        )
        for body in bodies:
            with self.subTest(body=bool(body)):
                client = await self._client_for(providers.httpx.Response(200, content=body))
                chunks: list[str] = []
                with mock.patch("providers.httpx.AsyncClient", return_value=client):
                    stream = providers._stream_openai(
                        "http://provider", "secret-key", "m",
                        [{"role": "user", "content": "hello"}], 10, 0.1,
                    )
                    with self.assertRaises(providers.ProviderError) as caught:
                        async for chunk in stream:
                            chunks.append(chunk)
                self.assertEqual(caught.exception.code, "empty_response")
                self.assertEqual(chunks, ["partial"] if body else [])


if __name__ == "__main__":
    unittest.main()
