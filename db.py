"""SQLite persistence for Litigation rooms and messages."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any


class Database:
    def __init__(self, path: str = "litigation.db") -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("pragma journal_mode=wal")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                create table if not exists rooms (
                    id text primary key,
                    title text not null,
                    participants text not null,
                    crosstalk integer not null default 0,
                    workspace text,
                    created_at real not null
                );
                create table if not exists messages (
                    id text primary key,
                    room_id text not null,
                    sender text not null,
                    role text not null,
                    content text not null,
                    raw text,
                    created_at real not null
                );
                create index if not exists idx_messages_room on messages(room_id, created_at);
                """
            )
            # migration for databases created before workspaces existed
            cols = {r[1] for r in self._conn.execute("pragma table_info(rooms)")}
            if "workspace" not in cols:
                self._conn.execute("alter table rooms add column workspace text")

    # -- rooms --------------------------------------------------------------

    def create_room(self, title: str, participants: list[dict[str, Any]],
                    crosstalk: bool = False, workspace: str | None = None) -> dict[str, Any]:
        room_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "insert into rooms (id, title, participants, crosstalk, workspace, created_at)"
                " values (?,?,?,?,?,?)",
                (room_id, title, json.dumps(participants), int(crosstalk), workspace, now),
            )
        return {"id": room_id, "title": title, "participants": participants,
                "crosstalk": crosstalk, "workspace": workspace, "created_at": now}

    def list_rooms(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "select * from rooms order by created_at desc").fetchall()
        return [self._room_row(r) for r in rows]

    def get_room(self, room_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "select * from rooms where id = ?", (room_id,)).fetchone()
        return self._room_row(row) if row else None

    def update_room(self, room_id: str, **fields: Any) -> None:
        allowed = {"title": "title", "crosstalk": "crosstalk",
                   "participants": "participants", "workspace": "workspace"}
        sets, vals = [], []
        for key, col in allowed.items():
            if key in fields:
                sets.append(f"{col} = ?")
                val = fields[key]
                if key == "participants":
                    val = json.dumps(val)
                if key == "crosstalk":
                    val = int(bool(val))
                vals.append(val)
        if not sets:
            return
        vals.append(room_id)
        with self._lock, self._conn:
            self._conn.execute(f"update rooms set {', '.join(sets)} where id = ?", vals)

    def delete_room(self, room_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("delete from messages where room_id = ?", (room_id,))
            self._conn.execute("delete from rooms where id = ?", (room_id,))

    @staticmethod
    def _room_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "participants": json.loads(row["participants"]),
            "crosstalk": bool(row["crosstalk"]),
            "workspace": row["workspace"] if "workspace" in row.keys() else None,
            "created_at": row["created_at"],
        }

    # -- messages -----------------------------------------------------------

    def add_message(self, room_id: str, sender: str, role: str, content: str,
                    raw: str | None = None) -> dict[str, Any]:
        msg_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "insert into messages (id, room_id, sender, role, content, raw, created_at)"
                " values (?,?,?,?,?,?,?)",
                (msg_id, room_id, sender, role, content, raw, now),
            )
        return {"id": msg_id, "room_id": room_id, "sender": sender, "role": role,
                "content": content, "raw": raw, "created_at": now}

    def list_messages(self, room_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "select * from (select * from messages where room_id = ?"
                " order by created_at desc limit ?) order by created_at asc",
                (room_id, limit)).fetchall()
        return [dict(r) for r in rows]
