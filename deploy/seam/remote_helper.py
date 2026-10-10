#!/usr/bin/env python3
"""Narrow JSON-over-SSH adapter for a dedicated Litigation SEAM instance.

The helper intentionally exposes only the SEAM CLI operations Litigation has
verified.  It does not start a network listener and it never executes caller
supplied commands.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROTOCOL = "litigation-seam/1"
MAX_REQUEST_BYTES = 600_000
MAX_TEXT_BYTES = 240_000
MAX_OUTPUT_BYTES = 2_000_000
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,240}$")
NAMESPACE_RE = re.compile(r"^litigation\.project\.[A-Za-z0-9_.:-]{1,200}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _namespace_for_project(project_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", ".", project_id).strip(".")
    if not safe:
        raise ValueError("project id cannot form a namespace")
    return f"litigation.project.{safe[:200]}"


def _root() -> Path:
    configured = os.environ.get("LITIGATION_SEAM_REMOTE_ROOT")
    return Path(configured).expanduser() if configured else Path.home() / ".local/share/litigation-seam"


def _seam_bin(root: Path) -> Path:
    configured = os.environ.get("LITIGATION_SEAM_BIN")
    return Path(configured).expanduser() if configured else root / ".venv/bin/seam"


def _safe_env(root: Path) -> dict[str, str]:
    return {
        "HOME": str(Path.home()),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PATH": f"{root / '.venv/bin'}:/usr/local/bin:/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
    }


def _db(root: Path) -> sqlite3.Connection:
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    conn = sqlite3.connect(state / "bridge.sqlite3", timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA recursive_triggers=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS requests (
          idempotency_key TEXT PRIMARY KEY,
          payload_hash TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts (
          attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
          idempotency_key TEXT NOT NULL REFERENCES requests(idempotency_key),
          outcome TEXT NOT NULL,
          response_json TEXT,
          error_text TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS successes (
          idempotency_key TEXT PRIMARY KEY REFERENCES requests(idempotency_key),
          response_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS requests_no_update BEFORE UPDATE ON requests
          BEGIN SELECT RAISE(ABORT, 'requests are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS requests_no_delete BEFORE DELETE ON requests
          BEGIN SELECT RAISE(ABORT, 'requests are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS attempts_no_update BEFORE UPDATE ON attempts
          BEGIN SELECT RAISE(ABORT, 'attempts are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS attempts_no_delete BEFORE DELETE ON attempts
          BEGIN SELECT RAISE(ABORT, 'attempts are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS successes_no_update BEFORE UPDATE ON successes
          BEGIN SELECT RAISE(ABORT, 'successes are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS successes_no_delete BEFORE DELETE ON successes
          BEGIN SELECT RAISE(ABORT, 'successes are append-only'); END;
        """
    )
    return conn


def _run(root: Path, args: list[str], *, input_text: str | None = None) -> dict[str, Any]:
    seam = _seam_bin(root)
    if not seam.is_file():
        raise RuntimeError("SEAM is not installed in the dedicated Litigation environment")
    command = [str(seam), "--db", str(root / "state/seam.sqlite3"), *args]
    result = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=120,
        env=_safe_env(root),
        check=False,
    )
    if len(result.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise RuntimeError("SEAM returned an oversized response")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        tail = detail[-1][:500] if detail else "unknown SEAM failure"
        raise RuntimeError(f"SEAM command failed: {tail}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("SEAM did not return valid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("SEAM returned an unexpected JSON shape")
    return value


def _command_available(root: Path, args: list[str]) -> bool:
    seam = _seam_bin(root)
    if not seam.is_file() or not os.access(seam, os.X_OK):
        return False
    try:
        result = subprocess.run(
            [str(seam), *args, "--help"], text=True, capture_output=True,
            timeout=15, env=_safe_env(root), check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "usage:" in result.stdout


def _validate_request(request: dict[str, Any]) -> None:
    if request.get("protocol") != PROTOCOL:
        raise ValueError("unsupported protocol")
    operation = request.get("operation")
    if operation not in {"status", "ingest", "search", "graph"}:
        raise ValueError("unsupported operation")
    if operation == "status":
        return
    namespace = request.get("namespace")
    if not isinstance(namespace, str) or not NAMESPACE_RE.fullmatch(namespace):
        raise ValueError("invalid Litigation project namespace")
    if operation == "ingest":
        key = request.get("idempotency_key")
        if not isinstance(key, str) or not ID_RE.fullmatch(key):
            raise ValueError("invalid idempotency key")
        text = request.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("ingest text is required")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("ingest text is too large")
        for field in ("source_ref", "agent_id"):
            value = request.get(field)
            if not isinstance(value, str) or not ID_RE.fullmatch(value):
                raise ValueError(f"invalid {field}")
        provenance = request.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError("provenance must be an object")
        if len(_canonical(provenance).encode("utf-8")) > 16_000:
            raise ValueError("provenance is too large")
        required = {"project_id", "event_id", "event_kind", "entity_id", "sequence"}
        if not required.issubset(provenance):
            raise ValueError("provenance is incomplete")
        if not isinstance(provenance.get("sequence"), int) or provenance["sequence"] < 0:
            raise ValueError("provenance sequence is invalid")
        for field in required - {"sequence"}:
            if not isinstance(provenance.get(field), str) or not ID_RE.fullmatch(provenance[field]):
                raise ValueError(f"invalid provenance {field}")
        project_id = str(provenance["project_id"])
        event_id = str(provenance["event_id"])
        if namespace != _namespace_for_project(project_id):
            raise ValueError("namespace does not match provenance project")
        if request["source_ref"] != f"litigation:{project_id}:{event_id}":
            raise ValueError("source_ref does not match provenance event")
        expected_key = "litigation:" + hashlib.sha256(
            f"{project_id}:{event_id}".encode("utf-8")
        ).hexdigest()
        if request["idempotency_key"] != expected_key:
            raise ValueError("idempotency key does not match provenance event")
        if request["agent_id"] != "litigation":
            raise ValueError("agent_id must be litigation")
    else:
        query = request.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 4_000:
            raise ValueError("query is required and must be at most 4000 characters")
        limit = request.get("limit", 10)
        if not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")


def _status(root: Path) -> dict[str, Any]:
    seam = _seam_bin(root)
    installed = seam.is_file() and os.access(seam, os.X_OK)
    ingest_ok = installed and _command_available(root, ["ingest"])
    knowledge_ok = installed and _command_available(root, ["knowledge", "search"])
    return {
        "protocol": PROTOCOL,
        "ok": True,
        "operation": "status",
        "reachable": True,
        "installed": installed,
        "database_exists": (root / "state/seam.sqlite3").is_file(),
        "capabilities": {
            "ingest": "available" if ingest_ok else "unavailable",
            "knowledge_search": "available" if knowledge_ok else "unavailable",
            "knowledge_graph": "available" if knowledge_ok else "unavailable",
            "reasoning_graph": "not_configured",
            "semantic_embeddings": "not_configured",
        },
    }


def _ingest(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    lock_path = root / "state/ingest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            return _ingest_locked(root, request)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _ingest_locked(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    key = str(request["idempotency_key"])
    payload_hash = hashlib.sha256(_canonical(request).encode("utf-8")).hexdigest()
    conn = _db(root)
    try:
        row = conn.execute(
            "SELECT payload_hash FROM requests WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if row and row[0] != payload_hash:
            raise ValueError("idempotency key was reused with different content")
        conn.execute(
            "INSERT OR IGNORE INTO requests VALUES (?, ?, ?, ?)",
            (key, payload_hash, _canonical(request), _now()),
        )
        cached = conn.execute(
            "SELECT response_json FROM successes WHERE idempotency_key = ?", (key,)
        ).fetchone()
        conn.commit()
        if cached:
            response = json.loads(cached[0])
            response["deduplicated"] = True
            return response
        try:
            provenance_header = _canonical(request["provenance"])
            ingest_text = f"Litigation provenance: {provenance_header}\n\n{request['text']}"
            if len(ingest_text.encode("utf-8")) > MAX_TEXT_BYTES:
                raise ValueError("ingest text plus provenance is too large")
            seam_result = _run(
                root,
                [
                    "ingest", "-", "--persist",
                    "--source-ref", str(request["source_ref"]),
                    "--agent-id", str(request["agent_id"]),
                    "--ns", str(request["namespace"]),
                    "--scope", "project", "--format", "json",
                ],
                input_text=ingest_text,
            )
            response = {
                "protocol": PROTOCOL,
                "ok": True,
                "operation": "ingest",
                "idempotency_key": key,
                "namespace": request["namespace"],
                "deduplicated": False,
                "seam": seam_result,
            }
            encoded = _canonical(response)
            with conn:
                conn.execute(
                    "INSERT INTO attempts(idempotency_key,outcome,response_json,error_text,created_at) "
                    "VALUES (?, 'succeeded', ?, NULL, ?)", (key, encoded, _now()),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO successes VALUES (?, ?, ?)", (key, encoded, _now()),
                )
            return response
        except Exception as exc:
            with conn:
                conn.execute(
                    "INSERT INTO attempts(idempotency_key,outcome,response_json,error_text,created_at) "
                    "VALUES (?, 'failed', NULL, ?, ?)",
                    (key, f"{type(exc).__name__}: {str(exc)[:500]}", _now()),
                )
            raise
    finally:
        conn.close()


def _knowledge(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    operation = str(request["operation"])
    result = _run(
        root,
        [
            "knowledge", "search", str(request["query"]),
            "--namespace", str(request["namespace"]),
            "--scope", "project", "--limit", str(request.get("limit", 10)),
            "--hops", "2" if operation == "search" else "3", "--format", "json",
        ],
    )
    return {
        "protocol": PROTOCOL,
        "ok": True,
        "operation": operation,
        "namespace": request["namespace"],
        "seam": result,
    }


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("request is too large")
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        _validate_request(request)
        root = _root()
        if request["operation"] == "status":
            response = _status(root)
        elif request["operation"] == "ingest":
            response = _ingest(root, request)
        else:
            response = _knowledge(root, request)
    except Exception as exc:
        response = {
            "protocol": PROTOCOL,
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc)[:600],
        }
    sys.stdout.write(_canonical(response) + "\n")
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
