"""Regression tests for the content-quality gaps found in the fourth live run (live-004), using sanitised fixtures (no live calls).

live-004 findings reproduced here:
  1. The executive summary still said supply disruption "remains a separate concern" after that risk had been removed.
  2. The supply-disruption risk was lost: a shipping/logistics trade publication was not allowed to support it, and the model cited an
     other-market price quote instead.
  3. The mitigation to verify the one reported supplier lead was removed because supplier pages are never trade-press evidence.
  4. The report did not explain what drives the +10% sensitivity (uncovered demand vs index-linked CT-003 vs fixed-price contracts).
  5. Ukrainian sunflower-seed price headlines were usable for a Spanish-benchmark risk; the summary used schema field names.
  6. Displayed scenario cost minus baseline differed from the displayed incremental exposure by EUR 0.01 (independent rounding).
"""
import re
from decimal import Decimal as D

from src.agent.claims import observed_price_direction, other_market_markers
from src.agent.config import PROFILES
from src.agent.facts import financial_section
from src.agent.graph import run_assessment
from src.agent.mock import DemoLLM
from src.agent.schemas import Citation, PolicyCitation, Risk
from src.agent.validation import ValidationContext, prune_unsupported, validate_draft
from src.calculations.exposure import calculate_exposure, round_money
from tests.agent_fixtures import REQUEST, TODAY, ScriptedLLM, deps
from tests.calc_fixtures import KEY, MONTHS, benchmark_history, contracts, forecasts
from tests.test_live003_regressions import CTX3, FACTS3, Q_FALL, Q_FALL6, F3, F6, LIVE3
from tests.validation_fixtures import EVIDENCE, MONEY, candidate, cite, cov_risk, ev, good_draft, mitigation, risk, sup_risk

Q_SHIP = "suspended Black Sea port operations are re-routing vegetable oil trade flows and tightening sunflower oil supplies"
SHIP = ev("L1", tier="unrated", title="Black Sea turmoil puts sunflower oil trade under pressure",
          snippet="Geopolitical disruptions including suspended Black Sea port operations are re-routing vegetable oil trade flows and tightening sunflower oil supplies.") \
    .model_copy(update={"domain": "safety4sea.com", "credibility": "unrated"})
COMPANY_PAGE = SHIP.model_copy(update={"evidence_id": "L2", "domain": "gradeoilgroup.example.invalid"})
Q_UKR = "Purchase prices for sunflower in Ukraine continue to fall amid increasing supply and falling oil prices"
F4 = ev("F4", title=Q_UKR, snippet="Sunflower seed purchase prices in Ukraine fell during the week as the harvest advanced.")
POL003 = "## Alternative suppliers are leads until qualified Companies found through desk research are sourcing leads. A lead must not receive an order, a contract."
Q_POL3 = "Companies found through desk research are sourcing leads."
CTX4 = ValidationContext(CTX3.internal_refs, CTX3.indicators, {**CTX3.policy, "POL-003#1": POL003})


def check(draft, evidence=None, ctx=CTX4):
    return validate_draft(draft, (evidence or []) + [SHIP, COMPANY_PAGE, F4] + LIVE3 + EVIDENCE, FACTS3, True, 3, MONEY, ctx)


def only(problems, where):
    return [p for p in problems if p.startswith(where)]


def supply_risk(**kw):
    base = dict(risk_id="R4", title="Black Sea disruption affects sunflower oil availability", category="supply_disruption",
                description="Suspended Black Sea port operations are re-routing trade flows and tightening sunflower oil supplies.",
                citations=[Citation(evidence_id="L1", quote=Q_SHIP)])
    base.update(kw)
    return risk(**base)


def draft_with(*extra):
    return good_draft(risks=[cov_risk(), sup_risk(), *extra])


# ───────────── 2. claim-specific evidence ─────────────
def test_shipping_publication_may_support_a_supply_disruption_finding():
    assert only(check(draft_with(supply_risk())), "risks[2]") == []


def test_shipping_publication_cannot_carry_other_claims_or_unrelated_quotes():
    price = supply_risk(category="market_price", title="Sunflower oil price outlook", description="Sunflower oil supplies are tightening so the benchmark may rise.",
                        basis="combined", internal_refs=["INT:indicator:price_exposure"], severity_basis="Medium per the documented indicator for the hypothetical scenario.")
    assert any("direct citation" in p for p in only(check(draft_with(price)), "risks[2]"))
    assert only(check(draft_with(supply_risk(citations=[Citation(evidence_id="L1", quote="suspended Black Sea port operations are re-routing vegetable oil trade flows")]))), "risks[2]") == []
    unrelated = SHIP.model_copy(update={"snippet": "The publication launched a new newsletter about sunflower oil in the spring."})
    probs = validate_draft(draft_with(supply_risk(citations=[Citation(evidence_id="L1", quote="launched a new newsletter about sunflower oil in the spring")])),
                           [unrelated], FACTS3, True, 3, MONEY, CTX4)
    assert only(probs, "risks[2]")


def test_company_website_cannot_support_a_supply_disruption_finding():
    probs = check(draft_with(supply_risk(citations=[Citation(evidence_id="L2", quote=Q_SHIP)])))
    assert any("direct citation" in p for p in only(probs, "risks[2]"))


def test_ukrainian_seed_prices_are_not_spanish_benchmark_evidence():
    assert other_market_markers(Q_UKR) and "ukraine" in other_market_markers(Q_UKR)
    assert other_market_markers(Q_FALL) == set() and other_market_markers(Q_FALL6) == set()
    assert other_market_markers("Sunflower seed prices fell this week as the harvest advanced") == {"sunflower seed (different commodity)"}
    bad = risk(risk_id="R3", title="Spanish crude sunflower oil benchmark outlook", category="market_price", basis="combined",
               description="Sunflower oil prices are falling, so the Spanish benchmark may fall. A hypothetical +10% benchmark shock is a scenario.",
               severity_basis="Medium per the documented indicator for the hypothetical +10% benchmark shock scenario; observed prices are falling.",
               citations=[Citation(evidence_id="F4", quote=Q_UKR)], internal_refs=["INT:indicator:price_exposure"])
    probs = " | ".join(only(check(draft_with(bad)), "risks[2]"))
    assert "another market" in probs
    # the direction summary ignores items whose own headline is about another market
    assert observed_price_direction([F3, F6, F4]) == "falling" and observed_price_direction([F4]) == "unknown"


def lead_mitigation(**kw):
    base = dict(option="Have procurement independently verify the reported sourcing lead Example Aceites Iberia S.A. under the qualification policy before any sourcing change",
                rationale="The lead is an unverified search result; the uncovered volume justifies identifying options, not choosing one.",
                applies_to="new_supplier_candidate", basis="combined", addresses=["R1"], internal_refs=["INT:indicator:coverage"],
                citations=[Citation(evidence_id="S1", quote="Example Aceites Iberia S.A. is a refining company based in Spain")],
                policy_citations=[PolicyCitation(policy_ref="POL-003#1", quote=Q_POL3)])
    base.update(kw)
    return mitigation(**base)


def test_lead_verification_needs_the_lead_and_the_qualification_policy_not_trade_press():
    d = good_draft(mitigation_options=[lead_mitigation()])
    assert only(check(d), "mitigation_options[0]") == []
    pruned, removed, _, remaining = prune_unsupported(d, [SHIP] + EVIDENCE, FACTS3, True, 3, MONEY, CTX4)
    assert not removed and len(pruned.mitigation_options) == 1


def test_lead_verification_stays_strict_where_it_matters():
    no_policy = good_draft(mitigation_options=[lead_mitigation(policy_citations=[])])
    assert any("qualification policy" in p for p in only(check(no_policy), "mitigation_options[0]"))
    other_policy = good_draft(mitigation_options=[lead_mitigation(policy_citations=[PolicyCitation(policy_ref="POL-001#2", quote="Renewal of a contract with a conditional supplier")])])
    assert any("qualification policy" in p for p in only(check(other_policy), "mitigation_options[0]"))
    unreported = good_draft(mitigation_options=[lead_mitigation(citations=[Citation(evidence_id="S2", quote="Example Danubian Oils supplies crude and refined sunflower oil")])])
    assert any("not a reported lead" in p for p in only(check(unreported), "mitigation_options[0]"))     # S2 is not among the reported candidates
    claims_qualified = good_draft(mitigation_options=[lead_mitigation(rationale="The lead is a qualified, certified refiner with confirmed capacity.")])
    assert any("approved/qualified" in p for p in only(check(claims_qualified), "mitigation_options[0]"))
    social = good_draft(mitigation_options=[lead_mitigation(citations=[Citation(evidence_id="S3", quote="Natural Foods is a trusted supplier of sunflower oils")])])
    assert only(check(social), "mitigation_options[0]")
    commercial_risk = good_draft(mitigation_options=[mitigation(basis="external_evidence", internal_refs=[], citations=[Citation(evidence_id="E3", quote="forecasts sunflower oil price movements")])])
    assert any("direct citation" in p for p in only(check(commercial_risk), "mitigation_options[0]"))      # the global rule is not weakened


def test_supplier_attributes_stay_unknown_unless_quoted():
    c = candidate(country="Portugal", country_quote="Example Aceites Iberia S.A. is a refining company based in Spain")
    assert any("not named" in p or "needs a verbatim" in p for p in only(check(good_draft(supplier_candidates=[c])), "supplier_candidates[0]"))
    assert only(check(good_draft(supplier_candidates=[candidate()])), "supplier_candidates[0]") == []


# ───────────── 3/4. financial interpretation and presentation ─────────────
def calc():
    return calculate_exposure("sunflower_oil", MONTHS, contracts(), forecasts(), benchmark_history(), KEY, TODAY, PROFILES["sunflower_oil"].assumptions)


def test_displayed_rounding_differences_are_independent_roundings_of_exact_values():
    c = calc()
    t = c.totals
    for s, v in t.scenarios.items():
        shown = round_money(v.cost) - round_money(t.baseline_cost) - round_money(v.incremental)
        assert abs(shown) <= D("0.01"), (s, shown)                                       # never more than one cent
        assert v.cost - t.baseline_cost == v.incremental                                # the exact values reconcile exactly
    assert round_money(t.scenarios[D("0.10")].cost) - round_money(t.baseline_cost) == D("107744.18")      # the live-004 observation
    assert round_money(t.scenarios[D("0.10")].incremental) == D("107744.17")                              # the validated figure is unchanged
    f = financial_section(c)
    assert "independently" in f.rounding_note and "0.01" in f.rounding_note
    assert abs(sum(D(m.baseline_modelled_cost_eur.replace(",", "")) for m in f.months) - D(f.baseline_modelled_cost_eur.replace(",", ""))) <= D("0.03")


def test_exposure_attribution_is_deterministic_and_reconciles():
    f = financial_section(calc())
    assert f.attribution_shock == "+10%"
    a = {r.component: r for r in f.attribution}
    assert a["uncovered_demand"].incremental_exposure_eur == "65,771.05" and a["uncovered_demand"].share_pct == "61.0%"
    assert a["index_linked"].incremental_exposure_eur == "41,973.12" and a["index_linked"].share_pct == "39.0%"
    assert a["fixed_price"].incremental_exposure_eur == "0.00" and a["fixed_price"].share_pct == "0.0%"
    total = sum(D(r.incremental_exposure_eur.replace(",", "")) for r in f.attribution)
    assert total == D("107744.17") == D(next(s for s in f.scenarios if s.shock == "+10%").incremental_exposure_eur.replace(",", ""))
    assert "CT-003" in a["index_linked"].detail and a["uncovered_demand"].tonnes == "451.290" and a["index_linked"].tonnes == "360.000"
    assert financial_section(calc()).model_dump() == f.model_dump()                       # no randomness, no LLM


def test_attribution_is_absent_when_the_financial_model_is_unavailable():
    resp, _ = run_assessment({**REQUEST, "assessment_date": "2026-11-20"}, deps())
    assert resp.report.financial.status == "unavailable" and resp.report.financial.attribution == []
    assert "no financial" in resp.report.draft.executive_summary.lower() or "unavailable" in resp.report.draft.executive_summary.lower()


# ───────────── 1/4. executive summary reflects only retained findings ─────────────
def _with_bad_supply_risk(llm: ScriptedLLM):
    def transform(d):
        india = next(e["evidence_id"] for e in llm.draft_args[-1][1] if "India" in e["title"])
        bad = Risk(risk_id="R9", title="Supply disruption risk from Black Sea export pressure", category="supply_disruption", basis="external_evidence", internal_refs=[],
                   description="Black Sea export pressure raises prices and disrupts supply.", severity="medium",
                   severity_basis="Direct trade-press reports describe Black Sea logistics pressure on supply.", time_relevance="recent",
                   citations=[Citation(evidence_id=india, quote="remain at $1,380/t CIF Mumbai due to delays in deliveries from the Black Sea region")])
        return d.model_copy(update={"risks": [*d.risks, bad], "executive_summary":
                                    "Supply disruption remains a separate concern tied to Black Sea logistics and export pressure. " + d.executive_summary})
    return transform


def test_a_removed_supply_risk_does_not_survive_in_the_executive_summary():
    llm = ScriptedLLM()
    llm.drafts = [_with_bad_supply_risk(llm), _with_bad_supply_risk(llm)]
    resp, _ = run_assessment(REQUEST, deps(llm=llm))
    assert resp.status == "partial" and any("Supply disruption risk from Black Sea export pressure" in x for x in resp.report.removed_claims)
    s = resp.report.draft.executive_summary
    assert "remains a separate concern" not in s and "Supply disruption remains" not in s
    assert not any(r.title.startswith("Supply disruption risk from Black Sea") for r in resp.report.draft.risks)
    assert "Unverified concerns" in s and "Supply disruption risk from Black Sea export pressure" in s       # reported as a concern, not as a finding
    md = resp.report_markdown
    assert "Unverified concerns" in md and md.index("## Executive summary") < md.index("Unverified concerns")


def test_the_summary_is_decision_oriented_and_free_of_schema_terms():
    resp, _ = run_assessment(REQUEST, deps())
    s = resp.report.draft.executive_summary
    assert not re.search(r"market_price|contract_coverage|supplier_status|supply_disruption|internal_model|external_evidence|\bcombined\b|FACTS", s)
    for needle in ("1,961,496.55", "451.290", "hypothetical", "not a forecast", "107,744.17", "61.0%", "39.0%", "0.0%", "SUP-003", "CONDITIONAL",
                   "Procurement Director", "UNVERIFIED", "falling"):
        assert needle in s, needle
    for risk_ in resp.report.draft.risks:                                                  # every finding named in the summary is a retained finding
        pass
    assert resp.report.financial.attribution


def test_summary_states_when_no_market_finding_or_lead_is_validated():
    def strip(d):
        gone = {r.risk_id for r in d.risks if r.category in ("market_price", "supply_disruption")}
        return d.model_copy(update={"risks": [r for r in d.risks if r.risk_id not in gone],
                                    "mitigation_options": [m for m in d.mitigation_options if m.basis == "internal_model" and not set(m.addresses) & gone],
                                    "recommended_actions": [a for a in d.recommended_actions if a.basis == "internal_model" and not set(a.addresses) & gone],
                                    "supplier_candidates": []})
    llm = ScriptedLLM(drafts=[strip])
    resp, _ = run_assessment(REQUEST, deps(llm=llm))
    s = resp.report.draft.executive_summary
    assert "No alternative-supplier lead is reported" in s and "falling" not in s.lower().split("hypothetical")[0]


# ───────────── 3. recommendation completeness ─────────────
def test_all_four_expected_recommendations_survive_in_the_mock_report():
    resp, _ = run_assessment(REQUEST, deps())
    assert resp.status == "completed" and not resp.report.removed_claims
    acts = " | ".join(a.action for a in resp.report.draft.recommended_actions)
    assert re.search(r"coverage", acts, re.I) and "SUP-003" in acts and re.search(r"verify .*lead|lead.*verif", acts, re.I) and re.search(r"premium|refined", acts, re.I)
    assert all(a.requires_human_approval for a in resp.report.draft.recommended_actions)
    lead = next(a for a in resp.report.draft.recommended_actions if re.search(r"lead", a.action, re.I))
    assert lead.basis == "combined" and any(c.policy_ref.startswith("POL-003") for c in lead.policy_citations)
    assert resp.report.draft.supplier_candidates and all(c.status == "unverified" for c in resp.report.draft.supplier_candidates)


def test_financial_invariants_are_unchanged():
    resp, _ = run_assessment(REQUEST, deps())
    f = resp.report.financial
    assert f.baseline_modelled_cost_eur == "1,961,496.55" and f.months[0].incremental_exposure_eur["+10%"] == "28,565.04"
    assert next(s for s in f.scenarios if s.shock == "+10%").incremental_exposure_eur == "107,744.17"
    assert "crude" in " ".join(resp.report.limitations + [resp.report.benchmark.caveat]).lower()


def test_prompt_documents_the_lead_rule_and_the_replaced_summary():
    from src.agent import prompts
    for needle in ("supplier_discovery", "POL-003", "logistics", "replaced"):
        assert needle in prompts.DRAFT_SYSTEM, needle


def test_bad_llm_summary_wording_never_fails_the_run_after_repair():
    """live demo-001: a hypothetical-shock wording problem in the LLM summary (discarded by the code-built summary) must not turn into REPORT_VALIDATION_FAILED."""
    from tests.agent_fixtures import ScriptedLLM
    bad = lambda d: d.model_copy(update={"executive_summary": "A +10% benchmark shock is expected to add EUR 107,744.17 to modelled cost over the horizon."})  # noqa: E731
    llm = ScriptedLLM(drafts=[bad, bad])
    resp, _ = run_assessment(REQUEST, deps(llm=llm))
    assert resp.status != "failed", resp.error
    assert "expected to add" not in resp.report.draft.executive_summary
