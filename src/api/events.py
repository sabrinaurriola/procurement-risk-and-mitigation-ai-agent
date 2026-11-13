"""Operational events: taxonomy, validation and a sink interface. S1 ships only NullEventSink and InMemoryEventSink (tests);
NO database writer exists yet (migration 002 is a DRAFT in sql/drafts/ and is not applied; see docs/day4-architecture.md section 8).

Every metric below comes from a value the API actually has. Nothing is invented:
  duration_ms           <- execution.elapsed_seconds * 1000 (the graph's own clock); for assessment.timeout the API's wall-clock wait
  input/output_tokens   <- execution.tokens (LLM tokens only; embedding tokens are in runs/usage.jsonl, not in the envelope -> not reported)
  cost_usd              <- ESTIMATE = src.agent.run.estimate_cost(model, tokens); None for mock runs, unknown models, or failed runs
  llm_calls/tool_calls/research_rounds <- execution.*
  research.source rows  <- report.source_status (absent when the assessment failed and produced no report)
  validation.removed    <- report.removed_claims (count and kinds only; claim text is never stored)
  benchmark/financial status, warnings, missing_data <- report.* / envelope counts
Events never contain report text, evidence, prompts, e-mail addresses or exception text.

The enumerations, coupling rules, detail keys and limits below mirror the CHECK constraints of the DRAFT migration
sql/drafts/002_ops_events.sql; tests/test_ops_events_sql.py parses that file and fails if the two drift apart."""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

log = logging.getLogger("procurement.api")

EventType = Literal["assessment.received", "assessment.replayed", "assessment.timeout", "assessment.interrupted", "assessment.finished",
                    "research.source", "validation.removed", "archive.stored", "archive.failed", "delivery.sent", "delivery.failed"]
EVENT_TYPES: tuple[str, ...] = get_args(EventType)
# Only OUTCOMES are events: "pending" lives in Notion; "skipped" does not exist (a failed assessment is never archived).
ArchiveStatus = Literal["incomplete", "stored", "failed"]
DeliveryStatus = Literal["sent", "failed"]
AssessmentStatus = Literal["completed", "partial", "failed"]
DETAIL_KEYS = frozenset({"source", "status_class", "status", "count", "kinds", "llm_calls", "tool_calls", "research_rounds", "warnings",
                         "missing_data", "benchmark_status", "financial_status", "cost_estimated", "http_wait_ms",
                         "notion_page_id", "report_sha256", "blocks_total", "blocks_appended", "duplicate_pages", "retry_count",
                         "notice_kind", "archive_failed"})
# ───────── value-level privacy (mirrors ck_detail_values / ck_detail_no_sensitive in the draft SQL; tests compare them) ─────────
# detail may carry only: counters, fixed-vocabulary words, one uppercase reason code, a UUID, a SHA-256, small count maps. Free text cannot fit.
SOURCES = ("tavily", "gdelt", "policy", "filtering", "other")
STATUS_CLASSES = ("ok", "partial", "unavailable", "not_configured", "not_used", "not_run", "fallback", "filtered", "other")
PROBLEM_CLASSES = frozenset({"unavailable", "partial", "not_configured"})
BENCHMARK_STATUSES = ("available", "stale", "unavailable")
FINANCIAL_STATUSES = ("available", "unavailable")
NOTICE_KINDS = ("report", "operator")
REMOVED_KINDS = ("risks", "mitigation_options", "supplier_candidates", "recommended_actions", "other")     # = report sections that can be pruned
REASON_CODE_RE = r"^[A-Z][A-Z0-9_]{1,39}$"
NOTION_ID_RE = r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$"
SHA256_RE = r"^[0-9a-f]{64}$"
TOOL_NAME_RE = r"^[a-z][a-z_]{0,30}$"
# Same alternation in Python (re.I) and SQL (~*): markers that must never appear anywhere in the serialised detail.
SENSITIVE_MARKERS = r"(://|@|bearer|api[_-]?key|passw(or)?d|secret|token|authorization|sk-[a-z0-9]|postgres(ql)?:)"
COUNTER_KEYS = frozenset({"count", "llm_calls", "research_rounds", "warnings", "missing_data", "http_wait_ms", "blocks_total", "blocks_appended",
                          "duplicate_pages", "retry_count"})
DETAIL_MAX_BYTES_DB = 4096          # the CHECK in the database (octet_length of jsonb::text)
DETAIL_MAX_BYTES = 4000             # stricter here, so formatting differences between json.dumps and jsonb::text can never trip the CHECK
KEY_RE = r"^[A-Za-z0-9_.:|-]{1,200}$"
INT_MAX = 2_147_483_647
REMOVED_RE = re.compile(r"^(\w+) '")
# Columns a database sink writes, in order (event_id and recorded_at are database defaults). Must equal the draft SQL (tested).
INSERT_COLUMNS = ("event_key", "occurred_at", "request_id", "component", "event_type", "severity", "mode", "assessment_status", "archive_status",
                  "delivery_status", "error_code", "duration_ms", "input_tokens", "output_tokens", "cost_usd", "detail")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _counter(v: Any) -> bool:
    return type(v) is int and 0 <= v <= INT_MAX                # bool is rejected (type() is exact)


def _in(values: tuple[str, ...]):
    return lambda v: isinstance(v, str) and v in values


def _rx(pattern: str):
    return lambda v: isinstance(v, str) and re.fullmatch(pattern, v) is not None


def _count_map(key_ok, max_len: int):
    return lambda v: isinstance(v, dict) and len(v) <= max_len and all(key_ok(k) and _counter(n) for k, n in v.items())


DETAIL_VALIDATORS = {
    **{k: _counter for k in COUNTER_KEYS},
    "source": _in(SOURCES), "status_class": _in(STATUS_CLASSES), "status": _rx(REASON_CODE_RE),
    "benchmark_status": _in(BENCHMARK_STATUSES), "financial_status": _in(FINANCIAL_STATUSES), "notice_kind": _in(NOTICE_KINDS),
    "notion_page_id": _rx(NOTION_ID_RE), "report_sha256": _rx(SHA256_RE),
    "cost_estimated": lambda v: type(v) is bool, "archive_failed": lambda v: type(v) is bool,
    "kinds": _count_map(lambda k: isinstance(k, str) and k in REMOVED_KINDS, len(REMOVED_KINDS)),
    "tool_calls": _count_map(lambda k: isinstance(k, str) and re.fullmatch(TOOL_NAME_RE, k) is not None, 10),
}
assert set(DETAIL_VALIDATORS) == DETAIL_KEYS                    # every allowed key has a value rule


class OperationalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_key: str = Field(pattern=KEY_RE)
    occurred_at: datetime = Field(default_factory=_utcnow)     # when the event HAPPENED, not when it is stored (an outbox flush may be late)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    component: Literal["api", "n8n"] = "api"
    event_type: EventType
    severity: Literal["info", "warning", "error"] = "info"
    mode: Literal["live", "mock"] | None = None
    assessment_status: AssessmentStatus | None = None
    archive_status: ArchiveStatus | None = None
    delivery_status: DeliveryStatus | None = None
    error_code: str | None = Field(default=None, pattern=r"^[A-Z_]{1,64}$")
    duration_ms: int | None = Field(default=None, ge=0, le=INT_MAX)
    input_tokens: int | None = Field(default=None, ge=0, le=INT_MAX)
    output_tokens: int | None = Field(default=None, ge=0, le=INT_MAX)
    cost_usd: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=6)
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        return v

    @field_validator("detail")
    @classmethod
    def _detail_ok(cls, v: dict[str, Any]) -> dict[str, Any]:
        extra = set(v) - DETAIL_KEYS
        if extra:
            raise ValueError(f"detail keys not allowed: {sorted(extra)}")
        for k, val in v.items():                                    # messages name the KEY only, never the offending value
            if not DETAIL_VALIDATORS[k](val):
                raise ValueError(f"detail value for {k!r} is not in its permitted form")
        text = json.dumps(v, ensure_ascii=False, default=str)
        if re.search(SENSITIVE_MARKERS, text, re.I):
            raise ValueError("detail contains a sensitive marker")
        if len(text.encode("utf-8")) > DETAIL_MAX_BYTES:
            raise ValueError(f"detail larger than {DETAIL_MAX_BYTES} bytes")
        return v

    @model_validator(mode="after")
    def _coupling(self) -> "OperationalEvent":
        """Same rules as the table CHECKs ck_event_key, ck_component_matches_type, ck_assessment_status_only_on_finished,
        ck_archive_status_matches, ck_delivery_status_matches and ck_metrics_placement."""
        t = self.event_type
        if not self.event_key.startswith(self.request_id + "|"):
            raise ValueError("event_key must start with '<request_id>|'")
        if (self.component == "n8n") != t.startswith(("archive.", "delivery.")):
            raise ValueError("component must be n8n exactly for archive.* and delivery.* events")
        if (t == "assessment.finished") != (self.assessment_status is not None):
            raise ValueError("assessment_status is required on assessment.finished and allowed nowhere else")
        allowed_archive = {"archive.stored": {"stored"}, "archive.failed": {"incomplete", "failed"}}
        if t in allowed_archive:
            if self.archive_status not in allowed_archive[t]:
                raise ValueError(f"{t} needs archive_status in {sorted(allowed_archive[t])}")
        elif self.archive_status is not None:
            raise ValueError("archive_status is only allowed on archive.* events")
        allowed_delivery = {"delivery.sent": "sent", "delivery.failed": "failed"}
        if t in allowed_delivery:
            if self.delivery_status != allowed_delivery[t]:
                raise ValueError(f"{t} needs delivery_status {allowed_delivery[t]!r}")
        elif self.delivery_status is not None:
            raise ValueError("delivery_status is only allowed on delivery.* events")
        if self.duration_ms is not None and t not in ("assessment.finished", "assessment.timeout"):
            raise ValueError("duration_ms is only allowed on assessment.finished and assessment.timeout")
        if (self.input_tokens is not None or self.output_tokens is not None or self.cost_usd is not None) and t != "assessment.finished":
            raise ValueError("token and cost metrics are only allowed on assessment.finished")
        return self

    def to_row(self) -> dict[str, Any]:
        """Column -> value for an INSERT (keys equal INSERT_COLUMNS). `detail` stays a dict; the sink wraps it as jsonb."""
        data = self.model_dump()
        return {c: data[c] for c in INSERT_COLUMNS}


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


def classify_source_status(text: str) -> tuple[str, str | None]:
    """Free-text research status -> (fixed-vocabulary class, optional uppercase reason code). The text itself is never stored: it can contain
    URLs, queries or exception text. Anything not recognised becomes class 'other' with no code."""
    t = str(text).strip()
    head = t.split(":", 1)[0].strip().lower()
    if head in STATUS_CLASSES and head not in ("filtered", "other"):
        cls = head
    elif t.lower().startswith("excluded search results"):
        cls = "filtered"
    else:
        cls = "other"
    code = None
    if cls == "unavailable":
        m = re.match(r"unavailable:\s*([A-Z][A-Z0-9_]{1,39})(?![A-Za-z0-9_])", t)
        code = m.group(1) if m else "OTHER"
    return cls, code


def _safe_counter_map(raw: Any, key_ok, max_len: int) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in list(raw.items())[:max_len] if isinstance(k, str) and key_ok(k) and _counter(v)}


def _int_or_none(v: Any) -> int | None:
    return v if _counter(v) else None


def events_from_envelope(env: dict[str, Any], *, model: str | None = None) -> list[OperationalEvent]:
    """Events for a FINISHED run, derived only from the envelope (no change to the agent graph)."""
    from src.agent.run import estimate_cost            # estimate from the documented price table; None when the model is unknown

    rid, status = env["request_id"], env["status"]
    ex = env.get("execution") or {}
    mode = "mock" if ex.get("mock") else "live"
    rep = env.get("report") or {}
    out: list[OperationalEvent] = []

    seen_sources: set[str] = set()
    for name, st in sorted((rep.get("source_status") or {}).items(), key=lambda kv: str(kv[0])):
        source = str(name) if str(name) in SOURCES else "other"
        cls, code = classify_source_status(str(st))
        key_name = source if source != "other" else f"other{len(seen_sources)}"          # distinct, bounded key even for unknown sources
        seen_sources.add(key_name)
        detail = {"source": source, "status_class": cls, **({"status": code} if code else {})}
        out.append(OperationalEvent(event_key=f"{rid}|research.source|{key_name}", request_id=rid, event_type="research.source", mode=mode,
                                    severity="warning" if cls in PROBLEM_CLASSES else "info", detail=detail))
    removed = rep.get("removed_claims") or []
    if removed:
        kinds: dict[str, int] = {}
        for r in removed:
            m = REMOVED_RE.match(str(r))
            k = m.group(1) if m and m.group(1) in REMOVED_KINDS else "other"        # a malformed entry can never leak a claim word as a key
            kinds[k] = kinds.get(k, 0) + 1
        out.append(OperationalEvent(event_key=f"{rid}|validation.removed", request_id=rid, event_type="validation.removed", mode=mode,
                                    severity="warning", detail={"count": len(removed), "kinds": kinds}))

    tokens = ex.get("tokens") or {}
    tin, tout = _int_or_none(tokens.get("input")), _int_or_none(tokens.get("output"))
    cost = None
    if not ex.get("mock") and tin is not None and tout is not None and status != "failed":
        c = estimate_cost(model, tin, tout)
        cost = Decimal(str(c)) if c is not None else None
    detail: dict[str, Any] = {"warnings": len(env.get("warnings") or []), "missing_data": len(env.get("missing_data") or []),
                              "llm_calls": _int_or_none(ex.get("llm_calls")), "research_rounds": _int_or_none(ex.get("research_rounds")),
                              "tool_calls": _safe_counter_map(ex.get("tool_calls"), lambda k: re.fullmatch(TOOL_NAME_RE, k) is not None, 10),
                              "cost_estimated": cost is not None}
    if rep:
        bs, fs = (rep.get("benchmark") or {}).get("status"), (rep.get("financial") or {}).get("status")
        detail["benchmark_status"] = bs if bs in BENCHMARK_STATUSES else None
        detail["financial_status"] = fs if fs in FINANCIAL_STATUSES else None
    err = env.get("error") or {}
    code = err.get("code")
    code = code if isinstance(code, str) and re.fullmatch(r"[A-Z_]{1,64}", code) else ("UNKNOWN_ERROR" if code else None)       # never free text
    elapsed = ex.get("elapsed_seconds")
    duration = int(round(float(elapsed) * 1000)) if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and 0 <= elapsed < INT_MAX / 1000 else None
    out.append(OperationalEvent(
        event_key=f"{rid}|assessment.finished", request_id=rid, event_type="assessment.finished", mode=mode, assessment_status=status,
        severity="error" if status == "failed" else ("warning" if status == "partial" else "info"),
        error_code=code, duration_ms=duration, input_tokens=tin, output_tokens=tout, cost_usd=cost,
        detail={k: v for k, v in detail.items() if v is not None}))
    return out
