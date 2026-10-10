"""Async, restart-safe synchronization from project history to SEAM."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import time
from typing import Any

from seam_bridge import SeamBridge, SeamSecretRejected


TERMINAL_HANDOFFS = {"completed", "needs_revision", "failed", "interrupted", "cancelled"}
MAX_CHUNK_BYTES = 196_000


def _chunks(text: str, limit: int = MAX_CHUNK_BYTES) -> list[str]:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(raw):
        end = min(start + limit, len(raw))
        while end > start:
            try:
                piece = raw[start:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        if end == start:
            raise ValueError("unable to split SEAM event as UTF-8")
        chunks.append(piece)
        start = end
    return chunks


class SeamSyncCoordinator:
    """Copies immutable project events into the bridge's durable outbox."""

    def __init__(
        self, store: Any, bridge: SeamBridge, *, poll_interval: float = 5.0,
        enabled: bool | None = None,
    ) -> None:
        self.store = store
        self.bridge = bridge
        self.poll_interval = max(0.25, float(poll_interval))
        if enabled is None:
            enabled = os.environ.get("LITIGATION_SEAM_SYNC_ENABLED", "1").strip().lower() not in {
                "0", "false", "no", "off",
            }
        self.enabled = bool(enabled)
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._sync_lock = asyncio.Lock()
        self._last_error: str | None = None
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.bridge.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA recursive_triggers=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sync_checkpoints (
                  checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  project_id TEXT NOT NULL,
                  event_seq INTEGER NOT NULL,
                  created_at REAL NOT NULL,
                  UNIQUE(project_id,event_seq)
                );
                CREATE TABLE IF NOT EXISTS sync_runs (
                  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  outcome TEXT NOT NULL,
                  detail TEXT,
                  created_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS sync_checkpoints_no_update
                  BEFORE UPDATE ON sync_checkpoints
                  BEGIN SELECT RAISE(ABORT, 'sync checkpoints are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS sync_checkpoints_no_delete
                  BEFORE DELETE ON sync_checkpoints
                  BEGIN SELECT RAISE(ABORT, 'sync checkpoints are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS sync_runs_no_update BEFORE UPDATE ON sync_runs
                  BEGIN SELECT RAISE(ABORT, 'sync runs are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS sync_runs_no_delete BEFORE DELETE ON sync_runs
                  BEGIN SELECT RAISE(ABORT, 'sync runs are append-only'); END;
                """
            )

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self.bridge.resume()
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="litigation-seam-sync")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            await asyncio.to_thread(self.bridge.begin_shutdown)
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None

    def notify(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self.sync_once()
                self._last_error = None
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    def _cursor(self, project_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT max(event_seq) FROM sync_checkpoints WHERE project_id=?", (project_id,)
            ).fetchone()
        return int(row[0] or 0)

    def _checkpoint(self, project_id: str, seq: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sync_checkpoints(project_id,event_seq,created_at) VALUES (?,?,?)",
                (project_id, int(seq), time.time()),
            )

    @staticmethod
    def _eligible(event: dict[str, Any]) -> bool:
        entity_type = str(event.get("entity_type") or "")
        kind = str(event.get("kind") or "")
        return (
            entity_type in {"issue", "proposal", "debate"}
            or (entity_type == "handoff" and kind in TERMINAL_HANDOFFS)
            or (entity_type == "project" and kind in {"created", "source_import_report"})
        )

    def _render_event(self, project: dict[str, Any], event: dict[str, Any]) -> str:
        project_id = str(project["id"])
        entity_type = str(event["entity_type"])
        entity_id = str(event["entity_id"])
        kind = str(event["kind"])
        lines = [
            f"Litigation project: {project.get('name', project_id)}",
            f"Project id: {project_id}",
            f"Event sequence: {event['seq']}",
            f"Event: {entity_type}/{kind}",
            f"Entity id: {entity_id}",
        ]
        if entity_type == "project":
            lines.extend([
                f"Description: {project.get('description', '')}",
                f"Source: {project.get('source_description', '')}",
            ])
        elif entity_type == "issue":
            issue = next(
                (item for item in self.store.list_issues(project_id) if item["id"] == entity_id),
                None,
            )
            if issue:
                lines.extend([
                    f"Issue title: {issue.get('title', '')}",
                    f"Issue body: {issue.get('body', '')}",
                ])
        elif entity_type == "handoff":
            handoff = self.store.get_handoff(entity_id)
            if handoff:
                lines.extend([
                    f"Agent: {handoff.get('agent_id', '')}",
                    f"Model: {handoff.get('model', '')}",
                    f"Mode: {handoff.get('mode', '')}",
                    f"Instructions: {handoff.get('instructions', '')}",
                    "Acceptance: " + json.dumps(handoff.get("acceptance") or [], ensure_ascii=False),
                ])
                for output in self.store.list_outputs(entity_id):
                    lines.extend([
                        f"Output id: {output.get('id', '')}",
                        f"Output state: {output.get('final_state', '')}",
                        f"Output: {output.get('raw_text', '')}",
                    ])
        elif entity_type == "proposal":
            proposal = self.store.get_proposal(entity_id)
            if proposal:
                lines.extend([
                    f"Proposal summary: {proposal.get('summary', '')}",
                    f"Branch: {proposal.get('branch', '')}",
                    f"Commit: {proposal.get('commit_sha', '')}",
                    "Changed paths: " + json.dumps(proposal.get("changed_paths") or [], ensure_ascii=False),
                ])
        payload = event.get("payload") or {}
        if payload:
            lines.append("Event payload: " + json.dumps(payload, sort_keys=True, ensure_ascii=False))
        return "\n".join(lines).strip()

    def _enqueue_event(
        self, project: dict[str, Any], event: dict[str, Any]
    ) -> tuple[int, int]:
        if not self._eligible(event):
            return (0, 0)
        text = self._render_event(project, event)
        base_event_id = f"event.{event['seq']}"
        try:
            self.bridge.reject_if_unsafe(str(project["id"]), base_event_id, text)
        except SeamSecretRejected:
            return (0, 1)
        parts = _chunks(text)
        queued = 0
        rejected = 0
        for index, part in enumerate(parts, start=1):
            event_id = base_event_id if len(parts) == 1 else f"{base_event_id}.part.{index}.of.{len(parts)}"
            provenance = {
                "entity_type": event["entity_type"],
                "created_at": event.get("created_at"),
                "part": index,
                "parts": len(parts),
            }
            try:
                self.bridge.enqueue_event(
                    str(project["id"]), event_id, str(event["kind"]),
                    str(event["entity_id"]), int(event["seq"]), part, provenance,
                )
                queued += 1
            except SeamSecretRejected:
                rejected += 1
        return queued, rejected

    def _scan_sync(self) -> dict[str, int]:
        totals = {"events_scanned": 0, "items_queued": 0, "items_rejected": 0}
        for project in self.store.list_projects():
            project_id = str(project["id"])
            cursor = self._cursor(project_id)
            while True:
                events = self.store.list_events(project_id, after=cursor, limit=500)
                if not events:
                    break
                for event in events:
                    queued, rejected = self._enqueue_event(project, event)
                    self._checkpoint(project_id, int(event["seq"]))
                    cursor = int(event["seq"])
                    totals["events_scanned"] += 1
                    totals["items_queued"] += queued
                    totals["items_rejected"] += rejected
                if len(events) < 500:
                    break
        return totals

    def _record_run(self, outcome: str, detail: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_runs(outcome,detail,created_at) VALUES (?,?,?)",
                (outcome, detail[:500] if detail else None, time.time()),
            )

    def status(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT outcome,detail,created_at FROM sync_runs ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
        return {
            "enabled": self.enabled,
            "running": self._task is not None and not self._task.done(),
            "last_error": self._last_error,
            "last_run": dict(row) if row else None,
        }

    async def sync_once(self) -> dict[str, Any]:
        if not self.enabled:
            counts = await asyncio.to_thread(self.bridge.outbox_counts)
            return {"enabled": False, "events_scanned": 0, "items_queued": 0,
                    "items_rejected": 0, "delivery": counts}
        async with self._sync_lock:
            try:
                scan = await asyncio.to_thread(self._scan_sync)
                delivery = await asyncio.to_thread(self.bridge.retry_pending, 100)
                result = {**scan, "delivery": delivery, "enabled": True}
                await asyncio.to_thread(self._record_run, "succeeded", json.dumps(result))
                self._last_error = None
                return result
            except Exception as exc:
                detail = f"{type(exc).__name__}: {str(exc)[:500]}"
                self._last_error = detail
                await asyncio.to_thread(self._record_run, "failed", detail)
                raise
