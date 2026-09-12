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
os.remove(tmp)
print("db OK")

import providers
reg = providers.registry()
ids = {e["id"] for e in reg}
assert ids >= {"gpt", "claude", "grok", "gemini", "openrouter", "huggingface",
               "opencode", "deepseek", "groq", "mistral", "together",
               "fireworks", "perplexity", "cerebras", "sambanova", "novita",
               "ollama", "lmstudio"}
assert providers.get_agent("claude")["api"] == "anthropic"
assert providers.get_agent("gemini")["api"] == "openai"
assert providers.get_agent("opencode")["api"] == "responses"
assert providers.get_agent("ollama")["base_url"].endswith("localhost:11434/v1")
assert not providers.get_agent("ollama").get("env_key")
print("providers OK:", len(reg), "entries:", [(e["id"], e["available"]) for e in reg])
