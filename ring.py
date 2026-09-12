"""Litigation decoder ring.

A compact, versioned JSON envelope that agents use to talk to each other
instead of unstructured prose. Short keys keep it token-cheap; the `v` field
lets the format evolve. The UI decodes envelopes to human-readable text and
can show the raw envelope for inspection.
"""
from __future__ import annotations

import json
import re
from typing import Any

ENVELOPE_VERSION = 1

KINDS = ("claim", "rebuttal", "note", "verdict")

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


def make_envelope(agent: str, kind: str, text: str,
                  verdict: dict[str, Any] | None = None) -> dict[str, Any]:
    env: dict[str, Any] = {"v": ENVELOPE_VERSION, "from": agent, "kind": kind, "text": text}
    if verdict is not None:
        env["verdict"] = verdict
    return env


def encode(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, separators=(",", ":"))


def _valid(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("v"), int)
        and isinstance(obj.get("from"), str)
        and isinstance(obj.get("kind"), str)
        and isinstance(obj.get("text"), str)
    )


def decode(text: str) -> dict[str, Any] | None:
    """Tolerantly decode an envelope out of a model reply.

    Models wrap JSON in code fences, preambles, or trailing commentary; we
    strip fences and extract the outermost {...} block. Returns None when no
    valid envelope is present.
    """
    if not text:
        return None
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        candidates.append(cleaned[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if _valid(obj):
            return obj
    return None


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for the savings readout."""
    return max(1, len(text) // 4)


def tally(verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    """Tally verdict envelopes into a majority / confidence / dissent report.

    `verdicts` items: {"agent": str, "verdict": {"choice","confidence","rationale"}}
    Agents whose reply failed to parse as an envelope are the caller's problem;
    only well-formed verdicts are tallied, and they are reported by agent id.
    """
    counts: dict[str, int] = {}
    confs: list[float] = []
    rows: list[dict[str, Any]] = []
    for item in verdicts:
        v = item.get("verdict") or {}
        choice = str(v.get("choice", "?")).strip() or "?"
        try:
            conf = max(0.0, min(1.0, float(v.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        counts[choice] = counts.get(choice, 0) + 1
        confs.append(conf)
        rows.append({
            "agent": item.get("agent", "?"),
            "choice": choice,
            "confidence": conf,
            "rationale": str(v.get("rationale", ""))[:600],
        })
    majority = max(counts, key=counts.get) if counts else None  # type: ignore[arg-type]
    dissent = [r for r in rows if majority is None or r["choice"] != majority]
    return {
        "counts": counts,
        "majority": majority,
        "mean_confidence": round(sum(confs) / len(confs), 3) if confs else 0.0,
        "unanimous": bool(counts) and len(counts) == 1,
        "votes": rows,
        "dissent": dissent,
    }
