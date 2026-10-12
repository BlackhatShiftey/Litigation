# Litigation provider settings — subscriptions, API keys, local models

Litigation has **no global billing mode**. Subscription models, API-key
models, and local models are three provider families you configure once in
the settings page and then **select per conversation** in the New-chat
dialog — a room can mix all three. There is deliberately no mode control in
the top navigation.

## The settings page (`/setup`)

Open it via the ⚙ gear in the top bar (or `http://127.0.0.1:8766/setup`).
It has one section per family:

- **Subscription models** — each account-backed CLI with live status, its
  login hint, a **Test connection** button, and the GLM · Z.ai Coding Plan
  credential field (`ZAI_API_KEY`), which that route needs even though it is
  subscription-billed.
- **API keys · cloud models** — one password field per pay-as-you-go key.
  Values are written to a local `0600` file and applied to the running
  server immediately; they are never echoed back, only shown as set ✓.
- **Local models** — its own section for Ollama and LM Studio (and any
  keyless `LITIGATION_EXTRA_MODELS` endpoints): live running/not-running
  probe of the local port, the default model, and a persisted model
  override per server.

A fresh install redirects `/` here exactly once. **Finish setup** (or any
save action) records that settings were visited and stops the redirect.

## Per-conversation selection

The New-chat dialog lists three groups — **Subscription models**,
**API models**, and **Local models** — and everything is always offered.
Check any combination; the billing family is a property of each participant
you pick, not of the room or the app. Existing rooms keep the participants
they were created with. This replaces the earlier global auth-mode control.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/setup` | Family inventory: subscription ids, per-key set/unset booleans, credential state, local servers with reachability and model overrides |
| `POST /api/setup/keys` | Store/clear keys (`{"keys": {"OPENAI_API_KEY": "…"}}`; empty value clears) |
| `DELETE /api/setup/keys/{ENV}` | Remove one stored key |
| `POST /api/setup/local/{id}` | Set a local server's model override (`{"model": "llama3.1"}`; empty clears) |
| `POST /api/setup/dismiss` | Mark settings visited (stops the one-time redirect) |

Key names are validated against the provider registry (`env_key` entries
plus subscription `required_env` credentials like `ZAI_API_KEY`); unknown
names are rejected without touching the store.

## Storage, precedence, and security

| Store | Default path | Override |
| --- | --- | --- |
| Settings JSON (versioned, `surface: "litigation"`, model overrides, first-run marker) | `~/.local/share/litigation/setup.json` | `LITIGATION_SETUP_PATH` |
| Key file (`KEY=VALUE`, `0600`, atomic writes) | `~/.config/litigation/keys.env` | `LITIGATION_KEYS_PATH` |

- Keys and model overrides load after the repository `.env` and override it,
  so the settings surface stays authoritative once used. Key values are
  single-line only; a value can never forge another entry.
- Key values arrive over the loopback-only server (equivalent trust to
  hand-editing `.env`), apply to the process environment immediately, and
  are **never echoed back** — responses carry set/unset booleans only.
- An unreadable settings file never blocks boot; `GET /api/setup` reports it
  as a visible `store_error`, and the next save replaces it.

## Headless configuration

```bash
litigation setup --set OPENAI_API_KEY      # hidden prompt
litigation setup --local-model ollama=llama3.1
litigation setup --clear OPENAI_API_KEY
litigation setup --done                    # stop the first-run redirect
litigation setup --status
```

## SEAM absorption design notes

- `setup_store.py` has no chat, room, or database coupling; the settings
  record is versioned and tagged with `surface: "litigation"` so a future
  host can migrate it deliberately.
- `setup_routes.py` owns the page, static assets, and API behind `/setup`
  and `/api/setup` only. Re-hosting the surface means re-mounting one router.
- The only chat-side integration is the New-chat dialog's grouping and the
  one-time first-run redirect; rooms, transcripts, sends, projects, and
  rules are untouched by settings changes.
