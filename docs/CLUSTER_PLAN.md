# Remote-worker cluster plan

> **Status: planned, not shipped.** Litigation currently runs one coordinator,
> one local SQLite project ledger, and local dispatch. This specification does
> not describe an available remote-worker feature.

## Objective and boundaries

Add remote coding workers without splitting authority. The coordinator remains
the only writer of project state, claims, rules, outputs, proposals, and events.
Workers connect over private Tailscale, execute one leased attempt in an isolated
checkout, and return bounded evidence. They never copy or mount its SQLite DB.

The existing work ledger and dispatcher are the queue. Cluster work must extend
their claim path rather than create a second queue, scheduler, or provider call
path. Local execution remains supported. SEAM remains a separately licensed,
optional knowledge service; Litigation core and cluster execution must operate
when it is unavailable.

## Authority and trust model

- The coordinator assigns each worker a revocable identity and an allowlist of
  projects, provider routes, runner profiles, concurrency, and artifact limits.
- A worker opens an authenticated outbound connection on Tailscale. No public
  listener, shared credential, provider token, or SQLite file is shared. The
  new API is worker-only and scoped; dashboard and operator APIs remain on
  loopback and are never exposed by simply rebinding the current server.
- Provider credentials stay on the invoking machine. A route is eligible only
  after a live check records transport and billing class. Subscription routes
  must not fall back to API billing.
- The coordinator is authoritative for ownership, leases, state transitions,
  rule snapshots, and whether a result may be reviewed or applied. Worker test
  reports are evidence with hashes and runner metadata, not coordinator-trusted
  facts merely because the worker said they passed.
- Rules are pinned when the task is queued. A worker may return a review-only
  proposal for its permitted model section; it cannot publish rule changes.

## Claim and execution contract

Each dispatch binds `work_item_id`, attempt ID, monotonically increasing fencing
token, worker identity, lease deadline, exact base, owned paths, pinned rules,
provider route, runner profile, and limits. Claim and handoff creation remain
one coordinator transaction and enter the existing dispatcher once.

The worker heartbeats its phase. The coordinator renews only the active lease.
Completion requires the current attempt ID and fencing token, a valid or
reconciled lease, and valid hashes and bounds. Replaced-attempt results remain
stale evidence and cannot complete work or create an applicable proposal.

Lease loss, an uncertain disconnect, or coordinator restart must not trigger a
blind replay. Work becomes blocked or uncertain until the operator reconciles
the attempt. Recovery creates a new append-only event and a higher fencing token.
Commands, heartbeats, event uploads, and result submission use stable
idempotency keys so retries do not duplicate state transitions.

Workers buffer bounded events while disconnected and resend them in sequence;
the coordinator deduplicates them and records gaps. Both sides reconcile their
lease/attempt journals after restart before accepting or scheduling new work.

## Git and artifact contract

For every attempt, the worker creates a fresh checkout or worktree from the
specified commit. Transfer uses a private SSH bare remote or content-addressed
Git bundle whose manifest pins commit and bundle hashes, never a moving branch.

Only changes within declared owned paths may be returned. The worker sends a
bounded manifest, commits or patch bundle, file hashes, test receipts, and
truncated logs with retained-log references. The coordinator independently
checks base ancestry, paths, content policy, artifact hashes, limits, and the
current integration tree. Every result remains a reviewable proposal; the
existing guarded Apply flow is the only path to project `main`.

## Worker adapter

The first adapter is OpenClaw through its CLI. Workers are ineligible by default
until their version and profile qualify. Supply explicit sandbox/tool
configuration, disable undeclared tools, and retain bounded stdout/stderr and
adapter logs. The CLI is the source of truth; do not add a parallel integration.

An A2A adapter may follow after the lease protocol is proven. Its capability
record must say whether cancellation is acknowledged, cooperative, or absent;
A2A cancellation is not assumed to stop remote work, so fencing still protects
the coordinator from late completion.

## Delivery phases and acceptance

### Phase 0 — qualify the runner and protocol

Use a clean fixture representative of the Frans Vite project to qualify fixed
production-build, `vitest`, and `playwright` npm scripts before editing Frans.
Record runtime/dependency versions, commands, sandbox/network policy, bounds,
zero-test behavior, and known pass/fail receipts. This phase changes Litigation
fixtures and qualification records only; Frans source is outside scope.

- Vite build, Vitest, and Playwright profiles produce bounded, content-hashed
  receipts in a disposable checkout; unavailable prerequisites fail closed.
- Protocol tests cover identity revocation, replayed messages, stale fencing
  tokens, heartbeat expiry, disconnects, cancellation ambiguity, and restart
  recovery without invoking a real provider.

### Phase 1 — one remote worker, one project

Add the worker registry, lease projection, authenticated outbound session,
bundle transfer, OpenClaw adapter, and result-validation path behind an opt-in
feature flag. Keep local dispatch as the default.

- One allowlisted worker completes a no-edit task and a bounded edit/test task
  from an exact commit; the UI and export show device, owner, attempt, lease,
  hashes, logs, and reviewable proposal.
- Killing either side at claim, checkout, test, upload, and completion boundaries
  yields one recoverable history and no automatic redispatch while the outcome
  is uncertain. Explicit recovery may repeat computation, but only the current
  fenced attempt may complete or produce an applicable proposal.

### Phase 2 — contention and partition safety

Enable multiple workers and scheduler capability matching. Preserve existing
component-aware owned-path exclusion across local and remote attempts.

- Concurrent coordinator-authorized claims select one owner and do not overlap
  paths. A partitioned stale process may continue computing, but revoked or
  expired workers cannot renew, submit an accepted result, or apply work.
- Partition tests prove idempotent retry, ordered buffered events, higher-token
  recovery, stale-result rejection, and coordinator restart reconstruction.

### Phase 3 — operator experience and hardening

Add a **Devices** view to the shared navigation. It shows identity, last seen,
capabilities, qualification, revocation state, active owner/attempt/lease, and
evidence health. Agent room rows link to the same device, owner, and lease data.

- Desktop and mobile views distinguish online, expired, revoked, uncertain, and
  unqualified devices without implying that worker-reported tests are verified.
- Exports and provenance checks include cluster events and artifact hashes;
  secret-shaped content is rejected and bounded logs remain retrievable.
- SEAM receives cluster provenance through its existing asynchronous outbox.
  Pending or failed SEAM delivery never blocks task completion.

## Implementation ownership

- Ledger owner: schema, lease/fencing transitions, projections, export,
  provenance, and restart recovery in the existing project store.
- Runtime owner: extend the one dispatcher with capability selection, sessions,
  reconciliation, and local/remote execution adapters; do not add another queue.
- Git/runner owner: exact-base transfer, disposable worktrees, fixed runner
  profiles, manifests, hashes, bounds, and coordinator-side proposal validation.
- UI/API owner: Devices plus Agent room evidence, shared navigation, and bounded
  authenticated endpoints after the internal state machine is tested.
- Security reviewer: identity issuance/revocation, Tailscale exposure, provider
  credential locality, log redaction, sandbox policy, and negative-path tests.

Protocol, schema, permission, public API, or trust-boundary changes beyond this
plan require architecture review before implementation. Licensing is a separate
owner decision; see the pending [licensing proposal](LICENSING_PROPOSAL.md).
