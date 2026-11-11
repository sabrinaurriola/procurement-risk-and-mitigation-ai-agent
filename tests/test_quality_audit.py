"""The independent audit must PASS on a good report and FAIL for each planted defect (otherwise it proves nothing)."""
import pytest

from src.agent.config import PROFILES
from src.agent.facts import financial_section
from src.agent.graph import run_assessment
from src.agent.quality import audit_report, render_audit
from src.agent.schemas import Citation, PolicyCitation
from src.calculations.exposure import calculate_exposure
from tests.agent_fixtures import REQUEST, TODAY, deps
from tests.calc_fixtures import KEY, MONTHS, benchmark_history, contracts, forecasts


@pytest.fixture(scope="module")
def good():
    resp, _ = run_assessment(REQUEST, deps())
    assert resp.status == "completed"
    return resp.report


@pytest.fixture(scope="module")
def expected():
    calc = calculate_exposure("sunflower_oil", MONTHS, contracts(), forecasts(), benchmark_history(), KEY, TODAY, PROFILES["sunflower_oil"].assumptions)
    return financial_section(calc)


def failed(report, expected_financial=None):
    return {c.id for c in audit_report(report, expected_financial=expected_financial) if not c.passed}


def with_draft(report, **changes):
    return report.model_copy(update={"draft": report.draft.model_copy(update=changes)})


def edit_item(report, field, index, **changes):
    items = list(getattr(report.draft, field))
    items[index] = items[index].model_copy(update=changes)
    return with_draft(report, **{field: items})


def risk_index(report, category):
    return next(i for i, r in enumerate(report.draft.risks) if r.category == category)


def evid(report, title_part):
    return next(e for e in report.evidence if title_part in e.title)


def test_good_report_passes_every_check(good, expected):
    checks = audit_report(good, expected_financial=expected)
    assert len(checks) == 10 and all(c.passed for c in checks), [(c.id, c.detail) for c in checks if not c.passed]
    assert "10 of 10 checks passed" in render_audit(checks)


# Q1 ───────────────────────────────────────────
def test_q1_internal_finding_citing_external_evidence(good):
    i = risk_index(good, "contract_coverage")
    bad = edit_item(good, "risks", i, citations=[Citation(evidence_id=evid(good, "Palm oil").evidence_id, quote="record palm oil stocks are pressuring")])
    assert "Q1" in failed(bad)


def test_q1_unresolvable_internal_reference(good):
    i = risk_index(good, "contract_coverage")
    assert "Q1" in failed(edit_item(good, "risks", i, internal_refs=["INT:contract:CT-999"]))
    assert "Q1" in failed(edit_item(good, "risks", i, internal_refs=[]))


# Q2 ───────────────────────────────────────────
def test_q2_social_commercial_irrelevant_or_unquoted_sources_cannot_support_a_claim(good):
    i = risk_index(good, "supply_disruption")
    social = evid(good, "Forum thread")
    assert "Q2" in failed(edit_item(good, "risks", i, citations=[Citation(evidence_id=social.evidence_id, quote="sunflower oil shortages")]))
    commercial = evid(good, "price report")
    assert "Q2" in failed(edit_item(good, "risks", i, citations=[Citation(evidence_id=commercial.evidence_id, quote="forecasts sunflower oil price movements")]))
    strong = evid(good, "Black Sea shipping")
    assert "Q2" in failed(edit_item(good, "risks", i, citations=[Citation(evidence_id=strong.evidence_id, quote="pirates closed every port")]))
    assert "Q2" in failed(edit_item(good, "risks", i, citations=[Citation(evidence_id="E99", quote="anything at all here")]))
    assert "Q2" in failed(edit_item(good, "risks", i, citations=[]))


def test_q2_combined_finding_needs_both_sides(good):
    i = risk_index(good, "market_price")
    assert "Q2" in failed(edit_item(good, "risks", i, internal_refs=[]))
    assert "Q1" in failed(edit_item(good, "risks", i, internal_refs=[]))


# Q3 ───────────────────────────────────────────
def test_q3_unsupported_candidate_attributes(good):
    cands = list(good.draft.supplier_candidates)
    danube = next(i for i, c in enumerate(cands) if c.name == "Example Danubian Oils")
    assert "Q3" in failed(edit_item(good, "supplier_candidates", danube, country="Spain", country_quote=cands[danube].product_quote))      # 'across Europe'
    crudo = next(i for i, c in enumerate(cands) if "Crudo" in c.name)
    assert "Q3" in failed(edit_item(good, "supplier_candidates", crudo, product_type="refined_food_grade"))                              # crude != refined
    social = evid(good, "Any sunflower oil suppliers")
    assert "Q3" in failed(edit_item(good, "supplier_candidates", crudo, identity_citation=Citation(evidence_id=social.evidence_id, quote="Natural Foods is a trusted supplier")))
    assert "Q3" in failed(edit_item(good, "supplier_candidates", crudo, rationale="A certified supplier with confirmed capacity."))
    assert "Q3" in failed(with_draft(good, supplier_candidates=cands * 2))                                                                   # more than 3


# Q4 / Q5 ──────────────────────────────────────
def test_q4_missing_supplier_status_risk_or_indicator(good):
    no_risk = with_draft(good, risks=[r for r in good.draft.risks if r.category != "supplier_status"])
    assert "Q4" in failed(no_risk)
    no_ind = good.model_copy(update={"indicators": [i for i in good.indicators if i.category != "supplier_status"]})
    assert "Q4" in failed(no_ind)


def test_q5_severity_must_follow_indicators_and_high_needs_two_strong_sources(good):
    assert "Q5" in failed(edit_item(good, "risks", risk_index(good, "contract_coverage"), severity="high"))
    assert "Q5" in failed(edit_item(good, "risks", risk_index(good, "supply_disruption"), severity="high"))
    assert "Q5" in failed(edit_item(good, "risks", risk_index(good, "supply_disruption"), severity_basis="n/a"))


# Q6 ───────────────────────────────────────────
def test_q6_recommendations_must_follow_from_risks_and_need_approval(good):
    assert "Q6" in failed(edit_item(good, "recommended_actions", 0, addresses=["R99"]))
    assert "Q6" in failed(edit_item(good, "recommended_actions", 0, requires_human_approval=False))
    ext = next(i for i, a in enumerate(good.draft.recommended_actions) if a.basis == "external_evidence")
    assert "Q6" in failed(edit_item(good, "recommended_actions", ext, action="Switch to a new supplier now"))


# Q7 ───────────────────────────────────────────
def test_q7_tampered_or_inconsistent_financials(good, expected):
    months = list(good.financial.months)
    months[0] = months[0].model_copy(update={"baseline_modelled_cost_eur": "999,999.99"})
    tampered = good.model_copy(update={"financial": good.financial.model_copy(update={"months": months})})
    assert "Q7" in failed(tampered)
    shifted = good.model_copy(update={"financial": good.financial.model_copy(update={"baseline_modelled_cost_eur": "1,961,497.55"})})
    assert "Q7" in failed(shifted, expected)
    months = list(good.financial.months)
    months[1] = months[1].model_copy(update={"uncovered_t": "10.000"})
    assert "Q7" in failed(good.model_copy(update={"financial": good.financial.model_copy(update={"months": months})}))


# Q8 ───────────────────────────────────────────
def test_q8_policy_citations_must_be_retrieved_and_quoted_verbatim(good):
    i = next(i for i, m in enumerate(good.draft.mitigation_options) if m.policy_citations)
    assert "Q8" in failed(edit_item(good, "mitigation_options", i, policy_citations=[PolicyCitation(policy_ref="POL-009#0", quote="invented requirement text here")]))
    ref = good.draft.mitigation_options[i].policy_citations[0].policy_ref
    assert "Q8" in failed(edit_item(good, "mitigation_options", i, policy_citations=[PolicyCitation(policy_ref=ref, quote="a requirement that is not in this passage")]))
    assert "Q8" in failed(edit_item(good, "mitigation_options", i, policy_citations=[], rationale="Company policy requires a quarterly review."))
    unavailable = good.model_copy(update={"policy": good.policy.model_copy(update={"status": "unavailable", "passages": []})})
    assert "Q8" in failed(unavailable)


# Q9 / Q10 ─────────────────────────────────────
def test_q9_invented_amounts_and_percentages_in_prose(good):
    assert "Q9" in failed(with_draft(good, executive_summary="Exposure will reach EUR 999,999 over the horizon given uncovered demand growth."))
    assert "Q9" in failed(with_draft(good, executive_summary="Coverage should be raised to a 70% target to remain within the stated policy."))
    assert "Q9" in failed(with_draft(good, executive_summary="Contract coverage leaves EUR 451.290 t uncovered across the three months of the horizon."))


def test_q10_contact_details_and_missing_mock_label(good):
    e = list(good.evidence)
    e[0] = e[0].model_copy(update={"snippet": e[0].snippet + " write to buyer@example.invalid or call +34 611 222 333"})
    assert "Q10" in failed(good.model_copy(update={"evidence": e}))
    assert "Q10" in failed(good.model_copy(update={"warnings": [w for w in good.warnings if not w.startswith("MOCK")]}))


def test_q8_regulatory_compliance_and_agent_decision_claims_are_caught(good):
    assert "Q8" in failed(with_draft(good, executive_summary="Following the coverage review keeps the company compliant with EU regulation on supply contracts."))
    assert "Q8" in failed(with_draft(good, executive_summary="The agent approves the alternative supplier after the coverage review of uncovered volume."))


def test_audit_catches_live003_price_direction_and_scenario_defects(good):
    i = risk_index(good, "market_price")
    base = good.draft.risks[i]
    assert base.basis == "combined" and "falling" in base.description                       # the mock now reports the observed direction
    upward = edit_item(good, "risks", i, description="Trade press shows continuing sunflower-oil price pressure and rising prices on uncovered volume.",
                       title="Market price pressure")
    assert "Q2" in failed(upward)
    assert "Q9" in failed(with_draft(good, executive_summary="Modelled baseline cost is EUR 1,961,496.55 and a +10% benchmark shock is forecast to add EUR 107,744.17."))
    internal = next(r for r in good.draft.risks if r.category == "market_price" and r.basis == "internal_model")
    assert internal.severity == next(x.severity for x in good.indicators if x.category == "price_exposure")
    assert "Q5" in failed(edit_item(good, "risks", good.draft.risks.index(internal), severity="high"))


def test_audit_checks_lead_verification_recommendations(good):
    i = next(i for i, a in enumerate(good.draft.recommended_actions) if "reported lead" in a.action)
    assert "Q2" not in failed(good)
    assert "Q2" in failed(edit_item(good, "recommended_actions", i, policy_citations=[]))                 # lacks the qualification policy
    assert "Q2" in failed(with_draft(good, supplier_candidates=[]))                                       # cites a lead that is not reported
