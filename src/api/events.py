"""Operational events: taxonomy, validation and a sink interface. S1 ships only NullEventSink and InMemoryEventSink (tests);
NO database writer exists yet (migration 002 is not applied; see docs/day4-architecture.md).

Every metric below comes from a value the API actually has. Nothing is invented:
  duration_ms           <- execution.elapsed_seconds * 1000 (the graph's own clock); for assessment.timeout the API's wall-clock wait
  input/output_tokens   <- execution.tokens (LLM tokens only; embedding tokens are in runs/usage.jsonl, not in the envelope -> not reported)
  cost_usd              <- ESTIMATE = src.agent.run.estimate_cost(model, tokens); None for mock runs, unknown models, or failed runs
  llm_calls/tool_calls/research_rounds <- execution.*
  research.source rows  <- report.source_status (absent when the assessment failed and produced no report)
  validation.removed    <- report.removed_claims (count and kinds only; claim text is never stored)
  benchmark/financial status, warnings, missing_data <- report.* / envelope counts
Events never contain report text, evidence, prompts, e-mail addresses or exception text."""
from __future__ import annotations

import json
import logging
import re
import threading
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger("procurement.api")

EventType = Literal["assessment.received", "assessment.replayed", "assessment.rejected", "assessment.timeout", "assessment.interrupted",
                    "assessment.finished", "research.source", "validation.removed",
                    "archive.stored", "archive.failed", "delivery.sent", "delivery.failed"]
DETAIL_KEYS = {"source", "status_class", "status", "count", "kinds", "llm_calls", "tool_calls", "research_rounds", "warnings", "missing_data",
               "benchmark_status", "financial_status", "cost_estimated", "http_wait_ms", "reason", "notion_page_id", "report_sha256"}
REMOVED_RE = re.compile(r"^(\w+) '")


class OperationalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_key: str = Field(min_length=1, max_length=200)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    component: Literal["api", "n8n"] = "api"
    event_type: EventType
    severity: Literal["info", "warning", "error"] = "info"
    mode: Literal["live", "mock"] | None = None
    assessment_status: Literal["completed", "partial", "failed"] | None = None
    archive_status: Literal["pending", "incomplete", "stored", "failed"] | None = None
    delivery_status: Literal["pending", "sent", "failed"] | None = None
    error_code: str | None = Field(default=None, pattern=r"^[A-Z_]{1,64}$")
    duration_ms: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("detail")
    @classmethod
    def _detail_ok(cls, v: dict[str, Any]) -> dict[str, Any]:
        extra = set(v) - DETAIL_KEYS
        if extra:
            raise ValueError(f"detail keys not allowed: {sorted(extra)}")
        if len(json.dumps(v, default=str)) > 4096:
            raise ValueError("detail larger than 4096 bytes")
        return v


class EventSink(Protocol):
    def emit(self, event: OperationalEvent) -> None: ...


class NullEventSink:
    def emit(self, event: OperationalEvent) -> None:
        return None


class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[OperationalEvent] = []
        self._lock = threading.Lock()

    def emit(self, event: OperationalEvent) -> None:
        with self._lock:
            self.events.append(event)

    def types(self) -> list[str]:
        with self._lock:
            return [e.event_type for e in self.events]


def _is_source_problem(status: str) -> bool:
    return status.startswith(("unavailable", "partial", "not_configured"))


def events_from_envelope(env: dict[str, Any], *, model: str | None = None) -> list[OperationalEvent]:
    """Events for a FINISHED run, derived only from the envelope (no change to the agent graph)."""
    from src.agent.run import estimate_cost            # estimate from the documented price table; None when the model is unknown

    rid, status = env["request_id"], env["status"]
    ex = env.get("execution") or {}
    mode = "mock" if ex.get("mock") else "live"
    rep = env.get("report") or {}
    out: list[OperationalEvent] = []

    for name, st in sorted((rep.get("source_status") or {}).items()):
        st = str(st)
        out.append(OperationalEvent(event_key=f"{rid}|research.source|{name}", request_id=rid, event_type="research.source", mode=mode,
                                    severity="warning" if _is_source_problem(st) else "info",
                                    detail={"source": name, "status_class": st.split(":")[0].strip()[:40], "status": st[:200]}))
    removed = rep.get("removed_claims") or []
    if removed:
        kinds: dict[str, int] = {}
        for r in removed:
            m = REMOVED_RE.match(str(r))
            k = m.group(1) if m else "other"
            kinds[k] = kinds.get(k, 0) + 1
        out.append(OperationalEvent(event_key=f"{rid}|validation.removed", request_id=rid, event_type="validation.removed", mode=mode,
                                    severity="warning", detail={"count": len(removed), "kinds": kinds}))

    tokens = ex.get("tokens") or {}
    tin, tout = tokens.get("input"), tokens.get("output")
    cost = None
    if not ex.get("mock") and tin is not None and tout is not None and status != "failed":
        c = estimate_cost(model, int(tin), int(tout))
        cost = Decimal(str(c)) if c is not None else None
    detail: dict[str, Any] = {"warnings": len(env.get("warnings") or []), "missing_data": len(env.get("missing_data") or []),
                              "llm_calls": ex.get("llm_calls"), "tool_calls": ex.get("tool_calls") or {}, "research_rounds": ex.get("research_rounds"),
                              "cost_estimated": cost is not None}
    if rep:
        detail["benchmark_status"] = (rep.get("benchmark") or {}).get("status")
        detail["financial_status"] = (rep.get("financial") or {}).get("status")
    err = env.get("error") or {}
    out.append(OperationalEvent(
        event_key=f"{rid}|assessment.finished", request_id=rid, event_type="assessment.finished", mode=mode, assessment_status=status,
        severity="error" if status == "failed" else ("warning" if status == "partial" else "info"),
        error_code=err.get("code"), duration_ms=int(round(float(ex["elapsed_seconds"]) * 1000)) if ex.get("elapsed_seconds") is not None else None,
        input_tokens=tin, output_tokens=tout, cost_usd=cost, detail={k: v for k, v in detail.items() if v is not None}))
    return out
