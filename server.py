"""Litigation server — a messenger where the participants are LLMs."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import providers
import ring
from db import Database

load_dotenv()

app = FastAPI(title="Litigation")
DB = Database(os.environ.get("LITIGATION_DB", "litigation.db"))

HISTORY_TURNS = 24          # messages of context sent to each model
CROSSTALK_MAX_TOKENS = 700  # keep cross-talk envelopes cheap

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


def _label_map(participants: list[dict[str, Any]]) -> dict[str, str]:
    return {p["id"]: p.get("label", p["id"]) for p in participants}


def _room_system(agent_id: str, participants: list[dict[str, Any]]) -> str:
    labels = _label_map(participants)
    others = ", ".join(f"{labels[p]} ({p['id']})" for p in participants if p["id"] != agent_id)
    you = labels.get(agent_id, agent_id)
    base = (
        f"You are {you}, an AI participant in 'Litigation', a group chat with a human "
        f"user" + (f" and these other AI models: {others}" if others else "") + ". "
        "Other models' messages are shown prefixed with their name in [brackets]. "
        "Answer the human directly, be concise, and stay in character."
    )
    if others:
        base += " " + RING_SPEC
    return base


def _build_messages(agent_id: str, history: list[dict[str, Any]],
                    participants: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Map the room transcript onto a single-agent conversation.

    The human's turns are `user`; the target agent's own past turns are
    `assistant`; every other agent's turns are `user`-role with a [Name] prefix.
    """
    labels = _label_map(participants)
    msgs: list[dict[str, str]] = [{"role": "system", "content": _room_system(agent_id, participants)}]
    for m in history[-HISTORY_TURNS:]:
        if m["sender"] == "user":
            msgs.append({"role": "user", "content": m["content"]})
        elif m["sender"] == agent_id:
            msgs.append({"role": "assistant", "content": m["content"]})
        else:
            name = labels.get(m["sender"], m["sender"])
            msgs.append({"role": "user", "content": f"[{name}] {m['content']}"})
    return msgs


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class RoomCreate(BaseModel):
    title: str = ""
    participants: list[dict[str, Any]]
    crosstalk: bool = False


class SendBody(BaseModel):
    text: str
    targets: list[str] | None = None  # None = everyone in the room


class VoteBody(BaseModel):
    question: str = ""



@app.get("/api/models")
def list_models() -> list[dict[str, Any]]:
    # Never expose key values or env var contents; availability only.
    return [{"id": e["id"], "label": e["label"], "color": e["color"],
             "model": e["model"], "available": e["available"],
             "local": not e.get("env_key")}
            for e in providers.registry()]


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
    return DB.create_room(title, cleaned, body.crosstalk)


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
def patch_room(room_id: str, body: dict[str, Any]) -> dict[str, Any]:
    room = DB.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="room not found")
    DB.update_room(room_id,
                   title=body.get("title", room["title"]),
                   crosstalk=bool(body.get("crosstalk", room["crosstalk"])))
    return DB.get_room(room_id)  # type: ignore[return-value]


@app.post("/api/rooms/{room_id}/send")
async def send(room_id: str, body: SendBody) -> StreamingResponse:
    room = DB.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="room not found")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message text is required")
    wanted = set(body.targets) if body.targets else None
    targets = [p for p in room["participants"] if wanted is None or p["id"] in wanted]
    if not targets:
        raise HTTPException(status_code=400, detail="no participants targeted")

    DB.add_message(room_id, "user", "user", text)
    history = DB.list_messages(room_id)

    return StreamingResponse(_run_turn(room, targets, history),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _run_turn(room: dict[str, Any], targets: list[dict[str, Any]],
                    history: list[dict[str, Any]]) -> AsyncIterator[str]:
    """One turn: fan out to targets in parallel, then one cross-talk ring round."""
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    replies: dict[str, str] = {}
    labels = _label_map(room["participants"])

    async def worker(p: dict[str, Any]) -> None:
        agent = providers.get_agent(p["id"]) or {}
        agent = {**agent, "model": p.get("model") or agent.get("model", "")}
        await queue.put({"type": "start", "agent": p["id"], "label": p.get("label", p["id"])})
        try:
            chunks: list[str] = []
            async for delta in providers.stream_completion(
                    agent, _build_messages(p["id"], history, room["participants"])):
                chunks.append(delta)
                await queue.put({"type": "delta", "agent": p["id"], "text": delta})
            full = "".join(chunks).strip()
            msg = DB.add_message(room["id"], p["id"], "assistant", full)
            replies[p["id"]] = full
            await queue.put({"type": "done", "agent": p["id"], "content": full, "message": msg})
        except Exception as exc:  # noqa: BLE001 - one provider outage must not kill the turn
            await queue.put({"type": "error", "agent": p["id"],
                             "error": f"{type(exc).__name__}: {exc}"[:300]})

    async def drain() -> None:
        await asyncio.gather(*(worker(p) for p in targets))
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

