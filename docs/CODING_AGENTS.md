# Litigation coding agents

Coding agent mode lets a subscription-backed model work through a controller rather than giving the provider process direct access to the host. Choose **Coding agent · tools + tests** in Projects or pass `--mode agent` to `litigation_cli.py`.

The controller exposes bounded actions for listing and reading project files, writing UTF-8 files, running fixed test profiles, searching an attached SEAM project namespace, proposing an improvement to the model's own rules, handing one follow-up to a peer, and finishing. Each provider adapter applies the restrictions supported by its CLI. OpenAI, Claude, Grok, GLM, and Cline routes disable or withhold their native tool surfaces; Antigravity runs in plan and sandbox mode, but its CLI cannot fully disable installed user plugins. Project writes and tests still pass only through Litigation's bounded controller, and every accepted action and result is appended to the project ledger.

## Branch and test lifecycle

Each task gets `codex/<handoff-id>` in a managed local Git worktree. A root task starts from current `main`. A peer task starts from its parent's committed tip while retaining the original `main` commit as its integration base. Delegation is limited to one child per task and depth two.

Tests use Bubblewrap with the project mounted read-only, a private empty home, no network namespace, bounded processes, memory, time, file descriptors, and output. Available profiles are:

- `python-unittest`: `python3 -B -m unittest discover -s tests -t .`
- `python-pytest`: `python3 -B -m pytest -q`
- `node-test`: `node --test`

A profile relies on its runtime and test dependency already being installed on the Litigation host. Coding agents are not given an arbitrary shell and cannot install packages; an unavailable runtime or dependency is recorded as a failed or unavailable test receipt.

A test run with zero discovered tests does not qualify. The agent can finish only when a test passed against the exact current tree fingerprint. A later write invalidates that result and requires another test run.

On finish, the controller commits only controller-written paths and validates the entire cumulative diff from the integration base. Deletes, renames, symlinks, executable files, binary content, secret-shaped paths or content, and oversized changes are rejected. `main` changes only through the existing explicit Apply action, which checks ancestry, current `main`, repository cleanliness, the complete diff, and fast-forward status.

## Durable receipts and handoffs

The output records the rule snapshot, integration and working commits, model-turn hashes, accepted write content and hashes, test command and tree fingerprint, exit status, bounded output, truncation or timeout state, peer requests, and rule-improvement proposals. Parent completion and peer creation form a durable outbox: startup reconstructs a missing child exactly once after an interruption. Cancelled parents do not dispatch peers.

Example:

```bash
./litigation_cli.py handoff PROJECT_ID claude \
  "Repair the failing parser and add regression coverage" \
  --request-id parser-repair-001 --mode agent \
  --acceptance "The regression test fails before the repair and passes afterward"

./litigation_cli.py status HANDOFF_ID --wait
./litigation_cli.py outputs HANDOFF_ID
./litigation_cli.py events PROJECT_ID --limit 500
```

## Rules and SEAM

The exact active global and model-specific rules are captured when a task is queued. A model may submit a reviewable proposal only for its own rules section; it cannot publish changes or alter global rules.

When the Alpine SEAM bridge is available, the controller offers bounded `knowledge_search` and `knowledge_graph` actions scoped to the current Litigation project ID. A bridge failure is recorded as an unavailable tool result and does not silently invent knowledge. Agent execution remains usable while the remote SEAM instance is offline.

Managed repositories, branches, commits, diffs, and execution receipts stay local. Subscription-backed model inference still uses each provider's network service and account.

Shared work coordination, ownership, recovery, and local provenance checks are described in [WORK_LEDGER.md](WORK_LEDGER.md).
