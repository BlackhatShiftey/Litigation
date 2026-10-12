"""FastAPI routes for Litigation's install-time and runtime auth-mode choice.

Self-contained on purpose: the page, the flip endpoint the shared navigation
uses, and the local key writer all live behind /setup and /api/setup so the
surface can be re-hosted (e.g. absorbed by SEAM) without chat/room coupling.

Security posture: key values arrive over the loopback-only server, are written
to a 0600 user file, applied to the process environment, and are never echoed
back — responses carry set/unset booleans only.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import providers
from setup_store import MODES, SetupStore, SetupStoreError

# GLM's subscription route authenticates with this credential instead of a
# provider CLI login, so it is presented inside the subscription card.
_SUBSCRIPTION_CREDENTIAL_LABELS = {"ZAI_API_KEY": "GLM · Z.ai Coding Plan credential"}


class ModeBody(BaseModel):
    mode: Literal["subscription", "api", "mixed"]


class KeysBody(BaseModel):
    keys: dict[str, str] = Field(min_length=1)


def _known_env_names() -> tuple[set[str], set[str]]:
    """(API key names, subscription credential names) from the registry."""
    api_keys: set[str] = set()
    credentials: set[str] = set()
    for entry in providers.registry():
        if entry.get("api") == "cli":
            credentials.update(entry.get("required_env", []))
        else:
            name = entry.get("env_key", "")
            if name:
                api_keys.add(name)
    return api_keys, credentials


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
            mode = store.auth_mode()
        except SetupStoreError:
            mode = "unset"
            store_error = "setup preference file is unreadable; choosing a mode replaces it"
        api_names, credential_names = _known_env_names()
        return {
            "auth_mode": mode,
            "configured": mode in MODES,
            "modes": list(MODES),
            "api_keys": [
                {"env_key": name,
                 "label": _label_for_env(name),
                 "set": bool(os.environ.get(name, "").strip())}
                for name in sorted(api_names)
            ],
            "subscription_credentials": [
                {"env_key": name,
                 "label": _SUBSCRIPTION_CREDENTIAL_LABELS.get(name, name),
                 "set": bool(os.environ.get(name, "").strip())}
                for name in sorted(credential_names)
            ],
            **({"store_error": store_error} if store_error else {}),
        }

    @router.post("/api/setup/mode")
    def set_mode(body: ModeBody) -> dict[str, Any]:
        try:
            store.set_auth_mode(body.mode)
        except SetupStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"auth_mode": body.mode}

    @router.post("/api/setup/keys")
    def set_keys(body: KeysBody) -> dict[str, Any]:
        api_names, credential_names = _known_env_names()
        allowed = api_names | credential_names
        unknown = sorted(set(body.keys) - allowed)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown key name(s): {', '.join(unknown)}")
        updates = {name: value.strip() for name, value in body.keys.items()}
        try:
            state = store.write_keys(updates)
            store.apply_to_environ(updates)
        except SetupStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"keys": state}

    @router.delete("/api/setup/keys/{env_key}")
    def clear_key(env_key: str) -> dict[str, Any]:
        api_names, credential_names = _known_env_names()
        if env_key not in (api_names | credential_names):
            raise HTTPException(status_code=404, detail="unknown key name")
        try:
            state = store.write_keys({env_key: None})
            store.apply_to_environ({env_key: None})
        except SetupStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"env_key": env_key, "set": state.get(env_key, False)}

    return router


def _label_for_env(name: str) -> str:
    for entry in providers.registry():
        if entry.get("env_key") == name:
            return str(entry.get("label", name))
    return name
