"""Bounded LangGraph workflow: load (read-only SQL) -> calculate (deterministic module) -> research loop
(Tavily + GDELT, <= max_rounds, tool-call and wall-clock budgets) -> draft (LLM) -> validate (one repair, then removal of
unsupported claims) -> finalize.

Topology is fixed. The LLM never chooses tools or does arithmetic: it screens evidence, may refine search queries, and
drafts the interpretation prose. There is no purchasing, approval or contract tool anywhere in the graph.
Scope note: real alternative-supplier DISCOVERY is in scope; monitoring public news about the company's EXISTING
suppliers is a documented future capability and is not implemented (the existing suppliers are fictional).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from src.agent import claims
from src.agent.config import PROFILES, Limits
from src.agent.credibility import STRONG_TIERS
from src.agent.data import DataSource
from src.agent.evidence import normalise_url, prefilter, to_evidence
from src.agent.facts import (benchmark_section, facts_for_llm, facts_text, financial_section, horizon_months, indicators,
                             internal_refs, monetary_values, policy_context, policy_map, supplier_exposure)
from src.agent.llm import LLMClient, LLMError
from src.agent.render import render_markdown
from src.agent.summary import compose_summary
from src.agent.schemas import (AssessmentRequest, AssessmentResponse, DraftReport, ErrorInfo, EvidenceItem,
                               EvidenceJudgements, Execution, RefinedQueries, Report)
from src.agent.validation import ValidationContext, inject_required_risks, prune_unsupported, validate_draft
from src.calculations.exposure import calculate_exposure
from src.rag.retrieve import PolicyResult, PolicyRetriever, build_queries
from src.tools.search import SearchTool

LIMITATIONS = [
    "Policy passages come from fictional internal documents; they are internal context only, not evidence of real regulatory compliance or of a supplier's current compliance.",
    "Existing suppliers, contracts and demand forecasts are synthetic (fictional Bakery Foods Inc.); no public-news monitoring "
    "of existing suppliers is performed.",
    "External evidence comes from web/news search and may be incomplete, outdated or biased; material claims need human verification.",
    "Alternative-supplier candidates are real companies found by search; they are unverified sourcing leads only.",
]
DISCLAIMERS = [
    "Advisory decision support only: the agent cannot approve suppliers, place orders or change contracts.",
    "Supplier candidates are unverified search results, not approved, qualified or capacity-confirmed suppliers; independent procurement verification and qualification are required before any action.",
    "Financial figures are modelled scenarios under stated assumptions, not supplier quotes or forecasts.",
]
EVIDENCE_USABLE = ("direct", "contextual")


@dataclass
class Deps:
    data: DataSource
    llm: LLMClient
    tavily: SearchTool | None = None
    gdelt: SearchTool | None = None
    policy: PolicyRetriever | None = None
    limits: Limits = field(default_factory=Limits)
    clock: Callable[[], float] = time.monotonic
    today: Callable[[], date] = date.today
    mock: bool = False
    require_research: bool = False   # live runs: stop BEFORE any LLM call if neither source answers successfully


class AgentState(TypedDict, total=False):
    request: dict
    as_of: date
    months: list[date]
    started_at: float
    deadline_at: float
    internal: Any
    calc: Any
    benchmark: Any
    financial: Any
    suppliers: list
    indicators: list
    internal_refs: dict
    policy: Any
    warnings: list[str]
    missing_data: list[str]
    partial_reasons: list[str]
    round: int
    queue: list[dict]
    executed: list[str]
    raw_new: list
    evidence: list
    rejections: dict
    usable: dict
    calls: dict
    call_log: dict
    call_records: list
    source_status: dict
    draft: Any
    problems: list[str]
    removed: list[str]
    notes: list[str]
    repairs: int
    error: dict | None
    response: Any
    trace: list[dict]


def _add(state: AgentState, key: str, *items: str) -> list[str]:
    return list(state.get(key, [])) + [i for i in items if i not in state.get(key, [])]


def _trace(state: AgentState, node: str, **info) -> list[dict]:
    return list(state.get("trace", [])) + [{"node": node, **info}]


def _usable_risk(evidence: list[EvidenceItem]) -> list[EvidenceItem]:
    return [e for e in evidence if e.relevance in EVIDENCE_USABLE and e.kind == "risk" and e.published_date and e.credibility != "social"]


def _strong(evidence: list[EvidenceItem]) -> list[EvidenceItem]:
    return [e for e in evidence if e.relevance == "direct" and e.credibility in STRONG_TIERS and e.kind == "risk" and e.published_date]


def _describe_sources(state: AgentState, deps: Deps) -> dict[str, str]:
    """Human-readable research status that separates HTTP success from usable evidence."""
    out: dict[str, str] = {}
    usable = state.get("usable", {})
    for name, obj in (("tavily", deps.tavily), ("gdelt", deps.gdelt)):
        recs = [r for r in state.get("call_records", []) if r["tool"] == name]
        if obj is None:
            out[name] = "not_configured"
        elif not recs:
            out[name] = "not_used"
        else:
            ok = [r for r in recs if r["ok"]]
            attempts = sum(r["attempts"] for r in recs)
            retries = attempts - len(recs)
            statuses = sorted({str(r["http_status"]) for r in recs if r["http_status"] is not None}) or ["none"]
            errs = sorted({a["code"] for r in recs for a in r.get("attempt_log", []) if a.get("code")})
            if not ok:
                out[name] = (f"unavailable: {recs[-1]['error_code']} ({len(recs)} failed calls, {attempts} attempts, HTTP {', '.join(statuses)})")
            else:
                results = sum(r["results"] for r in ok)
                u = usable.get(name, {"passed": 0, "retained": 0, "direct": 0})
                head = f"{'ok' if len(ok) == len(recs) else 'partial'}: {len(ok)} of {len(recs)} calls succeeded, HTTP {', '.join(statuses)}, {results} results returned"
                if results == 0:
                    notes = sorted({r["note"] for r in ok if r.get("note")})
                    head += f" - NO RESULTS ({'; '.join(notes) or 'empty response'})"
                else:
                    head += f"; {u['passed']} passed pre-filter, {u['retained']} judged usable ({u['direct']} direct)"
                if retries:
                    head += f"; {retries} retry attempt(s)" + (f" after {', '.join(errs)}" if errs else "")
                out[name] = head
    return out


def build_graph(deps: Deps):
    lim = deps.limits

    # ───────── nodes ─────────
    def load_internal(state: AgentState) -> dict:
        req = state["request"]
        try:
            internal = deps.data.load(req["ingredient"])
        except Exception as exc:  # noqa: BLE001 - sanitised below; never leak connection details
            return {"error": {"code": "INTERNAL_DATA_UNAVAILABLE", "message": "Required procurement data could not be retrieved"},
                    "trace": _trace(state, "load_internal", ok=False, error_class=type(exc).__name__)}
        missing = []
        if not internal.benchmark_key:
            missing.append("No benchmark series is defined for this ingredient")
        if not internal.contracts:
            missing.append("No contracts found for this ingredient")
        if not internal.forecasts:
            missing.append("No demand forecasts found for this ingredient")
        return {"internal": internal, "missing_data": _add(state, "missing_data", *missing),
                "trace": _trace(state, "load_internal", ok=True, contracts=len(internal.contracts),
                                forecasts=len(internal.forecasts), observations=len(internal.observations))}

    def calculate(state: AgentState) -> dict:
        req, internal, as_of = state["request"], state["internal"], state["as_of"]
        profile = PROFILES[req["ingredient"]]
        months = horizon_months(as_of, req["horizon_months"])
        calc = calculate_exposure(req["ingredient"], months, internal.contracts, internal.forecasts, internal.observations,
                                  internal.benchmark_key or "", as_of, profile.assumptions)
        fin, bench = financial_section(calc), benchmark_section(calc, profile)
        suppliers = supplier_exposure(internal, req["ingredient"], months)
        inds = indicators(calc, internal, req["ingredient"])
        refs = internal_refs(calc, internal, req["ingredient"], inds, suppliers)
        warnings, missing, partial = list(state.get("warnings", [])), list(state.get("missing_data", [])), []
        warnings += [w for w in calc.warnings if w not in warnings]
        if calc.status != "available":
            partial.append("FINANCIAL_UNAVAILABLE: modelled exposure could not be calculated")
            warnings += [f"FINANCIAL_UNAVAILABLE: {r}" for r in calc.unavailable_reasons]
            missing += [r for r in calc.unavailable_reasons if r not in missing]
        return {"calc": calc, "months": months, "benchmark": bench, "financial": fin, "suppliers": suppliers,
                "indicators": inds, "internal_refs": refs, "warnings": warnings, "missing_data": missing,
                "partial_reasons": list(state.get("partial_reasons", [])) + partial,
                "trace": _trace(state, "calculate", status=calc.status, benchmark=bench.status,
                                indicators={i.category: i.severity for i in inds})}

    def retrieve_policy(state: AgentState) -> dict:
        req = state["request"]
        if deps.policy is None:
            res = PolicyResult("unavailable", "none", [], "policy retrieval is not configured", 0)
        else:
            queries = build_queries(req["ingredient"], {i.category for i in state.get("indicators", [])})
            try:
                res = deps.policy.retrieve(queries, req["ingredient"], lim.policy_top_k)
            except Exception as exc:  # noqa: BLE001 - never leak details; the limitation is surfaced instead
                res = PolicyResult("unavailable", "none", [], f"policy retrieval failed ({type(exc).__name__})", 0)
        warnings, partial = list(state.get("warnings", [])), list(state.get("partial_reasons", []))
        if res.status == "unavailable":
            msg = f"POLICY_CONTEXT_UNAVAILABLE: {res.note or 'no policy passage was retrieved'}; no policy guidance is given"
            warnings.append(msg)
            partial.append(msg)
        elif res.status == "fallback":
            warnings.append(f"POLICY_RETRIEVAL_FALLBACK: full-text search was used instead of vector search ({res.note})")
        return {"policy": res, "warnings": warnings, "partial_reasons": partial,
                "trace": _trace(state, "retrieve_policy", status=res.status, method=res.method, passages=len(res.passages),
                                refs=[p.policy_ref for p in res.passages], note=res.note, embedding_calls=res.embedding_calls)}

    def _budget_left(state: AgentState, tool: str) -> int:
        cap = lim.max_tavily_calls if tool == "tavily" else lim.max_gdelt_calls
        return cap - state.get("calls", {}).get(tool, 0)

    def _available(state: AgentState, tool: str) -> bool:
        obj = deps.tavily if tool == "tavily" else deps.gdelt
        return obj is not None and not str(state.get("source_status", {}).get(tool, "")).startswith("unavailable")

    def plan_research(state: AgentState) -> dict:
        rnd = state.get("round", 0) + 1
        profile = PROFILES[state["request"]["ingredient"]]
        queue: list[dict] = []
        if deps.clock() >= state["deadline_at"]:
            return {"round": rnd, "queue": [], "warnings": _add(state, "warnings", "RESEARCH_DEADLINE: wall-clock budget reached; research stopped"),
                    "trace": _trace(state, "plan_research", round=rnd, stopped="deadline")}
        if rnd == 1:
            queue += [{"tool": "tavily", "query": q, "kind": "risk"} for q in profile.risk_queries]
            queue.append({"tool": "gdelt", "query": profile.gdelt_query, "kind": "risk"})
            queue.append({"tool": "tavily", "query": profile.supplier_query, "kind": "supplier_discovery"})
        else:
            ctx = {"queries_already_run": state.get("executed", []),
                   "usable_risk_evidence_found": len(_usable_risk(state.get("evidence", []))),
                   "needed": lim.min_relevant_evidence}
            try:
                refined = RefinedQueries.model_validate(deps.llm.refine_queries(profile.display_name, ctx))
            except (LLMError, ValidationError) as exc:
                code = exc.code if isinstance(exc, LLMError) else "INVALID_QUERY_OUTPUT"
                return {"round": rnd, "queue": [], "warnings": _add(state, "warnings", f"RESEARCH_REFINEMENT_SKIPPED: {code}"),
                        "trace": _trace(state, "plan_research", round=rnd, stopped=code)}
            queue += [{"tool": "tavily", "query": q.strip(), "kind": "risk"} for q in refined.tavily_queries[:2] if q.strip()]
            if refined.gdelt_query.strip():
                queue.append({"tool": "gdelt", "query": refined.gdelt_query.strip(), "kind": "risk"})
        executed = set(state.get("executed", []))
        final, used = [], {"tavily": 0, "gdelt": 0}
        for q in queue:
            t = q["tool"]
            if q["query"] in executed or not _available(state, t) or used[t] >= _budget_left(state, t):
                continue
            used[t] += 1
            final.append(q)
        return {"round": rnd, "queue": final, "trace": _trace(state, "plan_research", round=rnd, queued=len(final))}

    def search(state: AgentState) -> dict:
        calls, log = dict(state.get("calls", {})), {k: dict(v) for k, v in state.get("call_log", {}).items()}
        raws, executed, warnings = [], list(state.get("executed", [])), list(state.get("warnings", []))
        records: list[dict] = []
        for q in state.get("queue", []):
            if deps.clock() >= state["deadline_at"]:
                warnings = _add({"warnings": warnings}, "warnings", "RESEARCH_DEADLINE: wall-clock budget reached; research stopped")
                break
            tool = deps.tavily if q["tool"] == "tavily" else deps.gdelt
            res = tool.search(q["query"], kind=q["kind"], lookback_days=lim.lookback_days, max_results=lim.max_results_per_call)
            calls[q["tool"]] = calls.get(q["tool"], 0) + 1
            executed.append(q["query"])
            entry = log.setdefault(q["tool"], {"ok": 0, "failed": 0, "last_error": None, "attempts": 0})
            entry["attempts"] += res.attempts
            records.append({"tool": q["tool"], "kind": q["kind"], "query": q["query"], "ok": res.ok, "http_status": res.http_status,
                            "attempts": res.attempts, "error_code": res.error_code, "results": len(res.items),
                            "attempt_log": res.attempt_log, "note": res.note, "params": res.params})
            if res.ok:
                entry["ok"] += 1
                raws += res.items
            else:
                entry["failed"] += 1
                entry["last_error"] = res.error_code
        status = {}
        for name, obj in (("tavily", deps.tavily), ("gdelt", deps.gdelt)):
            e = log.get(name)
            if obj is None:
                status[name] = "not_configured"
            elif e is None:
                status[name] = "not_used"
            elif e["failed"] == 0:
                status[name] = f"ok ({e['ok']} calls)"
            elif e["ok"] == 0:
                status[name] = f"unavailable: {e['last_error']} ({e['failed']} failed calls, {e['attempts']} attempts)"
            else:
                status[name] = f"partial: {e['failed']} of {e['ok'] + e['failed']} calls failed (last error {e['last_error']})"
        out = {"calls": calls, "call_log": log, "raw_new": raws, "executed": executed, "source_status": status,
               "warnings": warnings, "call_records": list(state.get("call_records", [])) + records,
               "trace": _trace(state, "search", results=len(raws), calls=dict(calls), records=records)}
        if deps.require_research and records and sum(e["ok"] for e in log.values()) == 0:
            out["error"] = {"code": "RESEARCH_UNAVAILABLE",
                            "message": "Neither Tavily nor GDELT returned a successful response; stopped before any LLM call"}
        return out

    def filter_evidence(state: AgentState) -> dict:
        profile = PROFILES[state["request"]["ingredient"]]
        existing = list(state.get("evidence", []))
        seen = {normalise_url(e.url) for e in existing}
        raw_new = state.get("raw_new", [])
        accepted, rejected = prefilter(raw_new, profile, seen, state["as_of"], lim.lookback_days)
        rejections = dict(state.get("rejections", {}))
        for r in rejected:
            rejections[r.reason] = rejections.get(r.reason, 0) + 1
        new = to_evidence(accepted, len(existing) + 1, state["as_of"], deps.mock)
        warnings = list(state.get("warnings", []))
        if new:
            payload = [{"evidence_id": e.evidence_id, "kind": e.kind, "title": e.title, "snippet": e.snippet[:300],
                        "published_date": str(e.published_date) if e.published_date else None, "domain": e.domain,
                        "source_tier": e.credibility} for e in new]
            judged = {}
            try:
                for j in EvidenceJudgements.model_validate(deps.llm.judge_evidence(profile.display_name, payload)).judgements:
                    judged[j.evidence_id] = j
            except (LLMError, ValidationError) as exc:
                code = exc.code if isinstance(exc, LLMError) else "INVALID_JUDGEMENT_OUTPUT"
                warnings = _add({"warnings": warnings}, "warnings", f"EVIDENCE_SCREENING_FAILED: {code}; new items left unjudged and not used")
            for e in new:
                j = judged.get(e.evidence_id)
                if j:
                    e.relevance, e.relevance_reason = j.relevance, j.reason
        usable = {k: dict(v) for k, v in state.get("usable", {}).items()}
        for src in ("tavily", "gdelt"):
            u = usable.setdefault(src, {"retrieved": 0, "passed": 0, "retained": 0, "direct": 0})
            u["retrieved"] += sum(1 for r in raw_new if r.source == src)
            u["passed"] += sum(1 for r in accepted if r.source == src)
            u["retained"] += sum(1 for e in new if e.source == src and e.relevance in EVIDENCE_USABLE)
            u["direct"] += sum(1 for e in new if e.source == src and e.relevance == "direct")
        evidence = existing + new
        return {"evidence": evidence, "rejections": rejections, "usable": usable, "raw_new": [], "queue": [], "warnings": warnings,
                "trace": _trace(state, "filter_evidence", accepted=len(new), rejected=len(rejected),
                                usable_risk=len(_usable_risk(evidence)), strong_direct=len(_strong(evidence)),
                                retrieved=len(raw_new),
                                rejected_items=[{"reason": r.reason, "url": r.url, "title": r.title} for r in rejected][:60],
                                judged=[{"id": e.evidence_id, "relevance": e.relevance, "tier": e.credibility, "domain": e.domain,
                                         "reason": e.relevance_reason} for e in new])}

    def route_after_filter(state: AgentState) -> str:
        can_search = any(_available(state, t) and _budget_left(state, t) > 0 for t in ("tavily", "gdelt"))
        if (len(_usable_risk(state.get("evidence", []))) < lim.min_relevant_evidence and state.get("round", 0) < lim.max_rounds
                and can_search and deps.clock() < state["deadline_at"]):
            return "plan_research"
        return "draft"

    def _facts(state: AgentState) -> dict:
        return facts_for_llm(state["benchmark"], state["financial"], state["suppliers"], state.get("warnings", []), LIMITATIONS,
                             state.get("indicators", []), state.get("internal_refs", {}), state.get("policy"),
                             {"observed_price_direction": claims.observed_price_direction(state.get("evidence", [])),
                              "basis": "direct trade-press/official items with price wording; the +10% benchmark shock is a hypothetical scenario, not an observation"})

    def draft(state: AgentState) -> dict:
        req = state["request"]
        profile = PROFILES[req["ingredient"]]
        ev = [{"evidence_id": e.evidence_id, "kind": e.kind, "source": e.source, "relevance": e.relevance,
               "source_tier": e.credibility, "domain": e.domain, "title": e.title, "snippet": e.snippet[:400], "url": e.url,
               "published_date": str(e.published_date) if e.published_date else None,
               "price_direction": claims.price_direction(e.title, e.snippet),
               "price_scope": "other-market price quotation" if claims.other_market_markers(e.snippet) else "none"}
              for e in state.get("evidence", []) if e.relevance in EVIDENCE_USABLE]
        try:
            raw = deps.llm.draft_report(profile.display_name, facts_text(_facts(state)), ev, state.get("problems", []), lim.max_candidates)
        except LLMError as exc:
            return {"error": {"code": "LLM_UNAVAILABLE", "message": f"Report drafting failed ({exc.code})"},
                    "trace": _trace(state, "draft", ok=False, code=exc.code)}
        return {"draft": raw, "trace": _trace(state, "draft", ok=True, attempt=state.get("repairs", 0) + 1)}

    def validate(state: AgentState) -> dict:
        problems: list[str] = []
        removed: list[str] = []
        notes: list[str] = []
        try:
            parsed = raw if isinstance(raw := state["draft"], DraftReport) else DraftReport.model_validate(raw)
        except ValidationError as exc:
            problems = [f"schema: {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()][:12]
            parsed = None
        if parsed is not None:
            spot = PROFILES[state["request"]["ingredient"]].assumptions.spot_premium
            ctx = ValidationContext(set(state.get("internal_refs", {})), list(state.get("indicators", [])), policy_map(state.get("policy")))
            # an internal exposure (coverage gap, non-approved supplier) is never silently omitted
            parsed, notes = inject_required_risks(parsed, ctx)
            args = (state.get("evidence", []), facts_text(_facts(state)), state["financial"].status == "available",
                    lim.max_candidates, monetary_values(state["benchmark"], state["financial"], spot), ctx)
            problems = validate_draft(parsed, *args)
            if problems and state.get("repairs", 0) + 1 > lim.max_repairs:
                # repair attempt used up: REMOVE what cannot be verified instead of keeping decorative citations
                pruned, removed, more_notes, remaining = prune_unsupported(parsed, *args)
                notes += [n for n in more_notes if n not in notes]
                if not remaining:
                    parsed, problems = pruned, []
        repairs = state.get("repairs", 0) + (1 if problems else 0)
        return {"draft": parsed if not problems else state["draft"], "problems": problems, "repairs": repairs,
                "removed": removed, "notes": notes,
                "trace": _trace(state, "validate", problems=len(problems), repairs=repairs, removed=len(removed),
                                first_problems=problems[:6], removed_items=removed[:10])}

    def route_after_validate(state: AgentState) -> str:
        if not state.get("problems"):
            return "finalize"
        return "draft" if state.get("repairs", 0) <= lim.max_repairs else "finalize"

    def finalize(state: AgentState) -> dict:
        return {"response": _finalize(state, deps)}

    def route_after_load(state: AgentState) -> str:
        return "finalize" if state.get("error") else "calculate"

    def route_after_draft(state: AgentState) -> str:
        return "finalize" if state.get("error") else "validate"

    def timed(fn):
        """Record per-node wall-clock time on the node's trace entry."""
        def wrapper(state: AgentState) -> dict:
            t0 = deps.clock()
            out = fn(state)
            if out.get("trace"):
                out["trace"][-1]["elapsed_s"] = round(deps.clock() - t0, 3)
            return out
        wrapper.__name__ = fn.__name__
        return wrapper

    g = StateGraph(AgentState)
    for name, fn in (("load_internal", load_internal), ("calculate", calculate), ("retrieve_policy", retrieve_policy), ("plan_research", plan_research),
                     ("search", search), ("filter_evidence", filter_evidence), ("draft", draft), ("validate", validate),
                     ("finalize", finalize)):
        g.add_node(name, timed(fn))
    g.add_edge(START, "load_internal")
    g.add_conditional_edges("load_internal", route_after_load, {"calculate": "calculate", "finalize": "finalize"})
    g.add_edge("calculate", "retrieve_policy")
    g.add_edge("retrieve_policy", "plan_research")
    g.add_edge("plan_research", "search")
    g.add_conditional_edges("search", lambda st: "finalize" if st.get("error") else "filter_evidence",
                            {"filter_evidence": "filter_evidence", "finalize": "finalize"})
    g.add_conditional_edges("filter_evidence", route_after_filter, {"plan_research": "plan_research", "draft": "draft"})
    g.add_conditional_edges("draft", route_after_draft, {"validate": "validate", "finalize": "finalize"})
    g.add_conditional_edges("validate", route_after_validate, {"draft": "draft", "finalize": "finalize"})
    g.add_edge("finalize", END)
    return g.compile()


# ───────── finalisation (pure function of the state) ─────────
def _execution(state: AgentState, deps: Deps) -> Execution:
    u = deps.llm.usage()
    return Execution(elapsed_seconds=round(deps.clock() - state["started_at"], 3), research_rounds=state.get("round", 0),
                     tool_calls=dict(state.get("calls", {})), llm_calls=u.get("calls", 0),
                     tokens={"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0)}, mock=deps.mock)


def _finalize(state: AgentState, deps: Deps) -> AssessmentResponse:
    req = state["request"]
    execution = _execution(state, deps)
    warnings = list(state.get("warnings", []))
    if deps.mock and not any(w.startswith("MOCK") for w in warnings):
        warnings.insert(0, "MOCK RUN: search results and the drafted text come from fixtures, not live market intelligence")
    error = state.get("error")
    if not error and state.get("problems"):
        error = {"code": "REPORT_VALIDATION_FAILED",
                 "message": "The drafted report failed validation after the allowed repair attempt: " + "; ".join(state["problems"][:5])}
    if error:
        return AssessmentResponse(request_id=req["request_id"], assessment_date=state["as_of"], status="failed", report_title=None, report_markdown=None,
                                  report=None, warnings=warnings, missing_data=list(state.get("missing_data", [])),
                                  limitations=[], error=ErrorInfo(**error), execution=execution)
    profile = PROFILES[req["ingredient"]]
    partial = list(state.get("partial_reasons", []))
    evidence: list[EvidenceItem] = state.get("evidence", [])
    status_map = _describe_sources(state, deps)
    pol = state.get("policy")
    status_map["policy"] = (f"{pol.status}: {pol.method}, {len(pol.passages)} passages" + (f" ({pol.note})" if pol.note else "")) if pol else "not_run"
    for name in ("tavily", "gdelt"):
        s = status_map.get(name, "not_used")
        if s.startswith(("unavailable", "partial", "not_configured")):
            partial.append(f"RESEARCH_SOURCE_{name.upper()}: {s}")
            warnings.append(f"RESEARCH_SOURCE_{name.upper()}: {s}")
        elif "NO RESULTS" in s:
            warnings.append(f"RESEARCH_SOURCE_{name.upper()}: HTTP success but no usable results - {s}")
    if not _usable_risk(evidence):
        partial.append("NO_RELEVANT_EVIDENCE: no relevant, dated external risk evidence was retained")
        warnings.append("NO_RELEVANT_EVIDENCE: no relevant, dated external risk evidence was retained")
    elif not _strong(evidence):
        partial.append("NO_STRONG_EXTERNAL_EVIDENCE: no direct official or trade-press source was retained, so external risks cannot be asserted")
        warnings.append("NO_STRONG_EXTERNAL_EVIDENCE: no direct official or trade-press source was retained, so external risks cannot be asserted")
    removed = list(state.get("removed", []))
    if removed:
        partial.append(f"UNSUPPORTED_CLAIMS_REMOVED: {len(removed)} item(s) removed")
        warnings.append(f"UNSUPPORTED_CLAIMS_REMOVED: {len(removed)} drafted item(s) could not be verified against the evidence and were removed (listed in the report)")
    warnings += [n for n in state.get("notes", []) if n not in warnings]
    if state["benchmark"].status != "available":
        partial.append(f"BENCHMARK_{state['benchmark'].status.upper()}")
    rej = state.get("rejections", {})
    if rej:
        status_map["filtering"] = "excluded search results (kept out of the report): " + ", ".join(f"{k}={v}" for k, v in sorted(rej.items()))
    summary = compose_summary(draft=state["draft"], indicators=list(state.get("indicators", [])), financial=state["financial"],
                              suppliers=state["suppliers"], removed=removed, partial=partial,
                              observed=claims.observed_price_direction(evidence), horizon_months=req["horizon_months"],
                              policy_status=pol.status if pol else "unavailable")
    report = Report(request_id=req["request_id"], ingredient=req["ingredient"], horizon_months=req["horizon_months"],
                    assessment_date=state["as_of"], title=f"{profile.display_name} Procurement Risk Assessment",
                    draft=state["draft"].model_copy(update={"executive_summary": summary}), indicators=list(state.get("indicators", [])), benchmark=state["benchmark"],
                    financial=state["financial"], policy=policy_context(pol), supplier_exposure=state["suppliers"], evidence=evidence,
                    source_status=status_map, removed_claims=removed, warnings=warnings,
                    missing_data=list(state.get("missing_data", [])), limitations=list(LIMITATIONS), disclaimers=list(DISCLAIMERS))
    status = "partial" if partial else "completed"
    return AssessmentResponse(request_id=req["request_id"], assessment_date=state["as_of"], status=status, report_title=report.title,
                              report_markdown=render_markdown(report, status), report=report, warnings=warnings,
                              missing_data=report.missing_data, limitations=report.limitations, error=None, execution=execution)


def run_assessment(request: dict | AssessmentRequest, deps: Deps) -> tuple[AssessmentResponse, list[dict]]:
    """Validate the request, run the graph, return (response, trace). Never raises for business failures."""
    try:
        req = request if isinstance(request, AssessmentRequest) else AssessmentRequest.model_validate(request)
    except ValidationError as exc:
        rid = request.get("request_id", "invalid") if isinstance(request, dict) else "invalid"
        rid = rid if isinstance(rid, str) and rid and len(rid) <= 64 else "invalid"
        msg = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return (AssessmentResponse(request_id=rid, status="failed", report_title=None, report_markdown=None, report=None,
                                   warnings=[], missing_data=[], limitations=[], error=ErrorInfo(code="INVALID_REQUEST", message=msg),
                                   execution=Execution(elapsed_seconds=0.0, research_rounds=0, tool_calls={}, llm_calls=0,
                                                       tokens={"input": 0, "output": 0}, mock=deps.mock)), [])
    now = deps.clock()
    today = deps.today()
    as_of = req.assessment_date or today
    initial: AgentState = {"request": req.model_dump(), "as_of": as_of, "started_at": now,
                           "deadline_at": now + deps.limits.deadline_seconds, "trace": [],
                           "warnings": [f"ASSESSMENT_DATE_OVERRIDE: assessed as of {as_of} (today is {today}); data freshness is judged against {as_of}"]
                           if as_of != today else [], "missing_data": [],
                           "partial_reasons": [], "round": 0, "evidence": [], "calls": {}, "call_log": {}, "call_records": [],
                           "executed": [], "problems": [], "repairs": 0, "removed": [], "notes": [], "usable": {}}
    final = build_graph(deps).invoke(initial, config={"recursion_limit": 60})
    return final["response"], final.get("trace", [])
