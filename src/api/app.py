"""FastAPI assessment interface (S1: mock mode).

    uvicorn src.api.app:create_app --factory --host 127.0.0.1 --port 8000

Environment variable NAMES (values only in the git-ignored .env, never printed): ASSESSMENT_API_KEY (required; the app refuses to start
without it), ALLOW_PAID_CALLS (live gate), OPENAI_MODEL (cost estimate only), ASSESSMENT_STORE_DIR (optional override of runs/assessments), ASSESSMENT_DEADLINE_SECONDS (HTTP wait, default 150).
Responses never contain credentials, file paths, connection details or exception text."""
from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, Path as PathParam, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api import notion_blocks
from src.api.events import EventSink, NullEventSink
from src.api.models import REQUEST_ID_RE, ApiRequest
from src.api.runners import RunnerFactory, default_runner_factory
from src.api.service import AssessmentService, error_body
from src.api.store import FileStore

VERSION = "0.1.0-s1"
DEFAULT_STORE = Path(__file__).resolve().parents[2] / "runs" / "assessments"
log = logging.getLogger("procurement.api")


def _db_ok() -> bool:
    """Read-only SELECT 1. Returns a bool only; never any detail."""
    try:
        from src.db.preflight import connect
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except BaseException:                      # SystemExit from a missing DATABASE_URL included
        return False


class NotionVerifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    children: list[dict[str, Any]] = Field(max_length=5000)          # the blocks currently on the Notion page, as Notion returned them


def _safe(text: str, limit: int = 160) -> str:
    return " ".join(str(text).split())[:limit]


def create_app(*, store_dir: Path | None = None, api_key: str | None = None, runner_factory: RunnerFactory | None = None,
               sink: EventSink | None = None, deadline_seconds: float | None = None, live_allowed: Callable[[], bool] | None = None,
               db_check: Callable[[], bool] | None = None, overdue_after_seconds: float = 600.0) -> FastAPI:
    load_dotenv()
    key = api_key if api_key is not None else os.getenv("ASSESSMENT_API_KEY", "")
    if not key:
        raise RuntimeError("ASSESSMENT_API_KEY is not set; refusing to start without authentication")
    deadline = deadline_seconds if deadline_seconds is not None else float(os.getenv("ASSESSMENT_DEADLINE_SECONDS") or 150.0)
    allowed = live_allowed or (lambda: os.getenv("ALLOW_PAID_CALLS") == "1")
    store = FileStore(store_dir or Path(os.getenv("ASSESSMENT_STORE_DIR") or DEFAULT_STORE))
    service = AssessmentService(store, runner_factory or default_runner_factory, sink or NullEventSink(), live_allowed=allowed,
                                model=os.getenv("OPENAI_MODEL") or None, overdue_after_seconds=overdue_after_seconds)
    check_db = db_check or _db_ok
    app = FastAPI(title="Procurement Risk Assessment API", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service

    def respond(result: tuple[int, dict, dict]) -> JSONResponse:
        status, body, headers = result
        return JSONResponse(body, status_code=status, headers=headers)

    def require_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
        if x_api_key is None or not hmac.compare_digest(x_api_key.encode("utf-8"), key.encode("utf-8")):
            raise StarletteHTTPException(status_code=401, detail="UNAUTHORIZED")

    # ───────── safe errors ─────────
    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [{"field": _safe(".".join(str(p) for p in e["loc"][1:]) or "body", 80), "message": _safe(e["msg"])} for e in exc.errors()[:10]]
        body = error_body("INVALID_REQUEST", "The request is invalid.")
        body["error"]["problems"] = problems
        return JSONResponse(body, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "UNAUTHORIZED", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "ERROR")
        message = {"UNAUTHORIZED": "A valid X-API-Key header is required.", "NOT_FOUND": "Not found.", "METHOD_NOT_ALLOWED": "Method not allowed."}.get(code, "Request failed.")
        return JSONResponse(error_body(code, message), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled %s", type(exc).__name__)
        return JSONResponse(error_body("INTERNAL_ERROR", "Unexpected internal error."), status_code=500)

    # ───────── routes ─────────
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": VERSION, "live_enabled": bool(allowed()), "db": "ok" if check_db() else "error"}

    @app.post("/assessments", dependencies=[Depends(require_key)])
    def post_assessment(req: ApiRequest) -> JSONResponse:
        return respond(service.submit(req, deadline))

    @app.get("/assessments/{request_id}", dependencies=[Depends(require_key)])
    def get_assessment(request_id: str = PathParam(pattern=REQUEST_ID_RE.pattern)) -> JSONResponse:
        return respond(service.get(request_id))

    @app.get("/assessments/{request_id}/report.md", dependencies=[Depends(require_key)], response_model=None)
    def get_report(request_id: str = PathParam(pattern=REQUEST_ID_RE.pattern)):
        status, body, headers = service.get(request_id)
        if status != 200:
            return respond((status, body, headers))
        md = body.get("report_markdown")
        if not md:
            return JSONResponse(error_body("REPORT_NOT_AVAILABLE", "This assessment produced no report.", request_id), status_code=404)
        return PlainTextResponse(md, media_type="text/markdown; charset=utf-8")

    # ───────── Notion archive support (pure rendering of the stored report; FastAPI never calls Notion) ─────────
    def archivable(request_id: str):
        """(None, (source_markdown, envelope)) for a finished completed/partial record, else (response, None)."""
        status, body, headers = service.get(request_id)
        if status != 200:
            return respond((status, body, headers)), None
        md = body.get("report_markdown")
        if body.get("assessment_status") not in ("completed", "partial") or not md:
            return JSONResponse(error_body("REPORT_NOT_AVAILABLE", "This assessment has no archivable report.", request_id), status_code=404), None
        return None, (md, body)

    @app.get("/assessments/{request_id}/notion-blocks", dependencies=[Depends(require_key)])
    def get_notion_blocks(request_id: str = PathParam(pattern=REQUEST_ID_RE.pattern), offset: int = Query(default=0, ge=0, le=1_000_000),
                          limit: int = Query(default=notion_blocks.MAX_PAGE, ge=1, le=notion_blocks.MAX_PAGE)) -> JSONResponse:
        early, found = archivable(request_id)
        if early is not None:
            return early
        md, env = found
        blocks = notion_blocks.render_blocks(md)
        rep = env.get("report") or {}
        page = blocks[offset:offset + limit]
        nxt = offset + len(page)
        return JSONResponse({
            "request_id": request_id, "assessment_status": env["assessment_status"], "mode": "mock" if (env.get("execution") or {}).get("mock") else "live",
            "report_sha256": notion_blocks.sha256_text(md),                                   # SOURCE: exact report.md bytes, never altered
            "canonical_sha256": notion_blocks.sha256_text(notion_blocks.canonical(md)),         # what the blocks reproduce (layout-normalised)
            "blocks_sha256": notion_blocks.sha256_text(json.dumps(blocks, sort_keys=True, separators=(",", ":"))),
            "total_blocks": len(blocks), "offset": offset, "limit": limit, "count": len(page), "next_offset": nxt if nxt < len(blocks) else None,
            "page": {"title": (env.get("report_title") or f"Assessment {request_id}")[:200], "properties": {
                "request_id": request_id, "ingredient": rep.get("ingredient"), "horizon_months": rep.get("horizon_months"),
                "assessment_date": env.get("assessment_date"), "mode": "mock" if (env.get("execution") or {}).get("mock") else "live",
                "assessment_status": env["assessment_status"], "warnings": len(env.get("warnings") or []),
                "claims_removed": len(rep.get("removed_claims") or []), "schema_version": env.get("schema_version")}},
            "blocks": page})

    @app.post("/assessments/{request_id}/notion-verify", dependencies=[Depends(require_key)])
    def verify_notion_prefix(body: NotionVerifyBody, request_id: str = PathParam(pattern=REQUEST_ID_RE.pattern)) -> JSONResponse:
        """Read-only check (does not start or change anything): are the blocks already on the Notion page exactly the expected prefix?"""
        early, found = archivable(request_id)
        if early is not None:
            return early
        md, _ = found
        expected = notion_blocks.render_blocks(md)
        return JSONResponse({"request_id": request_id, "report_sha256": notion_blocks.sha256_text(md),
                             "canonical_sha256": notion_blocks.sha256_text(notion_blocks.canonical(md)),
                             **notion_blocks.verify_prefix(expected, body.children)})

    return app
