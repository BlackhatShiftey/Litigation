# Litigation ⚖️

A local dashboard where subscription-backed model CLIs, API providers, and
local models share one chatroom with you. Address one model, a few, or all of
them, then call a vote and let the whole room decide.

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
- **Streaming** — API providers stream token-by-token over SSE. Subscription
  CLI replies appear when their isolated command finishes.
- **Persistence** — rooms and transcripts in a local SQLite DB (`litigation.db`).
- **Durable projects** — share committed local files and tracked issues with
  subscription agents, preserve append-only handoffs and outputs, run bounded
  debates, and review isolated local Git proposals before applying them.

## Setup

```bash
./install.sh     # creates .venv, installs requirements
./start.sh
```

Open <http://127.0.0.1:8766>. On a fresh install you're taken to `/setup` to
choose how Litigation authenticates inference — **your subscriptions** (each
provider's locally signed-in CLI) or **API keys** (pay-as-you-go, stored in a
local `0600` file and applied without a restart). The choice is flippable any
time from the segmented control in the top navigation; new rooms follow the
current mode while existing rooms keep the routes they were created with.
Headless installs can use `python3 litigation_cli.py setup --mode …` and
`--set KEY`. Details: [docs/SETUP.md](docs/SETUP.md).

Click **New chat** and use **Test connection** beside each subscription
provider. A CLI being installed is shown as **Installed · not verified** until
that live check succeeds. Connection results stay only in server memory and
reset when the server restarts.

Open <http://127.0.0.1:8766/projects> for persistent project work. Projects use
a separate local database and managed Git repositories, so closing the browser
does not stop queued work. See [docs/PROJECTS.md](docs/PROJECTS.md) for the
workflow, local CLI, storage layout, and safety boundaries, and
[docs/CODING_AGENTS.md](docs/CODING_AGENTS.md) for isolated coding, test receipts,
peer lineage, model rules, and optional SEAM knowledge tools.
Review shared and model-specific rules at <http://127.0.0.1:8766/rules> and the
SEAM connection and project knowledge status at <http://127.0.0.1:8766/seam>.
See [docs/RULES.md](docs/RULES.md) and [docs/SEAM.md](docs/SEAM.md) for their
append-only history, approval, deployment, and synchronization details.

## Next: remote-worker cluster

Remote workers are the next planned project phase; they are **not shipped**.
The [cluster plan](docs/CLUSTER_PLAN.md) specifies a central authoritative
coordinator, leased and fenced work over private Tailscale, exact-base Git
worktrees, bounded evidence, and phased acceptance tests. The repository's
[source-available licensing proposal](docs/LICENSING_PROPOSAL.md) is also
pending an owner decision and does not grant license rights.

## Terminal CLI

`install.sh` puts a `litigation` command on your PATH (a symlink to
`litigation_cli.py`, pure stdlib) that plugs into the terminal of the device
the server runs on — and, with `LITIGATION_URL` set, into any terminal that
can reach the server (for example Termux over Tailscale):

```bash
litigation rooms                         # list chat rooms
litigation room <id> --limit 20          # room + recent transcript
litigation new-room -p grok -p claude --crosstalk
litigation send <id> "address the court" # streams replies live, token by token
litigation send <id> "review this" --target grok --mode code
litigation vote <id> "ship or hold?"
litigation models                        # provider availability + billing
litigation check claude                  # subscription connection check
litigation setup --mode subscription     # headless install-time choice
```

The same command also covers the durable Projects workflow (handoffs, debates,
proposals, shared work) — see `litigation --help`.

## Subscription accounts

The subscription group in the new-room dialog uses the accounts already
authenticated by each provider's local CLI. It does not ask for pay-as-you-go
provider keys.

| Participant | Local route | Setup |
|---|---|---|
| OpenAI | Codex CLI using a ChatGPT subscription | Run `codex login`, confirm `codex login status` reports **Logged in using ChatGPT**, then test the connection. Litigation disables the CLI's native shell, plugins, rules, and web search for this route. |
| Grok | Grok CLI | Run `grok login --oauth`, complete browser sign-in, then test the connection. |
| Gemini | Antigravity `agy` CLI | Run `agy` and complete its Google account sign-in if prompted. Litigation labels this participant **Gemini (Antigravity)**. |
| GLM | Claude CLI routed to Z.ai | Use the credential issued by the Z.ai Coding Plan as `ZAI_API_KEY`. This route is labeled subscription billing and does not fall back to a pay-as-you-go provider. |
| Cline | Isolated Cline CLI config with `cline-pass` fixed | Run `cline auth cline-pass` if the login has expired. Litigation disables installed tool categories and prevents a provider/model fallback. |
| Claude | Claude CLI | Run `claude auth login`, choose the Claude subscription account, then test the connection. |

The check sends only `LITIGATION_OK` in non-coding mode and succeeds only when
the provider returns that exact text. Installed, configured, and authenticated
are deliberately separate states.

## API and local providers

The second group keeps the existing API and local options. API-backed providers
are clearly marked **API billing**; they use their documented environment
variable from `.env`. Local Ollama and LM Studio entries are marked **Local**.
Every provider still supports its `<PREFIX>_MODEL` override.

**Built-in providers** include the six subscription routes above plus GPT,
OpenRouter, HuggingFace (Inference Providers router), OpenCode Zen, DeepSeek, Groq,
Mistral, Together, Fireworks, Perplexity, Cerebras, SambaNova, Novita ·
Ollama and LM Studio (local, no key needed — just leave the server running).

Not built in but trivially added: vLLM, llama.cpp server, LiteLLM proxy, and
any other OpenAI-compatible endpoint — see below. Enterprise gateways
(Azure OpenAI, Vertex AI, Bedrock) also speak OpenAI-compatible (or
Anthropic-compatible) wire formats; point an entry at your deployment.

OpenAI, Claude, Grok, Gemini through Antigravity, GLM, and Cline are integrated
through their local command-line harnesses for subscription-backed inference.
All six can use Projects **Coding agent** mode: Litigation owns the bounded file
tools, networkless test sandbox, isolated Git branches, receipts, and peer
handoffs while each provider supplies model inference. Legacy provider-native
coding requests remain separate and chat-only subscription routes reject those
requests instead of receiving shell access or silently switching billing routes.

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
| `project_store.py` | Append-only project, issue, handoff, output, and event persistence |
| `local_git.py` | Managed local repositories and isolated proposal branches |
| `project_runtime.py` | Persistent background dispatcher and debate orchestration |
| `project_routes.py` | Projects REST API and UI routes |
| `litigation_cli.py` | Local handoff, status, output, debate, and proposal client |

## API sketch

```
GET  /api/models                     → available providers
POST /api/models/{id}/check          → safe subscription connection check
GET/POST /api/rooms                  → list / create rooms
GET/PATCH/DELETE /api/rooms/{id}     → room + transcript
POST /api/rooms/{id}/send            → SSE stream (start/delta/done/ring/error)
POST /api/rooms/{id}/vote            → tallied verdicts
GET/POST /api/projects               → list / create managed projects
GET/POST /api/projects/{id}/handoffs → list / queue durable agent work
GET      /api/handoffs/{id}/outputs  → persisted agent outputs
POST     /api/projects/{id}/debates  → one initial + one rebuttal round
POST     /api/proposals/{id}/apply   → guarded local fast-forward apply
```
