"""Regression fixtures for the problems found in the second live run (live-002), using sanitised/paraphrased evidence.

live-002 findings reproduced here:
  1. internal coverage/exposure findings cited unrelated external evidence (E3, E5) - decorative citations
  2. "Horus Oil (Spain)": country not stated in the source ("across Europe")
  3. "Natural Foods (Spain)": identity from a social-media post, country inferred from a phone prefix
  4. the relevance judge accepted weak sources (palm-oil-centred, US-market, social media) as relevant
  5. severity labels without an explainable basis (coverage called HIGH)
  6. SUP-003's conditional approval status was not used
  7. third-party email/phone persisted in evidence snippets
  8. HTTP 200 with zero GDELT articles reported as plain 'ok'
"""
import json
from decimal import Decimal as D

from src.agent.credibility import redact_contacts
from src.agent.graph import run_assessment
from src.agent.mock import MockSearch
from src.agent.schemas import Citation
from src.agent.validation import prune_unsupported, validate_draft
from tests.agent_fixtures import REQUEST, TODAY, ScriptedLLM, deps
from tests.validation_fixtures import (CTX, FACTS, MONEY, candidate, cite, cov_risk, ev, good_draft, mitigation, risk, sup_risk)

E3_Q = "Vegetable oil prices under pressure from record palm oil stocks and rising sunflower oil supply"
E5_Q = "US sunflower oil market caught in a supply-demand tug-of-war as stocks rise"
LIVE2_EVIDENCE = [
    ev("E1", relevance="contextual", snippet="Palm oil prices stay elevated as supply tightens; sunflower oil supplies are also reported tight after shipping disruptions."),
    ev("E2", snippet="The Black Sea region remains the most important corridor for sunflower oil exports."),
    ev("E3", relevance="contextual", snippet=E3_Q + "."),
    ev("E4", tier="commercial", snippet="Sunflower oil price trends, index, chart and forecast for 2026."),
    ev("E5", tier="commercial", relevance="contextual", snippet=E5_Q + "."),
    ev("E7", kind="supplier_discovery", tier="unrated", published=None, title="Sunflower Oil Suppliers in Europe - Bulk and Wholesale",
       snippet="Horus Oil supplies crude, refined and high-oleic sunflower oil to commercial buyers across Europe."),
    ev("E10", kind="supplier_discovery", tier="social", published=None, title="Any sunflower oil suppliers from Spain?",
       snippet="Natural Foods is a trusted supplier of high-quality sunflower oils. WhatsApp [phone removed] Email: [email removed]"),
    ev("E12", kind="supplier_discovery", tier="unrated", published=None, title="Sunflower oil - Elitagro",
       snippet="Elitagro: we are a factory specialising in the production of top-quality sunflower oils."),
]


def live2_draft(**kw):
    """A fully valid draft against the live-002 evidence set (external risk cites E2, the one direct trade-press item)."""
    ext = risk(citations=[cite("E2", "The Black Sea region remains the most important corridor")],
               description="Trade press says the Black Sea corridor is critical, so supply disruption there matters.")
    base = dict(risks=[cov_risk(), sup_risk(), ext])
    base.update(kw)
    return good_draft(**base)


def check(d, evidence=LIVE2_EVIDENCE):
    return validate_draft(d, evidence, FACTS, True, 3, MONEY, CTX)


# ── 1. internal findings no longer carry unrelated external citations ──
def test_live002_internal_claims_citing_e3_and_e5_are_rejected():
    r1 = cov_risk(title="Uncovered volume remains exposed to benchmark moves", severity="medium",
                  description="Modelled uncovered volume leaves price exposure on the benchmark.",
                  citations=[Citation(evidence_id="E3", quote=E3_Q), Citation(evidence_id="E5", quote=E5_Q)], internal_refs=[])
    p = check(good_draft(risks=[r1, sup_risk(), risk(citations=[cite("E2", "The Black Sea region remains the most important corridor")],
                                                       description="Trade press says the Black Sea corridor is critical, so supply disruption matters.")]))
    assert any(x.startswith("risks[0]") and "must not cite external evidence" in x for x in p)
    assert any(x.startswith("risks[0]") and "at least one internal reference" in x for x in p)


def test_live002_same_claims_relabelled_as_external_are_rejected_for_weak_or_irrelevant_sources():
    for basis_claim in ("Uncovered volume remains exposed to benchmark moves", "Coverage tightens as contracted volume falls short of demand"):
        r = risk(risk_id="R3", category="market_price", severity="high", title=basis_claim,
                 description=f"{basis_claim}; supply and price conditions are volatile.",
                 citations=[Citation(evidence_id="E3", quote=E3_Q), Citation(evidence_id="E5", quote=E5_Q)])
        p = check(good_draft(risks=[cov_risk(), sup_risk(), r]))
        assert any("no direct citation from an official or trade-press source" in x for x in p)   # contextual / commercial only
        assert any("HIGH severity needs at least two distinct direct" in x for x in p)


def test_live002_a_lenient_judge_label_still_cannot_make_weak_or_unrelated_sources_decisive():
    lenient = [e.model_copy(update={"relevance": "direct"}) if e.evidence_id in ("E3", "E5") else e for e in LIVE2_EVIDENCE]
    internal_claim = risk(risk_id="R3", category="market_price", title="Uncovered volume remains exposed",
                          description="Committed volume falls short of forecast demand in later months.",
                          citations=[Citation(evidence_id="E3", quote=E3_Q)])
    p = check(good_draft(risks=[cov_risk(), sup_risk(), internal_claim]), evidence=lenient)
    assert any("does not relate to the claim" in x for x in p)                                # quote is about palm-oil stocks
    commercial_only = risk(risk_id="R3", category="market_price", title="US stocks rise",
                           description="US sunflower oil stocks rise amid a supply-demand tug-of-war.",
                           citations=[Citation(evidence_id="E5", quote=E5_Q)])
    p = check(good_draft(risks=[cov_risk(), sup_risk(), commercial_only]), evidence=lenient)
    assert any("no direct citation from an official or trade-press source" in x for x in p)   # commercial tier is never decisive


def test_live002_unsupported_items_are_removed_visibly_and_the_internal_risk_is_restored_by_rule():
    bad_cov = cov_risk(severity="high", citations=[Citation(evidence_id="E3", quote=E3_Q)], internal_refs=[])
    dependent = mitigation(addresses=["R1"])
    d = good_draft(risks=[bad_cov, sup_risk()], mitigation_options=[dependent], recommended_actions=[], supplier_candidates=[])
    out, removed, notes, remaining = prune_unsupported(d, LIVE2_EVIDENCE, FACTS, True, 3, MONEY, CTX)
    assert remaining == [] and removed and any("Contract coverage gap" in x for x in removed)
    cov = [r for r in out.risks if r.category == "contract_coverage"]
    assert len(cov) == 1 and cov[0].severity == "medium" and cov[0].citations == [] and cov[0].internal_refs   # rule-based, no decoration
    assert any("RULE_BASED_RISK_ADDED" in n for n in notes)
    assert "E3" not in json.dumps(out.model_dump(mode="json"))


# ── 2 & 3. supplier locations stay unknown unless the source states them ──
def test_live002_horus_oil_country_must_be_unknown():
    spain = candidate(name="Horus Oil", country="Spain", country_quote="supplies crude, refined and high-oleic sunflower oil to commercial buyers across Europe",
                      product_type="both", product_quote="supplies crude, refined and high-oleic sunflower oil",
                      identity_citation=cite("E7", "Horus Oil supplies crude, refined and high-oleic sunflower oil to commercial buyers"))
    assert any("not named in the quote" in x for x in check(live2_draft(supplier_candidates=[spain])))
    unknown = candidate(name="Horus Oil", country="unknown", country_quote="", product_type="both",
                        product_quote="supplies crude, refined and high-oleic sunflower oil",
                        identity_citation=cite("E7", "Horus Oil supplies crude, refined and high-oleic sunflower oil to commercial buyers"))
    assert check(live2_draft(supplier_candidates=[unknown])) == []


def test_live002_natural_foods_social_post_phone_prefix_cannot_create_a_spanish_lead():
    c = candidate(name="Natural Foods", country="Spain", country_quote="Natural Foods is a trusted supplier of high-quality sunflower oils",
                  product_type="unknown", product_quote="", identity_citation=cite("E10", "Natural Foods is a trusted supplier of high-quality sunflower oils"))
    p = check(live2_draft(supplier_candidates=[c]))
    assert any("social-media source cannot establish" in x for x in p) and any("not named in the quote" in x for x in p)


def test_live002_a_company_site_without_a_stated_country_must_stay_unknown_even_with_a_spanish_domain():
    claims_spain = candidate(name="Elitagro", country="Spain", country_quote="we are a factory specialising in the production of top-quality sunflower oils",
                             product_type="unknown", product_quote="", identity_citation=cite("E12", "Elitagro: we are a factory specialising in the production"))
    assert any("not named in the quote" in x for x in check(live2_draft(supplier_candidates=[claims_spain])))
    honest = candidate(name="Elitagro", country="unknown", country_quote="", product_type="unknown", product_quote="",
                       identity_citation=cite("E12", "Elitagro: we are a factory specialising in the production"))
    assert check(live2_draft(supplier_candidates=[honest])) == []


def test_live002_crude_and_refined_are_not_conflated():
    refined_claim = candidate(name="Horus Oil", country="unknown", country_quote="", product_type="refined_food_grade",
                              product_quote="supplies crude and unspecified sunflower oil" , identity_citation=cite("E7", "Horus Oil supplies crude, refined and high-oleic sunflower oil"))
    assert any("product" in x for x in check(live2_draft(supplier_candidates=[refined_claim])))


# ── 4. evidence screening ──
def test_live002_workflow_labels_weak_sources_contextual_or_irrelevant_and_never_cites_them_as_support():
    resp, _ = run_assessment(REQUEST, deps())
    by = {e.title.replace("[MOCK FIXTURE] ", ""): e for e in resp.report.evidence}
    assert by["Palm oil stocks weigh on vegetable oil prices"].relevance == "contextual"          # palm-centred
    assert by["Sunflower oil price report and forecast"].relevance == "contextual"                # market-research vendor
    assert by["Forum thread: sunflower oil shortages"].relevance == "irrelevant"                  # social media
    supporting = {c.evidence_id for r in resp.report.draft.risks for c in r.citations}
    weak = {e.evidence_id for e in resp.report.evidence if e.credibility in ("social", "commercial") or e.relevance != "direct"}
    assert supporting and supporting.isdisjoint(weak)


# ── 5. explainable severity ──
def test_live002_severity_is_tied_to_documented_indicators_not_judgement():
    p = check(good_draft(risks=[cov_risk(severity="high"), sup_risk(), risk()]))
    assert any("severity must be medium per the documented contract_coverage indicator" in x for x in p)
    resp, _ = run_assessment(REQUEST, deps())
    ind = {i.category: i for i in resp.report.indicators}
    assert ind["contract_coverage"].severity == "medium" and "46." in ind["contract_coverage"].value      # 190 t / 410 t uncovered in Feb
    assert ind["price_exposure"].severity == "medium" and "+5.49%" in ind["price_exposure"].value
    for r in resp.report.draft.risks:
        assert len(r.severity_basis) >= 20 and (r.category not in ("contract_coverage", "supplier_status") or r.severity == ind[r.category].severity)


# ── 6. supplier approval status ──
def test_live002_conditional_status_of_sup003_is_represented_in_indicators_risks_and_report():
    resp, _ = run_assessment(REQUEST, deps())
    r = resp.report
    sup = next(i for i in r.indicators if i.category == "supplier_status")
    assert "SUP-003 (conditional)" in sup.value and sup.severity == "medium"
    risk_ = next(x for x in r.draft.risks if x.category == "supplier_status")
    assert risk_.basis == "internal_model" and not risk_.citations and "INT:indicator:supplier_status" in risk_.internal_refs
    assert "CONDITIONAL" in resp.report_markdown and next(s for s in r.supplier_exposure if s.supplier_id == "SUP-003").approval_status == "conditional"


# ── financial figures unchanged ──
def test_live002_financial_figures_are_unchanged():
    resp, _ = run_assessment(REQUEST, deps())
    f = resp.report.financial
    assert f.baseline_modelled_cost_eur == "1,961,496.55"
    assert {s.shock: s.incremental_exposure_eur for s in f.scenarios} == {
        "-20%": "-215,488.34", "-10%": "-107,744.17", "+0%": "0.00", "+10%": "107,744.17", "+20%": "215,488.34"}
    assert [m.baseline_modelled_cost_eur for m in f.months] == ["636,650.40", "669,929.75", "654,916.40"]
    assert f.months[0].incremental_exposure_eur["+10%"] == "28,565.04"
    assert (f.total_forecast_t, f.total_contract_commitments_t, f.total_uncovered_t) == ("1,230.000", "778.710", "451.290")
    assert resp.report.benchmark.price_eur_per_tonne == "1,457.40"


# ── 7. personal contact details ──
def test_live002_contact_details_in_snippets_are_not_persisted_or_sent_to_the_llm():
    raw = "Natural Foods is a trusted supplier of high-quality sunflower oils. WhatsApp: +34 611 23 33 82 Email: comercial@naturefoods.pro"
    clean = redact_contacts(raw)
    assert "611" not in clean and "@" not in clean and "trusted supplier" in clean
    llm = ScriptedLLM()
    resp, trace = run_assessment(REQUEST, deps(llm=llm))
    sent = json.dumps(llm.judged) + json.dumps(llm.draft_args[0][1])
    stored = json.dumps(resp.model_dump(mode="json"), default=str) + json.dumps(trace, default=str)
    for blob in (sent, stored):
        assert "+34 6" not in blob and "sales@" not in blob and "buyer@" not in blob


# ── 8. research diagnostics ──
def test_live002_http_success_with_no_articles_is_not_reported_as_plain_ok():
    resp, _ = run_assessment(REQUEST, deps(gdelt=MockSearch("gdelt", TODAY, empty=True)))
    s = resp.report.source_status["gdelt"]
    assert s.startswith("ok:") and "NO RESULTS" in s and "0 results returned" in s
    assert any("no usable results" in w for w in resp.warnings)
