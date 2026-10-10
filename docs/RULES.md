# Litigation rules ledger

Open `/rules` to review the rulebook that Litigation attaches to agent work. The
index contains a Global section followed by Claude, OpenAI, GLM, Gemini, Cline,
and Grok sections. OpenAI covers Codex, GPT, and Astra identities; Gemini covers
Antigravity.

Global rules always have authority. A model section adds working guidance but
cannot expand tools, filesystem access, network access, or other permissions.
Runtime permission checks remain code-controlled.

## Revisions and proposals

Every published revision is a new SQLite record. Update, delete, and replace
operations are rejected by database triggers. Publishing requires the currently
active revision number, so a stale browser cannot overwrite a newer revision.

People can publish a revision or record a proposal from the Rules page. Agent
code calls `RulesStore.propose` directly with its canonical model scope and a
source handoff identifier. Agents cannot submit Global proposals, propose for a
different model, or activate their own suggestion. Accepting a proposal creates
a new immutable revision and retains the proposal decision.

## Runtime snapshots

`RulesStore.get_effective(model_id)` and `load_effective_rules(model_id)` return
the Global and model revisions, full content, content hashes, combined text, and
a snapshot hash. A handoff should capture that value when it is queued. Later
rule changes then affect new work without rewriting the instructions attached to
old work.

Room chat, ring cross-talk, and voting load the active Global rules plus the
matching model section for each provider request. Extra API or local providers
without a model-specific section receive the active Global rules. These room
prompts use the current revisions on each request. Project handoffs differ by
capturing an immutable effective-rules snapshot when queued, so later revisions
cannot rewrite the policy record for work already in progress.

The database defaults to
`~/.local/share/litigation/rules.sqlite3`. Set `LITIGATION_RULES_DB` to isolate a
test or another Litigation instance. The tracked Markdown files under `rules/`
seed only missing sections; they never replace revisions already in the ledger.
