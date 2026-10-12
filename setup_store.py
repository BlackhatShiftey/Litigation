"""Durable setup preference and local key store for Litigation.

The store records how the user wants Litigation to authenticate inference —
subscription logins, API keys, or both — and holds API-key values in a
user-owned file outside the repository. It is deliberately self-contained
(no chat, room, or database coupling) so the same surface can travel with
Litigation when it is absorbed into another host such as SEAM.

Preference file (JSON, atomic writes):
    {"version": 1, "surface": "litigation", "auth_mode": "...", "chosen_at": "..."}

Key file (KEY=VALUE lines, 0600, atomic writes). Values are raw bytes on one
line; newlines are rejected so a value can never forge another entry.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODES = ("subscription", "api", "mixed")
UNSET = "unset"
_VERSION = 1
_SURFACE = "litigation"
_KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class SetupStoreError(RuntimeError):
    pass


def default_setup_path() -> Path:
    return Path(os.environ.get("LITIGATION_SETUP_PATH",
                               "~/.local/share/litigation/setup.json")).expanduser()


def default_keys_path() -> Path:
    return Path(os.environ.get("LITIGATION_KEYS_PATH",
                               "~/.config/litigation/keys.env")).expanduser()


class SetupStore:
    def __init__(self, setup_path: Path | None = None,
                 keys_path: Path | None = None) -> None:
        self.setup_path = setup_path or default_setup_path()
        self.keys_path = keys_path or default_keys_path()

    # -- preference ---------------------------------------------------------

    def auth_mode(self) -> str:
        """One of MODES, or UNSET when no choice has been made yet."""
        if not self.setup_path.exists():
            return UNSET
        try:
            data = json.loads(self.setup_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SetupStoreError(
                f"setup preference file is unreadable: {self.setup_path}") from exc
        mode = data.get("auth_mode")
        return mode if mode in MODES else UNSET

    def set_auth_mode(self, mode: str) -> dict[str, Any]:
        if mode not in MODES:
            raise SetupStoreError(f"unknown auth mode: {mode!r}")
        record = {
            "version": _VERSION,
            "surface": _SURFACE,
            "auth_mode": mode,
            "chosen_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write(self.setup_path, (json.dumps(record, indent=2) + "\n").encode())
        return record

    # -- API keys -----------------------------------------------------------

    def read_keys(self) -> dict[str, str]:
        if not self.keys_path.exists():
            return {}
        keys: dict[str, str] = {}
        try:
            lines = self.keys_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SetupStoreError(
                f"key file is unreadable: {self.keys_path}") from exc
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if _KEY_NAME.match(name):
                keys[name] = value
        return keys

    def write_keys(self, updates: dict[str, str | None]) -> dict[str, bool]:
        """Merge updates into the key file; None or "" removes an entry.

        Returns the resulting set/unset state for every requested name.
        """
        keys = self.read_keys()
        for name, value in updates.items():
            if not _KEY_NAME.match(name):
                raise SetupStoreError(f"invalid key name: {name!r}")
            if value is None or value == "":
                keys.pop(name, None)
            else:
                if any(ch in value for ch in ("\n", "\r")):
                    raise SetupStoreError("key values must be single-line")
                keys[name] = value
        blob = "".join(f"{name}={keys[name]}\n" for name in sorted(keys))
        _atomic_write(self.keys_path, blob.encode(), private=True)
        return {name: bool(keys.get(name)) for name in updates}

    def apply_to_environ(self, updates: dict[str, str | None],
                         env: dict[str, str] | None = None) -> None:
        """Apply cleared/supplied values to the environment immediately."""
        target = os.environ if env is None else env
        for name, value in updates.items():
            if value:
                target[name] = value
            else:
                target.pop(name, None)

    def load_keys_into(self, env: dict[str, str] | None = None) -> list[str]:
        """Load every stored key into the environment (overriding)."""
        keys = self.read_keys()
        target = os.environ if env is None else env
        for name, value in keys.items():
            target[name] = value
        return sorted(keys)


def _atomic_write(path: Path, payload: bytes, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if private:
        try:
            current_mode = path.stat().st_mode & 0o777
        except OSError:
            current_mode = 0o600
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, current_mode if private else 0o644)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
