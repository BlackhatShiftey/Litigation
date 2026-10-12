"""Provider adapters — model-agnostic chat completion with SSE streaming.

Every model in Litigation is an entry in a registry. `api` selects the wire
protocol: "openai" (OpenAI-compatible /chat/completions — covers OpenAI, xAI,
Gemini's compat endpoint, OpenRouter, Ollama, DeepSeek, LM Studio, ...),
"anthropic" (native Messages API), "responses" (OpenAI Responses API), or
"cli" — a local subscription CLI (codex/claude/grok) run headless as a
subprocess, authenticated by the user's own login instead of an API key.
Adding a model is a registry entry or a LITIGATION_EXTRA_MODELS env blob,
not new code.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import shutil
import tempfile
from typing import Any, AsyncIterator

import httpx

BASE_REGISTRY: list[dict[str, Any]] = [
    # -- the big four ------------------------------------------------------
    {
        "id": "gpt", "label": "GPT", "color": "#10a37f",
        "api": "openai", "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY", "default_model": "gpt-4o-mini",
        "model_env": "OPENAI_MODEL",
    },
    {
        # ChatGPT via the codex CLI's own login (subscription, no API key)
        "id": "astra", "label": "Astra", "color": "#74aa9c",
        "api": "cli", "env_key": "", "default_model": "",
        "model_env": "ASTRA_MODEL",
        "cli": [
            "codex", "exec", "--skip-git-repo-check", "--ignore-user-config",
            "--ignore-rules", "--ephemeral", "-s", "read-only",
            "--disable", "shell_tool", "--disable", "unified_exec",
            "--disable", "plugins", "-c", 'web_search="disabled"', "-",
        ],
        "cli_stdin": True, "cli_model_flag": "-m",
        "cli_strip_env": ["OPENAI_*"], "cli_timeout": 300,
        "cli_isolated_cwd": True, "cli_state_root": "~/.local/state/litigation/openai",
        "supports_coding": False, "transport": "codex-cli-safe", "billing": "subscription",
    },
    {
        # Tool-free ChatGPT subscription inference. Litigation owns the
        # bounded coding tools; this child cannot use Codex native tools.
        "id": "openai", "label": "OpenAI (ChatGPT)", "color": "#10a37f",
        "api": "cli", "env_key": "", "default_model": "",
        "model_env": "OPENAI_SUBSCRIPTION_MODEL",
        "cli": [
            "codex", "exec", "--skip-git-repo-check", "--ignore-user-config",
            "--ignore-rules", "--ephemeral", "-s", "read-only",
            "--disable", "shell_tool", "--disable", "unified_exec",
            "--disable", "plugins", "-c", 'web_search="disabled"', "-",
        ],
        "cli_stdin": True, "cli_model_flag": "-m", "cli_strip_env": ["OPENAI_*"],
        "cli_timeout": 300, "cli_isolated_cwd": True,
        "cli_state_root": "~/.local/state/litigation/openai",
        "transport": "codex-cli-safe", "billing": "subscription", "supports_coding": False,
        "setup_hint": "Run `codex login status` and sign in with ChatGPT.",
    },
    {
        # Claude via the claude CLI's claude.ai login (subscription, no API key)
        "id": "claude", "label": "Claude", "color": "#d97757",
        "api": "cli", "env_key": "", "default_model": "sonnet",
        "model_env": "CLAUDE_MODEL",
        "cli": [
            "claude", "-p", "--tools", "", "--safe-mode",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--no-session-persistence", "--permission-mode", "dontAsk",
            "--permission-prompts", "none",
        ],
        "cli_stdin": True, "cli_model_flag": "--model",
        "cli_strip_env": [
            "ANTHROPIC_*", "CLAUDE_CODE_USE_*", "CLAUDE_CODE_OAUTH_TOKEN",
        ],
        "cli_timeout": 300, "transport": "claude-cli",
        "billing": "subscription", "supports_coding": False,
        "setup_hint": "Run `claude auth login` and select your Claude subscription account.",
    },
    {
        # Grok via the grok CLI's xAI login (subscription, no API key)
        "id": "grok", "label": "Grok", "color": "#cbd5e1",
        "api": "cli", "env_key": "", "default_model": "",
        "model_env": "GROK_MODEL",
        "cli": [
            "grok", "--tools", "", "--deny", "*",
            "--no-subagents", "--disable-web-search", "--max-turns", "1",
        ],
        "cli_prompt_flag": "-p", "cli_model_flag": "--model", "cli_timeout": 300,
        "cli_strip_env": ["XAI_*", "GROK_API_KEY", "GROK_BASE_URL", "OPENAI_*"],
        "transport": "grok-cli", "billing": "subscription",
        "supports_coding": False,
        "setup_hint": "Run `grok login` and sign in to your Grok subscription account.",
    },
    {
        # ZCode via its own CLI headless print mode (claude-convention flags)
        "id": "zcode", "label": "ZCode", "color": "#f59e0b",
        "api": "cli", "env_key": "", "default_model": "",
        "model_env": "ZCODE_MODEL",
        "cli": ["zcode", "-p"],
        "cli_stdin": True, "cli_model_flag": "--model",
        "cli_coding_flags": ["--dangerously-skip-permissions"],
        "cli_strip_env": [], "cli_timeout": 600,
    },
    {
        # Antigravity (Google) via its agy CLI headless print mode
        "id": "antigravity", "label": "Antigravity", "color": "#34a853",
        "api": "cli", "env_key": "", "default_model": "gemini-3.8-flash-medium",
        "model_env": "ANTIGRAVITY_MODEL",
        "cli": [
            "agy", "--mode", "plan", "--sandbox",
            "--output-format", "json", "--print-timeout", "180s",
        ],
        "cli_prompt_flag": "-p", "cli_model_flag": "--model",
        "cli_output": "antigravity-json",
        "cli_prompt_transform": "prefix-user",
        "cli_timeout": 240, "transport": "antigravity-cli",
        "billing": "subscription", "supports_coding": False,
        "limitations": "Antigravity plan+sandbox mode cannot disable user plugins.",
        "setup_hint": "Open Antigravity and sign in to the Google account with your subscription.",
    },
    {
        "id": "gemini", "label": "Gemini (Antigravity)", "color": "#4285f4",
        "api": "cli", "env_key": "", "default_model": "gemini-3.8-flash-medium",
        "model_env": "GEMINI_MODEL",
        "cli": [
            "agy", "--mode", "plan", "--sandbox",
            "--output-format", "json", "--print-timeout", "180s",
        ],
        "cli_prompt_flag": "-p", "cli_model_flag": "--model",
        "cli_output": "antigravity-json",
        "cli_prompt_transform": "prefix-user",
        "cli_timeout": 240, "transport": "antigravity-cli",
        "billing": "subscription", "supports_coding": False,
        "limitations": "Antigravity plan+sandbox mode cannot disable user plugins.",
        "setup_hint": "Open Antigravity and sign in to the Google account with your subscription.",
    },
    {
        # ClinePass is selected explicitly to prevent fallback to Cline usage
        # billing. The isolated config disables every installed tool category.
        "id": "cline", "label": "Cline", "color": "#7f5af0",
        "api": "cli", "env_key": "",
        "default_model": "cline-pass/deepseek-v4.1-flash",
        "model_env": "CLINE_MODEL",
        "cli": [
            "cline", "--config",
            os.path.join(os.path.dirname(__file__), "config", "cline-subscription"),
            "--json", "--plan", "--auto-approve", "false",
            "--provider", "cline-pass", "--timeout", "50", "--retries", "1",
        ],
        "cli_model_flag": "--model", "cli_output": "cline-ndjson",
        "allowed_model_prefix": "cline-pass/",
        "cli_prompt_transform": "cline-safe", "cli_runtime_data": True,
        "cli_strip_env": ["CLINE_*"],
        "cli_env": {
            "CLINE_GLOBAL_SETTINGS_PATH": os.path.join(
                os.path.dirname(__file__), "config", "cline-subscription",
                "global-settings.json",
            ),
            "CLINE_MCP_SETTINGS_PATH": os.path.join(
                os.path.dirname(__file__), "config", "cline-subscription",
                "empty-mcp.json",
            ),
            "CLINE_NO_AUTO_UPDATE": "1",
        },
        "cli_env_paths": {
            "CLINE_PROVIDER_SETTINGS_PATH": "~/.cline/data/settings/providers.json",
        },
        "cli_timeout": 90, "transport": "cline-cli",
        "billing": "subscription", "supports_coding": False,
        "setup_hint": "Run `cline auth cline-pass` if your ClinePass login has expired.",
    },
    # -- aggregators / routers ----------------------------------------------
    {
        "id": "openrouter", "label": "OpenRouter", "color": "#8b5cf6",
        "api": "openai", "base_url": "https://openrouter.ai/v1",
        "env_key": "OPENROUTER_API_KEY", "default_model": "openrouter/auto",
        "model_env": "OPENROUTER_MODEL",
    },
    {
        "id": "huggingface", "label": "HuggingFace", "color": "#ffd21e",
        "api": "openai", "base_url": "https://router.huggingface.co/v1",
        "env_key": "HF_TOKEN", "default_model": "meta-llama/Llama-3.3-70B-Instruct:fastest",
        "model_env": "HF_MODEL",
    },
    {
        "id": "opencode", "label": "OpenCode Zen", "color": "#f97316",
        "api": "responses", "base_url": "https://opencode.ai/zen/v1",
        "env_key": "OPENCODE_API_KEY", "default_model": "qwen3-coder-480b",
        "model_env": "OPENCODE_MODEL",
    },
    # -- direct providers (all OpenAI-compatible) ---------------------------
    {
        # GLM Coding Plan through the supported Claude Code integration. The
        # inherited Anthropic environment is removed before the Z.ai routing
        # values are constructed in-memory from ZAI_API_KEY.
        "id": "glm", "label": "GLM", "color": "#38bdf8",
        "api": "cli", "env_key": "", "default_model": "glm-5.3",
        "model_env": "GLM_MODEL",
        "cli": [
            "claude", "-p", "--tools", "", "--safe-mode",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--no-session-persistence", "--permission-mode", "dontAsk",
            "--permission-prompts", "none",
        ],
        "cli_stdin": True, "cli_model_flag": "--model",
        "cli_strip_env": [
            "ANTHROPIC_*", "CLAUDE_CODE_USE_*", "CLAUDE_CODE_OAUTH_TOKEN",
            "ZAI_API_KEY",
        ],
        "cli_env": {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"},
        "cli_env_from": {"ANTHROPIC_AUTH_TOKEN": "ZAI_API_KEY"},
        "required_env": ["ZAI_API_KEY"], "cli_timeout": 300,
        "transport": "claude-cli-zai", "billing": "subscription",
        "supports_coding": False,
        "setup_hint": "Set ZAI_API_KEY to the key issued by your Z.ai Coding Plan.",
    },
    {
        # DeepSeek via the claude-ds bridge (Claude Code on DeepSeek's
        # Anthropic-compatible endpoint; key in ~/.config/claude-ds/api_key)
        "id": "deepseek", "label": "DeepSeek", "color": "#4d6bfe",
        "api": "cli", "env_key": "", "default_model": "",
        "model_env": "DEEPSEEK_MODEL",
        "cli": ["claude-ds", "-p"],
        "cli_stdin": True, "cli_model_flag": "--model",
        "cli_coding_flags": ["--dangerously-skip-permissions"],
        "cli_strip_env": [], "cli_timeout": 300,
    },
    {
        "id": "groq", "label": "Groq", "color": "#f55036",
        "api": "openai", "base_url": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY", "default_model": "llama-3.3-70b-versatile",
        "model_env": "GROQ_MODEL",
    },
    {
        "id": "mistral", "label": "Mistral", "color": "#ff7000",
        "api": "openai", "base_url": "https://api.mistral.ai/v1",
        "env_key": "MISTRAL_API_KEY", "default_model": "mistral-large-latest",
        "model_env": "MISTRAL_MODEL",
    },
    {
        "id": "together", "label": "Together", "color": "#0f6fff",
        "api": "openai", "base_url": "https://api.together.xyz/v1",
        "env_key": "TOGETHER_API_KEY",
        "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "model_env": "TOGETHER_MODEL",
    },
    {
        "id": "fireworks", "label": "Fireworks", "color": "#7c3aed",
        "api": "openai", "base_url": "https://api.fireworks.ai/inference/v1",
        "env_key": "FIREWORKS_API_KEY",
        "default_model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
        "model_env": "FIREWORKS_MODEL",
    },
    {
        "id": "perplexity", "label": "Perplexity", "color": "#20808d",
        "api": "openai", "base_url": "https://api.perplexity.ai",
        "env_key": "PERPLEXITY_API_KEY", "default_model": "sonar",
        "model_env": "PERPLEXITY_MODEL",
    },
    {
        "id": "cerebras", "label": "Cerebras", "color": "#f43f5e",
        "api": "openai", "base_url": "https://api.cerebras.ai/v1",
        "env_key": "CEREBRAS_API_KEY", "default_model": "llama-3.3-70b",
        "model_env": "CEREBRAS_MODEL",
    },
    {
        "id": "sambanova", "label": "SambaNova", "color": "#00b894",
        "api": "openai", "base_url": "https://api.sambanova.ai/v1",
        "env_key": "SAMBANOVA_API_KEY",
        "default_model": "Meta-Llama-3.3-70B-Instruct",
        "model_env": "SAMBANOVA_MODEL",
    },
    {
        "id": "novita", "label": "Novita", "color": "#16a34a",
        "api": "openai", "base_url": "https://api.novita.ai/v3/openai",
        "env_key": "NOVITA_API_KEY", "default_model": "deepseek/deepseek-v3",
        "model_env": "NOVITA_MODEL",
    },
    # -- local servers (no key needed; must be running) ----------------------
    {
        "id": "ollama", "label": "Ollama", "color": "#a78bfa",
        "api": "openai", "base_url": "http://localhost:11434/v1",
        "env_key": "", "default_model": "llama3.2",
        "model_env": "OLLAMA_MODEL",
    },
    {
        "id": "lmstudio", "label": "LM Studio", "color": "#e879f9",
        "api": "openai", "base_url": "http://localhost:1234/v1",
        "env_key": "", "default_model": "openai/gpt-oss-20b",
        "model_env": "LMSTUDIO_MODEL",
    },
]

_TIMEOUT = httpx.Timeout(connect=15.0, read=180.0, write=30.0, pool=15.0)


class ProviderError(RuntimeError):
    pass


SUBSCRIPTION_PROVIDER_IDS = frozenset(
    {"claude", "grok", "gemini", "glm", "cline", "openai"}
)
SUBSCRIPTION_PROVIDER_ALIASES = {
    "astra": "openai",
    "antigravity": "gemini",
    "agy": "gemini",
    "chatgpt": "openai",
    "codex": "openai",
    "gpt": "openai",
}
SUBSCRIPTION_REGISTRY_IDS = (
    "claude", "grok", "gemini", "glm", "cline", "openai", "astra",
)


def subscription_only() -> bool:
    """Return whether dispatch is restricted to built-in subscription routes.

    Subscription-only operation is the safe default. Legacy mixed-provider
    deployments must opt in explicitly with ``LITIGATION_SUBSCRIPTION_ONLY=0``.
    """
    return os.environ.get("LITIGATION_SUBSCRIPTION_ONLY", "1").strip() != "0"


def _canonical_subscription_id(agent_id: str) -> str | None:
    canonical = SUBSCRIPTION_PROVIDER_ALIASES.get(agent_id, agent_id)
    return canonical if canonical in SUBSCRIPTION_PROVIDER_IDS else None


def _resolve_availability(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve runtime metadata on a private list of trusted registry entries."""
    for e in entries:
        env_key = e.get("env_key", "")
        if e.get("api") == "cli":
            path = shutil.which(e["cli"][0]) if e.get("cli") else None
            required = e.get("required_env", [])
            configured = all(bool(os.environ.get(name, "").strip()) for name in required)
            e["installed"] = bool(path)
            e["configured"] = configured
            enabled = e.get("runtime_enabled", True)
            e["available"] = bool(path) and configured and enabled
            if not enabled:
                e["setup_status"] = "safety_blocked"
            elif not path:
                e["setup_status"] = "not_installed"
            elif not configured:
                e["setup_status"] = "not_configured"
            else:
                e["setup_status"] = "installed_unverified"
            e["auth_status"] = "unverified"
            if path:
                e["cli"] = [path, *e["cli"][1:]]
        else:
            configured = bool(e.get("base_url")) and (
                not env_key or bool(os.environ.get(env_key, "").strip())
            )
            e["installed"] = None
            e["available"] = configured
            e["setup_status"] = "configured_unverified" if configured else "not_configured"
            e["auth_status"] = "unverified"
        e["model"] = os.environ.get(e.get("model_env", ""), "").strip() or e.get("default_model", "")
        e.setdefault("api", "openai")
        e.setdefault("transport", e["api"])
        e.setdefault("billing", "api_or_local")
        e.setdefault("setup_hint", "")
    return entries


def registry() -> list[dict[str, Any]]:
    """The full model registry with availability resolved.

    HTTP providers are available when their env var is set and non-empty
    (for custom local models with no env_key, e.g. plain Ollama, it is True).
    CLI providers are installed when their binary is on PATH.  That does not
    prove the cached login is valid, so setup/auth metadata remains explicitly
    unverified until a caller performs a real request.
    """
    if subscription_only():
        # Select by built-in ID before reading extension metadata. A custom
        # entry cannot claim subscription billing and enter this allowlist.
        builtins = {str(e["id"]): e for e in BASE_REGISTRY}
        entries = [dict(builtins[agent_id]) for agent_id in SUBSCRIPTION_REGISTRY_IDS]
        for entry in entries:
            entry["canonical_id"] = (
                SUBSCRIPTION_PROVIDER_ALIASES.get(str(entry["id"]), str(entry["id"]))
            )
        return _resolve_availability(entries)

    entries = [dict(e) for e in BASE_REGISTRY]
    raw = os.environ.get("LITIGATION_EXTRA_MODELS", "").strip()
    if raw:
        try:
            extra = json.loads(raw)
            if isinstance(extra, list):
                entries.extend(extra)
        except json.JSONDecodeError:
            pass  # a malformed blob must not take the app down
    return _resolve_availability(entries)


def get_agent(agent_id: str) -> dict[str, Any] | None:
    for entry in registry():
        if entry["id"] == agent_id:
            return entry
    if subscription_only():
        canonical = _canonical_subscription_id(agent_id)
        if canonical:
            target = next((e for e in registry() if e["id"] == canonical), None)
            if target:
                alias = next((e for e in BASE_REGISTRY if e["id"] == agent_id), {})
                return {
                    **target,
                    "id": agent_id,
                    "label": alias.get("label", target["label"]),
                    "color": alias.get("color", target["color"]),
                    "canonical_id": canonical,
                }
    return None


def resolve_agent(agent_id: str, model: str | None = None) -> dict[str, Any]:
    """Resolve a participant to a dispatch-safe provider descriptor."""
    agent = get_agent(agent_id)
    if not agent:
        if subscription_only():
            raise ProviderError(
                f"{agent_id or 'provider'} is unavailable because Litigation is subscription-only"
            )
        raise ProviderError(f"unknown provider: {agent_id or '?'}")
    resolved = dict(agent)
    # Legacy `gpt` rooms stored an HTTP model such as gpt-4o-mini. The alias
    # now uses ChatGPT CLI login, where that stale override is incompatible.
    selected_model = (
        "" if subscription_only() and agent_id == "gpt"
        else str(model or "").strip()
    )
    resolved["model"] = selected_model or str(agent.get("model") or "")
    return resolved


def _resolve_dispatch_agent(
    agent: dict[str, Any], model: str | None,
) -> tuple[dict[str, Any], str]:
    """Fail closed if an untrusted/non-subscription descriptor reaches I/O."""
    if not subscription_only():
        return agent, str(model or agent.get("model") or "")
    resolved = resolve_agent(str(agent.get("id") or ""), model or str(agent.get("model") or ""))
    if resolved.get("api") != "cli" or resolved.get("billing") != "subscription":
        raise ProviderError("provider dispatch is blocked by subscription-only policy")
    return resolved, str(resolved.get("model") or "")


def _resolve_key(agent: dict[str, Any]) -> str:
    env_key = agent.get("env_key", "")
    key = os.environ.get(env_key, "").strip() if env_key else ""
    if env_key and not key:
        raise ProviderError(
            f"{agent.get('label', agent.get('id', '?'))} has no API key set ({env_key})")
    return key or "none"  # local servers (Ollama) accept any bearer



# -- subscription CLIs ------------------------------------------------------

def _serialize_prompt(messages: list[dict[str, str]]) -> str:
    """Flatten a chat transcript into one prompt for a single-shot CLI call."""
    parts: list[str] = []
    for m in messages:
        if m["role"] == "system":
            parts.append(f"[instructions] {m['content']}")
        elif m["role"] == "assistant":
            parts.append(f"[your earlier reply] {m['content']}")
        else:
            parts.append(m["content"])
    return "\n\n".join(parts)


def _cli_env(agent: dict[str, Any]) -> dict[str, str]:
    """Subprocess env with the agent's strip-list applied.

    `cli_strip_env` entries are exact names or `PREFIX_*` wildcards — e.g.
    claude must lose ANTHROPIC_* so the CLI uses the claude.ai login instead
    of a (possibly credit-less) API key.
    """
    rules = agent.get("cli_strip_env", [])

    def struck(name: str) -> bool:
        return any(r.endswith("*") and name.startswith(r[:-1]) or name == r
                   for r in rules)

    env = {k: v for k, v in os.environ.items() if not struck(k)}
    env.update(agent.get("cli_env", {}))
    for child_name, path in agent.get("cli_env_paths", {}).items():
        env[child_name] = os.path.expanduser(path)
    for child_name, source_name in agent.get("cli_env_from", {}).items():
        value = os.environ.get(source_name, "").strip()
        if not value:
            raise ProviderError(
                f"{agent.get('label', 'Provider')} is not configured. "
                f"{agent.get('setup_hint', '')}".strip()
            )
        env[child_name] = value
    return env


def _parse_cli_output(agent: dict[str, Any], raw: bytes) -> str:
    """Extract reply text from the CLI's documented output format."""
    text = raw.decode("utf-8", "replace").strip()
    output_format = agent.get("cli_output")
    if output_format == "antigravity-json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("Antigravity returned invalid JSON") from exc
        status = str(payload.get("status", "")).lower() if isinstance(payload, dict) else ""
        if status not in {"completed", "success"}:
            raise ProviderError("Antigravity did not complete the request")
        response = payload.get("response")
        if not isinstance(response, str) or not response.strip():
            raise ProviderError("Antigravity returned no response text")
        return response.strip()
    if output_format == "cline-ndjson":
        chunks: list[str] = []
        done_reason = ""
        for line in text.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProviderError("Cline returned invalid JSON") from exc
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "error":
                raise ProviderError("Cline reported an execution error")
            if payload.get("type") == "done":
                done_reason = str(payload.get("reason", "")).lower()
                continue
            event = payload.get("event")
            if payload.get("type") != "agent_event" or not isinstance(event, dict):
                continue
            if event.get("type") == "error":
                raise ProviderError("Cline reported an execution error")
            if event.get("type") == "done":
                done_reason = str(event.get("reason", "")).lower()
                continue
            if (event.get("type") == "content_start"
                    and event.get("contentType") == "text"
                    and isinstance(event.get("text"), str)):
                chunks.append(event["text"])
        if done_reason != "completed":
            raise ProviderError("Cline did not complete the request")
        result = "".join(chunks).strip()
        if not result:
            raise ProviderError("Cline returned no response text")
        return result
    return text


async def _stop_process_group(proc: asyncio.subprocess.Process) -> None:
    """Terminate the whole CLI process tree, escalating after a short grace."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    if proc.returncode is None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=1.0)
    # The group can still contain descendants after its leader exits. Always
    # send SIGKILL to any surviving members before considering cleanup done.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    if proc.returncode is None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=1.0)


def _cli_failure_category(stdout: bytes, stderr: bytes) -> str:
    """Map known CLI failures to safe, actionable categories."""
    diagnostic = (stderr + b"\n" + stdout).decode("utf-8", "replace").lower()
    if "unsupported_client" in diagnostic or "unsupported client" in diagnostic:
        return "client authentication is no longer supported"
    if any(term in diagnostic for term in (
        "login required", "not logged in", "unauthorized", "authentication",
        "oauth", "sign in", "signin",
    )):
        return "authentication is required"
    if any(term in diagnostic for term in (
        "quota", "rate limit", "usage limit", "resource exhausted",
    )):
        return "subscription quota is unavailable"
    if any(term in diagnostic for term in (
        "model not found", "unknown model", "invalid model",
    )):
        return "the selected model is unavailable"
    return "request failed"


def _cli_success_warning(stderr: bytes) -> str:
    """Detect CLIs that exit zero after emitting only a partial result."""
    diagnostic = stderr.decode("utf-8", "replace").lower()
    if any(term in diagnostic for term in (
        "print timeout", "turn still in progress", "partial output",
    )):
        return "timed out before completing the response"
    return ""


def _transform_cli_prompt(agent: dict[str, Any], prompt: str) -> str:
    transform = agent.get("cli_prompt_transform")
    if transform == "cline-safe":
        # Cline otherwise interprets controller action labels such as
        # `read_files` as requests to invoke its own (disabled) native tools.
        instruction = (
            "Do not invoke any native Cline tool. No native tools are available. "
            "Return your entire answer as literal plain text. "
        )
        if "You are a coding agent controlled by Litigation." in prompt:
            # Agent-mode prompts use a stable controller marker. Ordinary
            # room chat must remain free-form rather than being forced to JSON.
            instruction += (
                "When the request names actions such as list_files, read_files, write_file, "
                "or run_tests, those are JSON labels for an external Litigation controller, "
                "not tools for you to call. Emit exactly one JSON object and never a tool call "
                "or Markdown fence. "
            )
        instruction += "\n\n"
        return json.dumps(instruction + prompt, ensure_ascii=True).replace("@", r"\u0040")
    if transform == "prefix-user":
        return f"User message:\n{prompt}"
    return prompt


async def _stream_cli(agent: dict[str, Any], prompt: str, cwd: str | None = None,
                      coding: bool = False, model: str = "") -> AsyncIterator[str]:
    """Run a subscription CLI headless and yield its reply as one delta.

    Entry fields: `cli` (command prefix; the binary is resolved to an
    absolute path by registry()), `cli_stdin` (pass prompt on stdin instead
    of as the trailing argv), `cli_outfile` (codex: write the final message
    to a temp file via --output-last-message), `cli_model_flag`,
    `cli_strip_env`, `cli_timeout`, `cli_coding_flags` (appended in coding
    mode to grant autonomous file/command access).

    Coding mode is accepted only for legacy harness entries that explicitly
    provide coding flags. Subscription chat entries apply their own tool and
    process restrictions. Cline runs in a unique retained state/workspace
    directory so its transcripts are preserved and project hooks cannot load.
    """
    if coding and not agent.get("supports_coding", bool(agent.get("cli_coding_flags"))):
        raise ProviderError(f"{agent.get('label', 'Provider')} is configured for chat only")
    if not agent.get("runtime_enabled", True):
        reason = agent.get("blocked_reason", "This provider route is disabled.")
        raise ProviderError(f"{agent.get('label', 'Provider')} is unavailable: {reason}")
    cmd = list(agent["cli"])
    if coding:
        cmd += [str(f) for f in agent.get("cli_coding_flags", [])]
    outfile = ""
    if agent.get("cli_outfile"):
        fd, outfile = tempfile.mkstemp(prefix="litigation-cli-", suffix=".txt")
        os.close(fd)
        cmd += ["--output-last-message", outfile]
    selected_model = model or agent.get("model", "")
    allowed_prefix = agent.get("allowed_model_prefix", "")
    if allowed_prefix and not selected_model.startswith(allowed_prefix):
        raise ProviderError(
            f"{agent.get('label', 'Provider')} model must use the {allowed_prefix} subscription route"
        )
    if agent.get("cli_model_flag") and selected_model:
        cmd += [agent["cli_model_flag"], selected_model]
    stdin_mode = bool(agent.get("cli_stdin"))
    prompt = _transform_cli_prompt(agent, prompt)
    if not stdin_mode:
        prompt_flag = agent.get("cli_prompt_flag")
        if prompt_flag:
            cmd += [str(prompt_flag), prompt]
        else:
            cmd.append(prompt)
    timeout_s = float(agent.get("cli_timeout", 300))
    runtime_dir = ""
    try:
        child_env = _cli_env(agent)
        if agent.get("cli_isolated_cwd"):
            state_root = os.path.expanduser(
                agent.get("cli_state_root", "~/.local/state/litigation/provider")
            )
            os.makedirs(state_root, mode=0o700, exist_ok=True)
            runtime_dir = tempfile.mkdtemp(prefix="run-", dir=state_root)
        if agent.get("cli_runtime_data"):
            legacy_hooks = os.path.expanduser("~/Documents/Cline/Hooks")
            if os.path.isdir(legacy_hooks):
                with os.scandir(legacy_hooks) as entries:
                    if next(entries, None) is not None:
                        raise ProviderError(
                            "Cline is unavailable while legacy global hooks are installed"
                        )
            state_root = os.path.expanduser(
                agent.get("cli_state_root", "~/.local/state/litigation/cline")
            )
            os.makedirs(state_root, mode=0o700, exist_ok=True)
            runtime_dir = tempfile.mkdtemp(prefix="run-", dir=state_root)
            child_env["CLINE_DATA_DIR"] = os.path.join(runtime_dir, "data")
        process_cwd = cwd or runtime_dir or "/tmp"
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_mode else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env, cwd=process_cwd, start_new_session=True)
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(prompt.encode() if stdin_mode else None),
                timeout=timeout_s)
        except asyncio.TimeoutError:
            await _stop_process_group(proc)
            raise ProviderError(
                f"{agent.get('label', agent.get('id', '?'))}: cli timed out after {timeout_s:.0f}s")
        except asyncio.CancelledError:
            await _stop_process_group(proc)
            raise
        warning = _cli_success_warning(err_b) if proc.returncode == 0 else ""
        if warning:
            raise ProviderError(f"{agent.get('label', '?')} CLI {warning}")
        if outfile:
            try:
                with open(outfile, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                text = ""
        else:
            try:
                text = _parse_cli_output(agent, out_b)
            except ProviderError:
                if proc.returncode == 0:
                    raise
                text = ""
        if proc.returncode != 0:
            hint = agent.get("setup_hint", "")
            suffix = f" {hint}" if hint else ""
            category = _cli_failure_category(out_b, err_b)
            raise ProviderError(
                f"{agent.get('label', '?')} CLI {category} "
                f"(exit {proc.returncode}).{suffix}")
        if not text.strip():
            raise ProviderError(f"{agent.get('label', '?')} CLI returned no response text")
        yield text.strip()
    finally:
        if outfile:
            try:
                os.remove(outfile)
            except OSError:
                pass


# -- streaming ------------------------------------------------------------


async def stream_completion(agent: dict[str, Any], messages: list[dict[str, str]],
                            model: str | None = None, max_tokens: int = 2048,
                            temperature: float = 0.7, cwd: str | None = None,
                            coding: bool = False) -> AsyncIterator[str]:
    """Stream text deltas from one agent over its native API.

    cwd/coding only affect `api: "cli"` providers: coding mode runs the
    harness inside the given workspace directory with auto-approve flags.
    HTTP providers ignore them (they have no file tools on this wire).
    """
    agent, model = _resolve_dispatch_agent(agent, model)
    api = agent.get("api", "openai")
    if api == "cli":
        async for delta in _stream_cli(agent, _serialize_prompt(messages),
                                       cwd=cwd, coding=coding, model=model):
            yield delta
        return
    key = _resolve_key(agent)
    base = agent["base_url"].rstrip("/")
    if api == "anthropic":
        async for delta in _stream_anthropic(base, key, model, messages, max_tokens, temperature):
            yield delta
    elif api == "responses":
        async for delta in _stream_responses(base, key, model, messages, max_tokens, temperature):
            yield delta
    else:
        async for delta in _stream_openai(base, key, model, messages, max_tokens, temperature):
            yield delta


async def complete(agent: dict[str, Any], messages: list[dict[str, str]],
                   model: str | None = None, max_tokens: int = 2048,
                   temperature: float = 0.7, cwd: str | None = None,
                   coding: bool = False) -> str:
    chunks: list[str] = []
    async for delta in stream_completion(agent, messages, model, max_tokens,
                                         temperature, cwd=cwd, coding=coding):
        chunks.append(delta)
    return "".join(chunks)


async def _stream_openai(base: str, key: str, model: str,
                         messages: list[dict[str, str]], max_tokens: int,
                         temperature: float) -> AsyncIterator[str]:
    url = f"{base}/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {"model": model, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "stream": True}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                detail = (await resp.aread()).decode("utf-8", "replace")[:400]
                raise ProviderError(f"{model}: provider error {resp.status_code}: {detail}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield text


async def _stream_anthropic(base: str, key: str, model: str,
                            messages: list[dict[str, str]], max_tokens: int,
                            temperature: float) -> AsyncIterator[str]:
    url = f"{base}/v1/messages"
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "Content-Type": "application/json"}
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    convo = [m for m in messages if m["role"] != "system"]
    body: dict[str, Any] = {"model": model, "max_tokens": max_tokens,
                            "temperature": temperature, "messages": convo, "stream": True}
    if system:
        body["system"] = system
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                detail = (await resp.aread()).decode("utf-8", "replace")[:400]
                raise ProviderError(f"{model}: provider error {resp.status_code}: {detail}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    obj = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "content_block_delta":
                    delta = obj.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield delta["text"]


async def _stream_responses(base: str, key: str, model: str,
                            messages: list[dict[str, str]], max_tokens: int,
                            temperature: float) -> AsyncIterator[str]:
    """OpenAI Responses API (/responses) — used by OpenCode Zen.

    Wire differences from chat/completions: input instead of messages,
    max_output_tokens instead of max_tokens, and SSE events of the form
    {"type": "response.output_text.delta", "delta": "..."}.
    """
    url = f"{base}/responses"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body: dict[str, Any] = {"model": model, "input": messages,
                            "max_output_tokens": max_tokens,
                            "temperature": temperature, "stream": True}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                detail = (await resp.aread()).decode("utf-8", "replace")[:400]
                raise ProviderError(f"{model}: provider error {resp.status_code}: {detail}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "response.output_text.delta":
                    text = obj.get("delta")
                    if isinstance(text, str) and text:
                        yield text
