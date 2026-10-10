import ring

env = ring.make_envelope("gpt", "claim", "test")
assert ring.decode(ring.encode(env)) == env
# tolerant decode: envelope wrapped in fences + preamble
wrapped = "Sure! ```json\n" + ring.encode(env) + "\n```"
assert ring.decode(wrapped) == env
assert ring.decode("no envelope here") is None
t = ring.tally([
    {"agent": "a", "verdict": {"choice": "A", "confidence": 0.9, "rationale": "r1"}},
    {"agent": "b", "verdict": {"choice": "B", "confidence": 0.4, "rationale": "r2"}},
    {"agent": "c", "verdict": {"choice": "A", "confidence": 0.7, "rationale": "r3"}},
])
assert t["majority"] == "A" and t["counts"] == {"A": 2, "B": 1}
assert len(t["dissent"]) == 1 and not t["unanimous"]
print("ring OK:", t["majority"], t["counts"], "conf", t["mean_confidence"])

from db import Database
import os, tempfile
tmp = tempfile.mktemp(suffix=".db")
db = Database(tmp)
room = db.create_room("Test", [{"id": "gpt", "label": "GPT", "model": "gpt-4o-mini"}])
db.add_message(room["id"], "user", "user", "hello")
db.update_room(room["id"], crosstalk=True)
got = db.get_room(room["id"])
assert got["crosstalk"] and len(got["participants"]) == 1
assert [m["content"] for m in db.list_messages(room["id"])] == ["hello"]
db.delete_room(room["id"])
assert db.get_room(room["id"]) is None
# workspace persistence + migration on a pre-workspace schema
room2 = db.create_room("Ws", [{"id": "claude", "label": "Claude", "model": "sonnet"}],
                       workspace="/tmp/lit-ws-selftest")
assert db.get_room(room2["id"])["workspace"] == "/tmp/lit-ws-selftest"
db.update_room(room2["id"], workspace=None)
assert db.get_room(room2["id"])["workspace"] is None
with db._lock, db._conn:
    db._conn.execute("alter table rooms drop column workspace")
    cols = {r[1] for r in db._conn.execute("pragma table_info(rooms)")}
    assert "workspace" not in cols
db._init_schema()  # must re-add the column without losing rows
assert db.get_room(room2["id"])["workspace"] is None
os.remove(tmp)
print("db OK")

import providers
reg = providers.registry()
ids = {e["id"] for e in reg}
assert ids >= {"gpt", "claude", "grok", "gemini", "openrouter", "huggingface",
               "opencode", "deepseek", "groq", "mistral", "together",
               "fireworks", "perplexity", "cerebras", "sambanova", "novita",
               "ollama", "lmstudio", "astra", "zcode", "antigravity"}
assert providers.get_agent("claude")["api"] == "cli"
assert providers.get_agent("gemini")["api"] == "cli"
assert providers.get_agent("opencode")["api"] == "responses"
assert providers.get_agent("ollama")["base_url"].endswith("localhost:11434/v1")
assert not providers.get_agent("ollama").get("env_key")
# Subscription chat routes report installation separately from authentication
# and never enable coding permission bypasses.
for cid in ("claude", "grok", "gemini"):
    ent = providers.get_agent(cid)
    assert ent["api"] == "cli" and ent["billing"] == "subscription", cid
    assert ent["auth_status"] == "unverified", cid
    assert not ent.get("supports_coding"), cid
    assert not ent.get("cli_coding_flags"), cid
import os as _os
assert _os.path.basename(providers.get_agent("antigravity")["cli"][0]) == "agy"
print("providers OK:", len(reg), "entries:", [(e["id"], e["available"]) for e in reg])

# coding-mode helpers: workspace bootstrap, diff capture, status_files
import asyncio
import server

async def ws_checks() -> None:
    ws = await server._resolve_workspace(tempfile.mkdtemp(prefix="lit-ws-"))
    assert os.path.isdir(ws) and os.path.isdir(os.path.join(ws, ".git"))
    # empty repo → HEAD falls back to the empty tree
    assert await server._head_ref(ws) == server.EMPTY_TREE
    with open(os.path.join(ws, "hello.py"), "w") as f:
        f.write("print('hi')\n")
    diff = await server._commit_and_diff(ws, "testagent", server.EMPTY_TREE)
    assert "hello.py" in diff and server.status_files(diff) == ["hello.py"]
    # second turn with no changes → empty diff
    before = await server._head_ref(ws)
    assert await server._commit_and_diff(ws, "testagent", before) == ""
    # refusal to use /
    try:
        await server._resolve_workspace("/")
        raise SystemExit("root workspace was not rejected")
    except Exception:
        pass

asyncio.run(ws_checks())
print("coding-mode OK")
