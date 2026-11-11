"""Evidence, internal references and a fully valid draft, shared by the validation and regression tests."""
from datetime import date, timedelta
from decimal import Decimal

from src.agent.schemas import (Action, Citation, DraftReport, EvidenceItem, Indicator, Mitigation, PolicyCitation, Risk, SupplierCandidate)
from src.agent.validation import ValidationContext

TODAY = date(2026, 11, 10)

Q_SUPPLY = "shipping delays are tightening sunflower oil supplies to Europe"          # inside E1's snippet
Q_SPAIN = "is a refining company based in Spain producing refined, food-grade sunflower oil"   # inside S1's snippet
Q_REFINED = "producing refined, food-grade sunflower oil"
Q_CRUDE_REFINED = "supplies crude and refined sunflower oil to commercial buyers across Europe"  # inside S2's snippet


def ev(i, kind="risk", relevance="direct", tier="trade_press", published=TODAY - timedelta(days=5), title="Fixture item", snippet=""):
    return EvidenceItem(evidence_id=i, source="tavily", kind=kind, url=f"https://{tier}.example.invalid/{i}", domain=f"{tier}.example.invalid",
                        credibility=tier, title=title, snippet=snippet, published_date=published, retrieved_on=TODAY, query="q",
                        relevance=relevance)


EVIDENCE = [
    ev("E1", snippet="Black Sea shipping delays are tightening sunflower oil supplies to Europe and freight costs remain elevated."),
    ev("E2", relevance="contextual", snippet="Record palm oil stocks are pressuring vegetable oil prices; sunflower oil is a minor comparison."),
    ev("E3", tier="commercial", snippet="A market-research vendor forecasts sunflower oil price movements for the coming quarter."),
    ev("E4", tier="social", snippet="Forum post about sunflower oil shortages near the Black Sea ports and rising prices."),
    ev("E5", relevance="irrelevant", snippet="Unrelated fuel market commentary."),
    ev("S1", kind="supplier_discovery", tier="unrated", published=None, title="Example Aceites Iberia S.A. - refined sunflower oil",
       snippet="Example Aceites Iberia S.A. is a refining company based in Spain producing refined, food-grade sunflower oil for industrial bakeries."),
    ev("S2", kind="supplier_discovery", tier="unrated", published=None, title="Example Danubian Oils - sunflower oil",
       snippet="Example Danubian Oils supplies crude and refined sunflower oil to commercial buyers across Europe."),
    ev("S3", kind="supplier_discovery", tier="social", published=None, title="Any sunflower oil suppliers?",
       snippet="Natural Foods is a trusted supplier of sunflower oils. WhatsApp [phone removed] Email: [email removed]"),
    ev("S4", kind="supplier_discovery", relevance="contextual", tier="commercial", published=None, title="Sunflower oil - 88 suppliers",
       snippet="Directory page listing many sunflower oil manufacturers and traders."),
]

REFS = {"INT:indicator:coverage", "INT:indicator:supplier_status", "INT:indicator:price_exposure", "INT:calc:baseline",
        "INT:coverage:2027-02", "INT:contract:CT-003", "INT:supplier:SUP-003", "INT:benchmark"}
INDICATORS = [
    Indicator(ref="INT:indicator:coverage", category="contract_coverage", severity="medium",
              value="peak uncovered share 46.3% in 2027-02", rule="peak uncovered share >= 50% high; >= 25% medium; > 0% low"),
    Indicator(ref="INT:indicator:supplier_status", category="supplier_status", severity="medium",
              value="SUP-003 (conditional): 46.2% of contract-committed volume", rule="conditional supplier: medium"),
]
CTX = ValidationContext(set(REFS), INDICATORS)
MONEY = {Decimal("1961496.55"), Decimal("107744.17"), Decimal("1457.40"), Decimal("150")}
FACTS = ('{"financial": {"baseline_modelled_cost_eur": "1,961,496.55", "total_uncovered_t": "451.290"}, "severity_indicators": ['
         '{"value": "peak uncovered share 46.3% in 2027-02 (190.000 t of 410.000 t forecast)", "rule": "peak uncovered share >= 50% high; >= 25% medium; > 0% low"}, '
         '{"value": "SUP-003 (conditional): 46.2% of contract-committed volume over the horizon"}]}')


def cite(i="E1", q=Q_SUPPLY):
    return Citation(evidence_id=i, quote=q)


def risk(**kw):
    base = dict(risk_id="R3", title="Supply disruption reported", description="Trade press reports that shipping delays are tightening supply.",
                category="supply_disruption", basis="external_evidence", severity="medium",
                severity_basis="Single direct trade-press source; not enough for high severity.", time_relevance="recent",
                citations=[cite()], internal_refs=[])
    base.update(kw)
    return Risk(**base)


def cov_risk(**kw):
    base = dict(risk_id="R1", title="Contract coverage gap", description="Committed volume does not cover forecast demand in later months.",
                category="contract_coverage", basis="internal_model", severity="medium",
                severity_basis="Documented indicator: peak uncovered share 46.3% falls in the medium band.", time_relevance="horizon",
                citations=[], internal_refs=["INT:indicator:coverage"])
    base.update(kw)
    return Risk(**base)


def sup_risk(**kw):
    base = dict(risk_id="R2", title="Existing supplier approval status", description="SUP-003 holds an index-linked contract but is only conditionally approved.",
                category="supplier_status", basis="internal_model", severity="medium",
                severity_basis="Documented indicator: a conditional supplier with an active contract is medium.", time_relevance="horizon",
                citations=[], internal_refs=["INT:indicator:supplier_status", "INT:supplier:SUP-003"])
    base.update(kw)
    return Risk(**base)


def mitigation(**kw):
    base = dict(option="Review contract coverage for the months with the largest uncovered volume", rationale="Uncovered volume carries market exposure.",
                applies_to="contract_strategy", basis="internal_model", addresses=["R1"], citations=[], internal_refs=["INT:indicator:coverage"],
                policy_citations=[])
    base.update(kw)
    return Mitigation(**base)


def action(**kw):
    base = dict(action="Decide whether to extend contract coverage", rationale="The coverage table shows uncovered tonnes.",
                next_step="Procurement manager reviews renewal options.", requires_human_approval=True, basis="internal_model",
                addresses=["R1"], citations=[], internal_refs=["INT:indicator:coverage"], policy_citations=[])
    base.update(kw)
    return Action(**base)


def candidate(**kw):
    base = dict(name="Example Aceites Iberia S.A.", country="Spain", country_quote=Q_SPAIN, product_type="refined_food_grade",
                product_quote=Q_REFINED, identity_citation=cite("S1", Q_SPAIN), rationale="Self-reported refiner found by search.",
                verification_needed="Verify identity, specification, capacity, certifications and pricing independently.", status="unverified")
    base.update(kw)
    return SupplierCandidate(**base)


def good_draft(**kw):
    base = dict(executive_summary="Modelled baseline cost is EUR 1,961,496.55 and 451.290 t of demand remains uncovered over the horizon.",
                risks=[cov_risk(), sup_risk(), risk()], mitigation_options=[mitigation()], supplier_candidates=[candidate()],
                recommended_actions=[action()], uncertainties=["The benchmark is a crude-oil price."])
    base.update(kw)
    return DraftReport(**base)
