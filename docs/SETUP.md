# Litigation setup surface — subscription vs API keys

Litigation asks **at install time** how it should authenticate inference —
subscription logins or pay-as-you-go API keys — and keeps the choice flippable
**during use**. The whole surface lives behind `/setup` and `/api/setup` and is
deliberately self-contained so it can travel with Litigation when the project
is absorbed as a surface into SEAM.

## First run and the runtime flip

- A fresh install has no recorded choice. Opening `/` redirects to `/setup`,
  which offers **Use my subscriptions**, **Use API keys**, and **Decide later**
  (which persists `mixed` and shows every provider).
- Every page's shared navigation carries a segmented control —
  **Subscriptions · API keys · All** — that flips the mode with one click via
  `POST /api/setup/mode`. No restart is needed; an open provider picker
  re-renders immediately.
- Headless installs use the CLI against the same endpoints:

```bash
python3 litigation_cli.py setup --mode subscription
python3 litigation_cli.py setup --set ZAI_API_KEY     # hidden prompt
python3 litigation_cli.py setup --clear OPENAI_API_KEY
python3 litigation_cli.py setup --status
```

## Mode semantics

- The mode governs **new rooms only**. Existing rooms keep the participants
  and billing routes they were created with; a flip never silently changes
  billing mid-conversation.
- `subscription` — new rooms may use subscription CLI routes, local servers,
  and non-subscription CLIs; API-billed providers are rejected with a message
  pointing at the flip control.
- `api` — new rooms may use API-billed providers, local servers, and
  non-subscription CLIs; subscription CLI routes are rejected the same way.
- `mixed` / unset-choice-recorded — everything is offered (today's behavior
  minus the redirect).
- Local servers (Ollama, LM Studio) never need a key and stay available under
  every mode.
- GLM's subscription route authenticates with the `ZAI_API_KEY` credential
  rather than a CLI login, so the subscription card collects it explicitly.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/setup` | Auth mode, per-key set/unset booleans, subscription credential state |
| `POST /api/setup/mode` | Persist `subscription`, `api`, or `mixed` |
| `POST /api/setup/keys` | Store/clear keys (`{"keys": {"OPENAI_API_KEY": "…"}}`; empty value clears) |
| `DELETE /api/setup/keys/{ENV}` | Remove one stored key |

Key names are validated against the provider registry (`env_key` entries plus
subscription `required_env` credentials like `ZAI_API_KEY`); unknown names are
rejected without touching the store.

## Storage, precedence, and security

| Store | Default path | Override |
| --- | --- | --- |
| Preference JSON (versioned, `surface: "litigation"`) | `~/.local/share/litigation/setup.json` | `LITIGATION_SETUP_PATH` |
| Key file (`KEY=VALUE`, `0600`, atomic writes) | `~/.config/litigation/keys.env` | `LITIGATION_KEYS_PATH` |

- The key file loads after the repository `.env` and overrides it, so the
  setup surface stays authoritative once the user has chosen it. Values are
  single-line only; a value can never forge another entry.
- Key values arrive over the loopback-only server (equivalent trust to
  hand-editing `.env`), are applied to the process environment immediately,
  and are **never echoed back** — API responses carry set/unset booleans only.
- An unreadable preference file is reported as a visible `store_error` in
  `GET /api/setup`; choosing a mode replaces it. It never crashes the server.

## SEAM absorption design notes

- `setup_store.py` has no chat, room, or database coupling; the preference
  record is versioned and tagged with `surface: "litigation"` so a future
  host can migrate or rename it deliberately.
- `setup_routes.py` owns the page, static assets, and API behind `/setup` and
  `/api/setup` only. Re-hosting the surface means re-mounting one router.
- The navigation flip control is injected by `shared-nav.js`, which every
  surface already loads.
- The only enforcement point is new-room creation in the server; rooms,
  transcripts, sends, projects, and rules are untouched by mode changes.
