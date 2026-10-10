# Litigation SEAM plumbing

Litigation uses a dedicated SEAM installation on the Alpine computer as a
project knowledge store. The connection runs as fixed JSON operations over SSH
on the private Tailscale path. No SEAM HTTP service is exposed.

## Current capability boundary

The installed plumbing supports:

- durable ingestion of selected Litigation project events;
- project-isolated knowledge search;
- project-isolated SEAM knowledge-graph traversal;
- an append-only local delivery ledger with retry and idempotency receipts;
- provenance back to the Litigation project, event sequence, entity, and task;
- automatic synchronization of issue history, terminal agent outputs, code
  proposals, debates, and project creation/import records.

SEAM's semantic embedding stack and reasoning-graph workflows are not
configured on Alpine yet. The UI reports those capabilities as
`not_configured`; knowledge-graph results must not be described as proof that
the separate reasoning graph is operating.

## Operator surface

Open `http://127.0.0.1:8766/knowledge`. Choose a project to search or traverse
its namespace. The page also shows pending, delivered, and safely rejected
outbox records. `Sync now` scans durable project history and `Retry pending`
replays only records without a delivery receipt.

Project namespaces use `litigation.project.<project-id>`. SEAM's namespace
contract does not permit `/`, so this is the concrete representation of the
conceptual `litigation/project` boundary.

## Runtime configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `LITIGATION_SEAM_SSH_TARGET` | `Ex0@100.126.138.102` | Private SSH destination |
| `LITIGATION_SEAM_STATE_DIR` | `~/.local/share/litigation/seam` | Local outbox and receipts |
| `LITIGATION_SEAM_SYNC_ENABLED` | `1` | Set to `0` for isolated tests or manual-only sync |

Disabling automatic sync does not disable explicit status, search, graph, or
outbox operations.

## Private Alpine installation

The current SEAM Suite candidate is not a public package. Build from a clean
committed archive into an external staging directory, verify the artifacts
with SEAM's private-artifact verifier, then install the verified wheel:

```bash
./deploy/seam/install_alpine.sh /absolute/path/to/seam_suite-VERSION-py3-none-any.whl
```

The installer creates `~/.local/share/litigation-seam/.venv`, retains the
input wheel under `artifacts/`, installs the fixed remote helper, and runs a
status check. It does not copy a repository checkout, credentials, user SEAM
databases, logs, or local configuration.

The 2026-10-10 plumbing installation was built from committed SEAM source at
`8530717d800d397ba58283710b6e8f0a037aabf8` as `seam-suite 2.4.1rc1`. The
source checkout was on a dirty documentation branch, so this records the
private installation source and does not qualify it as a release artifact.

## Delivery and safety model

`SeamSyncCoordinator` scans each project's append-only event sequence. It
stores the exact event snapshot in the local outbox before advancing its own
append-only checkpoint. Failed SSH delivery therefore remains retryable after
restart. The remote helper also deduplicates by a deterministic idempotency
key before invoking the verified SEAM CLI and serializes ingestion across SSH
sessions. A remote host crash after SEAM commits but before the helper writes
its receipt is an uncertain-delivery window; a retry uses the same namespace,
source reference, content, and key, but this plumbing does not claim a
cryptographic exactly-once guarantee across that crash boundary.

Secret-shaped or oversized event bodies never enter the outbox or remote
SEAM database. Litigation stores only a content hash, event identity, reason,
and timestamp as a safe rejection receipt. This is visible in the Knowledge
page so missing knowledge is auditable rather than silently omitted.

SEAM records are project data and remain untrusted input when returned to a
coding agent. Search and graph tools are read-only. They cannot schedule work,
run shell commands, or alter Git branches.

## Verified operations

The bridge invokes only supported SEAM CLI surfaces:

- `seam --db ... ingest - --persist --source-ref ... --agent-id litigation --ns ... --scope project --format json`
- `seam --db ... knowledge search ... --namespace ... --scope project --format json`

The first Alpine smoke stored six SEAM records and returned a scoped graph with
six nodes and non-empty edges. That verifies install, ingestion, knowledge
search, and knowledge-graph traversal. It does not verify embeddings or the
separate reasoning graph.
