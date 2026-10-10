"""Durable local bridge to Litigation's dedicated remote SEAM instance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


PROTOCOL = "litigation-seam/1"
DEFAULT_TARGET = "Ex0@100.126.138.102"
MAX_TEXT_BYTES = 220_000
MAX_PAYLOAD_BYTES = 600_000
TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+$")
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,240}$")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|secret[_-]?key|password)\b"
        r"\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"
    ),
)


class SeamBridgeError(RuntimeError):
    """Safe bridge error."""


class SeamSecretRejected(SeamBridgeError):
    """Raised after a safe, content-free rejection receipt is stored."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _contains_secret(value: Any) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in SECRET_PATTERNS)
    if isinstance(value, dict):
        return any(_contains_secret(key) or _contains_secret(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item) for item in value)
    return False


def project_namespace(project_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", ".", project_id).strip(".")
    if not safe:
        raise SeamBridgeError("project id cannot form a SEAM namespace")
    return f"litigation.project.{safe[:200]}"


class SeamBridge:
    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        ssh_target: str | None = None,
        ssh_binary: str = "ssh",
        remote_command: str = "exec $HOME/.local/share/litigation-seam/remote-helper",
        timeout: float = 45.0,
    ) -> None:
        self.state_dir = Path(
            state_dir
            or os.environ.get("LITIGATION_SEAM_STATE_DIR")
            or Path.home() / ".local/share/litigation/seam"
        ).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        self.ssh_target = ssh_target or os.environ.get("LITIGATION_SEAM_SSH_TARGET", DEFAULT_TARGET)
        if not TARGET_RE.fullmatch(self.ssh_target):
            raise SeamBridgeError("invalid SEAM SSH target")
        self.ssh_binary = ssh_binary
        self.remote_command = remote_command
        self.timeout = timeout
        self._active_lock = threading.Lock()
        self._active: set[subprocess.Popen[str]] = set()
        self._shutdown = threading.Event()
        self.db_path = self.state_dir / "outbox.sqlite3"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA recursive_triggers=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS outbox_items (
                  item_id TEXT PRIMARY KEY,
                  event_key TEXT UNIQUE NOT NULL,
                  project_id TEXT NOT NULL,
                  event_id TEXT NOT NULL,
                  payload_hash TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS delivery_attempts (
                  attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  item_id TEXT NOT NULL REFERENCES outbox_items(item_id),
                  outcome TEXT NOT NULL,
                  response_json TEXT,
                  error_code TEXT,
                  error_text TEXT,
                  created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                  item_id TEXT PRIMARY KEY REFERENCES outbox_items(item_id),
                  response_json TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rejection_receipts (
                  event_key TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  event_id TEXT NOT NULL,
                  content_sha256 TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS outbox_items_no_update BEFORE UPDATE ON outbox_items
                  BEGIN SELECT RAISE(ABORT, 'outbox is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS outbox_items_no_delete BEFORE DELETE ON outbox_items
                  BEGIN SELECT RAISE(ABORT, 'outbox is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS delivery_attempts_no_update BEFORE UPDATE ON delivery_attempts
                  BEGIN SELECT RAISE(ABORT, 'attempt history is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS delivery_attempts_no_delete BEFORE DELETE ON delivery_attempts
                  BEGIN SELECT RAISE(ABORT, 'attempt history is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS deliveries_no_update BEFORE UPDATE ON deliveries
                  BEGIN SELECT RAISE(ABORT, 'delivery receipts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS deliveries_no_delete BEFORE DELETE ON deliveries
                  BEGIN SELECT RAISE(ABORT, 'delivery receipts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS rejection_receipts_no_update BEFORE UPDATE ON rejection_receipts
                  BEGIN SELECT RAISE(ABORT, 'rejection receipts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS rejection_receipts_no_delete BEFORE DELETE ON rejection_receipts
                  BEGIN SELECT RAISE(ABORT, 'rejection receipts are append-only'); END;
                """
            )

    def _remote(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._shutdown.is_set():
            raise SeamBridgeError("SEAM bridge is shutting down")
        encoded = _canonical(payload)
        if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise SeamBridgeError("SEAM request exceeds the bridge limit")
        command = [
            self.ssh_binary,
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
            self.ssh_target,
            self.remote_command,
        ]
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
            with self._active_lock:
                self._active.add(process)
            stdout, _stderr = process.communicate(encoded, timeout=self.timeout)
            returncode = process.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            raise SeamBridgeError("the Alpine SEAM instance is unreachable") from exc
        finally:
            if process is not None:
                with self._active_lock:
                    self._active.discard(process)
        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise SeamBridgeError("the Alpine SEAM helper returned invalid JSON") from exc
        if not isinstance(response, dict) or response.get("protocol") != PROTOCOL:
            raise SeamBridgeError("the Alpine SEAM helper returned an invalid protocol response")
        if returncode != 0 or not response.get("ok"):
            detail = str(response.get("detail") or "remote SEAM operation failed")[:600]
            raise SeamBridgeError(detail)
        return response

    def cancel_active(self) -> None:
        """Terminate active SSH transports during a bounded server shutdown."""
        with self._active_lock:
            active = list(self._active)
        for process in active:
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 2.0
        for process in active:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()

    def begin_shutdown(self) -> None:
        self._shutdown.set()
        self.cancel_active()

    def resume(self) -> None:
        self._shutdown.clear()

    def status(self) -> dict[str, Any]:
        try:
            response = self._remote({"protocol": PROTOCOL, "operation": "status"})
        except SeamBridgeError as exc:
            return {
                "protocol": PROTOCOL, "ok": False, "reachable": False,
                "installed": False, "detail": str(exc),
                "capabilities": {
                    "ingest": "unknown", "knowledge_search": "unknown",
                    "knowledge_graph": "unknown", "reasoning_graph": "not_configured",
                    "semantic_embeddings": "not_configured",
                },
            }
        response["outbox"] = self.outbox_counts()
        return response

    def _rejection(self, project_id: str, event_id: str, value: Any, reason: str) -> None:
        event_key = f"{project_id}:{event_id}"
        digest = hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO rejection_receipts VALUES (?,?,?,?,?,?)",
                (event_key, project_id, event_id, digest, reason, time.time()),
            )

    def reject_if_unsafe(self, project_id: str, event_id: str, value: Any) -> None:
        """Record a content-free receipt and reject before content is chunked."""
        if _contains_secret(value):
            self._rejection(project_id, event_id, value, "secret_shaped")
            raise SeamSecretRejected(
                "event was retained locally as a content-free secret rejection receipt"
            )

    def enqueue_event(
        self,
        project_id: str,
        event_id: str,
        kind: str,
        entity_id: str,
        sequence: int,
        text: str,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        for label, value in (("project_id", project_id), ("event_id", event_id),
                             ("kind", kind), ("entity_id", entity_id)):
            if not isinstance(value, str) or not ID_RE.fullmatch(value):
                raise SeamBridgeError(f"invalid {label}")
        if not isinstance(sequence, int) or sequence < 0:
            raise SeamBridgeError("sequence must be a non-negative integer")
        if not isinstance(text, str) or not text.strip():
            raise SeamBridgeError("SEAM event text is required")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            self._rejection(project_id, event_id, {"text": text}, "oversized")
            raise SeamSecretRejected("event was retained locally as an oversized rejection receipt")
        provenance = provenance or {}
        if not isinstance(provenance, dict) or len(_canonical(provenance).encode("utf-8")) > 16_000:
            raise SeamBridgeError("SEAM provenance must be an object no larger than 16 KiB")
        content = {"text": text, "provenance": provenance}
        self.reject_if_unsafe(project_id, event_id, content)
        namespace = project_namespace(project_id)
        event_key = f"{project_id}:{event_id}"
        key_digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()
        payload = {
            "protocol": PROTOCOL,
            "operation": "ingest",
            "idempotency_key": f"litigation:{key_digest}",
            "namespace": namespace,
            "source_ref": f"litigation:{project_id}:{event_id}",
            "agent_id": "litigation",
            "text": text,
            "provenance": {
                "project_id": project_id, "event_id": event_id, "event_kind": kind,
                "entity_id": entity_id, "sequence": sequence, **provenance,
            },
        }
        payload_json = _canonical(payload)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        item_id = f"seam_{payload_hash}"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_hash FROM outbox_items WHERE event_key=?", (event_key,)
            ).fetchone()
            if row and row[0] != payload_hash:
                raise SeamBridgeError("event id was reused with different SEAM content")
            conn.execute(
                "INSERT OR IGNORE INTO outbox_items VALUES (?,?,?,?,?,?,?)",
                (item_id, event_key, project_id, event_id, payload_hash, payload_json, time.time()),
            )
        return {"item_id": item_id, "event_key": event_key, "queued": True}

    def deliver(self, item_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            delivered = conn.execute(
                "SELECT response_json FROM deliveries WHERE item_id=?", (item_id,)
            ).fetchone()
            if delivered:
                return json.loads(delivered[0])
            row = conn.execute(
                "SELECT payload_json FROM outbox_items WHERE item_id=?", (item_id,)
            ).fetchone()
            if not row:
                raise SeamBridgeError("SEAM outbox item not found")
            payload = json.loads(row[0])
        try:
            response = self._remote(payload)
        except Exception as exc:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO delivery_attempts(item_id,outcome,response_json,error_code,error_text,created_at) "
                    "VALUES (?, 'failed', NULL, ?, ?, ?)",
                    (item_id, type(exc).__name__, str(exc)[:600], time.time()),
                )
            raise
        encoded = _canonical(response)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO delivery_attempts(item_id,outcome,response_json,error_code,error_text,created_at) "
                "VALUES (?, 'succeeded', ?, NULL, NULL, ?)",
                (item_id, encoded, time.time()),
            )
            conn.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?)",
                (item_id, encoded, time.time()),
            )
        return response

    def publish_event(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        queued = self.enqueue_event(*args, **kwargs)
        response = self.deliver(str(queued["item_id"]))
        return {**queued, "delivered": True, "remote": response}

    def retry_pending(self, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT o.item_id FROM outbox_items o LEFT JOIN deliveries d ON d.item_id=o.item_id "
                "WHERE d.item_id IS NULL ORDER BY o.created_at,o.item_id LIMIT ?", (limit,)
            ).fetchall()
        succeeded = 0
        failed = 0
        for row in rows:
            if self._shutdown.is_set():
                break
            try:
                self.deliver(str(row[0]))
                succeeded += 1
            except SeamBridgeError:
                failed += 1
        return {"attempted": len(rows), "succeeded": succeeded, "failed": failed,
                "counts": self.outbox_counts()}

    def outbox_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            total = int(conn.execute("SELECT count(*) FROM outbox_items").fetchone()[0])
            delivered = int(conn.execute("SELECT count(*) FROM deliveries").fetchone()[0])
            rejected = int(conn.execute("SELECT count(*) FROM rejection_receipts").fetchone()[0])
        return {"total": total, "delivered": delivered, "pending": total - delivered,
                "rejected": rejected}

    def list_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT o.item_id,o.event_key,o.project_id,o.event_id,o.created_at,"
                "CASE WHEN d.item_id IS NULL THEN 'pending' ELSE 'delivered' END AS status,"
                "(SELECT count(*) FROM delivery_attempts a WHERE a.item_id=o.item_id) attempts "
                "FROM outbox_items o LEFT JOIN deliveries d ON d.item_id=o.item_id "
                "ORDER BY o.created_at DESC,o.item_id DESC LIMIT ?", (limit,),
            ).fetchall()
            items = [dict(row) for row in rows]
            rejected = conn.execute(
                "SELECT event_key,project_id,event_id,reason,created_at FROM rejection_receipts "
                "ORDER BY created_at DESC,event_key DESC LIMIT ?", (limit,),
            ).fetchall()
            items.extend({**dict(row), "status": "rejected", "attempts": 0} for row in rejected)
        return sorted(items, key=lambda item: float(item["created_at"]), reverse=True)[:limit]

    def search(self, project_id: str, query: str, limit: int = 10) -> dict[str, Any]:
        return self._knowledge("search", project_id, query, limit)

    def graph(self, project_id: str, query: str, limit: int = 10) -> dict[str, Any]:
        return self._knowledge("graph", project_id, query, limit)

    def _knowledge(self, operation: str, project_id: str, query: str, limit: int) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 4_000:
            raise SeamBridgeError("query is required and must be at most 4000 characters")
        limit = max(1, min(int(limit), 50))
        return self._remote({
            "protocol": PROTOCOL, "operation": operation,
            "namespace": project_namespace(project_id), "query": query.strip(), "limit": limit,
        })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Litigation Alpine SEAM bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    retry = sub.add_parser("retry")
    retry.add_argument("--limit", type=int, default=50)
    outbox = sub.add_parser("outbox")
    outbox.add_argument("--limit", type=int, default=100)
    for name in ("search", "graph"):
        command = sub.add_parser(name)
        command.add_argument("project_id")
        command.add_argument("query")
        command.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)
    bridge = SeamBridge()
    if args.command == "status":
        result: Any = bridge.status()
    elif args.command == "retry":
        result = bridge.retry_pending(args.limit)
    elif args.command == "outbox":
        result = {"items": bridge.list_outbox(args.limit), "counts": bridge.outbox_counts()}
    else:
        result = getattr(bridge, args.command)(args.project_id, args.query, args.limit)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
