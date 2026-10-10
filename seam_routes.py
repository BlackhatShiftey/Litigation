"""FastAPI routes for Litigation's Alpine SEAM bridge."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from seam_bridge import SeamBridge, SeamBridgeError, SeamSecretRejected
from seam_sync import SeamSyncCoordinator


class SeamPublish(BaseModel):
    event_id: str
    kind: str
    entity_id: str
    sequence: int = Field(ge=0)
    text: str
    provenance: dict[str, Any] = Field(default_factory=dict)


def create_seam_router(
    bridge: SeamBridge,
    coordinator: SeamSyncCoordinator | None = None,
    project_store: Any | None = None,
) -> APIRouter:
    router = APIRouter()

    def require_project(project_id: str) -> None:
        if project_store is not None and not project_store.get_project(project_id):
            raise HTTPException(status_code=404, detail="project not found")

    @router.get("/knowledge", include_in_schema=False)
    def knowledge_page() -> FileResponse:
        return FileResponse(Path(__file__).parent / "seam.html")

    @router.get("/seam.js", include_in_schema=False)
    def knowledge_script() -> FileResponse:
        return FileResponse(Path(__file__).parent / "seam.js", media_type="text/javascript")

    @router.get("/seam.css", include_in_schema=False)
    def knowledge_styles() -> FileResponse:
        return FileResponse(Path(__file__).parent / "seam.css", media_type="text/css")

    @router.get("/api/seam/status")
    async def seam_status() -> dict[str, Any]:
        result = await asyncio.to_thread(bridge.status)
        result["sync"] = coordinator.status() if coordinator is not None else {
            "enabled": False, "running": False, "last_error": None, "last_run": None,
        }
        return result

    @router.get("/api/seam/outbox")
    async def seam_outbox(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
        items, counts = await asyncio.gather(
            asyncio.to_thread(bridge.list_outbox, limit),
            asyncio.to_thread(bridge.outbox_counts),
        )
        return {"items": items, "counts": counts}

    @router.post("/api/seam/retry")
    async def seam_retry(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
        return await asyncio.to_thread(bridge.retry_pending, limit)

    @router.post("/api/seam/sync")
    async def seam_sync() -> dict[str, Any]:
        if coordinator is None:
            raise HTTPException(status_code=503, detail="SEAM project synchronization is not configured")
        return await coordinator.sync_once()

    @router.post("/api/projects/{project_id}/seam/publish", status_code=202)
    async def seam_publish(project_id: str, body: SeamPublish) -> dict[str, Any]:
        require_project(project_id)
        try:
            queued = await asyncio.to_thread(
                bridge.enqueue_event,
                project_id, body.event_id, body.kind, body.entity_id,
                body.sequence, body.text, body.provenance,
            )
        except SeamSecretRejected as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SeamBridgeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if coordinator is not None:
            coordinator.notify()
        return queued

    async def knowledge(operation: str, project_id: str, query: str, limit: int) -> dict[str, Any]:
        require_project(project_id)
        try:
            method = bridge.search if operation == "search" else bridge.graph
            return await asyncio.to_thread(method, project_id, query, limit)
        except SeamBridgeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/api/projects/{project_id}/seam/search")
    async def seam_search(
        project_id: str, q: str = Query(..., min_length=1, max_length=4000),
        limit: int = Query(10, ge=1, le=50),
    ) -> dict[str, Any]:
        return await knowledge("search", project_id, q, limit)

    @router.get("/api/projects/{project_id}/seam/graph")
    async def seam_graph(
        project_id: str, q: str = Query(..., min_length=1, max_length=4000),
        limit: int = Query(10, ge=1, le=50),
    ) -> dict[str, Any]:
        return await knowledge("graph", project_id, q, limit)

    return router
