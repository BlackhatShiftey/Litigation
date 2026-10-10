# Local projects and agent handoffs

Litigation Projects is a local workspace for sharing project context with the
five subscription routes, keeping their responses, and reviewing code changes
before they reach a project branch. It uses no GitHub service and performs no
Git network operations.

## What is durable

Projects, issues, issue comments, handoffs, captured context, outputs,
proposals, debates, and status changes are stored in a separate SQLite database.
The underlying records and event history are append-only. A retry or follow-up
creates a new linked handoff; it never replaces the earlier request or output.

The server owns the background dispatcher. Closing a browser does not stop a
handoff. On restart, queued work can continue, work that had already started is
marked `interrupted`, and interrupted provider calls are never replayed
automatically.

The default local state is:

```text
~/.local/share/litigation/projects/
  projects.sqlite3
  runtime.lock
  <project-id>/
    repo/
    worktrees/<handoff-id>/
```

Set `LITIGATION_PROJECTS_DIR` to change the root and
`LITIGATION_PROJECTS_DB` to place the project database elsewhere. The room chat
database remains independent.

## Agent work

Each handoff targets one of `grok`, `gemini`, `glm`, `cline`, or `claude` and
uses that provider's configured subscription CLI. Litigation does not fall back
to a paid API. At most two provider jobs run at once, and one job per provider
runs at a time.

The runtime captures the selected committed files with hashes, linked issue
state and history, parent handoff, and source outputs before queuing the job. It
rejects context above its explicit limit instead of silently dropping material.
Provider subprocesses receive this immutable snapshot. They cannot run shell
commands or edit the managed repository.

When a project is created from an existing local Git checkout, Litigation
imports only the committed `HEAD`. The project detail includes a durable import
report with included and excluded counts and a reason for every exclusion;
working-tree changes, secret-shaped files or content, binaries, and unsupported
tree entries remain outside the managed repository.

Analysis and review modes save a written output. Code mode requires a versioned
JSON response containing full-file writes. Litigation validates it, applies it
only inside an isolated local branch, and records a proposal with its diff and
commit. Applying a proposal is a separate explicit action guarded by the
expected current main commit, a clean repository, and a fast-forward check.

A debate creates one independent position per selected agent. After every
initial position reaches a terminal state, each successful participant receives
one rebuttal handoff linked to the successful first-round outputs. Failed peers
remain visible and do not erase completed work.

## Browser and CLI

Open `/projects` in the local Litigation server. The browser reads the same REST
API used by the command-line client:

```bash
python3 litigation_cli.py --help
python3 litigation_cli.py projects
python3 litigation_cli.py handoff PROJECT_ID claude \
  "Review the error handling" --request-id review-001 --mode review --wait
python3 litigation_cli.py outputs HANDOFF_ID
python3 litigation_cli.py debate PROJECT_ID "Which design should we use?" \
  --request-id debate-001 --agent claude --agent grok
```

`GET /api/projects/{id}/export` downloads a bounded JSON record containing
project metadata, append-only history, captured handoffs, outputs, issues,
proposals, and the current committed file manifest. It does not separately
package the full working tree, but captured handoff context, agent outputs, and
proposal diffs may contain selected code or file contents. Review the export
before sharing it. Environment values, provider credentials, private session
links, and command diagnostics are not included. If stored project records
contain secret-shaped material, export fails explicitly instead of copying that
material into a shareable file.

## Status model

Handoffs move through `queued`, `running`, and one terminal state:
`completed`, `needs_revision`, `failed`, `interrupted`, or `cancelled`.
`cancel_requested` is visible while a running provider process is being stopped.
Malformed code output becomes `needs_revision`, while the raw response remains
available for a linked retry.

