"""FastAPI routes for Litigation's append-only model rules ledger."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from rules_store import RevisionConflict, RulesStore, RulesStoreError


class RulePublish(BaseModel):
    content: str
    summary: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=1)


class RuleProposalCreate(BaseModel):
    scope: str
    content: str
    summary: str = Field(min_length=1, max_length=2000)
    evidence_handoff_id: str | None = None


class RuleProposalAccept(BaseModel):
    expected_revision: int = Field(ge=1)


class RuleProposalReject(BaseModel):
    reason: str = Field(default="", max_length=2000)


def _safe_error(exc: Exception) -> None:
    if isinstance(exc, RevisionConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, RulesStoreError):
        message = str(exc)
        status = 404 if "not found" in message else 400
        raise HTTPException(status_code=status, detail=message) from exc
    raise HTTPException(status_code=400, detail="the rules operation could not be completed") from exc


def create_rules_router(store: RulesStore) -> APIRouter:
    router = APIRouter()
    base = Path(__file__).parent

    @router.get("/rules", include_in_schema=False)
    def rules_page() -> FileResponse:
        return FileResponse(base / "rules.html")

    @router.get("/rules.js", include_in_schema=False)
    def rules_script() -> FileResponse:
        return FileResponse(base / "rules.js", media_type="text/javascript")

    @router.get("/rules.css", include_in_schema=False)
    def rules_styles() -> FileResponse:
        return FileResponse(base / "rules.css", media_type="text/css")

    @router.get("/api/rules")
    def list_rule_sections() -> dict[str, Any]:
        return {"items": store.list_scopes()}

    @router.get("/api/rules/proposals")
    def list_rule_proposals(
        scope: str | None = None,
        status: Literal["proposed", "accepted", "rejected"] | None = None,
    ) -> dict[str, Any]:
        try:
            return {"items": store.list_proposals(scope, status)}
        except Exception as exc:
            _safe_error(exc)
            raise AssertionError("unreachable")

    @router.post("/api/rules/proposals", status_code=201)
    def create_rule_proposal(body: RuleProposalCreate) -> dict[str, Any]:
        try:
            return store.propose(
                body.scope, body.content, body.summary, body.evidence_handoff_id,
                proposer="user",
            )
        except Exception as exc:
            _safe_error(exc)
            raise AssertionError("unreachable")

    @router.post("/api/rules/proposals/{proposal_id}/accept")
    def accept_rule_proposal(proposal_id: str, body: RuleProposalAccept) -> dict[str, Any]:
        try:
            return store.accept_proposal(proposal_id, body.expected_revision)
        except Exception as exc:
            _safe_error(exc)
            raise AssertionError("unreachable")

    @router.post("/api/rules/proposals/{proposal_id}/reject")
    def reject_rule_proposal(proposal_id: str, body: RuleProposalReject) -> dict[str, Any]:
        try:
            return store.reject_proposal(proposal_id, body.reason)
        except Exception as exc:
            _safe_error(exc)
            raise AssertionError("unreachable")

    @router.get("/api/rules/{scope}")
    def get_rule_section(scope: str) -> dict[str, Any]:
        try:
            return store.get_active(scope)
        except Exception as exc:
            _safe_error(exc)
            raise AssertionError("unreachable")

    @router.get("/api/rules/{scope}/history")
    def get_rule_history(
        scope: str, limit: int = Query(100, ge=1, le=500),
        before_revision: int | None = Query(None, ge=2),
    ) -> dict[str, Any]:
        try:
            page = store.list_revisions(scope, limit + 1, before_revision)
            has_more = len(page) > limit
            items = page[:limit]
            return {
                "items": items,
                "next_before_revision": items[-1]["revision"] if has_more and items else None,
            }
        except Exception as exc:
            _safe_error(exc)
            raise AssertionError("unreachable")

    @router.post("/api/rules/{scope}/revisions", status_code=201)
    def publish_rule_revision(scope: str, body: RulePublish) -> dict[str, Any]:
        try:
            return store.publish(
                scope, body.content, body.summary,
                expected_revision=body.expected_revision, origin="user",
            )
        except Exception as exc:
            _safe_error(exc)
            raise AssertionError("unreachable")

    return router
