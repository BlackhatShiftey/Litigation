"""Append-only model rule revisions and evidence-linked improvement proposals."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


MODEL_SCOPES = ("claude", "openai", "glm", "gemini", "cline", "grok")
ALL_SCOPES = ("global", *MODEL_SCOPES)
SCOPE_TITLES = {
    "global": "Global",
    "claude": "Claude",
    "openai": "OpenAI",
    "glm": "GLM",
    "gemini": "Gemini",
    "cline": "Cline",
    "grok": "Grok",
}
MAX_RULE_BYTES = 256 * 1024


class RulesStoreError(RuntimeError):
    """Safe rules persistence or validation failure."""


class RevisionConflict(RulesStoreError):
    """The caller edited an outdated active revision."""


def canonical_model_scope(model_id: str) -> str:
    """Map provider/model identities onto one extensible rule scope."""
    value = (model_id or "").strip().lower()
    aliases = {
        "antigravity": "gemini", "agy": "gemini",
        "codex": "openai", "gpt": "openai", "astra": "openai",
        "chatgpt": "openai", "openai": "openai",
    }
    for needle, scope in aliases.items():
        if needle in value:
            return scope
    for scope in MODEL_SCOPES:
        if scope in value:
            return scope
    raise RulesStoreError("no rules section exists for this model")


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RulesStore:
    """SQLite ledger whose active state is derived from immutable revisions/events."""

    def __init__(self, db_path: str, seed_root: str | Path | None = None) -> None:
        self.path = db_path
        self.seed_root = Path(seed_root or Path(__file__).parent / "rules")
        if db_path != ":memory:":
            Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("pragma foreign_keys=on")
        self._conn.execute("pragma recursive_triggers=on")
        self._conn.execute("pragma journal_mode=wal")
        self._conn.execute("pragma busy_timeout=5000")
        self._init_schema()
        self._seed_missing_scopes()

    def _init_schema(self) -> None:
        tables = ("rule_scopes", "rule_revisions", "rule_proposals", "rule_events")
        with self._lock, self._conn:
            self._conn.executescript(
                """
                create table if not exists rule_scopes (
                    id text primary key,
                    title text not null,
                    display_order integer not null unique,
                    created_at real not null
                );
                create table if not exists rule_revisions (
                    id text primary key,
                    scope text not null references rule_scopes(id),
                    revision integer not null,
                    content text not null,
                    content_hash text not null,
                    origin text not null,
                    summary text not null,
                    evidence_handoff_id text,
                    proposal_id text,
                    created_at real not null,
                    unique(scope, revision)
                );
                create table if not exists rule_proposals (
                    id text primary key,
                    scope text not null references rule_scopes(id),
                    content text not null,
                    content_hash text not null,
                    summary text not null,
                    evidence_handoff_id text,
                    proposer text not null,
                    created_at real not null
                );
                create table if not exists rule_events (
                    seq integer primary key autoincrement,
                    scope text not null references rule_scopes(id),
                    entity_type text not null,
                    entity_id text not null,
                    kind text not null,
                    payload_json text not null,
                    created_at real not null
                );
                create index if not exists idx_rule_revisions_scope
                    on rule_revisions(scope, revision desc);
                create index if not exists idx_rule_proposals_scope
                    on rule_proposals(scope, created_at desc);
                create index if not exists idx_rule_events_entity
                    on rule_events(entity_type, entity_id, seq);
                """
            )
            for table in tables:
                self._conn.execute(
                    f"create trigger if not exists immutable_{table}_update "
                    f"before update on {table} begin select raise(abort, 'immutable record'); end"
                )
                self._conn.execute(
                    f"create trigger if not exists immutable_{table}_delete "
                    f"before delete on {table} begin select raise(abort, 'immutable record'); end"
                )

    @contextmanager
    def _immediate_transaction(self):
        """Serialize a state check and its writes across store instances."""
        with self._lock:
            self._conn.execute("begin immediate")
            try:
                yield
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    @contextmanager
    def _read_transaction(self):
        """Keep multi-row snapshots on one SQLite read version."""
        with self._lock:
            self._conn.execute("begin")
            try:
                yield
            finally:
                self._conn.rollback()

    @staticmethod
    def _id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _validate_content(content: str) -> str:
        text = content.strip() + "\n"
        size = len(text.encode("utf-8"))
        if not text.strip():
            raise RulesStoreError("rule content is required")
        if size > MAX_RULE_BYTES:
            raise RulesStoreError("rule content exceeds 256 KiB")
        return text

    @staticmethod
    def _validate_scope(scope: str) -> str:
        value = scope.strip().lower()
        if value not in ALL_SCOPES:
            raise RulesStoreError("unknown rules section")
        return value

    def _seed_path(self, scope: str) -> Path:
        return self.seed_root / ("GLOBAL.md" if scope == "global" else f"{scope}/RULES.md")

    def _seed_missing_scopes(self) -> None:
        now = time.time()
        with self._lock, self._conn:
            for order, scope in enumerate(ALL_SCOPES):
                self._conn.execute(
                    "insert or ignore into rule_scopes(id,title,display_order,created_at) values(?,?,?,?)",
                    (scope, SCOPE_TITLES[scope], order, now),
                )
                exists = self._conn.execute(
                    "select 1 from rule_revisions where scope=? limit 1", (scope,)
                ).fetchone()
                if exists:
                    continue
                path = self._seed_path(scope)
                try:
                    content = self._validate_content(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError) as exc:
                    raise RulesStoreError(f"could not read seed rules for {scope}") from exc
                revision_id = self._id()
                self._conn.execute(
                    "insert into rule_revisions "
                    "(id,scope,revision,content,content_hash,origin,summary,evidence_handoff_id,proposal_id,created_at) "
                    "values(?,?,?,?,?,?,?,?,?,?)",
                    (revision_id, scope, 1, content, _hash_text(content), "seed",
                     "Initial tracked rules", None, None, now),
                )
                self._append_event(scope, "revision", revision_id, "published", {"revision": 1, "origin": "seed"})

    def _append_event(
        self, scope: str, entity_type: str, entity_id: str, kind: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        cur = self._conn.execute(
            "insert into rule_events(scope,entity_type,entity_id,kind,payload_json,created_at) "
            "values(?,?,?,?,?,?)",
            (scope, entity_type, entity_id, kind, self._json(payload or {}), now),
        )
        return {"seq": cur.lastrowid, "scope": scope, "entity_type": entity_type,
                "entity_id": entity_id, "kind": kind, "payload": payload or {}, "created_at": now}

    def _latest_event(self, entity_type: str, entity_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "select * from rule_events where entity_type=? and entity_id=? order by seq desc limit 1",
            (entity_type, entity_id),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    @staticmethod
    def _revision_row(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def get_active(self, scope: str) -> dict[str, Any]:
        scope = self._validate_scope(scope)
        with self._lock:
            row = self._conn.execute(
                "select * from rule_revisions where scope=? order by revision desc limit 1", (scope,)
            ).fetchone()
            if not row:
                raise RulesStoreError("rules section has no active revision")
            return self._revision_row(row)

    def list_scopes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("select * from rule_scopes order by display_order").fetchall()
            result = []
            for row in rows:
                item = dict(row)
                active = self.get_active(item["id"])
                item.update({"revision": active["revision"], "content_hash": active["content_hash"],
                             "origin": active["origin"], "published_at": active["created_at"]})
                result.append(item)
            return result

    def list_revisions(
        self, scope: str, limit: int = 100, before_revision: int | None = None,
    ) -> list[dict[str, Any]]:
        scope = self._validate_scope(scope)
        count = max(1, min(int(limit), 501))
        sql = "select * from rule_revisions where scope=?"
        values: list[Any] = [scope]
        if before_revision is not None:
            sql += " and revision<?"
            values.append(int(before_revision))
        sql += " order by revision desc limit ?"
        values.append(count)
        with self._lock:
            rows = self._conn.execute(sql, values).fetchall()
            return [self._revision_row(row) for row in rows]

    def publish(
        self, scope: str, content: str, summary: str, *, expected_revision: int,
        origin: str = "user", evidence_handoff_id: str | None = None,
        proposal_id: str | None = None,
    ) -> dict[str, Any]:
        scope = self._validate_scope(scope)
        content = self._validate_content(content)
        if origin not in {"user", "proposal_accepted"}:
            raise RulesStoreError("unsupported revision origin")
        with self._immediate_transaction():
            return self._publish_locked(
                scope, content, summary, expected_revision=expected_revision,
                origin=origin, evidence_handoff_id=evidence_handoff_id,
                proposal_id=proposal_id,
            )

    def _publish_locked(
        self, scope: str, content: str, summary: str, *, expected_revision: int,
        origin: str, evidence_handoff_id: str | None, proposal_id: str | None,
    ) -> dict[str, Any]:
        """Insert a revision inside the caller's transaction."""
        active = self.get_active(scope)
        if int(expected_revision) != int(active["revision"]):
            raise RevisionConflict(
                f"rules changed after revision {expected_revision}; reload before publishing"
            )
        if content == active["content"]:
            raise RulesStoreError("the proposed rules match the active revision")
        revision = int(active["revision"]) + 1
        revision_id, now = self._id(), time.time()
        self._conn.execute(
            "insert into rule_revisions "
            "(id,scope,revision,content,content_hash,origin,summary,evidence_handoff_id,proposal_id,created_at) "
            "values(?,?,?,?,?,?,?,?,?,?)",
            (revision_id, scope, revision, content, _hash_text(content), origin,
             summary.strip()[:2000], evidence_handoff_id, proposal_id, now),
        )
        self._append_event(scope, "revision", revision_id, "published",
                           {"revision": revision, "origin": origin, "proposal_id": proposal_id})
        return self.get_active(scope)

    def propose(
        self, scope: str, content: str, summary: str, evidence_handoff_id: str | None,
        *, proposer: str = "user", proposer_scope: str | None = None,
    ) -> dict[str, Any]:
        scope = self._validate_scope(scope)
        content = self._validate_content(content)
        proposer = proposer.strip().lower()
        if proposer not in {"user", "agent"}:
            raise RulesStoreError("unknown proposal origin")
        canonical_proposer = None
        if proposer == "agent":
            if scope == "global":
                raise RulesStoreError("agents cannot propose changes to global rules")
            canonical_proposer = canonical_model_scope(proposer_scope or "")
            if canonical_proposer != scope:
                raise RulesStoreError("agents may propose changes only to their own rules section")
            if not (evidence_handoff_id or "").strip():
                raise RulesStoreError("agent rule proposals require handoff evidence")
        proposal_id, now = self._id(), time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "insert into rule_proposals "
                "(id,scope,content,content_hash,summary,evidence_handoff_id,proposer,created_at) "
                "values(?,?,?,?,?,?,?,?)",
                (proposal_id, scope, content, _hash_text(content), summary.strip()[:2000],
                 evidence_handoff_id, canonical_proposer or "user", now),
            )
            self._append_event(scope, "proposal", proposal_id, "proposed",
                               {"proposer": canonical_proposer or "user", "evidence_handoff_id": evidence_handoff_id})
            return self.get_proposal(proposal_id)  # type: ignore[return-value]

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("select * from rule_proposals where id=?", (proposal_id,)).fetchone()
            if not row:
                return None
            item = dict(row)
            event = self._latest_event("proposal", proposal_id)
            item["status"] = event["kind"] if event else "proposed"
            item["decision"] = event["payload"] if event and event["kind"] != "proposed" else None
            return item

    def list_proposals(self, scope: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        values: list[Any] = []
        sql = "select id from rule_proposals"
        if scope:
            sql += " where scope=?"
            values.append(self._validate_scope(scope))
        sql += " order by created_at desc, id"
        with self._lock:
            rows = self._conn.execute(sql, values).fetchall()
            items = [self.get_proposal(row["id"]) for row in rows]
            return [item for item in items if item and (not status or item["status"] == status)]

    def accept_proposal(self, proposal_id: str, expected_revision: int) -> dict[str, Any]:
        with self._immediate_transaction():
            proposal = self.get_proposal(proposal_id)
            if not proposal:
                raise RulesStoreError("rule proposal not found")
            if proposal["status"] != "proposed":
                raise RulesStoreError("rule proposal has already been decided")
            revision = self._publish_locked(
                proposal["scope"], proposal["content"], proposal["summary"],
                expected_revision=expected_revision, origin="proposal_accepted",
                evidence_handoff_id=proposal["evidence_handoff_id"], proposal_id=proposal_id,
            )
            self._append_event(proposal["scope"], "proposal", proposal_id, "accepted",
                               {"revision": revision["revision"]})
            return {"proposal": self.get_proposal(proposal_id), "revision": revision}

    def reject_proposal(self, proposal_id: str, reason: str = "") -> dict[str, Any]:
        with self._immediate_transaction():
            proposal = self.get_proposal(proposal_id)
            if not proposal:
                raise RulesStoreError("rule proposal not found")
            if proposal["status"] != "proposed":
                raise RulesStoreError("rule proposal has already been decided")
            self._append_event(proposal["scope"], "proposal", proposal_id, "rejected",
                               {"reason": reason.strip()[:2000]})
            return self.get_proposal(proposal_id)  # type: ignore[return-value]

    def get_effective(self, model_id: str) -> dict[str, Any]:
        model_scope = canonical_model_scope(model_id)
        with self._read_transaction():
            global_rules = self.get_active("global")
            model_rules = self.get_active(model_scope)
        combined = (
            "# Effective Litigation rules\n\n"
            "The global section has authority. Model-specific rules cannot expand permissions "
            "or override global requirements.\n\n"
            f"{global_rules['content'].rstrip()}\n\n{model_rules['content'].rstrip()}\n"
        )
        descriptor = {
            "model_scope": model_scope,
            "global_revision": global_rules["revision"],
            "global_hash": global_rules["content_hash"],
            "model_revision": model_rules["revision"],
            "model_hash": model_rules["content_hash"],
            "combined_hash": _hash_text(combined),
        }
        return {
            "model_scope": model_scope,
            "global": global_rules,
            "model": model_rules,
            "combined_text": combined,
            "snapshot_hash": _hash_text(self._json(descriptor)),
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def default_rules_db_path() -> str:
    return os.environ.get(
        "LITIGATION_RULES_DB",
        str(Path.home() / ".local" / "share" / "litigation" / "rules.sqlite3"),
    )


def load_effective_rules(
    model_id: str, db_path: str | None = None, seed_root: str | Path | None = None,
) -> dict[str, Any]:
    """Load a snapshot for enqueue-time capture without importing the server."""
    store = RulesStore(db_path or default_rules_db_path(), seed_root)
    try:
        return store.get_effective(model_id)
    finally:
        store.close()
