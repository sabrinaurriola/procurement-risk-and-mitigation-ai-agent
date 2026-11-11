"""Request model, request hash and the response envelope built by the API layer.

`run_state` (running / finished) describes the EXECUTION of a request; `assessment_status` (completed / partial / failed) describes the
RESULT of a finished assessment. They are never merged: a request that is still running has no assessment status."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from src.agent.schemas import AssessmentRequest, AssessmentResponse

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ApiRequest(AssessmentRequest):
    """Same validation as the agent's request (extra fields forbidden) plus the run mode. `live` is the specification default; it is refused
    unless the server flag allows paid calls, so an omitted mode can never spend anything by accident."""
    mode: Literal["live", "mock"] = "live"


def request_hash(req: ApiRequest) -> str:
    """SHA-256 of the parameters that define the work (spec section 8): ingredient, horizon, assessment_date (as sent), mode."""
    canonical = {"ingredient": req.ingredient, "horizon_months": req.horizon_months,
                 "assessment_date": req.assessment_date.isoformat() if req.assessment_date else None, "mode": req.mode}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode("utf-8")).hexdigest()


def agent_request(req: ApiRequest) -> dict[str, Any]:
    """The dict the LangGraph entry point expects (no API-only fields)."""
    out: dict[str, Any] = {"request_id": req.request_id, "ingredient": req.ingredient, "horizon_months": req.horizon_months}
    if req.assessment_date is not None:
        out["assessment_date"] = req.assessment_date.isoformat()
    return out


def build_envelope(response: AssessmentResponse, *, replay: bool = False) -> dict[str, Any]:
    """Finished-run envelope. Everything except the four API-layer fields is the existing AssessmentResponse. `status` is kept for the
    contract with n8n and equals `assessment_status`."""
    env = response.model_dump(mode="json")
    rid = env["request_id"]
    env.update({
        "run_state": "finished",
        "assessment_status": env["status"],
        "idempotent_replay": replay,
        "report_location": {"markdown": f"/assessments/{rid}/report.md" if env.get("report_markdown") else None, "json": f"/assessments/{rid}"},
    })
    return env


def running_body(request_id: str, started_at: str, *, overdue: bool = False) -> dict[str, Any]:
    return {"request_id": request_id, "run_state": "running", "assessment_status": None, "started_at": started_at, "overdue": overdue}
