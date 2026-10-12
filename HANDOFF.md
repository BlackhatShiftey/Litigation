# Handoff — Litigation subscription setup

**Date:** 2026-10-10
**Project root:** `/mnt/data/projects/Github-Repositories/Litigation`
**State:** Implemented in the working tree and running locally at
`http://127.0.0.1:8766`. All five subscription connection checks and a shared
chat turn passed on 2026-10-10. Changes are uncommitted.

## 2026-10-12 — Install-time auth choice: subscriptions vs API keys

Installers now choose how Litigation authenticates inference, and the choice
stays flippable during use. A fresh install redirects `/` to the new `/setup`
wizard (Canticle-themed): **Use my subscriptions**, **Use API keys**, or
**Decide later** (`mixed`). Every page's shared navigation gains a segmented
**Subscriptions · API keys · All** control injected by `shared-nav.js`; one
click persists the mode via `POST /api/setup/mode` with no restart, and an
open provider picker re-renders live. `litigation_cli.py setup --mode/--set/
--clear/--status` provides the headless path, and `install.sh` is the new
install entry point.

New implementation: `setup_store.py` (versioned preference JSON at
`~/.local/share/litigation/setup.json`, `surface: "litigation"`; key file at
`~/.config/litigation/keys.env`, `0600`, atomic writes), `setup_routes.py`
(`/setup` + `/api/setup*`, self-contained for SEAM absorption), and
`setup.html`/`setup.js`/`setup.css`. Key values arrive over loopback, apply to
the process environment immediately (overriding the repo `.env`), and are
never echoed back — responses carry set/unset booleans only. The GLM
subscription route collects `ZAI_API_KEY` inside the subscription card.

Mode semantics: **new rooms only**. `subscription` rejects API-billed
participants; `api` rejects subscription CLI routes; local servers and
non-subscription CLIs stay available; existing rooms and their sends are
never revisited. The picker classifies by registry `billing` metadata instead
of the hardcoded `SUBSCRIPTION_TARGETS` list (which missed the codex-CLI
`openai` route), and Test-connection buttons now follow the server's
checkable set. See `docs/SETUP.md`.

Verification: 24 new tests in `tests/test_setup_routes.py` (mode round-trip,
corrupt-preference visibility, 0600 key writes, value-free responses, unknown
key rejection, clear/delete, and the room enforcement matrix including a
mid-session flip). Full suite: 124 tests, 116 pass — the 8 failures are all
in `test_agent_execution.py` sandbox receipts and reproduce on a pristine
HEAD worktree, so they predate this change. Selftest, node syntax checks
(including extracted inline chat JS), `bash -n`, and `git diff --check`
passed. A live isolated instance (port 8767, temp state) verified the /setup
page, unset→subscription→api→mixed flows, enforcement messages, key storage
permissions, and the CLI. The user's real preference/keys paths remain
untouched, so the first-run wizard will appear on the next visit; the running
8766 server must be restarted to pick up the new code.

## Current user route

Run `./start.sh` and open <http://127.0.0.1:8766>. The launcher changes to the
repository directory and uses `.venv/bin/python`; it does not install global
packages, create credentials, or modify provider configuration.

The **New chat** dialog refreshes provider state when opened. Its first section
contains the five requested subscription participants:

| Participant | Transport | Billing/setup behavior |
|---|---|---|
| Grok | `grok` CLI | Uses the CLI's subscription login. |
| Gemini (Antigravity) | `agy` CLI | Uses the Google account signed into Antigravity. The UI hides the duplicate `antigravity` alias and presents `gemini`. |
| GLM | Claude CLI routed to Z.ai | Requires the Z.ai Coding Plan credential in `ZAI_API_KEY`; no API-billing fallback. |
| Cline | Isolated Cline CLI config with `cline-pass` fixed | Tool categories are disabled and provider/model fallback is rejected. |
| Claude | `claude` CLI | Uses the Claude subscription login. |

Installed or configured does not mean authenticated. Selectable CLI routes begin
as `installed_unverified`; the UI never labels them authenticated. Each requested
subscription row has **Test connection**, which calls
`POST /api/models/{id}/check` in non-coding mode. The request contains the
harmless `LITIGATION_OK` probe and succeeds only for that exact reply.

Check state is held only in `server.MODEL_CHECKS` as `verified` or `error` plus
a UTC timestamp and safe message. No credential, provider response, stderr, or
raw exception is retained or returned. Restarting the server clears it.

## API and interface facts

- `GET /api/models` returns a safe metadata whitelist: identity/display fields,
  `installed`, `configured`, `available`, `setup_status`, `auth_status`,
  `transport`, `billing`, setup guidance, and the ephemeral connection result.
- `POST /api/models/{id}/check` accepts `claude`, `grok`, `gemini`, `glm`, and
  `cline`; `antigravity` is retained as a compatible alias. Unknown IDs and
  non-subscription providers are rejected without invoking a provider.
- API and local providers remain in the second dialog section. API routes are
  marked **API billing** and local servers are marked **Local**.
- Room transcripts, ring cross-talk, voting, model overrides, and the existing
  coding workspace flow remain intact.
- Subscription chat adapters reject coding use when their registry metadata
  does not support it. In a coding room they can still receive the reviewer
  prompt without gaining file-editing flags.

## Current verification evidence

On 2026-10-10, direct isolated CLI smokes returned the expected sentinel for
Claude Pro, Grok OAuth, GLM Coding Plan, Cline Pass, and Gemini through
Antigravity. This establishes the underlying account/CLI routes at that time.
After restarting with the final adapters, all five
`POST /api/models/{id}/check` calls passed. The initial two-message echo probe
was rejected by some models; the final check uses a single natural user prompt
and still requires the exact sentinel response.

The saved **Subscription room** contains Grok, Gemini (Antigravity), GLM, Cline,
and Claude with ring cross-talk off. A normal greeting turn streamed replies
from all five, emitted `turn_done`, and reported no errors. Antigravity took
about 50 seconds in the final checks; the other providers completed sooner.
The existing nine rooms and twelve messages were preserved before adding the
starter room and its greeting exchange.

All 17 focused provider/API tests and the existing isolated self-test passed.
Desktop and mobile browser checks confirmed all five verified badges and no
horizontal overflow in the setup dialog. Runtime logs are retained under
`~/.local/state/litigation/`; Cline session logs are retained separately by its
adapter. The browser was opened to the running dashboard.

## Files in this change

| File | Purpose |
|---|---|
| `providers.py` | Subscription registry metadata and isolated CLI transports. |
| `server.py` | Safe model metadata, in-memory check state, and connection-check route. |
| `index.html` | Subscription-first provider picker, state labels, setup hints, and connection checks. |
| `start.sh` | Repository-local launcher. |
| `README.md` | User setup and subscription account routing. |
| `tests/test_providers.py` | Provider CLI isolation and registry checks. |
| `tests/test_server_models.py` | Isolated API checks with provider completion mocked. |

## Verification boundary

Unit and route tests mock provider completion and therefore establish API
behavior, exact-reply handling, and non-disclosure behavior only. A provider is
live for this server session only after its UI connection check reports
**Verified now**. Do not infer current authentication from installation,
registry metadata, earlier handoffs, or an old server process.

## Projects and local agent handoffs — 2026-10-10 continuation

The new **Projects** section is implemented and running at
<http://127.0.0.1:8766/projects>. It supports tracked issues and comments,
durable handoffs, linked follow-ups and retries, saved outputs, two-round model
debates, local exports, and reviewed code proposals. The CLI entry point is
`python3 litigation_cli.py --help`; details are in `docs/PROJECTS.md`.

Project records live separately from chat rooms, by default under
`~/.local/share/litigation/projects/`. SQLite UPDATE/DELETE triggers protect
append-only records; current state is projected from events. Context snapshots
include selected files and hashes, issue history, parent receipts, and linked
outputs. Restart recovery preserves completed work and marks interrupted calls
without silently replaying them. These are application-level preservation
rules, not protection against an administrator replacing the database.

Code handoffs use the existing subscription routes without unrestricted shell
execution. Agents return file proposals; the controller validates them and
commits them in retained `codex/<handoff-id>` worktrees. Main changes only after
explicit Apply, with clean-tree, expected-base, and fast-forward checks. Git
uses no remote, hooks, signing, external diff, text conversion, or network
transport. Import copies only supported committed local HEAD files and records
exclusions. Provider inference still uses the providers' online services.

### Live acceptance receipts

The saved **Local Agent Workshop** project is
`project_b1f2e00d3e1e4445aa1bd9e1d9acb2aa`:

- Claude code handoff `9e15e5f9a06841cd8b164154f092bae3` produced `greeting.py`
  and `README.md` as a retained proposal.
- GLM handoff `fd9047af58034ae9885a0d2c8704ce9e` reviewed the actual linked
  output and tracked issue and returned an approval with no executed-check claim.
- The browser's Apply action advanced local main to
  `a442a564c94146206a1ade55a5aa737aa7231de6`; the repo is clean and has zero
  remotes. The example code was statically reviewed; its tests were not run.
- Grok and Cline each completed an initial position and rebuttal in debate
  `debate_c168a927c4a15074ab7a014325b8c469`. A concurrent recovery pass appended
  two initial-queued receipts pointing to the same tasks; no duplicate model
  jobs were created, and the UI deduplicates those references.
- Gemini through Antigravity completed UI-created handoff
  `8e9c7bc5d2b7488e8b1a618239944323`.
- All seven handoffs, their outputs, the applied proposal, and the closed
  demonstration issue survived server restart. A 103,714-byte JSON export was
  verified at `~/.local/share/litigation/projects/workshop-export-20261010-084807.json`.
  Exports include history, snapshots, outputs, and diffs; the full repository
  working tree is not separately bundled.

### Qualification and owning files

The final Python suite passed **52 tests**, including immutable records and
REPLACE rejection, stable idempotent replay, context snapshots, cancellation,
restart recovery, debate peer failure, bounded export, secret-shaped export
rejection, path validation, literal Git pathspecs, hook suppression, proposal
isolation, and stale/dirty apply rejection. The existing self-test passed in
isolated room/project databases. JavaScript syntax and whitespace checks passed.
Live browser checks covered creating a handoff, opening a code proposal,
applying it, and opening a debate rebuttal's saved output. Desktop (1440px),
tablet (780px), and mobile (390px) layouts have no document-level horizontal
overflow. Final rendered screenshots are saved under
`/home/terrabyte/.codex/visualizations/2026/10/10/01a125ee-d442-7691-8e99-b9015fab8c4a/`
as `litigation-projects-desktop.png` and `litigation-projects-mobile.png`.

New implementation: `project_store.py`, `local_git.py`, `project_runtime.py`,
`project_routes.py`, `litigation_cli.py`, `projects.html`, `projects.css`, and
`projects.js`, with matching tests and `docs/PROJECTS.md`. Existing subscription
setup and unrelated working-tree material remain uncommitted and preserved.

## 2026-10-10 — Coding agents, shared rules, and Alpine SEAM

The Projects page now supports **Coding agent · tools + tests** (`mode=agent`).
All six subscription routes have completed live file edits and actual sandboxed
tests: Claude, OpenAI through ChatGPT-authenticated Codex, Grok, GLM, Gemini
through Antigravity, and Cline Pass. This supplements the earlier proposal-only
`mode=code` workflow. Use `docs/CODING_AGENTS.md` for the tool protocol and CLI.

Each task retains its own `codex/<handoff-id>` local branch and worktree.
Automatic peer handoffs inherit the parent's committed tip and preserve the
original integration base. The controller records file content/hash receipts,
real test subprocess results, rule snapshots, outputs, and delegation history.
Passing tests must match the current tree; subsequent edits invalidate them.
Explicit Apply validates the full cumulative diff before fast-forwarding main.
No GitHub service or Git remote is required.

### Live coding acceptance

The persistent **Coding Agent Workshop** is
`project_781460db04d14e7e864ddc304d7ebbf6`, with issue
`9bee54b481d54cfcbde3aa3dc614d8e3`.

| Route | Completed handoff | Actual test result |
| --- | --- | --- |
| Claude | `80ddcaf2be4242588c88b3e5928f4118` | Intentional failure, repair, then 1 passing test |
| OpenAI | `104543833bc24351a344d36fcd530b0d` | Automatic peer child; added negative-number coverage; 2 passing tests |
| Grok | `7112ff62eec14018994786dac95a97d3` | Added edge-case test file; 3 passing tests |
| GLM | `af826d709c5b438089ee58d00ae91eb5` | Added edge-case test file; 3 passing tests |
| Gemini / Antigravity | `45a637614a864ead92ad50bd93625d38` | Added edge-case test file; 3 passing tests |
| Cline Pass | `0e347ab3539b423dbc2c1c181597c7f4` | Added edge-case test file; 3 passing tests |

The browser's Apply action applied the OpenAI cumulative proposal
`b1ba33a3d72c418e95d31bf7c2b3fe3d`, advancing local main through Claude's
`91de06fb9acb98bf34cffccd5f60a40760b8cb1b` to OpenAI's
`d27394428d5a97f298099b96e69675b5db3931a4`. The demonstration main was clean
and had zero remotes after Apply. Other agents' proposals remain separate.

Original unsuccessful GLM, Cline, and Gemini attempts remain in history, linked
to their retries. GLM exposed malformed action JSON; the controller now records
a protocol-error hash/receipt and requests correction within its existing
round/transcript/time bounds. Cline confused controller action names with native
tools; its coding prompt now requests literal controller JSON while ordinary
chat stays free-form. Antigravity's 90-second inner timeout was insufficient;
the verified retry used 180 seconds internally and 240 seconds externally.

### Rules and knowledge

`/rules` indexes Global, Claude, OpenAI, GLM, Gemini, Cline, and Grok. Published
revisions and proposal decisions are append-only, with stale-revision checks.
Project handoffs capture immutable global/model rule snapshots. Ordinary chat,
cross-talk, and voting receive active rules; unknown providers receive Global.
Agents may propose improvements only to their own section and cannot activate
them. Claude's evidence-linked proposal `104f60fd0f4343f08bfbb784f4cf735a` is
pending review; active Global and Claude rules remain revision 1.

`/knowledge` connects over private SSH to `Ex0@100.126.138.102` (Tailscale name
Alpine; verified Alpine Linux 3.24.1). The dedicated installation is under
`~/.local/share/litigation-seam/` on that host. SEAM Suite `2.4.1rc1` was built
from a clean archive of committed source
`8530717d800d397ba58283710b6e8f0a037aabf8`; this is private plumbing, not a
release-qualified artifact. The existing dirty SEAM checkout was not changed.
Ingestion, project-scoped search, and knowledge-graph traversal work. Reasoning
graph workflows and semantic embeddings are explicitly **not configured**.

The bridge keeps an append-only local outbox and retry receipts before SSH
delivery, with immutable event provenance and project namespace checks.
Delivery is at least once; a remote crash between SEAM ingestion and receipt
creation can cause redelivery. See `docs/SEAM.md` for installation and recovery.

### Verification and limits

The final isolated suite passed **93/93 tests**, including malformed-turn
recovery, branch inheritance, crash reconciliation, rule concurrency and
immutability, sandbox limits/cancellation, current-tree test qualification,
SEAM provenance, and room rule coverage. Python compilation, JavaScript/shell
syntax, and `git diff --check` passed. Desktop and 390px mobile browser checks
verified Projects, Rules, and live Knowledge results without horizontal overflow.

Tests use fixed Python unittest/pytest and Node profiles with installed runtime
dependencies; arbitrary shell commands and dependency installation are not
provided. Test subprocesses have no network or host home. Git remains local;
subscription inference requires provider network access. Antigravity uses
plan+sandbox but its CLI cannot fully disable installed user plugins, as recorded
in its provider limitations. Append-only protections are application/database
invariants, not protection against an administrator replacing files.

The source changes remain in this working tree, uncommitted. Existing project
data, unrelated files, old attempts, logs, and retained worktrees are preserved.

### Final restart and export receipt

The pre-restart project database receipt was recovered unchanged: 2 projects,
2 issues, 16 handoffs, 16 outputs, 7 proposals, and 106 project events. All six
successful subscription coding handoffs remained `completed`, while the three
original GLM, Gemini, and Cline attempts remained `failed` in append-only
history. The workshop repository remained clean on local `main` at
`d27394428d5a97f298099b96e69675b5db3931a4` with no configured remotes.

Issue `9bee54b481d54cfcbde3aa3dc614d8e3` was closed by event 107 with the actual
six-route acceptance result. A complete JSON export was written through the API
to `~/.local/share/litigation/projects/coding-workshop-export-20261010.json` and
parsed successfully: 1 issue, 9 handoffs, 9 outputs, 6 proposals, and 74
project-scoped events, with the demonstration issue closed and both completed
and failed handoff history retained.

SEAM synchronized the closure event and reported 39 delivered outbox entries,
zero pending, and zero rejected. Project-scoped search and graph requests both
returned namespace
`litigation.project.project_781460db04d14e7e864ddc304d7ebbf6` with live
results. Reasoning graph workflows and semantic embeddings remain not
configured.

At the final read-only process check, the healthy loopback service on port 8766
was owned by PID 1896079. `/tmp/litigation-projects-server.json` still recorded
PID 1898220, but that process no longer existed, so the metadata was stale. The
server was not restarted during this closeout.

After the live PID was independently confirmed, the temporary server metadata
was atomically corrected to PID 1896079 with log path `/tmp/litigation.log`.

### Shared agent room and navigation acceptance

Projects now includes an **Agent room** backed by the append-only work ledger.
It shows queued, claimed, running, completed, and blocked work with its owner,
owned paths, linked handoff, active agent progress, and an explicit local
provenance check. A bounded work form creates queue items, while **Pull work**
selects a subscription agent and optional model before the runtime enforces the
claim and isolated handoff.

The live shared-ledger item `4c4b9d9f1f514855841f1b509e882fa0` was claimed
by Grok through handoff `4f6cb51940a04dc98f64435471e849f4`. Grok listed
work, read the inherited files, ran two passing tests, and finished without
editing files. The work item reached `completed`. Replaying the same pull
request returned the same handoff, while a competing OpenAI pull received 409.
The Agent room provenance check reported SQLite `ok` with append-only
relationships intact.

Chat, Projects, Rules, and Knowledge now use the same top navigation, active
page marker, and **New chat** action. The New chat action was exercised from
Knowledge and opened the existing chat-room dialog. Each page was checked at
1440px and 390px with the correct active marker and no document-level
horizontal overflow. Agent room captures are
`litigation-agent-room-desktop.png` and `litigation-agent-room-mobile.png` in
the session visualization directory.

The integrated suite passed 100 tests after the work-ledger changes; the
export adjustment and its 10 focused tests also passed before this UI closeout.
JavaScript syntax, parsed navigation structure, static asset routes, and
`git diff --check` passed after the final UI changes.

### Final integrated verification

The complete isolated suite was rerun after the export and final UI changes:
**100 tests passed**, zero failures or errors. The final source was reloaded;
the loopback service is healthy and the shared work claim remains completed.
The updated export at
`~/.local/share/litigation/projects/coding-workshop-ledger-export-20261010.json`
is 382,112 bytes and contains 10 handoffs plus the complete shared-work intent
and claim history. The original export was retained.

Global rules revision 2 was published with explicit ledger inspection,
exclusive shared-work claims, path ownership, and recovery requirements.
Previous handoffs retain their original revision-1 snapshots. The live local
provenance report has no failed checks; it explicitly marks proposal ancestry
as unverified by that report (ancestry remains enforced when proposals are
created/applied). Local relation/hash checks do not establish cryptographic
provider identity or protect against an administrator rewriting storage.

## 2026-10-10 — Next priority: remote-worker cluster

The next implementation priority is the planned remote-worker cluster described
in `docs/CLUSTER_PLAN.md`. It is not implemented or live. Begin with Phase 0:
qualify fixed Vite production-build, Vitest, and Playwright profiles in a clean
fixture representative of Frans without editing Frans, then test lease/fencing
under replay, partition, revocation, cancellation, and restart conditions.

The coordinator must remain the sole ledger authority and SQLite writer. Extend
the existing claim and dispatcher path; do not create a second queue. Workers
use revocable scoped identities, exact Git bases, per-attempt worktrees, bounded
hashed artifacts, and stale-result fencing. Provider credentials remain local
to the invoking worker, with verified billing and no fallback. The existing
review-and-Apply boundary remains authoritative.

`docs/LICENSING_PROPOSAL.md` recommends BUSL 1.1 with self-hosted
personal/research/internal-business use, commercial licensing for third-party
hosted or managed Litigation service, and Apache-2.0 conversion after four
years per version. This is a pending policy proposal only. No license grant was
added; the owner must choose between that restriction and permissive MIT and
approve the exact license text after ownership and compatibility review.


## 2026-10-10 — Canticle dashboard theme, local qualification

Applied the local Canticle Cosmic UI Kit and company lockup across Chat,
Projects, Rules, and Knowledge. The shared navigation retains all section
links and New chat, with Tokyo Night surfaces, pink/cyan accents, Outfit
reading text, mono utility controls, visible focus and reduced-motion support.
Kit source hashes and fixed asset routes are recorded in docs/CANTICLE_THEME.md.
No provider, ledger, rule authority, or task-execution behavior changed.

The local server was gracefully restarted and serves the theme at port 8766.
All four pages were rendered at 1440px desktop and 390px mobile with loaded
local assets, correct active navigation, no JavaScript exceptions and no
page-level horizontal overflow. Existing horizontally scrollable policy strips
remain scrollable. New-chat provider selection, rule revision, handoff dialogs,
and Agent room were checked; the mobile provider grid was corrected. Emulated
reduced motion reduced control transitions to 0.001ms; visible focus was checked
in dialogs. Three project API tests and Python compilation passed in .venv;
git diff --check passed. The existing Starlette/httpx deprecation warning remains.

Rendered previews are retained under
/home/terrabyte/.codex/visualizations/2026/10/10/01a125ee-d442-7691-8e99-b9015fab8c4a/
with litigation-agentroom-canticle.png and litigation-agentroom-canticle-mobile.png
plus Knowledge and provider-picker previews. Theme changes are uncommitted and
not pushed. Prior cluster/licensing proposal edits and .blender-toolkit remain
preserved; no effective license was added.
