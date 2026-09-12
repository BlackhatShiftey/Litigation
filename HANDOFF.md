# Handoff — Litigation (multi-LLM chatroom dashboard)

**Date:** 2026-08-18
**Project root:** `/home/terrabyte/Github-Repositories/Litigation`
**Status:** Working, self-tested, live-tested. One cleanup item pending (below).

## What this project is

Litigation is a local web dashboard where multiple LLMs (GPT, Claude, Grok,
Gemini, OpenRouter, HuggingFace, OpenCode Zen, DeepSeek, Groq, Mistral,
Together, Fireworks, Perplexity, Cerebras, SambaNova, Novita, Ollama, LM
Studio) share one chatroom with the user. The user can address one model,
several, or all; agents can talk to each other using a compact JSON "decoder
ring" envelope instead of prose; and a "court vote" feature tallies majority /
confidence / dissent verdicts.

**IMPORTANT:** This project is completely separate from the Seam repo
(`/home/terrabyte/Documents/Projects/Seam`). Do NOT touch Seam for this work.

## Architecture (all files in project root)

| File | Purpose |
|---|---|
| `server.py` | FastAPI app. Endpoints: `GET /api/models`, `GET/POST /api/rooms`, `GET/PATCH/DELETE /api/rooms/{id}`, `POST /api/rooms/{id}/send` (SSE stream: `start`/`delta`/`done`/`error`/`phase`/`ring`/`turn_done` events), `POST /api/rooms/{id}/vote`. Turn orchestration in `_run_turn()`: parallel fan-out to targeted agents via asyncio queue + sentinel, then optional cross-talk round. Serves `index.html` at `/`. |
| `providers.py` | Registry of 18 providers (`BASE_REGISTRY`). Three wire adapters: `_stream_openai` (OpenAI-compatible chat/completions), `_stream_anthropic` (Messages API), `_stream_responses` (OpenAI Responses API — used by OpenCode Zen, endpoint `https://opencode.ai/zen/v1/responses`, verified against their docs). Availability = env key set (local servers with `env_key: ""` are always "available"). Extra providers can be added at runtime via `LITIGATION_EXTRA_MODELS` env var (JSON array). |
| `ring.py` | Decoder ring: `make_envelope`/`encode`/`decode` (tolerant — strips code fences, extracts `{...}`), `estimate_tokens` (~len/4), `tally` (majority/counts/mean_confidence/unanimous/votes/dissent). |
| `db.py` | SQLite (WAL) persistence: `rooms` + `messages` tables, thread-locked. |
| `index.html` | Single-file dark courtroom UI, vanilla JS, no build step. Sidebar room list, new-room modal with per-model checkboxes + model override inputs, addressing chips above composer, ring toggle (`crosstalk-btn`), "⚖ Call vote" button, SSE reader in `send()`, verdict cards, ring message bubbles with raw-envelope viewer. |
| `run_selftest.py` | Unit self-tests for ring/db/providers. Run with `.venv/bin/python run_selftest.py` — all pass. |
| `.env.example` | All 16 key env vars + model override docs. NO real keys stored in the repo. |
| `.venv/` | Python venv with fastapi, uvicorn, httpx, python-dotenv. |

## How to run

```bash
cd /home/terrabyte/Github-Repositories/Litigation
source .venv/bin/activate
cp .env.example .env   # fill keys as desired (user has OPENAI + ANTHROPIC in ambient env)
python server.py       # http://127.0.0.1:8766
```

## Environment facts

- User's ambient environment already has `OPENAI_API_KEY` and
  `ANTHROPIC_API_KEY` set (providers gpt/claude show `available: true`
  without a `.env`). Grok/Gemini/everything else currently `False`.

## What was verified live

1. `import server` clean; `run_selftest.py` passes (ring decode/tally, db
   CRUD, 18-provider registry, api-type assertions).
2. Server boot, `GET /api/models`, room create, real SSE send to GPT
   (`"Reply with exactly: OK"` → streamed `delta` "OK" → `done` →
   `turn_done`), room delete. Test DB artifacts were removed afterward.

## PENDING ITEM (the one thing to do next)

**A stale server process is still running and must be killed:**

- PID **357619** — `.venv/bin/python server.py` from the earlier test session,
  still bound to `127.0.0.1:8766`. It is running the OLD 4-provider code
  (started before the 18-provider registry update). A second start attempt
  failed with "address already in use" (log: `/tmp/litigation.log`).
- Action: `kill 357619` (verify with `ss -tlnp | grep 8766`), then restart:
  `cd /home/terrabyte/Github-Repositories/Litigation && .venv/bin/python server.py`
- Also: a test room `97dbe6dfc68f` ("GPT + Claude", crosstalk on) was created
  against that old server and may still exist in `litigation.db` — delete it
  via the UI or `DELETE /api/rooms/97dbe6dfc68f`, or just remove
  `litigation.db*` if the user doesn't care about test data.

## Verification checklist for the next agent

After killing PID 357619 and restarting:
1. `curl -s http://127.0.0.1:8766/api/models | python3 -m json.tool` should
   list **18** providers (previously the stale process returned only 4 —
   that's how we detected it).
2. Create a room with gpt + claude (both have keys), send a message, confirm
   both stream replies. Toggle Ring on, send again, confirm `ring` SSE events
   arrive and ring messages render as dashed bubbles.
3. Call a vote; confirm verdict card renders with counts/majority.

## Design decisions / context

- "Cline", "Claude Code", "Codex CLI" are agent harnesses, not chat-completion
  APIs — they can't join as chat participants. Their underlying models join
  via provider keys. This is documented in README.
- OpenCode Zen uses the OpenAI **Responses API**, not chat/completions — hence
  the third adapter. Verified against https://opencode.ai/docs/zen/.
- HuggingFace router: `https://router.huggingface.co/v1`, key `HF_TOKEN`,
  model names like `deepseek-ai/DeepSeek-R1:fastest` (`:fastest`/`:cheapest`/
  `:preferred` suffixes). Verified against HF Inference Providers docs.
- Default models are reasonable current picks; every provider has a
  `<PREFIX>_MODEL` env override, and the new-room modal has per-model
  override text inputs.
- DB stores `raw` envelope JSON alongside decoded text for ring messages and
  vote tallies so history re-renders correctly.
- `HISTORY_TURNS = 24` context window per turn; cross-talk capped at 700
  max_tokens per agent.

## Suggested skills

- If continuing feature work in this conversation's style, invoke the
  `implement` or `prototype` skills for the next feature.
- Use `review-pr` / `code-review` skills before pushing to GitHub if the user
  asks to publish (repo is not yet git-initialized: no `.git` in the project
  dir).
- If the user wants more provider integrations or design changes, `grill-me`
  or `to-spec` can help pin down requirements first.

## Not yet done (ideas the user may want)

- `git init` + initial commit (user hasn't asked yet; folder lives in
  `~/Github-Repositories` which suggests they will).
- Grok/Gemini keys not yet provided by user — those agents show offline.
- Could add: model discovery via `GET /v1/models` per provider for a model
  picker dropdown; multi-round cross-talk with a turn budget.

