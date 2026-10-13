from __future__ import annotations

import io
import json
import unittest
from unittest import mock

import litigation_cli as cli


class _FakeResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sse(events: list[dict]) -> list[bytes]:
    return [f"data: {json.dumps(e)}\n\n".encode() for e in events]


class IterSseTests(unittest.TestCase):
    def test_parses_data_lines_and_skips_junk(self) -> None:
        lines = [
            b": keep-alive comment\n",
            b"data: " + json.dumps({"type": "start", "agent": "grok"}).encode() + b"\n",
            b"\n",
            b"data: not json\n",
            b"data: " + json.dumps({"type": "delta", "text": "hi"}).encode() + b"\n",
            "data: " + json.dumps({"type": "done"}) + "\n",  # str input also works
        ]
        events = list(cli.iter_sse(lines))
        self.assertEqual([e.get("type") for e in events],
                         ["start", "delta", "done"])

    def test_non_dict_payloads_are_dropped(self) -> None:
        events = list(cli.iter_sse([b"data: [1,2]\n", b"data: 42\n"]))
        self.assertEqual(events, [])


class StreamTurnTests(unittest.TestCase):
    def _run(self, events: list[dict]) -> tuple[int, str]:
        out = io.StringIO()
        response = _FakeResponse(_sse(events))
        with mock.patch.object(cli.urllib.request, "urlopen",
                               return_value=response):
            code = cli.stream_turn("http://x", "room1", {"text": "hi"}, out=out)
        return code, out.getvalue()

    def test_streamed_and_full_replies_render(self) -> None:
        code, text = self._run([
            {"type": "start", "agent": "gpt", "label": "GPT"},
            {"type": "delta", "agent": "gpt", "text": "token "},
            {"type": "delta", "agent": "gpt", "text": "stream"},
            {"type": "done", "agent": "gpt", "content": "token stream"},
            {"type": "start", "agent": "claude", "label": "Claude"},
            {"type": "done", "agent": "claude", "content": "full reply"},
            {"type": "turn_done"},
        ])
        self.assertEqual(code, 0)
        self.assertIn("▸ GPT", text)
        self.assertIn("token stream", text)
        self.assertIn("▸ Claude", text)
        self.assertIn("full reply", text)
        # streamed agents end with a newline; headers are separated
        self.assertIn("\n▸ Claude", text)

    def test_dispatch_notice_renders_as_status(self) -> None:
        code, text = self._run([
            {"type": "start", "agent": "grok", "label": "Grok"},
            {"type": "dispatch", "agent": "grok", "kind": "queued",
             "message": "Waiting for capacity on the selected model."},
            {"type": "done", "agent": "grok", "content": "answer"},
            {"type": "turn_done"},
        ])
        self.assertEqual(code, 0)
        self.assertIn("… Waiting for capacity", text)

    def test_ring_diff_phase_and_error_events(self) -> None:
        code, text = self._run([
            {"type": "phase", "phase": "crosstalk"},
            {"type": "ring", "agent": "grok", "label": "Grok",
             "decoded": "claim: cheaper", "envelope": {}, "tokens_saved": 4},
            {"type": "start", "agent": "gpt", "label": "GPT"},
            {"type": "diff", "agent": "gpt", "label": "GPT", "files": 2,
             "diff": "..."},
            {"type": "error", "agent": "cline", "error": "ProviderError: no login"},
            {"type": "turn_done"},
        ])
        self.assertEqual(code, 1)
        self.assertIn("— crosstalk —", text)
        self.assertIn("◇ ring Grok: claim: cheaper", text)
        self.assertIn("[diff] 2 file(s) changed", text)
        self.assertNotIn("ProviderError", text)  # errors go to stderr, not stdout

    def test_http_error_is_translated(self) -> None:
        exc = cli.urllib.error.HTTPError(
            "url", 400, "Bad Request", {}, io.BytesIO(  # type: ignore[arg-type]
                json.dumps({"detail": "a room needs at least one participant"}).encode()))
        with mock.patch.object(cli.urllib.request, "urlopen", side_effect=exc):
            with self.assertRaisesRegex(RuntimeError, "at least one participant"):
                cli.stream_turn("http://x", "room1", {"text": "hi"})

    def test_clean_eof_without_turn_done_is_interrupted(self) -> None:
        response = _FakeResponse(_sse([
            {"type": "start", "agent": "grok", "label": "Grok"},
            {"type": "delta", "agent": "grok", "text": "partial"},
        ]))
        with mock.patch.object(cli.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "before turn completion"):
                cli.stream_turn("http://x", "room1", {"text": "hi"}, out=io.StringIO())


class ParserTests(unittest.TestCase):
    def test_send_command_shape(self) -> None:
        args = cli.build_parser().parse_args(
            ["send", "room1", "hello there", "--target", "grok", "--mode", "code"])
        self.assertEqual(args.command, "send")
        self.assertEqual(args.room_id, "room1")
        self.assertEqual(args.text, "hello there")
        self.assertEqual(args.target, ["grok"])
        self.assertEqual(args.mode, "code")

    def test_new_room_requires_participants(self) -> None:
        args = cli.build_parser().parse_args(
            ["new-room", "--participant", "grok", "--participant", "claude",
             "--title", "Test", "--crosstalk"])
        self.assertEqual(args.participant, ["grok", "claude"])
        self.assertTrue(args.crosstalk)

    def test_request_uses_caller_timeout_for_long_provider_operations(self) -> None:
        with mock.patch.object(cli.urllib.request, "urlopen", return_value=_FakeResponse([])) as opened:
            cli.request("POST", "/api/models/claude/check", url="http://x", timeout=900)
        self.assertEqual(opened.call_args.kwargs["timeout"], 900)


if __name__ == "__main__":
    unittest.main()
