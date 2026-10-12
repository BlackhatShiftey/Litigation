"""Litigation server — a messenger where the participants are LLMs."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import providers
import ring
from db import Database
from project_routes import create_project_router
from project_runtime import runtime_from_environment
from rules_routes import create_rules_router
from rules_store import RulesStore, RulesStoreError, default_rules_db_path
from seam_bridge import SeamBridge
from seam_routes import create_seam_router
from seam_sync import SeamSyncCoordinator
from setup_routes import create_setup_router
from setup_store import SetupStore, SetupStoreError

load_dotenv()

DB = Database(os.environ.get("LITIGATION_DB", "litigation.db"))
RULES_STORE = RulesStore(default_rules_db_path())
PROJECT_RUNTIME = runtime_from_environment(rules_store=RULES_STORE)
SEAM_BRIDGE = SeamBridge()
SEAM_SYNC = SeamSyncCoordinator(PROJECT_RUNTIME.store, SEAM_BRIDGE)
PROJECT_RUNTIME.memory_bridge = SEAM_BRIDGE
# Settings-managed keys and local model overrides win over the repository
# .env so the settings surface stays authoritative once the user has used it.
# A corrupt settings file must not block boot; the settings page reports it.
SETUP_STORE = SetupStore()
SETUP_STORE.load_keys_into()
try:
    SETUP_STORE.load_overrides_into()
except SetupStoreError:
    pass


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await PROJECT_RUNTIME.start()
    try:
        if os.environ.get("LITIGATION_SEAM_SYNC_ENABLED", "1") != "0":
            await SEAM_SYNC.start()
        yield
    finally:
        await SEAM_SYNC.stop()
        await PROJECT_RUNTIME.stop()


app = FastAPI(title="Litigation", lifespan=lifespan)
app.include_router(create_project_router(PROJECT_RUNTIME))
app.include_router(create_rules_router(RULES_STORE))
app.include_router(create_seam_router(SEAM_BRIDGE, SEAM_SYNC, PROJECT_RUNTIME.store))
app.include_router(create_setup_router(SETUP_STORE))

HISTORY_TURNS = 24          # messages of context sent to each model
CROSSTALK_MAX_TOKENS = 700  # keep cross-talk envelopes cheap

# A connection check is intentionally ephemeral: it records only a result and
# timestamp, never credentials, command output, or a provider response.
MODEL_CHECKS: dict[str, dict[str, str]] = {}
CHECKABLE_SUBSCRIPTIONS = {"claude", "grok", "gemini", "antigravity", "glm", "cline", "openai"}

RING_SPEC = (
    "When agents address EACH OTHER in this chat they use the 'ring': a compact "
    "JSON envelope instead of prose, e.g. "
    '{"v":1,"from":"<your-id>","kind":"claim|rebuttal|note","text":"<short message>"} '
    "It costs fewer tokens and parses cleanly. Reply with the envelope ONLY, no "
    "fences, no commentary."
)

VOTE_SPEC = (
    "Reply with ONE verdict envelope ONLY, no fences, no commentary: "
    '{"v":1,"from":"<your-id>","kind":"verdict","text":"<one-line summary>",'
    '"verdict":{"choice":"<your pick, keep it short e.g. A/B or a word>",'
    '"confidence":<0.0-1.0>,"rationale":"<1-2 sentences>"}}'
)

# git empty-tree hash — the "HEAD" of a repo with no commits yet
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
DIFF_MAX_CHARS = 6000  # per-agent diff excerpt posted into the room

CODE_SPEC = (
    "CODING MODE: you are working inside the shared git workspace at {ws}. "
    "Edit files and run commands directly with your own tools. Other "
    "participants' messages (prefixed [Name]) are instructions and reviews "
    "from co-workers — build on their work, don't redo it. When you finish, "
    "reply with a SHORT summary of what you changed and why; do NOT paste "
    "file contents, the room records your git diff automatically."
)

REVIEW_SPEC = (
    "CODING MODE (reviewer): you have NO file-editing tools on this wire, but "
    "coding agents are working in the shared workspace and their diffs appear "
    "in the transcript as ```diff blocks. Contribute concrete plans, reviews "
    "of those diffs, or unified-diff patches they can apply. Be specific and "
    "terse."
)


def _label_map(participants: list[dict[str, Any]]) -> dict[str, str]:
    return {p["id"]: p.get("label", p["id"]) for p in participants}


def _room_system(agent_id: str, participants: list[dict[str, Any]]) -> str:
    labels = _label_map(participants)
    others = ", ".join(f"{labels[p['id']]} ({p['id']})" for p in participants if p["id"] != agent_id)
    you = labels.get(agent_id, agent_id)
    base = (
        f"You are {you}, an AI participant in 'Litigation', a group chat with a human "
        f"user" + (f" and these other AI models: {others}" if others else "") + ". "
        "Other models' messages are shown prefixed with their name in [brackets]. "
        "Answer the human directly, be concise, and stay in character."
    )
    if others:
        base += " " + RING_SPEC
    try:
        effective = RULES_STORE.get_effective(agent_id)
        rules_text = str(effective["combined_text"])
    except RulesStoreError as exc:
        if str(exc) != "no rules section exists for this model":
            raise
        # Extra API/local providers still receive the Global section. Only a
        # genuinely unknown model scope takes this path; persistence failures
        # must remain visible rather than silently dropping policy.
        global_rules = RULES_STORE.get_active("global")
        rules_text = (
            "# Effective Litigation rules\n\n"
            "The Global section applies to this model.\n\n"
            f"{str(global_rules['content']).rstrip()}\n"
        )
    return base + "\n\nActive Litigation rules for this response:\n" + rules_text


def _build_messages(agent_id: str, history: list[dict[str, Any]],
                    participants: list[dict[str, Any]], coding: bool = False,
                    workspace: str = "", is_harness: bool = False) -> list[dict[str, str]]:
    """Map the room transcript onto a single-agent conversation.

    The human's turns are `user`; the target agent's own past turns are
    `assistant`; every other agent's turns are `user`-role with a [Name] prefix.
    """
    labels = _label_map(participants)
    msgs: list[dict[str, str]] = [{"role": "system", "content": _room_system(agent_id, participants)}]
    if coding:
        spec = CODE_SPEC.format(ws=workspace) if is_harness else REVIEW_SPEC
        msgs[0]["content"] += " " + spec
    for m in history[-HISTORY_TURNS:]:
        if m["sender"] == "user":
            msgs.append({"role": "user", "content": m["content"]})
        elif m["sender"] == agent_id:
            msgs.append({"role": "assistant", "content": m["content"]})
        else:
            name = labels.get(m["sender"], m["sender"])
            msgs.append({"role": "user", "content": f"[{name}] {m['content']}"})
    return msgs


# -- shared coding workspace ------------------------------------------------

async def _git(ws: str, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", ws, "-c", "user.name=litigation", "-c",
        "user.email=litigation@local", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out_b, err_b = await proc.communicate()
    out = out_b.decode("utf-8", "replace")
    if proc.returncode != 0:
        tail = err_b.decode("utf-8", "replace").strip()[-200:]
        raise RuntimeError(f"git {' '.join(args)} failed: {tail}")
    return proc.returncode, out


async def _resolve_workspace(raw: str) -> str:
    path = Path(raw).expanduser().resolve()
    if str(path) == "/":
        raise HTTPException(status_code=400, detail="workspace cannot be /")
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        await _git(str(path), "init", "-q")
    return str(path)


async def _head_ref(ws: str) -> str:
    try:
        _, out = await _git(ws, "rev-parse", "HEAD")
        return out.strip()
    except RuntimeError:
        return EMPTY_TREE


async def _commit_and_diff(ws: str, agent_id: str, before: str) -> str:
    """Stage everything, capture this agent's diff vs `before`, commit it."""
    await _git(ws, "add", "-A")
    _, status = await _git(ws, "status", "--porcelain")
    if not status.strip():
        return ""
    _, diff = await _git(ws, "diff", before)
    await _git(ws, "commit", "-q", "-m", f"litigation[{agent_id}]: turn changes")
    return diff


def status_files(diff: str) -> list[str]:
    """File paths touched by a unified diff (for the SSE diff event)."""
    files = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/")
            if len(parts) > 1:
                files.append(parts[-1])
    return files


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class RoomCreate(BaseModel):
    title: str = ""
    participants: list[dict[str, Any]]
    crosstalk: bool = False
    workspace: str = ""  # shared git workspace path for coding mode


class SendBody(BaseModel):
    text: str
    targets: list[str] | None = None  # None = everyone in the room
    mode: str = "chat"                # "chat" or "code"
    workspace: str | None = None      # per-turn override; persists on the room


class VoteBody(BaseModel):
    question: str = ""



@app.get("/api/models")
def list_models() -> list[dict[str, Any]]:
    # This whitelist prevents credentials, CLI arguments, and environment
    # details from crossing the API boundary.
    safe_keys = (
        "id", "label", "color", "model", "installed", "configured",
        "available", "setup_status", "auth_status", "transport", "billing",
        "setup_hint", "blocked_reason", "limitations",
    )
    result = []
    for entry in providers.registry():
        model = {key: entry.get(key) for key in safe_keys if key in entry}
        model["local"] = not entry.get("env_key") and entry.get("api") != "cli"
        model.update(MODEL_CHECKS.get(entry["id"], {}))
        result.append(model)
    return result


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _model_check_result(model_id: str, status: str, message: str) -> dict[str, Any]:
    result = {
        "id": model_id,
        "ok": status == "verified",
        "connection_status": status,
        "auth_status": status,
        "checked_at": _checked_at(),
        "message": message,
    }
    MODEL_CHECKS[model_id] = {
        "connection_status": status,
        "auth_status": status,
        "checked_at": result["checked_at"],
        "check_message": message,
    }
    return result


@app.post("/api/models/{model_id}/check")
async def check_model(model_id: str) -> dict[str, Any]:
    if model_id not in CHECKABLE_SUBSCRIPTIONS:
        known = {entry["id"] for entry in providers.registry()}
        if model_id not in known:
            raise HTTPException(status_code=404, detail="unknown model")
        raise HTTPException(status_code=400, detail="connection checks are limited to subscription providers")

    agent = providers.get_agent(model_id)
    if not agent:
        raise HTTPException(status_code=404, detail="unknown model")
    if not agent.get("available"):
        hint = agent.get("setup_hint") or "Complete this provider's setup, then try again."
        return _model_check_result(model_id, "error", str(hint))

    try:
        reply = await providers.complete(
            agent,
            [
                {
                    "role": "user",
                    "content": (
                        "This is a connection test. Please reply with exactly "
                        "LITIGATION_OK. Do not use tools or read files."
                    ),
                },
            ],
            max_tokens=32,
            temperature=0,
            coding=False,
        )
    except Exception:  # provider adapters sanitize internally; do not expose exception text here
        hint = agent.get("setup_hint") or "Check the provider login, then try again."
        return _model_check_result(model_id, "error", f"Connection failed. {hint}")

    if reply.strip() != "LITIGATION_OK":
        return _model_check_result(
            model_id,
            "error",
            "Connected, but the provider did not return the expected check response.",
        )
    return _model_check_result(model_id, "verified", "Connection verified for this server session.")


@app.get("/api/rooms")
def rooms() -> list[dict[str, Any]]:
    return DB.list_rooms()


@app.post("/api/rooms")
def create_room(body: RoomCreate) -> dict[str, Any]:
    if not body.participants:
        raise HTTPException(status_code=400, detail="a room needs at least one participant")
    known = {e["id"]: e for e in providers.registry()}
    cleaned = []
    for p in body.participants:
        entry = known.get(str(p.get("id", "")))
        if not entry:
            raise HTTPException(status_code=400, detail=f"unknown participant: {p.get('id')}")
        cleaned.append({"id": entry["id"], "label": entry["label"],
                        "model": str(p.get("model") or entry["model"])})
    title = body.title.strip() or " + ".join(p["label"] for p in cleaned)
    workspace = body.workspace.strip()
    if workspace:
        workspace = str(Path(workspace).expanduser().resolve())
    return DB.create_room(title, cleaned, body.crosstalk, workspace or None)


@app.get("/api/rooms/{room_id}")
def get_room(room_id: str) -> dict[str, Any]:
    room = DB.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="room not found")
    return {**room, "messages": DB.list_messages(room_id)}


@app.delete("/api/rooms/{room_id}")
def delete_room(room_id: str) -> dict[str, Any]:
    if not DB.get_room(room_id):
        raise HTTPException(status_code=404, detail="room not found")
    DB.delete_room(room_id)
    return {"ok": True}


@app.patch("/api/rooms/{room_id}")
async def patch_room(room_id: str, body: dict[str, Any]) -> dict[str, Any]:
    room = DB.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="room not found")
    updates: dict[str, Any] = {
        "title": body.get("title", room["title"]),
        "crosstalk": bool(body.get("crosstalk", room["crosstalk"])),
    }
    if "workspace" in body:
        ws = str(body["workspace"] or "").strip()
        updates["workspace"] = await _resolve_workspace(ws) if ws else None
    if "participants" in body:
        # Replacement semantics: the client sends the full new roster, so a
        # room can gain (or drop) counsel after the conversation started.
        requested = body.get("participants") or []
        if not requested:
            raise HTTPException(status_code=400, detail="a room needs at least one participant")
        known = {e["id"]: e for e in providers.registry()}
        existing = {p["id"]: dict(p) for p in room["participants"]}
        cleaned = []
        for p in requested:
            entry = known.get(str(p.get("id", "")))
            if not entry:
                raise HTTPException(status_code=400,
                                    detail=f"unknown participant: {p.get('id')}")
            current = existing.get(entry["id"], {})
            cleaned.append({
                "id": entry["id"], "label": entry["label"],
                "model": str(p.get("model") or current.get("model") or entry["model"]),
            })
        updates["participants"] = cleaned
    DB.update_room(room_id, **updates)
    return DB.get_room(room_id)  # type: ignore[return-value]


@app.post("/api/rooms/{room_id}/send")
async def send(room_id: str, body: SendBody) -> StreamingResponse:
    room = DB.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="room not found")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message text is required")
    coding = body.mode.strip() == "code"
    workspace = room.get("workspace") or ""
    if body.workspace and body.workspace.strip():
        workspace = await _resolve_workspace(body.workspace)
        DB.update_room(room_id, workspace=workspace)
    if coding:
        if not workspace:
            raise HTTPException(
                status_code=400,
                detail="code mode needs a workspace: set one on the room "
                       "(--workspace / modal field) or pass workspace in the send body")
        workspace = await _resolve_workspace(workspace)
    wanted = set(body.targets) if body.targets else None
    targets = [p for p in room["participants"] if wanted is None or p["id"] in wanted]
    if not targets:
        raise HTTPException(status_code=400, detail="no participants targeted")

    DB.add_message(room_id, "user", "user", text)
    history = DB.list_messages(room_id)

    return StreamingResponse(
        _run_turn(room, targets, history, coding=coding, workspace=workspace),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _run_turn(room: dict[str, Any], targets: list[dict[str, Any]],
                    history: list[dict[str, Any]], coding: bool = False,
                    workspace: str = "") -> AsyncIterator[str]:
    """One turn: fan out to targets in parallel, then one cross-talk ring round.

    In coding mode targets run SEQUENTIALLY (they share one writable git
    workspace, so parallel edits would race) and each harness's diff is
    committed and posted into the room for the others to review.
    """
    labels = _label_map(room["participants"])

    async def worker(p: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        agent = providers.get_agent(p["id"]) or {}
        agent = {**agent, "model": p.get("model") or agent.get("model", "")}
        is_harness = agent.get("api") == "cli" and agent.get(
            "supports_coding", bool(agent.get("cli_coding_flags")))
        yield {"type": "start", "agent": p["id"], "label": p.get("label", p["id"])}
        try:
            before = await _head_ref(workspace) if coding and is_harness else ""
            chunks: list[str] = []
            async for delta in providers.stream_completion(
                    agent, _build_messages(p["id"], history, room["participants"],
                                           coding=coding, workspace=workspace,
                                           is_harness=is_harness),
                    coding=coding and is_harness,
                    cwd=workspace if coding and is_harness else None):
                chunks.append(delta)
                yield {"type": "delta", "agent": p["id"], "text": delta}
            full = "".join(chunks).strip()
            msg = DB.add_message(room["id"], p["id"], "assistant", full)
            yield {"type": "done", "agent": p["id"], "content": full, "message": msg}
            if coding and is_harness:
                diff = await _commit_and_diff(workspace, p["id"], before)
                if diff:
                    excerpt = diff if len(diff) <= DIFF_MAX_CHARS else (
                        diff[:DIFF_MAX_CHARS] + f"\n… (diff truncated, full diff in {workspace})")
                    dmsg = DB.add_message(room["id"], p["id"], "assistant",
                                          f"```diff\n{excerpt}\n```", raw=diff)
                    yield {"type": "diff", "agent": p["id"],
                           "label": p.get("label", p["id"]),
                           "files": len(status_files(diff)),
                           "diff": excerpt, "message": dmsg}
        except Exception as exc:  # noqa: BLE001 - one provider outage must not kill the turn
            yield {"type": "error", "agent": p["id"],
                   "error": f"{type(exc).__name__}: {exc}"[:300]}

    if coding:
        for p in targets:  # sequential: shared writable workspace
            async for ev in worker(p):
                yield _sse(ev)
        yield _sse({"type": "turn_done"})
        return

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    replies: dict[str, str] = {}

    async def drain() -> None:
        async def pump(p: dict[str, Any]) -> None:
            async for ev in worker(p):
                if ev["type"] == "done":
                    replies[ev["agent"]] = ev.get("content", "")
                await queue.put(ev)
        await asyncio.gather(*(pump(p) for p in targets))
        await queue.put(None)  # sentinel: every worker finished

    drain_task = asyncio.create_task(drain())
    try:
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield _sse(ev)
    finally:
        drain_task.cancel()

    # -- cross-talk: one bounded ring round between the answering agents -----
    if room.get("crosstalk") and len(replies) > 1:
        yield _sse({"type": "phase", "phase": "crosstalk"})
        for p in targets:
            if p["id"] not in replies:
                continue
            others = [f"{labels[o]} :: {replies[o]}" for o in replies if o != p["id"]]
            if not others:
                continue
            prompt = (
                "The other agents in this room just replied to the human:\n\n"
                + "\n\n".join(others)
                + "\n\nRespond to them now using the ring envelope."
            )
            agent = providers.get_agent(p["id"]) or {}
            agent = {**agent, "model": p.get("model") or agent.get("model", "")}
            try:
                raw = await providers.complete(
                    agent,
                    [{"role": "system", "content": _room_system(p["id"], room["participants"])},
                     {"role": "user", "content": prompt}],
                    max_tokens=CROSSTALK_MAX_TOKENS,
                )
                env = ring.decode(raw)
                text_out = (env or {}).get("text") or raw.strip()
                msg = DB.add_message(room["id"], p["id"], "assistant", text_out,
                                     raw=(ring.encode(env) if env else raw))
                yield _sse({"type": "ring", "agent": p["id"], "label": p.get("label", p["id"]),
                            "envelope": env, "decoded": text_out,
                            "tokens_saved": max(0, ring.estimate_tokens(raw) - ring.estimate_tokens(text_out)),
                            "message": msg})
            except Exception as exc:  # noqa: BLE001
                yield _sse({"type": "error", "agent": p["id"],
                            "error": f"crosstalk failed: {type(exc).__name__}"[:200]})
    yield _sse({"type": "turn_done"})


@app.post("/api/rooms/{room_id}/vote")
async def vote(room_id: str, body: VoteBody) -> dict[str, Any]:
    room = DB.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="room not found")
    question = body.question.strip()
    history = DB.list_messages(room_id)
    if not question:
        # default to the most recent user message as the motion
        question = next((m["content"] for m in reversed(history) if m["sender"] == "user"), "")
    if not question:
        raise HTTPException(status_code=400, detail="no question and no prior user message to vote on")

    labels = _label_map(room["participants"])

    async def one(p: dict[str, Any]) -> dict[str, Any]:
        agent = providers.get_agent(p["id"]) or {}
        agent = {**agent, "model": p.get("model") or agent.get("model", "")}
        transcript = "\n".join(
            f"{'Human' if m['sender'] == 'user' else labels.get(m['sender'], m['sender'])}: {m['content']}"
            for m in history[-HISTORY_TURNS:]
        )
        prompt = (
            f"Room transcript so far:\n{transcript}\n\nMotion to decide: {question}\n\n{VOTE_SPEC}"
        )
        try:
            raw = await providers.complete(
                agent,
                [{"role": "system", "content": _room_system(p["id"], room["participants"])},
                 {"role": "user", "content": prompt}],
                max_tokens=500, temperature=0.3,
            )
        except Exception as exc:  # noqa: BLE001
            return {"agent": p["id"], "error": f"{type(exc).__name__}: {exc}"[:200], "raw": ""}
        env = ring.decode(raw)
        if env and env.get("verdict"):
            return {"agent": p["id"], "verdict": env["verdict"], "envelope": env, "raw": raw}
        return {"agent": p["id"], "error": "reply was not a valid verdict envelope",
                "raw": raw[:400]}

    results = await asyncio.gather(*(one(p) for p in room["participants"]))
    votes = [r for r in results if r.get("verdict")]
    errors = [r for r in results if r.get("error")]
    tally_out = ring.tally(votes)
    msg = DB.add_message(room["id"], "court", "system",
                         f"VOTE on: {question}\n{json.dumps(tally_out['counts'])} "
                         f"→ majority: {tally_out['majority']}", raw=json.dumps(tally_out))
    return {"question": question, "tally": tally_out, "errors": errors, "message": msg}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("LITIGATION_HOST", "127.0.0.1"),
                port=int(os.environ.get("LITIGATION_PORT", "8766")))

