# Shared work ledger

The work ledger is an append-only coordination layer over Litigation handoffs. A work item records a stable request ID, instructions, acceptance criteria, linked issues, and declared owned paths. Its state advances through `queued`, `claimed`, `running`, and `completed` or `blocked`; every transition is a project event.

`pull-work` selects a configured subscription agent and atomically creates the ordinary coding-agent handoff with the work claim. The normal dispatcher starts that handoff, so a pull cannot invoke a second execution path. An agent already running an ad hoc handoff can inspect `work_list` and use `work_claim`. The controller rejects a second claimant, a second active claim for one handoff, and overlapping declared paths across active shared-work claims. Path comparison uses path components, so `src` overlaps `src/pkg` but not `src2`. An empty path list owns the whole project. These locks apply to shared-work claims only; ordinary isolated review branches retain their existing behavior.

Claims survive restarts. A queued linked handoff remains resumable by the dispatcher. If execution had started, runtime recovery marks the handoff interrupted and the work blocked. Recovery requires an explicit matching attempt ID and creates new append-only `recovered` and `queued` events; the controller never silently replays interrupted work.

Examples:

```bash
./litigation_cli.py create-work PROJECT "Parser repair" "Repair parsing and add tests" \
  --request-id parser-work-001 --owned-path src/parser.py --owned-path tests
./litigation_cli.py work-items PROJECT
./litigation_cli.py pull-work WORK_ID grok --request-id parser-pull-001
./litigation_cli.py verify-provenance PROJECT
```

The API equivalents are `GET` and `POST /api/projects/{project_id}/work-items`, `POST /api/work-items/{id}/pull`, `POST /api/work-items/{id}/claim`, `POST /api/work-items/{id}/recover`, and `GET /api/projects/{project_id}/provenance`. Listing also includes active legacy and ad hoc handoffs so agents can avoid obvious duplicate work. Litigation does not claim semantic duplicate detection across arbitrary natural-language instructions.

The provenance endpoint checks SQLite integrity, required immutability triggers, same-project parent and source-output links, pinned rule content and snapshot hashes, proposal commit availability and base metadata, and compatible passing-test tree fingerprints. Missing evidence from older records is reported as `unverified`. A failed hash or relation is reported as `failed`. These are local consistency checks; they do not prove cryptographic model identity, external authorship, or remote provider behavior.
