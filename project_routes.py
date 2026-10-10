"""FastAPI routes for durable local projects and agent handoffs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from project_runtime import ProjectRuntime, ProjectRuntimeError


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    source_path: str | None = None


class IssueCreate(BaseModel):
    title: str
    body: str = ""


class IssueEventCreate(BaseModel):
    kind: Literal["comment", "closed", "reopened"]
    text: str = ""


class HandoffCreate(BaseModel):
    request_id: str
    agent_id: str
    model: str | None = None
    mode: Literal["analysis", "review", "code", "agent"] = "analysis"
    instructions: str
    acceptance: list[str] = Field(default_factory=list)
    file_paths: list[str] = Field(default_factory=list)
    issue_ids: list[str] = Field(default_factory=list)
    parent_handoff_id: str | None = None
    source_output_ids: list[str] = Field(default_factory=list)


class RetryCreate(BaseModel):
    request_id: str
    instructions: str | None = None


class FollowupCreate(BaseModel):
    request_id: str
    instructions: str
    agent_id: str | None = None
    model: str | None = None
    mode: Literal["analysis", "review", "code", "agent"] | None = None
    acceptance: list[str] = Field(default_factory=list)
    file_paths: list[str] = Field(default_factory=list)
    issue_ids: list[str] = Field(default_factory=list)
    source_output_ids: list[str] = Field(default_factory=list)


class ProposalApply(BaseModel):
    expected_main: str


class DebateCreate(BaseModel):
    request_id: str
    question: str
    agent_ids: list[str]
    source_output_ids: list[str] = Field(default_factory=list)
    issue_ids: list[str] = Field(default_factory=list)


class WorkItemCreate(BaseModel):
    request_id: str
    title: str
    instructions: str
    acceptance: list[str] = Field(default_factory=list)
    owned_paths: list[str] = Field(default_factory=list)
    issue_ids: list[str] = Field(default_factory=list)


class WorkPull(BaseModel):
    request_id: str
    agent_id: str
    model: str | None = None


class WorkClaim(BaseModel):
    attempt_id: str
    handoff_id: str


class WorkRecover(BaseModel):
    attempt_id: str
    reason: str


def _raise_safe(exc: Exception) -> None:
    if isinstance(exc, ProjectRuntimeError):
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail="the project operation could not be completed") from exc


def _require(value: dict[str, Any] | None, label: str) -> dict[str, Any]:
    if not value:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return value


def _handoff_summary(item: dict[str, Any]) -> dict[str, Any]:
    # Large immutable context and instructions are available on the detail
    # endpoint.  Collection endpoints stay cheap and never embed outputs.
    excluded = {"context", "file_context", "instructions"}
    return {key: value for key, value in item.items() if key not in excluded}


def _project_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key not in {"issues", "handoffs"}}


def create_project_router(runtime: ProjectRuntime) -> APIRouter:
    router = APIRouter()

    @router.get("/projects", include_in_schema=False)
    def projects_page() -> FileResponse:
        return FileResponse(Path(__file__).parent / "projects.html")

    @router.get("/projects.js", include_in_schema=False)
    def projects_script() -> FileResponse:
        return FileResponse(Path(__file__).parent / "projects.js", media_type="text/javascript")

    @router.get("/projects.css", include_in_schema=False)
    def projects_styles() -> FileResponse:
        return FileResponse(Path(__file__).parent / "projects.css", media_type="text/css")

    @router.get("/shared-nav.css", include_in_schema=False)
    def shared_nav_styles() -> FileResponse:
        return FileResponse(Path(__file__).parent / "shared-nav.css", media_type="text/css")

    @router.get("/shared-nav.js", include_in_schema=False)
    def shared_nav_script() -> FileResponse:
        return FileResponse(Path(__file__).parent / "shared-nav.js", media_type="text/javascript")

    @router.get("/api/projects")
    def list_projects() -> dict[str, Any]:
        return {"items": runtime.store.list_projects()}

    @router.post("/api/projects", status_code=201)
    async def create_project(body: ProjectCreate) -> dict[str, Any]:
        try:
            return _project_summary(
                await runtime.create_project(body.name, body.description, body.source_path)
            )
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")

    @router.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        return _project_summary(_require(runtime.project_detail(project_id), "project"))

    @router.get("/api/projects/{project_id}/files")
    async def list_files(project_id: str, ref: str | None = None) -> dict[str, Any]:
        _require(runtime.store.get_project(project_id), "project")
        try:
            resolved = ref or await runtime.git.head(project_id)
            return {"ref": resolved, "items": await runtime.git.list_files(project_id, resolved)}
        except Exception as exc:
            _raise_safe(ProjectRuntimeError("local Git files could not be listed"))
            raise AssertionError("unreachable")

    @router.get("/api/projects/{project_id}/files/content")
    async def read_file(project_id: str, path: str, ref: str | None = None) -> dict[str, Any]:
        _require(runtime.store.get_project(project_id), "project")
        try:
            resolved = ref or await runtime.git.head(project_id)
            files = await runtime.git.read_files(project_id, [path], resolved)
        except Exception as exc:
            _raise_safe(ProjectRuntimeError("the selected local Git file could not be read"))
            raise AssertionError("unreachable")
        if not files:
            raise HTTPException(status_code=404, detail="file not found")
        return files[0]

    @router.get("/api/projects/{project_id}/issues")
    def list_issues(project_id: str) -> dict[str, Any]:
        _require(runtime.store.get_project(project_id), "project")
        return {"items": runtime.store.list_issues(project_id)}

    @router.post("/api/projects/{project_id}/issues", status_code=201)
    def create_issue(project_id: str, body: IssueCreate) -> dict[str, Any]:
        _require(runtime.store.get_project(project_id), "project")
        if not body.title.strip():
            raise HTTPException(status_code=400, detail="issue title is required")
        if len(body.title) > 500 or len(body.body) > 64_000:
            raise HTTPException(status_code=413, detail="issue content is too large")
        try:
            return runtime.store.create_issue(project_id, body.title.strip(), body.body)
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")

    @router.post("/api/issues/{issue_id}/events", status_code=201)
    def append_issue_event(issue_id: str, body: IssueEventCreate) -> dict[str, Any]:
        if len(body.text) > 64_000:
            raise HTTPException(status_code=413, detail="issue event text is too large")
        try:
            return runtime.store.append_issue_event(issue_id, body.kind, body.text)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="issue not found") from exc
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")

    @router.get("/api/projects/{project_id}/handoffs")
    def list_handoffs(project_id: str) -> dict[str, Any]:
        _require(runtime.store.get_project(project_id), "project")
        return {"items": [_handoff_summary(item) for item in runtime.store.list_handoffs(project_id)]}

    @router.get("/api/projects/{project_id}/work-items")
    def list_work_items(
        project_id: str, status: str | None = None,
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, Any]:
        _require(runtime.store.get_project(project_id), "project")
        try:
            return runtime.work_ledger(project_id, status=status, limit=limit)
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")

    @router.post("/api/projects/{project_id}/work-items", status_code=201)
    def create_work_item(project_id: str, body: WorkItemCreate) -> dict[str, Any]:
        try:
            return runtime.store.create_work_item(project_id, **body.model_dump())
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")

    @router.post("/api/work-items/{work_item_id}/pull", status_code=202)
    async def pull_work_item(work_item_id: str, body: WorkPull) -> dict[str, Any]:
        try:
            return await runtime.pull_work_item(work_item_id, **body.model_dump())
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")

    @router.post("/api/work-items/{work_item_id}/claim")
    def claim_work_item(work_item_id: str, body: WorkClaim) -> dict[str, Any]:
        try:
            return runtime.claim_work_item(work_item_id, **body.model_dump())
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")

    @router.post("/api/work-items/{work_item_id}/recover")
    def recover_work_item(work_item_id: str, body: WorkRecover) -> dict[str, Any]:
        try:
            return runtime.recover_work_item(work_item_id, **body.model_dump())
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")

    @router.get("/api/projects/{project_id}/provenance")
    async def verify_provenance(project_id: str) -> dict[str, Any]:
        try:
            return await runtime.verify_provenance(project_id)
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")

    @router.post("/api/projects/{project_id}/handoffs", status_code=202)
    async def create_handoff(project_id: str, body: HandoffCreate) -> dict[str, Any]:
        try:
            item = await runtime.create_handoff(project_id, **body.model_dump())
            return {"id": item["id"], "state": item.get("status", "queued")}
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")

    @router.get("/api/handoffs/{handoff_id}")
    def get_handoff(handoff_id: str) -> dict[str, Any]:
        return _require(runtime.store.get_handoff(handoff_id), "handoff")

    @router.post("/api/handoffs/{handoff_id}/cancel")
    async def cancel_handoff(handoff_id: str) -> dict[str, Any]:
        try:
            item = await runtime.cancel_handoff(handoff_id)
            return {"id": item["id"], "state": item.get("status")}
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")

    @router.post("/api/handoffs/{handoff_id}/retry", status_code=202)
    async def retry_handoff(handoff_id: str, body: RetryCreate) -> dict[str, Any]:
        try:
            item = await runtime.retry_handoff(handoff_id, body.request_id, body.instructions)
            return {"id": item["id"], "state": item.get("status", "queued")}
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")

    @router.post("/api/handoffs/{handoff_id}/followup", status_code=202)
    async def followup_handoff(handoff_id: str, body: FollowupCreate) -> dict[str, Any]:
        original = _require(runtime.store.get_handoff(handoff_id), "handoff")
        prior_outputs = runtime.store.list_outputs(handoff_id)
        source_ids = list(body.source_output_ids)
        source_ids.extend(str(item["id"]) for item in prior_outputs if item.get("id"))
        try:
            item = await runtime.create_handoff(
                str(original["project_id"]),
                request_id=body.request_id,
                agent_id=body.agent_id or str(original["agent_id"]),
                model=body.model if body.model is not None else str(original.get("model") or ""),
                mode=body.mode or str(original["mode"]),
                instructions=body.instructions,
                acceptance=body.acceptance,
                file_paths=body.file_paths,
                issue_ids=body.issue_ids,
                parent_handoff_id=handoff_id,
                source_output_ids=list(dict.fromkeys(source_ids)),
            )
            return {"id": item["id"], "state": item.get("status", "queued")}
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")

    @router.get("/api/handoffs/{handoff_id}/outputs")
    def list_outputs(handoff_id: str) -> dict[str, Any]:
        _require(runtime.store.get_handoff(handoff_id), "handoff")
        return {"items": runtime.store.list_outputs(handoff_id)}

    @router.get("/api/projects/{project_id}/events")
    def list_events(
        project_id: str,
        after: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, Any]:
        _require(runtime.store.get_project(project_id), "project")
        items = runtime.store.list_events(project_id, after=after, limit=limit)
        next_after = after
        if items:
            marker = items[-1].get("seq", items[-1].get("sequence", after))
            if isinstance(marker, int):
                next_after = marker
        has_more = bool(items) and bool(
            runtime.store.list_events(project_id, after=next_after, limit=1)
        )
        return {
            "items": items, "after": after,
            "next_after": next_after, "has_more": has_more,
        }

    @router.get("/api/proposals/{proposal_id}")
    def get_proposal(proposal_id: str) -> dict[str, Any]:
        return _require(runtime.store.get_proposal(proposal_id), "proposal")

    @router.post("/api/proposals/{proposal_id}/apply")
    async def apply_proposal(proposal_id: str, body: ProposalApply) -> dict[str, Any]:
        try:
            return await runtime.apply_proposal(proposal_id, body.expected_main)
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")

    @router.post("/api/projects/{project_id}/debates", status_code=202)
    async def create_debate(project_id: str, body: DebateCreate) -> dict[str, Any]:
        try:
            return await runtime.create_debate(project_id, **body.model_dump())
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")

    @router.get("/api/projects/{project_id}/export")
    async def export_project(project_id: str) -> Response:
        try:
            bundle = await runtime.export_project(project_id)
        except Exception as exc:
            _raise_safe(exc)
            raise AssertionError("unreachable")
        raw = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
        return Response(
            content=raw,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="litigation-project-{project_id}.json"'
            },
        )

    return router
