"""FastAPI routes for Litigation's provider settings page.

Litigation has no global billing mode — subscription, API, and local models
are selected per conversation in the New-chat dialog. This surface owns
provider configuration: subscription login guidance and checks, pay-as-you-go
API keys, and local model servers (Ollama, LM Studio) with their model
overrides. Everything lives behind /setup and /api/setup so the surface can
be re-hosted (e.g. absorbed by SEAM) without chat/room coupling.

Security posture: key values arrive over the loopback-only server, are
written to a 0600 user file, applied to the process environment, and are
never echoed back — responses carry set/unset booleans only.
"""
from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import providers
from setup_store import SetupStore, SetupStoreError

# GLM's subscription route authenticates with this credential instead of a
# provider CLI login, so it is presented inside the subscription section.
_SUBSCRIPTION_CREDENTIAL_LABELS = {"ZAI_API_KEY": "GLM · Z.ai Coding Plan credential"}
_LOCAL_PROBE_TIMEOUT = 0.4


class KeysBody(BaseModel):
    keys: dict[str, str]


class LocalModelBody(BaseModel):
    model: str


def _subscription_ids() -> list[str]:
    return [str(e["id"]) for e in providers.registry()
            if e.get("billing") == "subscription" and e["id"] != "antigravity"]


def _local_entries() -> list[dict[str, Any]]:
    return [e for e in providers.registry()
            if e.get("api") != "cli" and not e.get("env_key")]


def _reachable(base_url: str) -> bool:
    try:
        parsed = urlparse(base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=_LOCAL_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def create_setup_router(store: SetupStore) -> APIRouter:
    router = APIRouter()
    base = Path(__file__).parent

    @router.get("/setup", include_in_schema=False)
    def setup_page() -> FileResponse:
        return FileResponse(base / "setup.html")

    @router.get("/setup.js", include_in_schema=False)
    def setup_script() -> FileResponse:
        return FileResponse(base / "setup.js", media_type="text/javascript")

    @router.get("/setup.css", include_in_schema=False)
    def setup_styles() -> FileResponse:
        return FileResponse(base / "setup.css", media_type="text/css")

    @router.get("/api/setup")
    def get_setup() -> dict[str, Any]:
        store_error = ""
        try:
            configured = store.is_configured()
            overrides = store.model_overrides()
        except SetupStoreError:
            configured = False
            overrides = {}
            store_error = "settings file is unreadable; saving replaces it"
        api_keys: list[dict[str, Any]] = []
        credential_names: set[str] = set()
        local_servers: list[dict[str, Any]] = []
        for entry in providers.registry():
            if entry.get("api") == "cli":
                credential_names.update(entry.get("required_env", []))
                continue
            env_key = entry.get("env_key", "")
            if env_key:
                api_keys.append({
                    "env_key": env_key,
                    "label": str(entry.get("label", env_key)),
                    "set": bool(os.environ.get(env_key, "").strip()),
                })
            else:
                model_env = str(entry.get("model_env", ""))
                local_servers.append({
                    "id": str(entry["id"]),
                    "label": str(entry.get("label", entry["id"])),
                    "base_url": str(entry.get("base_url", "")),
                    "default_model": str(entry.get("default_model", "")),
                    "model_env": model_env,
                    "model": overrides.get(model_env, "")
                              or os.environ.get(model_env, "").strip()
                              or str(entry.get("default_model", "")),
                    "reachable": _reachable(str(entry.get("base_url", ""))),
                })
        return {
            "first_run": not configured,
            "configured": configured,
            "subscription_ids": _subscription_ids(),
            "api_keys": api_keys,
            "subscription_credentials": [
                {"env_key": name,
                 "label": _SUBSCRIPTION_CREDENTIAL_LABELS.get(name, name),
                 "set": bool(os.environ.get(name, "").strip())}
                for name in sorted(credential_names)
            ],
            "local_servers": local_servers,
            **({"store_error": store_error} if store_error else {}),
        }

    @router.post("/api/setup/dismiss")
    def dismiss_first_run() -> dict[str, Any]:
        """Record that settings were visited; stops the one-time redirect."""
        try:
            store.mark_configured()
        except SetupStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"configured": True}

    @router.post("/api/setup/keys")
    def set_keys(body: KeysBody) -> dict[str, Any]:
        allowed: set[str] = set()
        for entry in providers.registry():
            if entry.get("api") == "cli":
                allowed.update(entry.get("required_env", []))
            elif entry.get("env_key"):
                allowed.add(str(entry["env_key"]))
        unknown = sorted(set(body.keys) - allowed)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown key name(s): {', '.join(unknown)}")
        updates = {name: value.strip() for name, value in body.keys.items()}
        try:
            state = store.write_keys(updates)
            store.apply_to_environ(updates)
            store.mark_configured()
        except SetupStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"keys": state}

    @router.delete("/api/setup/keys/{env_key}")
    def clear_key(env_key: str) -> dict[str, Any]:
        allowed = {str(e["env_key"]) for e in providers.registry()
                   if e.get("api") != "cli" and e.get("env_key")}
        allowed.update(name for e in providers.registry()
                       if e.get("api") == "cli" for name in e.get("required_env", []))
        if env_key not in allowed:
            raise HTTPException(status_code=404, detail="unknown key name")
        try:
            state = store.write_keys({env_key: None})
            store.apply_to_environ({env_key: None})
        except SetupStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"env_key": env_key, "set": state.get(env_key, False)}

    @router.post("/api/setup/local/{server_id}")
    def set_local_model(server_id: str, body: LocalModelBody) -> dict[str, Any]:
        entry = next((e for e in _local_entries() if e["id"] == server_id), None)
        if not entry:
            raise HTTPException(status_code=404, detail="unknown local server")
        model_env = str(entry.get("model_env", ""))
        if not model_env:
            raise HTTPException(status_code=400,
                                detail="this server has no model override")
        model = body.model.strip()
        try:
            store.set_model_override(model_env, model or None)
            store.apply_to_environ({model_env: model or None})
            store.mark_configured()
        except SetupStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": server_id, "model_env": model_env,
                "model": store.model_overrides().get(model_env, "")}

    return router
