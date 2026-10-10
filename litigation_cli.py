#!/usr/bin/env python3
"""Local CLI for Litigation's durable Projects API."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_URL = os.environ.get("LITIGATION_URL", "http://127.0.0.1:8766").rstrip("/")
TERMINAL = {"completed", "needs_revision", "failed", "interrupted", "cancelled"}


def request(method: str, path: str, body: dict[str, Any] | None = None, *, url: str) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw).get("detail", f"HTTP {exc.code}")
        except (json.JSONDecodeError, AttributeError):
            detail = f"HTTP {exc.code}"
        raise RuntimeError(str(detail)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach Litigation at {url}") from exc
    return json.loads(raw) if raw else None


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="litigation",
        description="Delegate durable local project work to subscription-backed agents.",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Litigation server URL")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("projects", help="list projects")
    create = commands.add_parser("create-project", help="create a managed local Git project")
    create.add_argument("name")
    create.add_argument("--description", default="")
    create.add_argument("--source-path")

    show = commands.add_parser("show-project", help="show one project")
    show.add_argument("project_id")

    issues = commands.add_parser("issues", help="list project issues")
    issues.add_argument("project_id")
    issue = commands.add_parser("create-issue", help="create a tracked issue")
    issue.add_argument("project_id")
    issue.add_argument("title")
    issue.add_argument("--body", default="")

    handoff = commands.add_parser("handoff", help="send a durable agent handoff")
    handoff.add_argument("project_id")
    handoff.add_argument("agent_id", choices=("grok", "gemini", "glm", "cline", "claude", "openai"))
    handoff.add_argument("instructions")
    handoff.add_argument("--request-id", required=True)
    handoff.add_argument("--mode", choices=("analysis", "review", "code", "agent"), default="analysis")
    handoff.add_argument("--model")
    handoff.add_argument("--acceptance", action="append", default=[])
    handoff.add_argument("--file", action="append", default=[])
    handoff.add_argument("--issue", action="append", default=[])
    handoff.add_argument("--source-output", action="append", default=[])
    handoff.add_argument("--parent")
    handoff.add_argument("--wait", action="store_true")

    status = commands.add_parser("status", help="show a handoff, optionally polling to terminal")
    status.add_argument("handoff_id")
    status.add_argument("--wait", action="store_true")
    status.add_argument("--interval", type=float, default=1.0)

    outputs = commands.add_parser("outputs", help="read persisted handoff outputs")
    outputs.add_argument("handoff_id")
    cancel = commands.add_parser("cancel", help="cancel a queued or running handoff")
    cancel.add_argument("handoff_id")
    retry = commands.add_parser("retry", help="retry as a linked new handoff")
    retry.add_argument("handoff_id")
    retry.add_argument("--request-id", required=True)
    retry.add_argument("--instructions")
    retry.add_argument("--wait", action="store_true")

    followup = commands.add_parser("followup", help="send a linked follow-up handoff")
    followup.add_argument("handoff_id")
    followup.add_argument("instructions")
    followup.add_argument("--request-id", required=True)
    followup.add_argument("--agent")
    followup.add_argument("--mode", choices=("analysis", "review", "code", "agent"))
    followup.add_argument("--acceptance", action="append", default=[])
    followup.add_argument("--file", action="append", default=[])
    followup.add_argument("--issue", action="append", default=[])
    followup.add_argument("--source-output", action="append", default=[])

    debate = commands.add_parser("debate", help="start one position and one rebuttal round")
    debate.add_argument("project_id")
    debate.add_argument("question")
    debate.add_argument("--request-id", required=True)
    debate.add_argument("--agent", action="append", required=True)
    debate.add_argument("--issue", action="append", default=[])
    debate.add_argument("--source-output", action="append", default=[])

    events = commands.add_parser("events", help="list append-only project events")
    events.add_argument("project_id")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)

    work = commands.add_parser("work-items", help="list shared work and active ownership")
    work.add_argument("project_id")
    work.add_argument("--status")
    work.add_argument("--limit", type=int, default=100)
    create_work = commands.add_parser("create-work", help="queue an idempotent shared work item")
    create_work.add_argument("project_id")
    create_work.add_argument("title")
    create_work.add_argument("instructions")
    create_work.add_argument("--request-id", required=True)
    create_work.add_argument("--acceptance", action="append", default=[])
    create_work.add_argument("--owned-path", action="append", default=[])
    create_work.add_argument("--issue", action="append", default=[])
    pull = commands.add_parser("pull-work", help="atomically assign queued work to a coding agent")
    pull.add_argument("work_item_id")
    pull.add_argument("agent_id", choices=("grok", "gemini", "glm", "cline", "claude", "openai"))
    pull.add_argument("--request-id", required=True)
    pull.add_argument("--model")
    claim = commands.add_parser("claim-work", help="link queued work to a running handoff")
    claim.add_argument("work_item_id")
    claim.add_argument("handoff_id")
    claim.add_argument("--attempt-id", required=True)
    recover = commands.add_parser("recover-work", help="explicitly requeue interrupted shared work")
    recover.add_argument("work_item_id")
    recover.add_argument("--attempt-id", required=True)
    recover.add_argument("--reason", required=True)
    verify = commands.add_parser("verify-provenance", help="check local project provenance evidence")
    verify.add_argument("project_id")

    proposal = commands.add_parser("proposal", help="show a code proposal")
    proposal.add_argument("proposal_id")
    apply = commands.add_parser("apply", help="apply a reviewed proposal to local main")
    apply.add_argument("proposal_id")
    apply.add_argument("--expected-main", required=True)

    export = commands.add_parser("export", help="save a bounded project JSON export")
    export.add_argument("project_id")
    export.add_argument("output", type=Path)
    return parser


def wait_for(handoff_id: str, *, url: str, interval: float = 1.0) -> dict[str, Any]:
    while True:
        item = request("GET", f"/api/handoffs/{urllib.parse.quote(handoff_id)}", url=url)
        if item.get("status") in TERMINAL:
            return item
        time.sleep(max(0.1, interval))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    url = args.url.rstrip("/")
    try:
        if args.command == "projects":
            result = request("GET", "/api/projects", url=url)
        elif args.command == "create-project":
            result = request("POST", "/api/projects", {
                "name": args.name, "description": args.description, "source_path": args.source_path,
            }, url=url)
        elif args.command == "show-project":
            result = request("GET", f"/api/projects/{args.project_id}", url=url)
        elif args.command == "issues":
            result = request("GET", f"/api/projects/{args.project_id}/issues", url=url)
        elif args.command == "create-issue":
            result = request("POST", f"/api/projects/{args.project_id}/issues", {
                "title": args.title, "body": args.body,
            }, url=url)
        elif args.command == "handoff":
            result = request("POST", f"/api/projects/{args.project_id}/handoffs", {
                "request_id": args.request_id, "agent_id": args.agent_id, "model": args.model,
                "mode": args.mode, "instructions": args.instructions,
                "acceptance": args.acceptance, "file_paths": args.file,
                "issue_ids": args.issue, "parent_handoff_id": args.parent,
                "source_output_ids": args.source_output,
            }, url=url)
            if args.wait:
                result = wait_for(result["id"], url=url)
        elif args.command == "status":
            result = wait_for(args.handoff_id, url=url, interval=args.interval) if args.wait else request(
                "GET", f"/api/handoffs/{args.handoff_id}", url=url
            )
        elif args.command == "outputs":
            result = request("GET", f"/api/handoffs/{args.handoff_id}/outputs", url=url)
        elif args.command == "cancel":
            result = request("POST", f"/api/handoffs/{args.handoff_id}/cancel", {}, url=url)
        elif args.command == "retry":
            result = request("POST", f"/api/handoffs/{args.handoff_id}/retry", {
                "request_id": args.request_id, "instructions": args.instructions,
            }, url=url)
            if args.wait:
                result = wait_for(result["id"], url=url)
        elif args.command == "followup":
            result = request("POST", f"/api/handoffs/{args.handoff_id}/followup", {
                "request_id": args.request_id, "instructions": args.instructions,
                "agent_id": args.agent, "mode": args.mode,
                "acceptance": args.acceptance, "file_paths": args.file,
                "issue_ids": args.issue, "source_output_ids": args.source_output,
            }, url=url)
        elif args.command == "debate":
            result = request("POST", f"/api/projects/{args.project_id}/debates", {
                "request_id": args.request_id, "question": args.question,
                "agent_ids": args.agent, "issue_ids": args.issue,
                "source_output_ids": args.source_output,
            }, url=url)
        elif args.command == "events":
            query = urllib.parse.urlencode({"after": args.after, "limit": args.limit})
            result = request("GET", f"/api/projects/{args.project_id}/events?{query}", url=url)
        elif args.command == "work-items":
            query = urllib.parse.urlencode({"limit": args.limit, **({"status": args.status} if args.status else {})})
            result = request("GET", f"/api/projects/{args.project_id}/work-items?{query}", url=url)
        elif args.command == "create-work":
            result = request("POST", f"/api/projects/{args.project_id}/work-items", {
                "request_id": args.request_id, "title": args.title,
                "instructions": args.instructions, "acceptance": args.acceptance,
                "owned_paths": args.owned_path, "issue_ids": args.issue,
            }, url=url)
        elif args.command == "pull-work":
            result = request("POST", f"/api/work-items/{args.work_item_id}/pull", {
                "request_id": args.request_id, "agent_id": args.agent_id, "model": args.model,
            }, url=url)
        elif args.command == "claim-work":
            result = request("POST", f"/api/work-items/{args.work_item_id}/claim", {
                "attempt_id": args.attempt_id, "handoff_id": args.handoff_id,
            }, url=url)
        elif args.command == "recover-work":
            result = request("POST", f"/api/work-items/{args.work_item_id}/recover", {
                "attempt_id": args.attempt_id, "reason": args.reason,
            }, url=url)
        elif args.command == "verify-provenance":
            result = request("GET", f"/api/projects/{args.project_id}/provenance", url=url)
        elif args.command == "proposal":
            result = request("GET", f"/api/proposals/{args.proposal_id}", url=url)
        elif args.command == "apply":
            result = request("POST", f"/api/proposals/{args.proposal_id}/apply", {
                "expected_main": args.expected_main,
            }, url=url)
        elif args.command == "export":
            bundle = request("GET", f"/api/projects/{args.project_id}/export", url=url)
            args.output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
            result = {"ok": True, "path": str(args.output), "project_id": args.project_id}
        else:
            raise AssertionError(args.command)
    except (RuntimeError, OSError) as exc:
        print(f"litigation: {exc}", file=sys.stderr)
        return 1
    print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
