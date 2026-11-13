"""Execution control: single-flight, idempotency, worker threads, deadline, restart recovery.

Rules (docs/day4-architecture.md section 4):
* The assessment runs in a server-owned worker thread. The HTTP handler only WAITS for it (up to the deadline); it never cancels it.
* A deadline hit is an HTTP-level signal ("still running"): 504 ASSESSMENT_TIMEOUT. The record is NOT marked failed and nothing is re-run.
* A request is marked failed (ASSESSMENT_INTERRUPTED) only when its worker is known to be dead: a running marker on disk that no live worker of
  THIS process owns (the previous process ended). There is never an automatic re-run; a new request_id is required.
* Same id + same hash after completion = replay of the stored envelope (no tool/LLM call). Same id + different hash = conflict.
* The finished record is written atomically BEFORE the worker is released, so a poller sees either 202 or the complete result."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from src.agent.schemas import AssessmentResponse, ErrorInfo, Execution
from src.api.events import EventSink, OperationalEvent, events_from_envelope
from src.api.models import ApiRequest, agent_request, build_envelope, request_hash, running_body
from src.api.runners import LiveNotAvailable, RunnerFactory
from src.api.store import FileStore, StoreError

log = logging.getLogger("procurement.api")
Result = tuple[int, dict[str, Any], dict[str, str]]
RETRY_AFTER = "5"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def error_body(code: str, message: str, request_id: str | None = None, run_state: str | None = None) -> dict[str, Any]:
    return {"request_id": request_id, "run_state": run_state, "assessment_status": None, "error": {"code": code, "message": message}}


def _failed_response(request_id: str, code: str, message: str, mock: bool) -> AssessmentResponse:
    return AssessmentResponse(request_id=request_id, status="failed", report_title=None, report_markdown=None, report=None, warnings=[],
                              missing_data=[], limitations=[], error=ErrorInfo(code=code, message=message),
                              execution=Execution(elapsed_seconds=0.0, research_rounds=0, tool_calls={}, llm_calls=0, tokens={}, mock=mock))


@dataclass
class _Running:
    request_id: str
    request_hash: str
    mode: str
    started_at: str
    started_mono: float
    done: threading.Event = field(default_factory=threading.Event)


class AssessmentService:
    def __init__(self, store: FileStore, runner_factory: RunnerFactory, sink: EventSink, *, live_allowed: Callable[[], bool],
                 model: str | None = None, overdue_after_seconds: float = 600.0, boot_id: str | None = None):
        self.store, self.runner_factory, self.sink = store, runner_factory, sink
        self.live_allowed, self.model, self.overdue_after = live_allowed, model, overdue_after_seconds
        self.boot_id = boot_id or uuid.uuid4().hex
        self._lock = threading.Lock()
        self._running: dict[str, _Running] = {}
        self._finished: dict[str, dict[str, Any]] = {}       # read-through cache; also serves a record whose file write failed (until restart)

    # ───────── events (never raise into the request path) ─────────
    def _emit(self, events: list[OperationalEvent | None]) -> None:
        for e in events:
            if e is None:                  # an event that could not be built (see _ev) is skipped
                continue
            try:
                self.sink.emit(e)
            except Exception as exc:          # a failing sink must never change an assessment
                log.error("event sink failed (%s) for %s", type(exc).__name__, e.event_type)

    def _ev(self, rid: str, etype: str, mode: str | None, **kw: Any) -> OperationalEvent | None:
        try:
            return OperationalEvent(event_key=f"{rid}|{etype}|{uuid.uuid4().hex[:12]}", request_id=rid, event_type=etype, mode=mode, **kw)
        except Exception as exc:              # validation failure: log the class only (the message may echo values) and carry on
            log.error("event construction failed (%s) for %s", type(exc).__name__, etype)
            return None

    def _derive(self, env: dict[str, Any]) -> list[OperationalEvent]:
        """Events for a finished envelope. Any failure yields no events: derivation must never affect an assessment or its recovery."""
        try:
            return events_from_envelope(env, model=self.model)
        except Exception as exc:
            log.error("event derivation failed (%s) for %s", type(exc).__name__, env.get("request_id"))
            return []

    # ───────── records ─────────
    def _get_final(self, rid: str) -> dict[str, Any] | None:
        if rid in self._finished:
            return self._finished[rid]
        rec = self.store.read_final(rid)
        if rec is not None:
            self._finished[rid] = rec
        return rec

    def _recover_dead_run(self, rid: str) -> list[OperationalEvent | None]:
        """Caller holds the lock. A marker with no live worker in this process means the previous process died mid-run."""
        if rid in self._running:
            return []
        marker = self.store.read_run(rid)
        if marker is None or self._get_final(rid) is not None:
            return []
        mode = marker.get("mode", "mock")
        env = build_envelope(_failed_response(rid, "ASSESSMENT_INTERRUPTED",
                                              "The service restarted while this assessment was running, so no result exists. Submit a new request_id.",
                                              mock=(mode == "mock")))
        rec = {"request_hash": marker.get("request_hash"), "finished_at": _now(), "envelope": env, "trace": []}
        self.store.write_final(rid, rec)
        self.store.clear_run(rid)
        self._finished[rid] = rec
        log.warning("assessment %s marked interrupted (no live worker)", rid)
        return [self._ev(rid, "assessment.interrupted", mode, severity="error", error_code="ASSESSMENT_INTERRUPTED"), *self._derive(env)]

    # ───────── submit ─────────
    def submit(self, req: ApiRequest, deadline_seconds: float) -> Result:
        rid, h = req.request_id, request_hash(req)
        pending_events: list[OperationalEvent | None] = []
        try:
            with self._lock:
                pending_events += self._recover_dead_run(rid)
                final = self._get_final(rid)
                if final is not None:
                    if final.get("request_hash") != h:
                        return 409, error_body("REQUEST_ID_CONFLICT", "This request_id was already used with different parameters. Use a new request_id.", rid), {}
                    pending_events.append(self._ev(rid, "assessment.replayed", req.mode))
                    return 200, {**final["envelope"], "idempotent_replay": True}, {}
                running = self._running.get(rid)
                if running is not None:
                    if running.request_hash != h:
                        return 409, error_body("REQUEST_ID_CONFLICT", "This request_id is running with different parameters. Use a new request_id.", rid, "running"), {}
                    return 409, error_body("ASSESSMENT_IN_PROGRESS", "This request is already running. Poll GET /assessments/{request_id}; do not POST again.",
                                           rid, "running"), {"Retry-After": RETRY_AFTER}
                if self._running:
                    return 429, error_body("BUSY", "Another assessment is running (limit: 1 concurrent). Retry later.", rid), {"Retry-After": RETRY_AFTER}
                if req.mode == "live" and not self.live_allowed():
                    return 403, error_body("LIVE_NOT_AUTHORISED", "Live mode is not authorised on this server; no external call was made.", rid), {}
                try:
                    runner = self.runner_factory(req.mode)
                except LiveNotAvailable:
                    return 501, error_body("LIVE_NOT_AVAILABLE", "Live mode is not enabled in this build; use mode=mock.", rid), {}
                run = _Running(rid, h, req.mode, _now(), time.monotonic())
                self.store.write_run(rid, {"request_hash": h, "mode": req.mode, "started_at": run.started_at, "boot_id": self.boot_id})
                self._running[rid] = run
                pending_events.append(self._ev(rid, "assessment.received", req.mode))
        except StoreError as exc:
            log.error("store error (%s) for %s", type(exc).__name__, rid)
            return 500, error_body("INTERNAL_ERROR", "Unexpected internal error.", rid), {}
        finally:
            self._emit(pending_events)

        threading.Thread(target=self._work, args=(run, runner, agent_request(req)), name=f"assessment-{rid}", daemon=True).start()
        t0 = time.monotonic()
        if run.done.wait(deadline_seconds):
            with self._lock:
                final = self._get_final(rid)
            env = final["envelope"] if final else error_body("INTERNAL_ERROR", "Unexpected internal error.", rid)
            internal = bool(env.get("error")) and env["error"].get("code") == "INTERNAL_ERROR"
            return (500 if internal else 200), env, {}
        self._emit([self._ev(rid, "assessment.timeout", req.mode, severity="warning", detail={"http_wait_ms": int((time.monotonic() - t0) * 1000)},
                             duration_ms=int((time.monotonic() - t0) * 1000))])
        body = error_body("ASSESSMENT_TIMEOUT", "The assessment is still running; the request deadline was reached. Poll GET /assessments/{request_id}.", rid, "running")
        body["started_at"] = run.started_at
        return 504, body, {"Retry-After": RETRY_AFTER}

    # ───────── worker ─────────
    def _work(self, run: _Running, runner: Callable, request: dict[str, Any]) -> None:
        rid = run.request_id
        trace: list = []
        try:
            response, trace = runner(request)
        except Exception as exc:               # message deliberately dropped: it may contain paths, hosts or credentials
            log.error("assessment %s crashed: %s", rid, type(exc).__name__)
            response = _failed_response(rid, "INTERNAL_ERROR", "Unexpected internal error.", mock=(run.mode == "mock"))
        env = build_envelope(response)
        rec = {"request_hash": run.request_hash, "finished_at": _now(), "envelope": env, "trace": trace}
        try:
            self.store.write_final(rid, rec)
            self.store.clear_run(rid)
        except Exception as exc:
            log.error("persisting %s failed (%s); served from memory until restart", rid, type(exc).__name__)
        events = self._derive(env)
        with self._lock:
            self._finished[rid] = rec
            self._running.pop(rid, None)
        self._emit(events)
        run.done.set()

    # ───────── reads ─────────
    def get(self, rid: str) -> Result:
        try:
            with self._lock:
                events = self._recover_dead_run(rid)
                final = self._get_final(rid)
                if final is not None:
                    result: Result = (200, final["envelope"], {})
                elif rid in self._running:
                    run = self._running[rid]
                    overdue = (time.monotonic() - run.started_mono) > self.overdue_after
                    result = (202, running_body(rid, run.started_at, overdue=overdue), {"Retry-After": RETRY_AFTER})
                else:
                    result = (404, error_body("NOT_FOUND", "Unknown request_id.", rid), {})
        except StoreError as exc:
            log.error("store error (%s) for %s", type(exc).__name__, rid)
            return 500, error_body("INTERNAL_ERROR", "Unexpected internal error.", rid), {}
        self._emit(events)
        return result
