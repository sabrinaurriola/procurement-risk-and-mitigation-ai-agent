"""A record of what a live run actually did: API attempts, successes, failures, timing and usage. Counts and codes only: no report text, no prompts,
no evidence text, no credentials, no URLs of the services (the queries are our own search strings)."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from src.agent.budget import USAGE_LOG, estimate_cost_usd

RECORD_FIELDS = ("tool", "kind", "query", "ok", "http_status", "attempts", "error_code", "results", "note")


def _usage_for(request_id: str, log: Path) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    if not log.exists():
        return out
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("request_id") != request_id:
            continue
        slot = out.setdefault(str(row.get("purpose")), {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        slot["calls"] += 1
        slot["input_tokens"] += int(row.get("input_tokens") or 0)
        slot["output_tokens"] += int(row.get("output_tokens") or 0)
    return out


def build_record(*, response: Any, trace: list[dict], mode: str, model: str | None, started_at: datetime, finished_at: datetime,
                 usage_log: Path = USAGE_LOG) -> dict[str, Any]:
    searches = []
    for t in trace:
        if t.get("node") == "search":
            for rec in t.get("records", []):
                row = {k: rec.get(k) for k in RECORD_FIELDS}
                row["attempt_error_codes"] = [a.get("code") for a in rec.get("attempt_log", []) if a.get("code")]
                searches.append(row)
    ex = response.execution
    report = response.report
    evidence = report.evidence if report else []
    if report:
        ev_summary = {"retained": len(evidence), "by_source": dict(Counter(e.source for e in evidence)),
                      "by_tier": dict(Counter(e.credibility for e in evidence)), "by_relevance": dict(Counter(e.relevance for e in evidence)), "from": "report"}
    else:                       # a failed run has no report: take the counts from the trace of the evidence-screening step
        steps = [t for t in trace if t.get("node") == "filter_evidence"]
        judged = [j for t in steps for j in t.get("judged", [])]
        ev_summary = {"retrieved": sum(t.get("retrieved", 0) for t in steps), "passed_prefilter": sum(t.get("accepted", 0) for t in steps),
                      "rejected_by_prefilter": sum(t.get("rejected", 0) for t in steps), "usable_risk": steps[-1].get("usable_risk", 0) if steps else 0,
                      "strong_direct": steps[-1].get("strong_direct", 0) if steps else 0,
                      "by_tier": dict(Counter(j.get("tier") for j in judged)), "by_relevance": dict(Counter(j.get("relevance") for j in judged)), "from": "trace"}
    tokens = ex.tokens or {}
    return {
        "schema": "live-record/1", "request_id": response.request_id, "mode": mode, "model": model,
        "started_at": started_at.isoformat(timespec="seconds"), "finished_at": finished_at.isoformat(timespec="seconds"),
        "assessment_status": response.status, "error_code": response.error.code if response.error else None,
        "execution": {"elapsed_seconds": ex.elapsed_seconds, "research_rounds": ex.research_rounds, "tool_calls": ex.tool_calls,
                      "llm_calls": ex.llm_calls, "tokens": tokens},
        "estimated_cost_usd": estimate_cost_usd(model, int(tokens.get("input", 0)), int(tokens.get("output", 0))),
        "research_attempts": {"calls": len(searches), "succeeded": sum(1 for s in searches if s["ok"]), "failed": sum(1 for s in searches if not s["ok"]),
                              "by_tool": dict(Counter(s["tool"] for s in searches)), "calls_detail": searches},
        "source_status": dict(report.source_status) if report else {},
        "evidence": ev_summary,
        "validation": {"claims_removed": len(report.removed_claims) if report else 0,
                       "removed_kinds": dict(Counter(x.split(" ", 1)[0] for x in (report.removed_claims if report else []))),
                       "warning_codes": sorted({w.split(":")[0] for w in response.warnings})},
        "usage_log_lines_for_this_request": _usage_for(response.request_id, usage_log),
        "node_timings_s": {t["node"]: t.get("elapsed_s") for t in trace if t.get("node") and t.get("elapsed_s") is not None},
    }


def write_trace(trace: list[dict], request_id: str, directory: Path) -> Path:
    """The agent's step trace of a live run (search records, screening decisions, validation problems) for diagnosis. Public web metadata and codes;
    no credentials. Kept in runs/ (git-ignored) next to the record."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{request_id}-live-trace.json"
    path.write_text(json.dumps(trace, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def write_record(record: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record['request_id']}-live-record.json"
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return path
