"""Append-only persistence for durable Litigation projects and agent work."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any


TERMINAL_HANDOFF_STATES = {
    "completed", "needs_revision", "failed", "interrupted", "cancelled",
}
MAX_CONTEXT_BYTES = 1024 * 1024
HANDOFF_STATE_KINDS = {
    "queued", "running", "cancel_requested", *TERMINAL_HANDOFF_STATES,
}
ISSUE_STATE_KINDS = {"open", "closed", "reopened"}
WORK_STATE_KINDS = {"queued", "claimed", "running", "completed", "blocked", "recovered"}
ACTIVE_WORK_STATES = {"claimed", "running"}
MAX_WORK_ITEMS = 500


class ProjectStoreError(RuntimeError):
    """A safe, user-facing project persistence failure."""


class ProjectStore:
    """Separate immutable-record/event store for project execution state."""

    def __init__(self, db_path: str) -> None:
        self.path = db_path
        if db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(db_path))
            os.makedirs(parent, mode=0o700, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("pragma foreign_keys=on")
        self._conn.execute("pragma recursive_triggers=on")
        self._conn.execute("pragma journal_mode=wal")
        self._conn.execute("pragma busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        tables = ("projects", "issues", "handoffs", "outputs", "proposals", "work_items", "project_events")
        with self._lock, self._conn:
            self._conn.executescript(
                """
                create table if not exists projects (
                    id text primary key,
                    name text not null,
                    description text not null,
                    initial_commit text not null,
                    source_description text not null default '',
                    created_at real not null
                );
                create table if not exists issues (
                    id text primary key,
                    project_id text not null references projects(id),
                    title text not null,
                    body text not null,
                    created_at real not null
                );
                create table if not exists handoffs (
                    id text primary key,
                    project_id text not null references projects(id),
                    agent_id text not null,
                    model text not null,
                    mode text not null,
                    instructions text not null,
                    context_json text not null,
                    base_commit text not null,
                    request_id text not null unique,
                    created_at real not null
                );
                create table if not exists outputs (
                    id text primary key,
                    handoff_id text not null references handoffs(id),
                    raw_text text not null,
                    parsed_json text,
                    final_state text not null,
                    created_at real not null
                );
                create table if not exists proposals (
                    id text primary key,
                    project_id text not null references projects(id),
                    handoff_id text not null references handoffs(id),
                    output_id text not null references outputs(id),
                    base_commit text not null,
                    branch text not null,
                    commit_sha text not null,
                    summary text not null,
                    diff_text text not null,
                    changed_paths_json text not null,
                    created_at real not null
                );
                create table if not exists work_items (
                    id text primary key,
                    project_id text not null references projects(id),
                    request_id text not null,
                    title text not null,
                    instructions text not null,
                    acceptance_json text not null,
                    owned_paths_json text not null,
                    issue_ids_json text not null,
                    created_at real not null,
                    unique(project_id, request_id)
                );
                create table if not exists project_events (
                    seq integer primary key autoincrement,
                    project_id text not null references projects(id),
                    entity_type text not null,
                    entity_id text not null,
                    kind text not null,
                    payload_json text not null,
                    created_at real not null
                );
                create index if not exists idx_issues_project on issues(project_id, created_at);
                create index if not exists idx_handoffs_project on handoffs(project_id, created_at);
                create index if not exists idx_outputs_handoff on outputs(handoff_id, created_at);
                create index if not exists idx_proposals_handoff on proposals(handoff_id, created_at);
                create index if not exists idx_work_items_project on work_items(project_id, created_at);
                create index if not exists idx_events_project on project_events(project_id, seq);
                create index if not exists idx_events_entity on project_events(entity_type, entity_id, seq);
                """
            )
            for table in tables:
                self._conn.execute(
                    f"create trigger if not exists immutable_{table}_update "
                    f"before update on {table} begin "
                    "select raise(abort, 'immutable record'); end"
                )
                self._conn.execute(
                    f"create trigger if not exists immutable_{table}_delete "
                    f"before delete on {table} begin "
                    "select raise(abort, 'immutable record'); end"
                )

    @staticmethod
    def _id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _decode(value: str | None, default: Any = None) -> Any:
        if value is None:
            return default
        return json.loads(value)

    def _insert_event(
        self,
        project_id: str,
        entity_type: str,
        entity_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        cur = self._conn.execute(
            "insert into project_events "
            "(project_id, entity_type, entity_id, kind, payload_json, created_at) "
            "values (?,?,?,?,?,?)",
            (project_id, entity_type, entity_id, kind, self._json(payload or {}), now),
        )
        return {
            "seq": cur.lastrowid, "project_id": project_id,
            "entity_type": entity_type, "entity_id": entity_id,
            "kind": kind, "payload": payload or {}, "created_at": now,
        }

    def _event_row(self, row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["payload"] = self._decode(out.pop("payload_json"), {})
        return out

    def _latest_event(
        self, entity_type: str, entity_id: str, kinds: set[str] | None = None
    ) -> dict[str, Any] | None:
        sql = "select * from project_events where entity_type=? and entity_id=?"
        values: list[Any] = [entity_type, entity_id]
        if kinds:
            sql += f" and kind in ({','.join('?' for _ in kinds)})"
            values.extend(sorted(kinds))
        sql += " order by seq desc limit 1"
        row = self._conn.execute(sql, values).fetchone()
        return self._event_row(row) if row else None

    # -- projects ---------------------------------------------------------

    def create_project(
        self,
        project_id: str,
        name: str,
        description: str,
        initial_commit: str,
        source_description: str = "",
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "insert into projects "
                "(id,name,description,initial_commit,source_description,created_at) "
                "values (?,?,?,?,?,?)",
                (project_id, name, description, initial_commit, source_description, now),
            )
            self._insert_event(project_id, "project", project_id, "created", {})
        return self.get_project(project_id)  # type: ignore[return-value]

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "select * from projects order by created_at desc, id"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "select * from projects where id=?", (project_id,)
            ).fetchone()
            if not row:
                return None
            out = dict(row)
            out["issues"] = self.list_issues(project_id)
            out["handoffs"] = self.list_handoffs(project_id)
            return out

    # -- issues -----------------------------------------------------------

    def create_issue(self, project_id: str, title: str, body: str) -> dict[str, Any]:
        issue_id, now = self._id(), time.time()
        with self._lock, self._conn:
            self._require_project(project_id)
            self._conn.execute(
                "insert into issues (id,project_id,title,body,created_at) values (?,?,?,?,?)",
                (issue_id, project_id, title, body, now),
            )
            self._insert_event(project_id, "issue", issue_id, "open", {})
        return self._issue(issue_id)  # type: ignore[return-value]

    def _issue(self, issue_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("select * from issues where id=?", (issue_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        event = self._latest_event("issue", issue_id, ISSUE_STATE_KINDS)
        out["status"] = "closed" if event and event["kind"] == "closed" else "open"
        return out

    def list_issues(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "select id from issues where project_id=? order by created_at, id",
                (project_id,),
            ).fetchall()
            return [self._issue(row["id"]) for row in rows]  # type: ignore[misc]

    def append_issue_event(self, issue_id: str, kind: str, text: str = "") -> dict[str, Any]:
        if kind not in {"comment", "closed", "reopened"}:
            raise ProjectStoreError("unsupported issue event")
        with self._lock, self._conn:
            issue = self._issue(issue_id)
            if not issue:
                raise ProjectStoreError("issue not found")
            if kind == "closed" and issue["status"] == "closed":
                return self._latest_event("issue", issue_id, ISSUE_STATE_KINDS)  # type: ignore[return-value]
            if kind == "reopened" and issue["status"] == "open":
                return self._latest_event("issue", issue_id, ISSUE_STATE_KINDS)  # type: ignore[return-value]
            return self._insert_event(
                issue["project_id"], "issue", issue_id, kind, {"text": text}
            )

    # -- handoffs ---------------------------------------------------------

    def create_handoff(
        self,
        project_id: str,
        agent_id: str,
        model: str,
        mode: str,
        instructions: str,
        acceptance: list[Any],
        file_context: list[Any],
        issue_ids: list[str],
        parent_handoff_id: str | None,
        source_output_ids: list[str],
        base_commit: str,
        request_id: str,
        submitted_payload: dict[str, Any] | None = None,
        snapshot_context: dict[str, Any] | None = None,
        work_item_id: str | None = None,
        work_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        context = {
            "project_id": project_id, "agent_id": agent_id, "model": model,
            "mode": mode, "instructions": instructions, "acceptance": acceptance,
            "file_context": file_context, "issue_ids": issue_ids,
            "parent_handoff_id": parent_handoff_id,
            "source_output_ids": source_output_ids, "base_commit": base_commit,
            "request_id": request_id,
        }
        if submitted_payload is not None:
            if not isinstance(submitted_payload, dict):
                raise ProjectStoreError("submitted_payload must be an object")
            # Keep the caller's pre-resolution request alongside the exact
            # resolved snapshot. It is the stable idempotency identity.
            context["submitted_payload"] = self._decode(
                self._json(submitted_payload), {}
            )
        if snapshot_context is not None:
            if not isinstance(snapshot_context, dict):
                raise ProjectStoreError("snapshot_context must be an object")
            context.update(self._decode(self._json(snapshot_context), {}))
        with self._lock:
            self._conn.execute("begin immediate")
            try:
                existing = self._conn.execute(
                    "select id,context_json from handoffs where request_id=?", (request_id,)
                ).fetchone()
                if existing:
                    previous = self._decode(existing["context_json"], {})
                    if submitted_payload is not None:
                        matches = (
                            "submitted_payload" in previous
                            and self._json(previous["submitted_payload"])
                            == self._json(context["submitted_payload"])
                        )
                    else:
                        matches = not any(
                            previous.get(key) != value for key, value in context.items()
                        )
                    if not matches:
                        raise ProjectStoreError("request_id was already used for a different handoff")
                    self._conn.commit()
                    return self.get_handoff(existing["id"])  # type: ignore[return-value]
                self._require_project(project_id)
                issue_context: list[dict[str, Any]] = []
                if parent_handoff_id:
                    parent = self.get_handoff(parent_handoff_id)
                    if not parent or parent["project_id"] != project_id:
                        raise ProjectStoreError("parent handoff not found in project")
                    context["parent_handoff"] = {
                        "id": parent["id"], "agent_id": parent["agent_id"],
                        "model": parent["model"], "mode": parent["mode"],
                        "instructions": parent["instructions"],
                        "acceptance": parent.get("acceptance", []),
                        "base_commit": parent["base_commit"],
                        "status": parent["status"],
                    }
                else:
                    context["parent_handoff"] = None
                for issue_id in issue_ids:
                    issue = self._issue(issue_id)
                    if not issue or issue["project_id"] != project_id:
                        raise ProjectStoreError("referenced issue not found in project")
                    events = self._conn.execute(
                        "select * from project_events where entity_type='issue' "
                        "and entity_id=? order by seq", (issue_id,),
                    ).fetchall()
                    issue_context.append({
                        **issue,
                        "events": [self._event_row(event) for event in events],
                    })
                context["issue_context"] = issue_context
                source_outputs: list[dict[str, Any]] = []
                for output_id in source_output_ids:
                    output = self._conn.execute(
                        "select o.* from outputs o join handoffs h on h.id=o.handoff_id "
                        "where o.id=? and h.project_id=?", (output_id, project_id),
                    ).fetchone()
                    if not output:
                        raise ProjectStoreError("referenced output not found in project")
                    source_outputs.append(self._output_row(output))
                context["source_outputs"] = source_outputs
                handoff_id, now = self._id(), time.time()
                context_json = self._json(context)
                if len(context_json.encode("utf-8")) > MAX_CONTEXT_BYTES:
                    raise ProjectStoreError("handoff context exceeds the 1 MiB limit")
                self._conn.execute(
                    "insert into handoffs "
                    "(id,project_id,agent_id,model,mode,instructions,context_json,"
                    "base_commit,request_id,created_at) values (?,?,?,?,?,?,?,?,?,?)",
                    (handoff_id, project_id, agent_id, model, mode, instructions,
                     context_json, base_commit, request_id, now),
                )
                self._insert_event(project_id, "handoff", handoff_id, "queued", {})
                if work_item_id:
                    if not work_attempt_id:
                        raise ProjectStoreError("work attempt_id is required")
                    self._claim_work_locked(
                        work_item_id, work_attempt_id, agent_id, handoff_id,
                        require_running_handoff=False,
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_handoff(handoff_id)  # type: ignore[return-value]

    def get_handoff_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "select * from handoffs where request_id=?", (request_id,)
            ).fetchone()
            return self._handoff_row(row) if row else None

    def _handoff_row(self, row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        context = self._decode(out.pop("context_json"), {})
        out["context"] = context
        for key in (
            "acceptance", "file_context", "issue_ids", "parent_handoff_id",
            "source_output_ids", "issue_context", "parent_handoff", "source_outputs",
            "rules_snapshot", "working_base", "integration_base", "lineage_depth",
        ):
            out[key] = context.get(key)
        event = self._latest_event("handoff", out["id"], HANDOFF_STATE_KINDS)
        out["status"] = event["kind"] if event else "queued"
        out["status_event_seq"] = event["seq"] if event else None
        return out

    def get_handoff(self, handoff_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "select * from handoffs where id=?", (handoff_id,)
            ).fetchone()
            return self._handoff_row(row) if row else None

    def list_handoffs(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "select * from handoffs where project_id=? order by created_at, id",
                (project_id,),
            ).fetchall()
            return [self._handoff_row(row) for row in rows]

    def list_queued(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("select * from handoffs order by created_at, id").fetchall()
            return [item for row in rows if (item := self._handoff_row(row))["status"] == "queued"]

    def claim_handoff(self, handoff_id: str) -> bool:
        with self._lock:
            self._conn.execute("begin immediate")
            try:
                handoff = self.get_handoff(handoff_id)
                if not handoff or handoff["status"] != "queued":
                    self._conn.commit()
                    return False
                self._insert_event(handoff["project_id"], "handoff", handoff_id, "running", {})
                claim = self.active_work_claim(handoff_id)
                if claim and claim["status"] == "claimed":
                    self._insert_event(
                        handoff["project_id"], "work_item", claim["id"], "running",
                        {"attempt_id": claim["attempt_id"], "worker_id": handoff["agent_id"],
                         "handoff_id": handoff_id},
                    )
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def append_task_event(
        self, handoff_id: str, kind: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with self._lock:
            self._conn.execute("begin immediate")
            try:
                handoff = self.get_handoff(handoff_id)
                if not handoff:
                    raise ProjectStoreError("handoff not found")
                current = handoff["status"]
                if current in TERMINAL_HANDOFF_STATES:
                    event = self._latest_event("handoff", handoff_id, HANDOFF_STATE_KINDS)
                    self._conn.commit()
                    return event  # type: ignore[return-value]
                target = kind
                if kind in {"cancel", "cancel_requested", "cancelled"}:
                    if current == "queued":
                        target = "cancelled"
                    elif current == "running":
                        target = "cancel_requested"
                    elif current == "cancel_requested":
                        event = self._latest_event("handoff", handoff_id, HANDOFF_STATE_KINDS)
                        self._conn.commit()
                        return event  # type: ignore[return-value]
                allowed = {
                    "queued": {"running", "cancelled"},
                    "running": {"cancel_requested", "completed", "needs_revision", "failed", "interrupted"},
                    "cancel_requested": {"cancelled"},
                }
                if target in HANDOFF_STATE_KINDS and target not in allowed.get(current, set()):
                    raise ProjectStoreError("invalid handoff transition")
                event = self._insert_event(
                    handoff["project_id"], "handoff", handoff_id, target, payload or {}
                )
                if target in TERMINAL_HANDOFF_STATES:
                    self._block_work_for_handoff_locked(handoff, target)
                self._conn.commit()
                return event
            except Exception:
                self._conn.rollback()
                raise

    def finish_handoff(
        self,
        handoff_id: str,
        raw_text: str,
        parsed: dict[str, Any] | None,
        final_state: str,
        proposal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if final_state not in TERMINAL_HANDOFF_STATES:
            raise ProjectStoreError("invalid terminal state")
        with self._lock:
            self._conn.execute("begin immediate")
            try:
                handoff = self.get_handoff(handoff_id)
                if not handoff:
                    raise ProjectStoreError("handoff not found")
                existing = self._conn.execute(
                    "select * from outputs where handoff_id=? order by created_at,id limit 1",
                    (handoff_id,),
                ).fetchone()
                if existing:
                    bundle = self._finish_bundle(handoff_id, existing["id"])
                    self._conn.commit()
                    return bundle
                if handoff["status"] not in {"running", "cancel_requested"}:
                    raise ProjectStoreError("handoff is not running")
                terminal = "cancelled" if handoff["status"] == "cancel_requested" else final_state
                output_id, now = self._id(), time.time()
                self._conn.execute(
                    "insert into outputs "
                    "(id,handoff_id,raw_text,parsed_json,final_state,created_at) values (?,?,?,?,?,?)",
                    (output_id, handoff_id, raw_text,
                     self._json(parsed) if parsed is not None else None, terminal, now),
                )
                proposal_row = None
                if proposal is not None:
                    proposal_id = str(proposal.get("id") or self._id())
                    self._conn.execute(
                        "insert into proposals "
                        "(id,project_id,handoff_id,output_id,base_commit,branch,commit_sha,"
                        "summary,diff_text,changed_paths_json,created_at) values (?,?,?,?,?,?,?,?,?,?,?)",
                        (proposal_id, handoff["project_id"], handoff_id, output_id,
                         proposal["base_commit"], proposal["branch"], proposal["commit_sha"],
                         proposal.get("summary", ""), proposal["diff_text"],
                         self._json(proposal.get("changed_paths", [])), now),
                    )
                    proposal_row = proposal_id
                    self._insert_event(
                        handoff["project_id"], "proposal", proposal_id,
                        "proposal_created", {"commit_sha": proposal["commit_sha"]},
                    )
                self._insert_event(
                    handoff["project_id"], "handoff", handoff_id, terminal,
                    {"output_id": output_id, "proposal_id": proposal_row},
                )
                claim = self.active_work_claim(handoff_id)
                if claim and claim["status"] in ACTIVE_WORK_STATES:
                    work_state = "completed" if terminal == "completed" else "blocked"
                    self._insert_event(
                        handoff["project_id"], "work_item", claim["id"], work_state,
                        {"attempt_id": claim["attempt_id"], "worker_id": handoff["agent_id"],
                         "handoff_id": handoff_id, "handoff_state": terminal,
                         "output_id": output_id, "proposal_id": proposal_row},
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self._finish_bundle(handoff_id, output_id)

    def _finish_bundle(self, handoff_id: str, output_id: str) -> dict[str, Any]:
        proposal = self._conn.execute(
            "select id from proposals where output_id=?", (output_id,)
        ).fetchone()
        return {
            "handoff": self.get_handoff(handoff_id),
            "output": self.get_output(output_id),
            "proposal": self.get_proposal(proposal["id"]) if proposal else None,
        }

    # -- shared work ledger ---------------------------------------------

    @staticmethod
    def _normalize_owned_paths(paths: list[str]) -> list[str]:
        if len(paths) > 100:
            raise ProjectStoreError("too many owned paths")
        normalized: list[str] = []
        for value in paths:
            if not isinstance(value, str):
                raise ProjectStoreError("owned paths must be strings")
            raw = value.strip()
            if raw.startswith("/") or "\\" in raw:
                raise ProjectStoreError("owned path is invalid")
            path = raw.strip("/")
            parts = tuple(part for part in path.split("/") if part)
            if not parts or path.startswith("/") or any(part in {".", ".."} for part in parts):
                raise ProjectStoreError("owned path is invalid")
            normalized.append("/".join(parts))
        return list(dict.fromkeys(normalized))

    @staticmethod
    def _paths_overlap(left: list[str], right: list[str]) -> bool:
        # Empty ownership deliberately means the whole project.
        if not left or not right:
            return True
        left_parts = [tuple(item.split("/")) for item in left]
        right_parts = [tuple(item.split("/")) for item in right]
        return any(
            a == b[:len(a)] or b == a[:len(b)]
            for a in left_parts for b in right_parts
        )

    def create_work_item(
        self, project_id: str, request_id: str, title: str, instructions: str,
        acceptance: list[str], owned_paths: list[str], issue_ids: list[str],
    ) -> dict[str, Any]:
        owned_paths = self._normalize_owned_paths(owned_paths)
        payload = {
            "title": title.strip(), "instructions": instructions.strip(),
            "acceptance": acceptance, "owned_paths": owned_paths, "issue_ids": issue_ids,
        }
        if not request_id.strip() or not payload["title"] or not payload["instructions"]:
            raise ProjectStoreError("request_id, title, and instructions are required")
        if len(payload["title"]) > 500 or len(payload["instructions"]) > 64_000:
            raise ProjectStoreError("work item content is too large")
        if len(acceptance) > 100 or len(issue_ids) > 100 or any(
            not isinstance(item, str) or len(item) > 4000 for item in acceptance
        ):
            raise ProjectStoreError("work item context exceeds its limits")
        with self._lock:
            self._conn.execute("begin immediate")
            try:
                self._require_project(project_id)
                existing = self._conn.execute(
                    "select id from work_items where project_id=? and request_id=?",
                    (project_id, request_id),
                ).fetchone()
                if existing:
                    item = self.get_work_item(existing["id"])
                    comparable = {key: item.get(key) for key in payload} if item else {}
                    if comparable != payload:
                        raise ProjectStoreError("request_id was already used for different work")
                    self._conn.commit()
                    return item  # type: ignore[return-value]
                for issue_id in issue_ids:
                    issue = self._issue(issue_id)
                    if not issue or issue["project_id"] != project_id:
                        raise ProjectStoreError("referenced issue not found in project")
                item_id, now = self._id(), time.time()
                self._conn.execute(
                    "insert into work_items (id,project_id,request_id,title,instructions,"
                    "acceptance_json,owned_paths_json,issue_ids_json,created_at) "
                    "values (?,?,?,?,?,?,?,?,?)",
                    (item_id, project_id, request_id, payload["title"], payload["instructions"],
                     self._json(acceptance), self._json(owned_paths), self._json(issue_ids), now),
                )
                self._insert_event(project_id, "work_item", item_id, "queued", {})
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_work_item(item_id)  # type: ignore[return-value]

    def _work_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["acceptance"] = self._decode(item.pop("acceptance_json"), [])
        item["owned_paths"] = self._decode(item.pop("owned_paths_json"), [])
        item["issue_ids"] = self._decode(item.pop("issue_ids_json"), [])
        event = self._latest_event("work_item", item["id"], WORK_STATE_KINDS)
        item["status"] = event["kind"] if event else "queued"
        item["updated_at"] = event["created_at"] if event else item["created_at"]
        detail = event["payload"] if event else {}
        item["attempt_id"] = detail.get("attempt_id")
        item["worker_id"] = detail.get("worker_id")
        item["handoff_id"] = detail.get("handoff_id")
        item["status_event_seq"] = event["seq"] if event else None
        return item

    def get_work_item(self, work_item_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "select * from work_items where id=?", (work_item_id,)
            ).fetchone()
            return self._work_row(row) if row else None

    def list_work_items(
        self, project_id: str, status: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), MAX_WORK_ITEMS))
        with self._lock:
            self._require_project(project_id)
            rows = self._conn.execute(
                "select * from work_items where project_id=? order by created_at,id",
                (project_id,),
            ).fetchall()
            items = [self._work_row(row) for row in rows]
            if status:
                items = [item for item in items if item["status"] == status]
            return items[:bounded]

    def list_all_work_items(self, project_id: str) -> list[dict[str, Any]]:
        """Return the complete ledger for the already size-bounded project export."""
        with self._lock:
            self._require_project(project_id)
            rows = self._conn.execute(
                "select * from work_items where project_id=? order by created_at,id",
                (project_id,),
            ).fetchall()
            return [self._work_row(row) for row in rows]

    def active_work_claim(self, handoff_id: str) -> dict[str, Any] | None:
        with self._lock:
            rows = self._conn.execute(
                "select w.* from work_items w join handoffs h on h.project_id=w.project_id "
                "where h.id=? order by w.created_at,w.id", (handoff_id,),
            ).fetchall()
            for row in rows:
                item = self._work_row(row)
                if item["status"] in ACTIVE_WORK_STATES and item.get("handoff_id") == handoff_id:
                    return item
            return None

    def _block_work_for_handoff_locked(
        self, handoff: dict[str, Any], handoff_state: str,
    ) -> None:
        claim = self.active_work_claim(str(handoff["id"]))
        if claim and claim["status"] in ACTIVE_WORK_STATES:
            self._insert_event(
                str(handoff["project_id"]), "work_item", str(claim["id"]), "blocked",
                {"attempt_id": claim.get("attempt_id"), "worker_id": handoff.get("agent_id"),
                 "handoff_id": handoff["id"], "handoff_state": handoff_state},
            )

    def _claim_work_locked(
        self, work_item_id: str, attempt_id: str, worker_id: str, handoff_id: str,
        *, require_running_handoff: bool,
    ) -> dict[str, Any]:
        item = self.get_work_item(work_item_id)
        handoff = self.get_handoff(handoff_id)
        if not item:
            raise ProjectStoreError("work item not found")
        if not handoff or handoff["project_id"] != item["project_id"]:
            raise ProjectStoreError("handoff not found in work item project")
        if handoff["agent_id"] != worker_id:
            raise ProjectStoreError("worker does not own the handoff")
        required = "running" if require_running_handoff else "queued"
        if handoff["status"] != required:
            raise ProjectStoreError(f"handoff must be {required}")
        prior = self._conn.execute(
            "select * from project_events where entity_type='work_item' and entity_id=? "
            "and json_extract(payload_json,'$.attempt_id')=? order by seq desc limit 1",
            (work_item_id, attempt_id),
        ).fetchone()
        if prior:
            payload = self._decode(prior["payload_json"], {})
            if payload.get("worker_id") != worker_id or payload.get("handoff_id") != handoff_id:
                raise ProjectStoreError("attempt_id was already used for a different claim")
            return item
        if item["status"] != "queued":
            raise ProjectStoreError("work item is not queued")
        existing_claim = self.active_work_claim(handoff_id)
        if existing_claim and existing_claim["id"] != work_item_id:
            raise ProjectStoreError("handoff already owns active shared work")
        rows = self._conn.execute(
            "select * from work_items where project_id=? order by created_at,id",
            (item["project_id"],),
        ).fetchall()
        for row in rows:
            other = self._work_row(row)
            if other["id"] != work_item_id and other["status"] in ACTIVE_WORK_STATES and self._paths_overlap(
                item["owned_paths"], other["owned_paths"]
            ):
                raise ProjectStoreError("owned paths overlap active shared work")
        payload = {"attempt_id": attempt_id, "worker_id": worker_id, "handoff_id": handoff_id}
        self._insert_event(item["project_id"], "work_item", work_item_id, "claimed", payload)
        if require_running_handoff:
            self._insert_event(item["project_id"], "work_item", work_item_id, "running", payload)
        return self.get_work_item(work_item_id)  # type: ignore[return-value]

    def claim_work_item(
        self, work_item_id: str, attempt_id: str, worker_id: str, handoff_id: str,
    ) -> dict[str, Any]:
        if not attempt_id.strip():
            raise ProjectStoreError("attempt_id is required")
        with self._lock:
            self._conn.execute("begin immediate")
            try:
                result = self._claim_work_locked(
                    work_item_id, attempt_id, worker_id, handoff_id,
                    require_running_handoff=True,
                )
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def recover_work_item(self, work_item_id: str, attempt_id: str, reason: str) -> dict[str, Any]:
        with self._lock:
            self._conn.execute("begin immediate")
            try:
                item = self.get_work_item(work_item_id)
                if not item:
                    raise ProjectStoreError("work item not found")
                if item["status"] not in {"claimed", "running", "blocked"}:
                    raise ProjectStoreError("work item is not recoverable")
                if item.get("attempt_id") != attempt_id:
                    raise ProjectStoreError("attempt_id does not own the work item")
                handoff = self.get_handoff(str(item.get("handoff_id") or ""))
                if handoff and handoff["status"] not in TERMINAL_HANDOFF_STATES:
                    raise ProjectStoreError("linked handoff is still active")
                payload = {"attempt_id": attempt_id, "reason": reason[:2000],
                           "handoff_id": item.get("handoff_id"), "worker_id": item.get("worker_id")}
                self._insert_event(item["project_id"], "work_item", work_item_id, "recovered", payload)
                self._insert_event(item["project_id"], "work_item", work_item_id, "queued", {})
                self._conn.commit()
                return self.get_work_item(work_item_id)  # type: ignore[return-value]
            except Exception:
                self._conn.rollback()
                raise

    def verify_local_provenance(self, project_id: str) -> dict[str, Any]:
        """Check local relational/event integrity without claiming cryptographic identity."""
        with self._lock:
            self._require_project(project_id)
            issues: list[dict[str, Any]] = []
            integrity = str(self._conn.execute("pragma integrity_check").fetchone()[0])
            if integrity != "ok":
                issues.append({"check": "sqlite_integrity", "severity": "failed", "detail": integrity})
            expected = {
                f"immutable_{table}_{operation}"
                for table in ("projects", "issues", "handoffs", "outputs", "proposals", "work_items", "project_events")
                for operation in ("update", "delete")
            }
            actual = {row[0] for row in self._conn.execute(
                "select name from sqlite_master where type='trigger'"
            ).fetchall()}
            for name in sorted(expected - actual):
                issues.append({"check": "immutability_trigger", "severity": "failed", "detail": f"missing {name}"})
            for handoff in self.list_handoffs(project_id):
                context = handoff.get("context") or {}
                parent_id = context.get("parent_handoff_id")
                if parent_id:
                    parent = self.get_handoff(str(parent_id))
                    if not parent or parent["project_id"] != project_id:
                        issues.append({"check": "parent_handoff", "severity": "failed",
                                       "handoff_id": handoff["id"], "detail": "missing same-project parent"})
                for output_id in context.get("source_output_ids") or []:
                    output = self.get_output(str(output_id))
                    source = self.get_handoff(str(output.get("handoff_id") or "")) if output else None
                    if not source or source["project_id"] != project_id:
                        issues.append({"check": "source_output", "severity": "failed",
                                       "handoff_id": handoff["id"], "detail": f"missing same-project output {output_id}"})
                if handoff["status"] in {"completed", "needs_revision"}:
                    terminal = self._latest_event("handoff", handoff["id"], TERMINAL_HANDOFF_STATES)
                    output_id = (terminal or {}).get("payload", {}).get("output_id")
                    output = self.get_output(str(output_id)) if output_id else None
                    if not output or output.get("handoff_id") != handoff["id"]:
                        issues.append({"check": "terminal_output", "severity": "failed",
                                       "handoff_id": handoff["id"], "detail": "terminal handoff output is missing"})
            return {"ok": not any(item["severity"] == "failed" for item in issues),
                    "integrity": integrity, "issues": issues}

    # -- outputs / proposals / events ------------------------------------

    def list_outputs(self, handoff_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "select * from outputs where handoff_id=? order by created_at,id",
                (handoff_id,),
            ).fetchall()
            return [self._output_row(row) for row in rows]

    def _output_row(self, row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["parsed"] = self._decode(out.pop("parsed_json"), None)
        proposal = self._conn.execute(
            "select id from proposals where output_id=?", (out["id"],)
        ).fetchone()
        out["proposal_id"] = proposal["id"] if proposal else None
        return out

    def get_output(self, output_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "select * from outputs where id=?", (output_id,)
            ).fetchone()
            return self._output_row(row) if row else None

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "select * from proposals where id=?", (proposal_id,)
            ).fetchone()
            if not row:
                return None
            out = dict(row)
            out["changed_paths"] = self._decode(out.pop("changed_paths_json"), [])
            apply_event = self._latest_event(
                "proposal", proposal_id,
                {"proposal_apply_started", "proposal_applied", "proposal_apply_failed",
                 "proposal_reconciled"},
            )
            out["apply_status"] = apply_event["kind"] if apply_event else "pending"
            return out

    def list_events(self, project_id: str, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        with self._lock:
            rows = self._conn.execute(
                "select * from project_events where project_id=? and seq>? "
                "order by seq limit ?", (project_id, int(after), bounded),
            ).fetchall()
            return [self._event_row(row) for row in rows]

    def append_project_event(
        self,
        project_id: str,
        entity_type: str,
        entity_id: str,
        kind: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        with self._lock, self._conn:
            self._require_project(project_id)
            return self._insert_event(
                project_id, entity_type, entity_id, kind, payload or {}
            )

    def recover_interrupted(self) -> None:
        with self._lock:
            self._conn.execute("begin immediate")
            try:
                rows = self._conn.execute("select * from handoffs order by created_at,id").fetchall()
                for row in rows:
                    handoff = self._handoff_row(row)
                    if handoff["status"] == "running":
                        self._insert_event(
                            handoff["project_id"], "handoff", handoff["id"],
                            "interrupted", {"reason": "runtime_recovery"},
                        )
                        self._block_work_for_handoff_locked(handoff, "interrupted")
                    elif handoff["status"] == "cancel_requested":
                        self._insert_event(
                            handoff["project_id"], "handoff", handoff["id"],
                            "cancelled", {"reason": "runtime_recovery"},
                        )
                        self._block_work_for_handoff_locked(handoff, "cancelled")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _require_project(self, project_id: str) -> None:
        row = self._conn.execute("select id from projects where id=?", (project_id,)).fetchone()
        if not row:
            raise ProjectStoreError("project not found")

    def _require_handoff(self, handoff_id: str) -> None:
        row = self._conn.execute("select id from handoffs where id=?", (handoff_id,)).fetchone()
        if not row:
            raise ProjectStoreError("handoff not found")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
