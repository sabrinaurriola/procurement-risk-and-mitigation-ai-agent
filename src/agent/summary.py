"""Deterministic executive summary, assembled from the VALIDATED report only (retained findings, deterministic figures, removed claims).

The LLM's own summary is not used: it is written before unsupported claims are removed and can still assert them. This summary names a risk
only if that risk survived validation, states the hypothetical shock as a scenario (never a forecast), and lists removed claims separately as
unverified concerns that a person may want to investigate."""
from __future__ import annotations

import re

from src.agent.schemas import DraftReport, FinancialSection, Indicator, SupplierExposure

REMOVED_RE = re.compile(r"^(\w+) '(.*)' removed: (.*)$", re.S)
KIND_LABEL = {"risks": "risk", "mitigation_options": "mitigation option", "supplier_candidates": "supplier lead", "recommended_actions": "recommended action"}
DIRECTION_TEXT = {"falling": "falling prices", "rising": "rising prices", "mixed": "mixed price signals"}


def unverified_concerns(removed: list[str], full: bool = False) -> list[str]:
    out = []
    for x in removed:
        m = REMOVED_RE.match(x)
        if m:
            kind, title, reason = m.groups()
            out.append(f"{KIND_LABEL.get(kind, kind)} '{title}' - {reason[:150].rstrip()}" if full else f"{KIND_LABEL.get(kind, kind)} '{title}'")
        else:
            out.append(x[:200])
    return out


def compose_summary(*, draft: DraftReport, indicators: list[Indicator], financial: FinancialSection, suppliers: list[SupplierExposure],
                    removed: list[str], partial: list[str], observed: str, horizon_months: int,
                    policy_status: str = "ok") -> str:
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
            line = f"- **Supplier governance:** {s.supplier_id} ({s.supplier_name}) is {s.approval_status.upper()}"
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
        lines.append(f"- **Alternative suppliers:** {n} UNVERIFIED sourcing lead(s) found by web search ({names}); none is approved or qualified.")
    else:
        lines.append("- **Alternative suppliers:** No alternative-supplier lead is reported (the evidence did not support one).")

    if draft.recommended_actions:
        lines.append("- **Decisions and next steps for the Procurement Manager (all need human approval):** "
                     + " ".join(f"({i}) {a.action.rstrip('.')}." for i, a in enumerate(draft.recommended_actions, 1)))
    else:
        lines.append("- **Decisions and next steps:** no recommended action survived validation; review the findings manually.")

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
