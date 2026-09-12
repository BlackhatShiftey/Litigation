"""Provider adapters — model-agnostic chat completion with SSE streaming.

Every model in Litigation is an entry in a registry. `api` selects the wire
protocol: "openai" (OpenAI-compatible /chat/completions — covers OpenAI, xAI,
Gemini's compat endpoint, OpenRouter, Ollama, DeepSeek, LM Studio, ...) or
"anthropic" (native Messages API). Adding a model is a registry entry or a
LITIGATION_EXTRA_MODELS env blob, not new code.
"""
from __future__ import annotations

import json
import os
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
        "id": "claude", "label": "Claude", "color": "#d97757",
        "api": "anthropic", "base_url": "https://api.anthropic.com",
        "env_key": "ANTHROPIC_API_KEY", "default_model": "claude-sonnet-4-5",
        "model_env": "ANTHROPIC_MODEL",
    },
    {
        "id": "grok", "label": "Grok", "color": "#cbd5e1",
        "api": "openai", "base_url": "https://api.x.ai/v1",
        "env_key": "XAI_API_KEY", "default_model": "grok-4",
        "model_env": "XAI_MODEL",
    },
    {
        "id": "gemini", "label": "Gemini", "color": "#4285f4",
        "api": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env_key": "GEMINI_API_KEY", "default_model": "gemini-2.5-flash",
        "model_env": "GEMINI_MODEL",
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
        "id": "deepseek", "label": "DeepSeek", "color": "#4d6bfe",
        "api": "openai", "base_url": "https://api.deepseek.com/v1",
        "env_key": "DEEPSEEK_API_KEY", "default_model": "deepseek-chat",
        "model_env": "DEEPSEEK_MODEL",
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


def registry() -> list[dict[str, Any]]:
    """The full model registry with availability resolved.

    Availability means "the env var for this provider is set and non-empty"
    (for custom local models with no env_key, e.g. plain Ollama, it is True).
    """
    entries = [dict(e) for e in BASE_REGISTRY]
    raw = os.environ.get("LITIGATION_EXTRA_MODELS", "").strip()
    if raw:
        try:
            extra = json.loads(raw)
            if isinstance(extra, list):
                entries.extend(extra)
        except json.JSONDecodeError:
            pass  # a malformed blob must not take the app down
    for e in entries:
        env_key = e.get("env_key", "")
        e["available"] = bool(e.get("base_url")) and (
            not env_key or bool(os.environ.get(env_key, "").strip())
        )
        e["model"] = os.environ.get(e["model_env"], "").strip() or e.get("default_model", "")
        e.setdefault("api", "openai")
    return entries


def get_agent(agent_id: str) -> dict[str, Any] | None:
    for entry in registry():
        if entry["id"] == agent_id:
            return entry
    return None


def _resolve_key(agent: dict[str, Any]) -> str:
    env_key = agent.get("env_key", "")
    key = os.environ.get(env_key, "").strip() if env_key else ""
    if env_key and not key:
        raise ProviderError(
            f"{agent.get('label', agent.get('id', '?'))} has no API key set ({env_key})")
    return key or "none"  # local servers (Ollama) accept any bearer



# -- streaming ------------------------------------------------------------


async def stream_completion(agent: dict[str, Any], messages: list[dict[str, str]],
                            model: str | None = None, max_tokens: int = 2048,
                            temperature: float = 0.7) -> AsyncIterator[str]:
    """Stream text deltas from one agent over its native API."""
    model = model or agent["model"]
    api = agent.get("api", "openai")
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
                   temperature: float = 0.7) -> str:
    chunks: list[str] = []
    async for delta in stream_completion(agent, messages, model, max_tokens, temperature):
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
