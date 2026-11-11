"""Independent audit of a FINAL report.

Why this exists: schema/Pydantic validation and the in-workflow validator prove a draft is well-formed, not that the report
is trustworthy. This module re-derives the key quality properties directly from the finished `Report` object, with its own
code (it does not call src.agent.validation), so a bug in one implementation cannot hide a defect in the other.
It checks structure and provenance. It cannot judge whether a live model's prose is insightful: that still needs a human.

Usage: python -m src.agent.quality --mock     (real read-only database data + fixtures; writes runs/day3-*.md)
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.agent import claims
from src.agent.credibility import COUNTRY_ALIASES
from src.agent.schemas import FinancialSection, Report

STRONG = {"official", "trade_press"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?:\+|00)\d{1,3}[\s.-]?\d{2,4}[\s.-]?\d{2,4}[\s.-]?\d{2,4}")
AMOUNT = re.compile(r"(?:€|EUR)\s?(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s?(?:€|EUR)")
PCT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?%")
REGULATORY = re.compile(r"\b(?:complian(?:t|ce)|comply|complies)\b[^.]{0,60}\b(?:regulations?|regulatory|law|laws|legal|directive|HACCP|ISO)\b|"
                        r"\b(?:regulations?|regulatory|legal|law)\b[^.]{0,40}\b(?:complian(?:t|ce)|comply|complies|requires?)\b", re.I)
AGENT_DECIDES = re.compile(r"\b(?:the agent|this agent|the system|the assessment)\b[^.]{0,40}\b(?:approves?|approved|qualif(?:ies|ied)|orders?|ordered|purchases?|purchased)\b", re.I)
POLICY_STATEMENT = re.compile(r"\bpolic(?:y|ies)\b[^.]{0,60}\b(?:requires?|states?|mandates?|stipulates?|specifies)\b|"
                              r"\b(?:according to|per|under)\b[^.]{0,25}\bpolic(?:y|ies)\b", re.I)
CERTAINTY = re.compile(r"\b(approved|pre-?qualified|qualified|certified|capacity[- ]confirmed|guarantee[sd]?|in stock|competitive price)\b", re.I)
REPLACE = re.compile(r"\b(replace|replacing|switch|switching|terminate|cancel|place (?:an? )?orders?|purchase|buy)\b", re.I)


@dataclass(frozen=True)
class AuditCheck:
    id: str
    name: str
    passed: bool
    detail: str


def _n(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).replace("’", "'").replace("“", '"').replace("”", '"').replace("–", "-")
    return re.sub(r"\s+", " ", s).strip().casefold()


def _dec(s: str) -> Decimal | None:
    try:
        return Decimal(s.replace(",", "").replace("+", ""))
    except InvalidOperation:
        return None


def _prose(r: Report) -> list[tuple[str, str]]:
    d = r.draft
    out = [("executive_summary", d.executive_summary)]
    out += [(f"risk {x.risk_id}", f"{x.title} {x.description} {x.severity_basis}") for x in d.risks]
    out += [(f"mitigation {i + 1}", f"{m.option} {m.rationale}") for i, m in enumerate(d.mitigation_options)]
    out += [(f"action {i + 1}", f"{a.action} {a.rationale} {a.next_step}") for i, a in enumerate(d.recommended_actions)]
    out += [(f"candidate {c.name}", f"{c.name} {c.rationale} {c.verification_needed}") for c in d.supplier_candidates]
    return out


def audit_report(r: Report, *, expected_financial: FinancialSection | None = None) -> list[AuditCheck]:
    ev = {e.evidence_id: e for e in r.evidence}
    pol = {p.policy_ref: p.content for p in r.policy.passages}
    d = r.draft
    risk_ids = {x.risk_id for x in d.risks}
    checks: list[AuditCheck] = []

    def add(cid, name, problems, ok_detail):
        checks.append(AuditCheck(cid, name, not problems, ok_detail if not problems else "; ".join(problems[:4])))

    # Q1 internal findings use internal records only, and every internal reference resolves to a record in the report
    p = []
    contracts = {s.contract_id for s in r.supplier_exposure}
    suppliers = {s.supplier_id for s in r.supplier_exposure}
    months = {m.month for m in r.financial.months}
    inds = {i.ref for i in r.indicators}

    def ref_ok(ref):
        kind, *rest = ref.split(":")[1:] if ref.startswith("INT:") else (None,)
        if kind == "contract":
            return rest[0] in contracts
        if kind == "supplier":
            return rest[0] in suppliers
        if kind == "coverage":
            return rest[0] in months
        if kind == "indicator":
            return ref in inds
        if kind == "calc":
            return r.financial.status == "available"
        if kind == "benchmark":
            return r.benchmark.status == "available"
        return False

    for label, items in (("risk", d.risks), ("mitigation", d.mitigation_options), ("action", d.recommended_actions)):
        for i, x in enumerate(items):
            tag = f"{label} {getattr(x, 'risk_id', i + 1)}"
            if x.basis == "internal_model" and x.citations:
                p.append(f"{tag}: internal finding cites external evidence")
            if x.basis in ("internal_model", "combined") and not x.internal_refs:
                p.append(f"{tag}: {x.basis} finding without internal reference")
            p += [f"{tag}: unresolvable internal reference {ref}" for ref in x.internal_refs if not ref_ok(ref)]
    add("Q1", "Internal findings rest on internal records/calculations only; references resolve", p,
        f"{sum(1 for x in d.risks if x.basis == 'internal_model')} internal risks checked")

    # Q2 external claims: real, relevant, non-social, verbatim-quoted evidence, at least one strong direct source
    p, n_ext = [], 0
    for label, items in (("risk", d.risks), ("mitigation", d.mitigation_options), ("action", d.recommended_actions)):
        for i, x in enumerate(items):
            tag = f"{label} {getattr(x, 'risk_id', i + 1)}"
            if x.basis == "external_evidence" and x.internal_refs:
                p.append(f"{tag}: external finding carries internal references")
            if x.basis == "combined" and not (x.citations and x.internal_refs):
                p.append(f"{tag}: combined finding lacks one side of its support")
            if x.basis == "internal_model":
                continue
            n_ext += 1
            strong = 0
            for c in x.citations:
                e = ev.get(c.evidence_id)
                if e is None:
                    p.append(f"{tag}: unknown evidence {c.evidence_id}")
                    continue
                if e.relevance not in ("direct", "contextual") or e.credibility == "social":
                    p.append(f"{tag}: {c.evidence_id} is {e.relevance}/{e.credibility}")
                if _n(c.quote) not in _n(f"{e.title} {e.snippet}"):
                    p.append(f"{tag}: quote for {c.evidence_id} is not in the source")
                if claims.other_market_markers(c.quote):          # a price quoted for another market is context, never a strong source
                    claim_text = " ".join(str(getattr(x, k, "")) for k in ("title", "description", "option", "action", "rationale"))
                    if getattr(x, "category", "") == "market_price" and claims.PRICE_WORD.search(claim_text):
                        p.append(f"{tag}: {c.evidence_id} is another market's price used for a market-price claim")
                    continue
                logistics = getattr(x, "category", "") == "supply_disruption" and claims.logistics_support(e.domain, c.quote)
                if e.relevance == "direct" and ((e.credibility in STRONG and e.published_date) or logistics):
                    strong += 1
            lead_only = bool(x.citations) and all(ev.get(c.evidence_id) is not None and ev[c.evidence_id].kind == "supplier_discovery" for c in x.citations)
            if lead_only:          # a supplier-verification recommendation rests on a reported lead plus the qualification policy (POL-003)
                reported = {c.identity_citation.evidence_id for c in d.supplier_candidates}
                if any(c.evidence_id not in reported for c in x.citations):
                    p.append(f"{tag}: verification recommendation cites a lead that is not reported")
                if not any(pc.policy_ref.startswith("POL-003") for pc in getattr(x, "policy_citations", [])):
                    p.append(f"{tag}: verification recommendation lacks the qualification policy citation")
            elif strong == 0:
                p.append(f"{tag}: no strong direct dated source")
    observed = claims.observed_price_direction(r.evidence)
    for x in d.risks:
        if x.category == "market_price" and x.basis != "internal_model":
            text = f"{x.title}. {x.description} {x.severity_basis}"
            if observed == "falling" and not claims.FALL_WORD.search(text):
                p.append(f"{x.risk_id}: does not state the falling price direction reported by the direct evidence")
            if observed == "rising" and not claims.RISE_WORD.search(text):
                p.append(f"{x.risk_id}: does not state the rising price direction reported by the direct evidence")
            if observed == "falling" and any(claims.UP_CLAIM.search(t) and not claims.FALL_WORD.search(t) for t in re.split(r"(?<=[.;])\s+", text)):
                p.append(f"{x.risk_id}: claims upward price pressure against falling direct evidence")
            if claims.INTERNAL_EXPOSURE.search(x.severity_basis) and x.basis == "external_evidence":
                p.append(f"{x.risk_id}: external_evidence finding uses internal exposure (should be combined)")
    add("Q2", "External/combined claims cite relevant, dated, verbatim-quoted sources (>=1 strong direct)", p, f"{n_ext} external/combined findings checked")

    # Q3 alternative suppliers remain unverified leads with supported attributes
    p = []
    if len(d.supplier_candidates) > 3:
        p.append("more than 3 candidates")
    for c in d.supplier_candidates:
        e = ev.get(c.identity_citation.evidence_id)
        if c.status != "unverified":
            p.append(f"{c.name}: not marked unverified")
        if e is None:
            p.append(f"{c.name}: identity evidence missing")
            continue
        text = _n(f"{e.title} {e.snippet}")
        if e.kind != "supplier_discovery" or e.credibility == "social":
            p.append(f"{c.name}: identity source is {e.kind}/{e.credibility}")
        if _n(c.identity_citation.quote) not in text or _n(c.name) not in text:
            p.append(f"{c.name}: identity not in source")
        if c.country != "unknown":
            al = COUNTRY_ALIASES.get(c.country, ())
            if _n(c.country_quote) not in text or not any(re.search(rf"\b{re.escape(a)}\b", _n(c.country_quote)) for a in al):
                p.append(f"{c.name}: country '{c.country}' not stated in source")
        elif c.country_quote:
            p.append(f"{c.name}: country unknown but quote given")
        pq = _n(c.product_quote)
        if c.product_type != "unknown":
            if pq not in text:
                p.append(f"{c.name}: product quote not in source")
            refined, crude = ("refin" in pq or "food-grade" in pq), "crude" in pq
            if not {"refined_food_grade": refined, "crude": crude, "both": refined and crude}[c.product_type]:
                p.append(f"{c.name}: product type '{c.product_type}' unsupported (crude vs refined)")
        if CERTAINTY.search(f"{c.name} {c.rationale} {c.verification_needed}"):
            p.append(f"{c.name}: asserts approval/certification/capacity/price")
    add("Q3", "Real supplier leads are UNVERIFIED; unsupported country/product stay unknown; crude vs refined distinguished", p,
        f"{len(d.supplier_candidates)} leads checked (unknown countries: {sum(1 for c in d.supplier_candidates if c.country == 'unknown')})")

    # Q4 existing-supplier approval statuses are represented
    p = []
    non_approved = [s for s in r.supplier_exposure if s.approval_status != "approved"]
    if non_approved:
        if not any(i.category == "supplier_status" for i in r.indicators):
            p.append("non-approved supplier present but no supplier-status indicator")
        if not any(x.category == "supplier_status" for x in d.risks):
            p.append("non-approved supplier present but no supplier-status risk")
        for s in non_approved:
            if not any(s.supplier_id in i.value and s.approval_status in i.value for i in r.indicators if i.category == "supplier_status"):
                p.append(f"{s.supplier_id} ({s.approval_status}) missing from the indicator")
    add("Q4", "Existing-supplier approval statuses (e.g. SUP-003 conditional) are represented",
        p, f"non-approved: {[(s.supplier_id, s.approval_status) for s in non_approved] or 'none'}")

    # Q5 severity is explainable
    p = []
    by_cat = {i.category: i.severity for i in r.indicators}
    for x in d.risks:
        if len(x.severity_basis.strip()) < 20:
            p.append(f"{x.risk_id}: no severity basis")
        if x.category in ("contract_coverage", "supplier_status") and by_cat.get(x.category) != x.severity:
            p.append(f"{x.risk_id}: severity {x.severity} != indicator {by_cat.get(x.category)}")
        if x.category == "market_price" and x.basis == "internal_model" and by_cat.get("price_exposure") != x.severity:
            p.append(f"{x.risk_id}: severity {x.severity} != indicator {by_cat.get('price_exposure')}")
        if x.category not in ("contract_coverage", "supplier_status") and x.basis != "internal_model" and x.severity == "high":
            strong_ids = {c.evidence_id for c in x.citations if ev.get(c.evidence_id) and ev[c.evidence_id].relevance == "direct"
                          and ev[c.evidence_id].credibility in STRONG}
            if len(strong_ids) < 2:
                p.append(f"{x.risk_id}: HIGH without two strong sources")
    add("Q5", "Risk severity has an explainable basis (documented indicator or >=2 strong sources for HIGH)", p, f"{len(d.risks)} risks checked")

    # Q6 recommendations follow from identified risks and need human approval
    p = []
    for label, items in (("mitigation", d.mitigation_options), ("action", d.recommended_actions)):
        for i, x in enumerate(items, 1):
            if not x.addresses or any(a not in risk_ids for a in x.addresses):
                p.append(f"{label} {i}: does not follow from an identified risk")
            text = getattr(x, "option", None) or x.action
            if x.basis == "external_evidence" and REPLACE.search(f"{text} {x.rationale}"):
                p.append(f"{label} {i}: replacement/purchasing language on external evidence alone")
    p += [f"action {i}: no human approval" for i, a in enumerate(d.recommended_actions, 1) if not a.requires_human_approval]
    add("Q6", "Every mitigation/action addresses an identified risk; all actions require human approval; no purchasing on search results alone",
        p, f"{len(d.mitigation_options)} mitigations, {len(d.recommended_actions)} actions checked")

    # Q7 financial figures: internal arithmetic consistency (+ identical to an independent recomputation when supplied)
    p = []
    f = r.financial
    if f.status == "available":
        tot = _dec(f.baseline_modelled_cost_eur or "")
        month_sum = sum((_dec(m.baseline_modelled_cost_eur) or Decimal(0)) for m in f.months)
        if tot is None or abs(tot - month_sum) > Decimal("0.05"):
            p.append(f"baseline total {tot} != sum of months {month_sum}")
        for m in f.months:
            fc, co, un = (_dec(m.forecast_t), _dec(m.contract_commitments_t), _dec(m.uncovered_t))
            if None in (fc, co, un) or abs(fc - (co + un)) > Decimal("0.002"):
                p.append(f"{m.month}: committed + uncovered != forecast")
            if _dec(m.incremental_exposure_eur.get("+0%", "0")) != 0:
                p.append(f"{m.month}: zero-shock exposure is not zero")
        scen = {s.shock: _dec(s.incremental_exposure_eur) for s in f.scenarios}
        if scen.get("+10%") is not None and scen.get("-10%") is not None and abs(scen["+10%"] + scen["-10%"]) > Decimal("0.02"):
            p.append("+10% and -10% exposures are not symmetric")
    if expected_financial is not None and expected_financial != f:
        p.append("financial section differs from the independent recomputation")
    add("Q7", "Financial figures are consistent and unchanged by the LLM" + (" (identical to independent recomputation)" if expected_financial else ""),
        p, f"status {f.status}; baseline EUR {f.baseline_modelled_cost_eur}")

    # Q8 policy citations point to actually retrieved passages and quote them verbatim; no invented requirements
    p, n_pol = [], 0
    for label, items in (("mitigation", d.mitigation_options), ("action", d.recommended_actions)):
        for i, x in enumerate(items, 1):
            text = f"{getattr(x, 'option', None) or x.action} {x.rationale}"
            if POLICY_STATEMENT.search(text) and not x.policy_citations:
                p.append(f"{label} {i}: states a policy requirement without citing a passage")
            for c in x.policy_citations:
                n_pol += 1
                if c.policy_ref not in pol:
                    p.append(f"{label} {i}: {c.policy_ref} was not retrieved")
                elif _n(c.quote) not in _n(pol[c.policy_ref]):
                    p.append(f"{label} {i}: quote not in {c.policy_ref}")
    if r.policy.status == "unavailable" and n_pol:
        p.append("policy citations although retrieval was unavailable")
    for where, prose in _prose(r):
        if REGULATORY.search(prose):
            p.append(f"{where}: presents internal policy as regulatory/legal compliance")
        if AGENT_DECIDES.search(prose):
            p.append(f"{where}: claims the agent approves, qualifies or purchases")
    add("Q8", "Policy citations refer to retrieved passages and quote them verbatim (no invented requirements)", p,
        f"{n_pol} policy citations checked; retrieval {r.policy.status} via {r.policy.method}")

    # Q9 amounts and percentages in AI prose come from structured facts / evidence / policy
    p = []
    money = {abs(v) for v in (_dec(x) for x in [f.baseline_modelled_cost_eur or "", r.benchmark.price_eur_per_tonne or "", "150"]) if v is not None}
    pct_ok = set(money)
    for s in f.scenarios:
        money |= {abs(_dec(s.scenario_cost_eur) or 0), abs(_dec(s.incremental_exposure_eur) or 0)}
        pct_ok.add(abs(_dec(s.pct_of_baseline.rstrip("%")) or 0))
    for a in f.attribution:                      # deterministic split of the hypothetical shock
        money.add(abs(_dec(a.incremental_exposure_eur) or 0))
        pct_ok.add(abs(_dec(a.share_pct.rstrip('%')) or 0))
    for m in f.months:
        money.add(_dec(m.baseline_modelled_cost_eur))
        money |= {abs(_dec(v)) for v in m.incremental_exposure_eur.values() if _dec(v) is not None}
    for src in [i.value + " " + i.rule for i in r.indicators] + [e.title + " " + e.snippet for e in r.evidence if e.relevance in ("direct", "contextual")] + list(pol.values()) + [r.financial.assumptions]:
        for t in re.findall(r"\d[\d,]*(?:\.\d+)?", src):
            v = _dec(t)
            if v is not None:
                pct_ok.add(v)
                if "e" in src.lower():
                    pass
    for where, text in _prose(r):
        for m in AMOUNT.finditer(text):
            v = _dec(m.group(1) or m.group(2))
            if v is not None and v not in {x for x in money if x is not None} | pct_ok:
                p.append(f"{where}: amount {v} not in the structured facts")
        for m in PCT.finditer(text):
            v = _dec(m.group(1))
            if v is not None and v not in pct_ok:
                p.append(f"{where}: percentage {m.group(1)}% not in the structured facts")
    for where, text in _prose(r):
        p += [f"{where}: {m}" for m in claims.shock_problems(text)]
    add("Q9", "Amounts and percentages in AI-written prose appear in the deterministic facts, evidence or policy", p, "no invented figures found")

    # Q10 privacy and provenance labelling
    p = []
    blob = " ".join(f"{e.title} {e.snippet} {e.url}" for e in r.evidence) + " ".join(x.content for x in r.policy.passages)
    if EMAIL.search(blob) or PHONE.search(blob):
        p.append("third-party email/phone present in stored evidence")
    if any(e.is_mock for e in r.evidence) and not any(w.startswith("MOCK") for w in r.warnings):
        p.append("mock evidence without a MOCK warning")
    p += [f"{e.evidence_id}: missing retrieval date/url" for e in r.evidence if not e.url or not e.retrieved_on]
    add("Q10", "No third-party contact details; mock data is labelled; sources carry URL and retrieval date", p, f"{len(r.evidence)} evidence items checked")
    return checks


def render_audit(checks: list[AuditCheck]) -> str:
    rows = ["| ID | Property checked | Result | Detail |", "|---|---|---|---|"]
    rows += [f"| {c.id} | {c.name} | {'PASS' if c.passed else '**FAIL**'} | {c.detail} |" for c in checks]
    return "\n".join(["# Independent report-quality audit", "", *rows, "",
                      f"**{sum(c.passed for c in checks)} of {len(checks)} checks passed.** This audit verifies structure, provenance and consistency; "
                      "it does not judge the insight or wording of live model prose, which needs human review."])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", required=True, help="real read-only database data + sanitised fixtures (no external calls)")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[2] / "runs")
    args = ap.parse_args(argv)
    from datetime import date

    from src.agent.config import PROFILES
    from src.agent.data import PostgresData
    from src.agent.facts import financial_section, horizon_months
    from src.agent.graph import Deps, run_assessment
    from src.agent.mock import DemoLLM, MockSearch, build_mock_policy
    from src.calculations.exposure import calculate_exposure

    today = date.today()
    data = PostgresData()
    deps = Deps(data, DemoLLM(), MockSearch("tavily", today), MockSearch("gdelt", today), policy=build_mock_policy(), mock=True)
    resp, _ = run_assessment({"request_id": "day3-mock", "ingredient": "sunflower_oil", "horizon_months": 3}, deps)
    if resp.report is None:
        print(f"FAILED: {resp.error}")
        return 1
    inp = data.load("sunflower_oil")
    calc = calculate_exposure("sunflower_oil", horizon_months(today, 3), inp.contracts, inp.forecasts, inp.observations,
                              inp.benchmark_key or "", today, PROFILES["sunflower_oil"].assumptions)
    checks = audit_report(resp.report, expected_financial=financial_section(calc))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "day3-mock-report.md").write_text(resp.report_markdown, encoding="utf-8")
    (args.out / "day3-quality-audit.md").write_text(render_audit(checks), encoding="utf-8")
    print(render_audit(checks))
    print(f"\nreport status: {resp.status.upper()} | warnings: {len(resp.warnings)}")
    return 0 if all(c.passed for c in checks) else 2


if __name__ == "__main__":
    sys.exit(main())
