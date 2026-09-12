# Litigation ⚖️

A local dashboard where GPT, Claude, Grok, Gemini (and any OpenAI-compatible
model, including local Ollama) share one chatroom with you. Address one model,
a few, or all of them — then call a vote and let the whole room decide.

## Features

- **Multi-model rooms** — pick any combination of providers per chat room.
- **Selective addressing** — chips above the composer toggle which models hear
  your next message (empty selection = everyone).
- **Decoder ring ("Ring") cross-talk** — when enabled, after answering you the
  agents address *each other* using a compact JSON envelope
  (`{"v":1,"from":"gpt","kind":"claim","text":"..."}`) instead of prose.
  Cheaper than NLP, machine-parseable, and the UI can show the raw envelope.
- **Court votes** — "⚖ Call vote" asks every participant for a verdict
  envelope, then tallies majority, confidence, and dissents into a verdict card.
- **Streaming** — every reply streams token-by-token over SSE.
- **Persistence** — rooms and transcripts in a local SQLite DB (`litigation.db`).

## Setup

```bash
cd ~/Github-Repositories/Litigation
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your API keys
python server.py       # http://127.0.0.1:8766
```

## Keys

Put provider keys in `.env` (never committed). A provider without a key simply
shows as offline in the new-room dialog. Every provider has a `<PREFIX>_MODEL`
override in `.env`.

**Built-in providers** (18): GPT, Claude, Grok, Gemini · OpenRouter,
HuggingFace (Inference Providers router), OpenCode Zen · DeepSeek, Groq,
Mistral, Together, Fireworks, Perplexity, Cerebras, SambaNova, Novita ·
Ollama and LM Studio (local, no key needed — just leave the server running).

Not built in but trivially added: vLLM, llama.cpp server, LiteLLM proxy, and
any other OpenAI-compatible endpoint — see below. Enterprise gateways
(Azure OpenAI, Vertex AI, Bedrock) also speak OpenAI-compatible (or
Anthropic-compatible) wire formats; point an entry at your deployment.

> Note: "Cline", "Claude Code", and "Codex CLI" aren't chat-completion APIs —
> they're agent harnesses. Their underlying models can still join via the
> provider keys above.

### Adding any other endpoint (vLLM, llama.cpp, LiteLLM, ...)

Any OpenAI-compatible endpoint can be added without code via
`LITIGATION_EXTRA_MODELS` in `.env`:

```json
LITIGATION_EXTRA_MODELS=[{"id":"vllm","label":"vLLM","color":"#f59e0b","base_url":"http://localhost:8000/v1","default_model":"my-model","env_key":""}]
```

Omit `env_key` (or leave it empty) for a local server that needs no key; set
`"api":"responses"` for an endpoint that speaks the OpenAI Responses API
(like OpenCode Zen).

## Layout

| File | Purpose |
|---|---|
| `server.py` | FastAPI app: rooms, SSE turn orchestration, cross-talk, votes |
| `providers.py` | Provider registry + OpenAI/Anthropic streaming adapters |
| `ring.py` | Decoder ring: envelope encode/decode/tally |
| `db.py` | SQLite persistence |
| `index.html` | Single-file UI (no build step) |

## API sketch

```
GET  /api/models                     → available providers
GET/POST /api/rooms                  → list / create rooms
GET/PATCH/DELETE /api/rooms/{id}     → room + transcript
POST /api/rooms/{id}/send            → SSE stream (start/delta/done/ring/error)
POST /api/rooms/{id}/vote            → tallied verdicts
```
