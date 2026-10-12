"""Durable settings and local key store for Litigation.

Litigation has no global billing mode: subscription vs API vs local models
are selected per conversation in the New-chat dialog. This store holds what
the settings page owns — API-key values in a user-owned file outside the
repository, non-secret local model overrides, and a first-run marker so a
fresh install can route to /setup exactly once.

Preference file (JSON, atomic writes):
    {"version": 1, "surface": "litigation", "configured_at": "...",
     "model_overrides": {"OLLAMA_MODEL": "llama3.1"}}

Key file (KEY=VALUE lines, 0600, atomic writes). Values are single-line, so
a value can never forge another entry. The module is deliberately
self-contained (no chat, room, or database coupling) so the same surface can
travel with Litigation when it is absorbed into another host such as SEAM.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_VERSION = 1
_SURFACE = "litigation"
_KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_MAX_OVERRIDE = 100


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

    # -- settings record ----------------------------------------------------

    def _read_record(self) -> dict[str, Any]:
        if not self.setup_path.exists():
            return {}
        try:
            data = json.loads(self.setup_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SetupStoreError(
                f"settings file is unreadable: {self.setup_path}") from exc
        return data if isinstance(data, dict) else {}

    def _write_record(self, updates: dict[str, Any]) -> dict[str, Any]:
        try:
            record = self._read_record()
        except SetupStoreError:
            record = {}  # a corrupt file is replaced by the next write
        record.setdefault("version", _VERSION)
        record.setdefault("surface", _SURFACE)
        record.update(updates)
        _atomic_write(self.setup_path,
                      (json.dumps(record, indent=2) + "\n").encode())
        return record

    def is_configured(self) -> bool:
        """False only on a fresh install that has never opened settings.

        Raises SetupStoreError for an unreadable file so the settings page
        can show it instead of silently treating it as a fresh install.
        """
        return bool(self._read_record())

    def mark_configured(self) -> dict[str, Any]:
        try:
            record = self._read_record()
        except SetupStoreError:
            record = {}  # a corrupt file is replaced by this write
        if "configured_at" not in record:
            return self._write_record(
                {"configured_at": datetime.now(timezone.utc).isoformat()})
        return record

    # -- local model overrides ------------------------------------------------

    def model_overrides(self) -> dict[str, str]:
        overrides = self._read_record().get("model_overrides") or {}
        return {str(k): str(v) for k, v in overrides.items()
                if _KEY_NAME.match(str(k))}

    def set_model_override(self, env_name: str, value: str | None) -> None:
        if not _KEY_NAME.match(env_name):
            raise SetupStoreError(f"invalid setting name: {env_name!r}")
        overrides = self.model_overrides()
        if value is None or value == "":
            overrides.pop(env_name, None)
        else:
            if len(value) > _MAX_OVERRIDE or any(
                    ch in value for ch in ("\n", "\r")):
                raise SetupStoreError("model overrides must be single-line and short")
            overrides[env_name] = value
        self._write_record({"model_overrides": overrides})

    # -- API keys -------------------------------------------------------------

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

    def load_overrides_into(self, env: dict[str, str] | None = None) -> list[str]:
        overrides = self.model_overrides()
        target = os.environ if env is None else env
        for name, value in overrides.items():
            target[name] = value
        return sorted(overrides)


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
