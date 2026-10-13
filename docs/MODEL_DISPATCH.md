# Model dispatch and large-request recovery

Litigation routes every provider completion through one in-process dispatcher.
Chat replies, decoder-ring cross-talk, votes, connection checks, and Projects
therefore use the same capacity rules. The dispatch key is the canonical
provider id plus the exact selected model. Aliases such as `astra`, `codex`,
and `gpt` share the OpenAI lane when they resolve to the same model; different
providers or models run independently.

This is worker-slot scheduling around each provider route. It does not create
remote replicas or claim that a provider has multiple backend servers.

## Admission and deadlines

Each event loop owns its lanes. A lane defaults to one active request, eight
pending requests, a 45-second queue wait, and a 720-second overall wall-clock
deadline. The deadline belongs to a producer task, so SSE heartbeat lines and
a slow or disconnected client cannot extend it. Cancelling or closing a stream
cancels and awaits its provider iterator and process group before releasing the
slot. Inactive lanes are bounded and evicted; active and pending lanes are not.

Global settings:

| Variable | Default | Valid range |
|---|---:|---:|
| `LITIGATION_DISPATCH_CONCURRENCY` | `1` | 1–32 |
| `LITIGATION_DISPATCH_MAX_PENDING` | `8` | 0–256 |
| `LITIGATION_DISPATCH_QUEUE_WAIT` | `45` seconds | 0.05–600 |
| `LITIGATION_DISPATCH_OVERALL_TIMEOUT` | `720` seconds | 1–890 |

`LITIGATION_DISPATCH_MODEL_OVERRIDES` accepts a JSON object keyed by
`provider:model`, for example:

```json
{"openai:gpt-6.1-sol":{"capacity":2,"max_pending":12,"queue_wait":60}}
```

Malformed, non-finite, boolean, or out-of-range values do not bypass the
bounds. `GET /api/dispatch` reports only provider/model names, configured
capacity, active and pending counts, timing bounds, and bounded counters. It
never includes prompts, output, commands, environment values, credentials, or
provider URLs.

## Retry rules

A normal request has at most two attempts. A timeout, transient transport
failure, rate limit, or empty response may retry once on the same exact model
and billing route only when no response delta has reached the caller. Auth,
unknown-model, policy, and ordinary request-rejection failures do not retry.
Native `coding=True` calls never replay. Litigation controller transcripts may
retry before output because the child provider has no native tools, but their
content is never trimmed or summarized.

HTTP streams must contain a protocol terminal event. A clean connection close
without that event fails even when the provider emitted partial text. Error
bodies are read only into a bounded private classifier and are never returned
to the UI or status endpoint.

## Large CLI prompts

Argument-vector prompts switch transport at 49,152 transformed UTF-8 bytes by
default (`LITIGATION_CLI_ARGV_THRESHOLD_BYTES`):

- Grok writes the complete transformed prompt to a retained `0600` file and
  passes `--prompt-file`. Set `LITIGATION_PROVIDER_STATE_ROOT` to place these
  retained prompt records inside an isolated server state directory.
- Cline writes its complete escaped, tool-disabled prompt to a retained `0600`
  file, opens that file as stdin, and supplies a short constant positional
  primer required by the installed CLI's JSON-mode bootstrap. The full prompt
  never enters argv. Litigation closes its file descriptor on success, error,
  timeout, or cancellation and retains the file as a private prompt record.
- Gemini through Antigravity sends one compact NDJSON record through stdin:
  `{"event":"user","message":{"role":"user","content":"..."}}`. It
  ignores intermediate `init` and `step_update` records and accepts text only
  from a terminal `event: "result"` whose nested result has
  `status: "SUCCESS"` and a non-empty `response`.

Claude, GLM, and OpenAI/Codex already use stdin. If the operating system still
returns `E2BIG`, Litigation selects the provider's verified alternate transport
once. It never removes prompt text to work around an argv limit.

Installed-CLI probes established the Cline file-backed stdin/primer contract
and Antigravity's nested stream-JSON contract before these encoders and parsers
were added. Automated fake-CLI tests verify exact 60 KiB and multibyte delivery,
private-file permissions and retention, descriptor cleanup, wire-byte
accounting, and malformed or unsuccessful terminal rejection. No live provider
request is part of the automated suite; application-level live acceptance is
recorded separately in `HANDOFF.md`. Normal turns passed on all six
subscription providers. Exact 61,440-byte ASCII and Unicode messages passed
through Grok, Cline, and Gemini with intact persistence and completed SSE
streams; the live same-model queue also respected its worker limit.

## Provider payload rejection

Operating-system transport recovery and provider payload recovery are
separate. When a provider rejects the serialized request size, Litigation
first removes only the oldest conversational history in a private request
copy. Every original system instruction and the latest user message remain,
and chat receives a visible `dispatch` notice. The SQLite transcript is not
modified.

If the protected system instructions plus latest user message still cannot fit,
ordinary chat may use the same admitted provider/model lane to summarize at
most eight UTF-8-safe chunks and synthesize a final response. Chunk boundaries
are fitted against the actual transformed/serialized request size, and every
original character appears in exactly one chunk. The UI keeps a visible notice
that the final answer uses summaries. Coding requests and Litigation controller
transcripts never use history removal or summarization; they fail with an
actionable `payload_too_large` error instead.

`LITIGATION_PROVIDER_PAYLOAD_BYTES` sets a conservative preflight ceiling
(default 262,144 serialized bytes, bounded to 8 KiB–16 MiB). A real provider
rejection derives a smaller recovery budget from the rejected request, so a
60 KiB rejection cannot resend the same 60 KiB request as one “chunk.”

## Client behavior

Chat SSE uses `type: "dispatch"` for queued, retry, transport, and context
recovery status. The browser and terminal render these as status, not assistant
content or provider errors. Context-recovery notices remain visible after the
browser refreshes the persisted transcript. If the SSE connection closes
without `turn_done`, clients report an interrupted/unknown delivery state and
do not repost automatically; the browser reloads the room when possible and
restores the draft only if the user has not typed newer input. The notice keeps
unknown delivery explicit so the transcript can be checked before retrying.
