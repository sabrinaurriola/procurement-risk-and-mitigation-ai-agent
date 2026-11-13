"""Deterministic executive summary, assembled from the VALIDATED report only (retained findings, deterministic figures, removed claims).

The LLM's own summary is not used: it is written before unsupported claims are removed and can still assert them. This summary names a risk
only if that risk survived validation, states the hypothetical shock as a scenario (never a forecast), and lists removed claims separately as
unverified concerns that a person may want to investigate. It is decision-oriented: the recommended actions are prioritised by the severity
of the risks they address and each names who is responsible, what approval is needed (only what the cited policy passages say) and which
evidence to review. Nothing is truncated."""
from __future__ import annotations

import re

from src.agent.schemas import Action, DraftReport, FinancialSection, Indicator, SupplierExposure

REMOVED_RE = re.compile(r"^(\w+) '(.*)' removed: (.*)$", re.S)
KIND_LABEL = {"risks": "risk", "mitigation_options": "mitigation option", "supplier_candidates": "supplier lead", "recommended_actions": "recommended action"}
DIRECTION_TEXT = {"falling": "falling prices", "rising": "rising prices", "mixed": "mixed price signals"}
SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}
OWNER = "Raw Materials Procurement Manager"


def unverified_concerns(removed: list[str], full: bool = False) -> list[str]:
    out = []
    for x in removed:
        m = REMOVED_RE.match(x)
        if m:
            kind, title, reason = m.groups()
            out.append(f"{KIND_LABEL.get(kind, kind)} '{title}' - {reason.strip()}" if full else f"{KIND_LABEL.get(kind, kind)} '{title}'")
        else:
            out.append(x)
    return out


def approval_needed(a: Action) -> str:
    """What approval the action needs, stated only as far as the CITED policy passages say it."""
    refs = {p.policy_ref for p in a.policy_citations}
    if any(r.startswith(("POL-001", "ASM-003")) for r in refs):
        return "written sign-off by the Procurement Director before any renewal or volume increase (POL-001 / ASM-003)"
    if any(r.startswith("POL-003") for r in refs):
        return "human approval before any commercial contact; the lead must first pass the qualification steps (POL-003)"
    return "human approval required (no approver is named in the policy passages retrieved for this action)"


def priority_actions(draft: DraftReport) -> list[tuple[str, Action, list]]:
    """(severity label, action, addressed risks), most severe first; draft order breaks ties."""
    by_id = {r.risk_id: r for r in draft.risks}
    rows = []
    for idx, a in enumerate(draft.recommended_actions):
        addressed = [by_id[x] for x in a.addresses if x in by_id]
        sev = max((SEVERITY_RANK[r.severity] for r in addressed), default=0)
        rows.append((-sev, -len(addressed), idx, a, addressed))
    rows.sort(key=lambda t: t[:3])
    label = {3: "HIGH", 2: "MEDIUM", 1: "LOW", 0: "UNRATED"}
    return [(label[-s], a, addressed) for s, _n, _i, a, addressed in rows]


def _review_list(a: Action, addressed: list) -> str:
    parts = []
    if addressed:
        parts.append("risks " + ", ".join(r.risk_id for r in addressed))
    ev = sorted({c.evidence_id for c in a.citations})
    if ev:
        parts.append("evidence " + ", ".join(ev))
    pol = sorted({p.policy_ref for p in a.policy_citations})
    if pol:
        parts.append("policy " + ", ".join(pol))
    if a.internal_refs:
        parts.append("the internal contract, forecast and supplier records cited in the risk")
    return "; ".join(parts) or "see the findings below"


def compose_summary(*, draft: DraftReport, indicators: list[Indicator], financial: FinancialSection, suppliers: list[SupplierExposure],
                    removed: list[str], partial: list[str], observed: str, horizon_months: int,
                    policy_status: str = "ok", mock: bool = False) -> str:
    ind = {i.category: i for i in indicators}
    lines: list[str] = []

    if financial.status == "available":
        line = (f"- **Exposure over the next {horizon_months} months:** modelled baseline procurement cost EUR {financial.baseline_modelled_cost_eur} for "
                f"{financial.total_forecast_t} t of forecast demand; {financial.total_contract_commitments_t} t is under contract and "
                f"{financial.total_uncovered_t} t is uncovered.")
        if "contract_coverage" in ind:
            line += f" Coverage: {ind['contract_coverage'].value}."
        lines.append(line)
        s10 = next((s for s in financial.scenarios if s.shock == financial.attribution_shock), None)
        if s10 and financial.attribution:
            parts = [f"{r.share_pct} from {r.label.split(' (')[0]}" + (f" ({r.detail.split(' - ')[1]})" if " - " in r.detail else "") for r in financial.attribution]
            lines.append(f"- **Sensitivity (a hypothetical scenario, not a forecast):** if the benchmark were {financial.attribution_shock.lstrip("+")} higher, modelled cost would rise by "
                         f"EUR {s10.incremental_exposure_eur} ({s10.pct_of_baseline} of baseline): " + ", ".join(parts) + ". Fixed-price contracts do not move with the benchmark.")
    else:
        why = (financial.unavailable_reasons or ["required data missing"])[0]
        lines.append(f"- **Exposure:** the financial model is unavailable ({why}); no euro estimate is given.")

    actions = priority_actions(draft)
    if actions:
        lines.append("- **Decisions requested, in priority order** (ranked by the severity of the risk each action addresses; all need human approval; "
                     f"this report only recommends; the {OWNER} initiates each action and approval and execution stay with people):")
        for n, (sev, a, addressed) in enumerate(actions, 1):
            risks = "; ".join(f"{r.risk_id} {r.title}" for r in addressed) or "no retained risk"
            lines.append(f"- **Priority {n} · {sev} — {a.action.rstrip('.')}.** Addresses: {risks}. Owner: {OWNER}. "
                         f"Approval needed: {approval_needed(a)}. Review before acting: {_review_list(a, addressed)}.")
    else:
        lines.append("- **Decisions requested:** no recommended action survived validation; review the findings manually.")

    kept = {c: [r for r in draft.risks if r.category == c] for c in ("market_price", "supply_disruption")}
    market = [r for r in kept["market_price"] if r.basis != "internal_model"]
    if market and observed in DIRECTION_TEXT:
        lines.append(f"- **Market evidence:** the trade press cited in this report describes {DIRECTION_TEXT[observed]} in international sunflower-oil markets; "
                     "this is an observation, not a forecast of the Spanish benchmark.")
    else:
        lines.append("- **Market evidence:** no validated market-price finding is reported.")
    if kept["supply_disruption"]:
        lines.append("- **Supply:** " + "; ".join(f"{r.risk_id} {r.title}" for r in kept["supply_disruption"]) + " (validated; see the risks below).")
    else:
        lines.append("- **Supply:** no validated supply-disruption finding is reported.")

    cited = {p.policy_ref for x in [*draft.mitigation_options, *draft.recommended_actions] for p in x.policy_citations}
    for s in suppliers:
        if s.approval_status != "approved":
            line = f"- **Supplier governance (fictional internal supplier):** {s.supplier_id} ({s.supplier_name}) is {s.approval_status.upper()}"
            if "supplier_status" in ind:
                rest = ind["supplier_status"].value.split(": ", 1)[-1]
                if "%" in rest:
                    line += f"; it accounts for {rest}"
            line += "."
            if cited & {"POL-001#2", "ASM-003#2"} or any(c.startswith(("POL-001", "ASM-003")) for c in cited):
                line += (" It may keep delivering under its existing contract; any renewal or volume increase needs written Procurement Director sign-off "
                         "and cannot be committed until its open conditions are closed (cited fictional policy passages below).")
            else:
                line += " The approval requirements could not be cited from the retrieved policy passages in this run."
            lines.append(line)

    n = len(draft.supplier_candidates)
    if n:
        names = ", ".join(c.name for c in draft.supplier_candidates)
        if mock:
            lines.append(f"- **Alternative suppliers:** {n} UNVERIFIED MOCK FIXTURE example(s) ({names}); these are NOT real companies and none is approved or qualified.")
        else:
            lines.append(f"- **Alternative suppliers:** {n} UNVERIFIED sourcing lead(s) found by web search ({names}); none is approved or qualified.")
    else:
        lines.append("- **Alternative suppliers:** No alternative-supplier lead is reported (the evidence did not support one).")

    if policy_status == "unavailable":
        lines.append("- **Policy guidance:** policy guidance is unavailable in this run; no internal-policy requirement is cited and none should be assumed.")
    elif policy_status == "fallback":
        lines.append("- **Policy guidance:** retrieved with the full-text fallback (vector search was unavailable); check the cited passages.")
    concerns = unverified_concerns(removed)
    if concerns:
        lines.append("- **Unverified concerns (drafted claims that could not be supported and were removed; they are not findings and may warrant human investigation):** "
                     + " | ".join(concerns))
    if partial:
        lines.append("- **Report status:** PARTIAL - " + "; ".join(p.split(":")[0] for p in partial[:4]) + ". See the warnings above.")
    return "\n".join(lines)
