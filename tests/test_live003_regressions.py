"""Regression tests for the content-quality defects found in the third live run (live-003), using short sanitised paraphrases
of the real trade-press items (no live calls).

live-003 findings reproduced here:
  1. R3 said trade press showed "continuing price pressure" while the direct sources reported FALLING prices, presented the
     hypothetical +10% shock as if it described the market, and was labelled external_evidence although it used the internal exposure.
  2. An India CIF Mumbai quotation was cited for a claim about the Spanish crude-sunflower-oil benchmark.
  3. The SUP-003 escalation action (POL-001#2 + ASM-003#2) was removed because a supporting quote shared no wording with the claim.
  4. "monthly" and "short-list" were attributed to POL-002, which says neither.
"""
from decimal import Decimal as D

from src.agent.claims import observed_price_direction, other_market_markers, price_direction
from src.agent.graph import run_assessment
from src.agent.schemas import Citation, Indicator, PolicyCitation
from src.agent.validation import ValidationContext, prune_unsupported, validate_draft
from tests.agent_fixtures import REQUEST, ScriptedLLM, deps
from tests.validation_fixtures import (CTX, EVIDENCE, FACTS, MONEY, REFS, action, candidate, cite, cov_risk, ev, good_draft, mitigation, risk,
                                       sup_risk)

# HISTORICAL FIXTURES: the POL-001 / ASM-003 strings below (POL001, ASM003, Q_P1, Q_A3) reproduce the wording at the time of live-003, BEFORE the
# policy consistency correction. They exercise validator logic only; the current policy text is in data/synthetic/documents and is tested in
# tests/test_policy_documents.py and tests/test_policy_graph.py.
# ───────────── sanitised paraphrases of the live-003 trade-press items ─────────────
F1 = ev("F1", snippet="Geopolitical disruptions including suspended Black Sea port operations are re-routing vegetable oil trade flows and tightening sunflower oil supplies.",
        title="CPO prices to stay above forecasts amid tightening supply")
F2 = ev("F2", title="Vegetable oil prices under pressure from record palm oil stocks and rising sunflower oil supply",
        snippet="Offer prices for Russian sunflower oil increased by $20-30/t to $1,250-1,270/t FOB Black Sea ports during the week amid restrictions on vessel calls.")
F3 = ev("F3", title="Palm and sunflower oil prices fall due to seasonal supply increases",
        snippet="Sunflower oil demand prices in India remain at $1,380-1,400/t CIF Mumbai due to delays in deliveries from the Black Sea region. "
                "Russian sunflower oil prices lost another $40-50/t in the week under pressure from increased sunflower production.")
F5 = ev("F5", title="Purchase prices for sunflower in Ukraine continue to fall amid increasing supply and falling oil prices",
        snippet="Sunflower oil demand prices in India fell by $30/t to $1,440/t CIF Mumbai during the week amid a sharp increase in supply from the Black Sea region.")
F6 = ev("F6", title="Palm oil prices are falling under pressure from large inventories, while sunflower oil prices are falling in anticipation of increased supply.",
        snippet="Traders expect increased pressure on sunflower oil prices in anticipation of a sharp increase in supply on the world market.")
LIVE3 = [F1, F2, F3, F5, F6]

Q_INDIA = "Sunflower oil demand prices in India remain at $1,380-1,400/t CIF Mumbai due to delays in deliveries from the Black Sea region."
Q_RUSS_UP = "Offer prices for Russian sunflower oil increased by $20-30/t to $1,250-1,270/t FOB Black Sea ports during the week"
Q_FALL = "Palm and sunflower oil prices fall due to seasonal supply increases"
Q_FALL6 = "sunflower oil prices are falling in anticipation of increased supply"
Q_MECH = "suspended Black Sea port operations are re-routing vegetable oil trade flows and tightening sunflower oil supplies"

IND = CTX.indicators + [Indicator(ref="INT:indicator:price_exposure", category="price_exposure", severity="medium",
                                  value="+10% benchmark shock = +5.49% of baseline modelled cost (EUR 107,744.17)",
                                  rule="+10% benchmark shock as % of baseline modelled cost: >= 10% high; >= 3% medium; > 0% low")]

POL001 = ("## Conditional suppliers Renewal of a contract with a conditional supplier, or an increase in its volume, requires sign-off by the "
          "Procurement Director after the open conditions are closed. The procurement team should avoid placing a large share of an ingredient's "
          "committed volume with conditionally approved suppliers. Any material share must be reported in the monthly risk review together with "
          "the planned closing date of the open conditions.")
ASM003 = ("## Conditions to close The supplier must submit an updated food-safety certificate and complete a follow-up audit. Until both conditions "
          "are closed, any renewal of CT-003 or increase of its volume requires sign-off by the Procurement Director.")
ASM001 = "## Summary Aceites del Sur S.A. holds the fixed-price contract CT-001 that runs until June 2027."
POL002 = ("## Escalation of growing uncovered volume When uncovered volume increases from one month to the next, for example because a contract "
          "ends during the horizon, the Raw Materials Procurement Manager performs a coverage review and recommends whether to extend, replace or add contracts.")
FACTS3 = FACTS + " +10% benchmark shock = +5.49% of baseline modelled cost (EUR 107,744.17); >= 10% high; >= 3% medium; > 0% low"
CTX3 = ValidationContext(set(REFS) | {"INT:contract:CT-003"}, IND, {"POL-001#2": POL001, "ASM-003#2": ASM003, "ASM-001#1": ASM001, "POL-002#3": POL002})

Q_P1 = "Renewal of a contract with a conditional supplier, or an increase in its volume, requires sign-off by the Procurement Director after the open conditions are closed."
Q_A3 = "The supplier must submit an updated food-safety certificate and complete a follow-up audit."
Q_COV = "When uncovered volume increases from one month to the next, for example because a contract ends during the horizon"


def problems_for(draft, evidence=None, ctx=CTX3):
    return validate_draft(draft, (evidence or []) + LIVE3 + EVIDENCE, FACTS3, True, 3, MONEY, ctx)


def only(problems, where):
    return [p for p in problems if p.startswith(where)]


def r3(**kw):
    """The R3 of live-003 as drafted."""
    base = dict(risk_id="R3", title="Benchmark-linked price exposure remains sensitive to market moves", category="market_price",
                description="The modelled procurement cost is sensitive to benchmark changes, with the +10% shock increasing baseline modelled cost.",
                basis="external_evidence", severity="medium",
                severity_basis="Severity is medium because the internal indicator shows a +10% benchmark shock equals +5.49% of baseline modelled cost, "
                               "and trade-press evidence shows continuing sunflower-oil price pressure and volatility.",
                time_relevance="Near-term horizon", citations=[Citation(evidence_id="F2", quote=Q_RUSS_UP), Citation(evidence_id="F3", quote=Q_INDIA)],
                internal_refs=[])
    base.update(kw)
    return risk(**base)


def r3_fixed(**kw):
    base = dict(title="Observed sunflower-oil prices are falling while Black Sea supply stays volatile",
                description="Trade press reports that sunflower oil prices are falling on seasonal supply increases; Black Sea port restrictions still add supply "
                            "volatility. Separately, a hypothetical +10% benchmark shock is a sensitivity scenario, not a price forecast.",
                basis="combined", severity="medium",
                severity_basis="Medium per the documented indicator: the hypothetical +10% benchmark shock scenario adds 5.49% of baseline modelled cost; "
                               "the observed direction in trade press is falling.",
                citations=[Citation(evidence_id="F3", quote=Q_FALL), Citation(evidence_id="F6", quote=Q_FALL6)],
                internal_refs=["INT:indicator:price_exposure"])
    base.update(kw)
    return r3(**base)


def draft_with(*extra_risks, **kw):
    return good_draft(risks=[cov_risk(), sup_risk(), *extra_risks], **kw)


# ───────────── 1. price direction, hypothetical shocks, basis ─────────────
def test_direction_of_the_live003_evidence_is_falling():
    assert [price_direction(e.title, e.snippet) for e in (F2, F3, F5, F6)] == ["mixed", "down", "down", "down"]
    assert observed_price_direction(LIVE3) == "falling"
    assert observed_price_direction([F2]) != "falling" and observed_price_direction([]) == "unknown"
    rising = [ev(f"U{i}", title="Sunflower oil prices rise on tight supply", snippet="Offer prices increased by 5 EUR/t.") for i in range(3)]
    assert observed_price_direction(rising) == "rising"


def test_original_r3_is_rejected_for_every_defect():
    probs = " | ".join(only(problems_for(draft_with(r3())), "risks[2]"))
    assert "other market" in probs                      # India CIF Mumbai / Russian FOB quotation for a general price claim
    assert "falling" in probs                           # 'continuing ... price pressure' against falling evidence
    assert "hypothetical" in probs                      # +10% shock described without scenario wording
    assert "combined" in probs                          # uses internal exposure but is labelled external_evidence


def test_corrected_r3_passes():
    assert only(problems_for(draft_with(r3_fixed())), "risks[2]") == []


def test_falling_evidence_must_be_reported_as_falling():
    no_dir = r3_fixed(title="Sunflower oil supply and Black Sea restrictions", description="Trade press discusses sunflower oil supply and Black Sea restrictions. A hypothetical +10% benchmark shock is a sensitivity scenario.",
                      severity_basis="Medium per the documented indicator: hypothetical +10% benchmark shock scenario; no price direction is stated.")
    assert any("observed price direction" in p for p in only(problems_for(draft_with(no_dir)), "risks[2]"))
    upward = r3_fixed(description="Trade press shows rising sunflower oil prices and upward price pressure. A hypothetical +10% benchmark shock is a scenario.")
    assert any("falling" in p for p in only(problems_for(draft_with(upward)), "risks[2]"))


def test_rising_evidence_may_be_reported_as_rising():
    rising = [ev(f"U{i}", title="Sunflower oil prices rise on tight supply", snippet="Sunflower oil prices rise as exports slow; Black Sea buyers pay more.") for i in (1, 2)]
    draft = draft_with(r3_fixed(title="Observed sunflower-oil prices are rising on tight exports", description="Trade press reports sunflower oil prices rising as exports slow. A hypothetical +10% benchmark shock is a sensitivity scenario.",
                                severity_basis="Medium per the documented indicator: hypothetical +10% benchmark shock scenario; observed prices are rising.",
                                citations=[Citation(evidence_id="U1", quote="Sunflower oil prices rise as exports slow")]))
    probs = validate_draft(draft, rising + [F1], FACTS3, True, 3, MONEY, CTX3)
    assert only(probs, "risks[2]") == []


def test_hypothetical_shock_is_never_a_forecast():
    for text in ("The +10% benchmark shock is expected to happen next quarter.", "Prices will rise by 10% so the shock adds cost.",
                 "A +10% shock is forecast for the benchmark."):
        probs = only(problems_for(draft_with(r3_fixed(description=text + " Sensitivity scenario of the model."))), "risks[2]")
        assert any("forecast" in p for p in probs), text
    assert any("hypothetical" in p for p in problems_for(draft_with(executive_summary="Modelled baseline cost is EUR 1,961,496.55 and a +10% benchmark shock adds EUR 107,744.17 to it.")))
    ok = "Modelled baseline cost is EUR 1,961,496.55; a hypothetical +10% benchmark shock scenario would add EUR 107,744.17 (not a forecast)."
    assert not [p for p in problems_for(draft_with(executive_summary=ok)) if p.startswith("executive_summary")]


def test_internal_only_price_sensitivity_needs_no_external_citation():
    sens = r3(title="Modelled sensitivity to the benchmark", basis="internal_model", citations=[], internal_refs=["INT:indicator:price_exposure"],
              description="In a hypothetical +10% benchmark shock scenario the modelled cost rises by 5.49% of baseline; this is a sensitivity, not a forecast.",
              severity_basis="Documented indicator: the hypothetical +10% benchmark shock scenario adds 5.49% of baseline modelled cost (medium band).")
    assert only(problems_for(draft_with(sens)), "risks[2]") == []
    assert any("price_exposure" in p for p in only(problems_for(draft_with(sens.model_copy(update={"internal_refs": ["INT:indicator:coverage"]}))), "risks[2]"))
    assert any("severity must be medium" in p for p in only(problems_for(draft_with(sens.model_copy(update={"severity": "high"}))), "risks[2]"))
    asserts = sens.model_copy(update={"description": "Sunflower oil prices are rising, and a hypothetical +10% benchmark shock scenario shows the sensitivity."})
    assert any("market direction" in p for p in only(problems_for(draft_with(asserts)), "risks[2]"))


def test_combined_claims_need_both_sides():
    no_internal = r3_fixed(internal_refs=[])
    no_external = r3_fixed(citations=[])
    assert any("BOTH" in p for p in only(problems_for(draft_with(no_internal)), "risks[2]"))
    assert any("BOTH" in p for p in only(problems_for(draft_with(no_external)), "risks[2]"))


# ───────────── 2. geographic / market relevance ─────────────
def test_other_market_price_quotes_are_detected():
    assert other_market_markers(Q_INDIA) >= {"india", "mumbai", "cif"}
    assert "fob" in other_market_markers(Q_RUSS_UP)
    assert other_market_markers(Q_MECH) == set()                                            # no price quotation: supply-chain mechanism
    assert other_market_markers("Spanish crude sunflower oil traded at EUR 1,450/t in Spain this week") == set()
    assert other_market_markers("Prices of 1,200 $/t CIF Rotterdam were reported for European buyers") == set()


def test_india_price_cannot_independently_support_a_spanish_benchmark_claim():
    bad = r3_fixed(title="Spanish crude sunflower oil benchmark outlook", citations=[Citation(evidence_id="F3", quote=Q_INDIA)],
                   description="Sunflower oil prices are falling and delivery delays from the Black Sea region could move the crude sunflower oil benchmark for Spain. A hypothetical +10% benchmark shock is a scenario.")
    probs = " | ".join(only(problems_for(draft_with(bad)), "risks[2]"))
    assert "other market" in probs


def test_foreign_price_may_appear_as_context_but_never_as_the_strong_source():
    named = r3_fixed(description="Trade press reports falling prices, for example in India (CIF Mumbai), as context for the benchmark. A hypothetical +10% benchmark shock is a scenario.",
                     citations=[Citation(evidence_id="F3", quote=Q_INDIA), Citation(evidence_id="F6", quote=Q_FALL6)])
    assert only(problems_for(draft_with(named)), "risks[2]") == []                          # named market + a valid direct citation
    only_ctx = named.model_copy(update={"citations": [Citation(evidence_id="F3", quote=Q_INDIA)]})
    assert any("direct citation" in p for p in only(problems_for(draft_with(only_ctx)), "risks[2]"))


def test_black_sea_supply_mechanism_is_preserved():
    mech = risk(risk_id="R4", title="Black Sea disruption affects European sunflower oil availability", category="supply_disruption",
                description="Suspended Black Sea port operations are re-routing trade flows and tightening sunflower oil supplies for European buyers.",
                citations=[Citation(evidence_id="F1", quote=Q_MECH)])
    assert only(problems_for(draft_with(mech)), "risks[2]") == []
    with_india = mech.model_copy(update={"citations": [Citation(evidence_id="F1", quote=Q_MECH), Citation(evidence_id="F3", quote=Q_INDIA)]})
    assert only(problems_for(draft_with(with_india)), "risks[2]") == []                      # price quote as extra context on a non-price claim


# ───────────── 3. SUP-003 escalation ─────────────
def esc(**kw):
    base = dict(action="Escalate SUP-003 renewal or any volume increase to the Procurement Director and request a status review of the open conditions.",
                rationale="SUP-003 is conditionally approved, not blocked; renewal or a volume increase needs Procurement Director sign-off while its open conditions are tracked.",
                next_step="Procurement manager prepares the escalation note.", basis="internal_model", addresses=["R2"],
                internal_refs=["INT:indicator:supplier_status", "INT:supplier:SUP-003"],
                policy_citations=[PolicyCitation(policy_ref="POL-001#2", quote=Q_P1), PolicyCitation(policy_ref="ASM-003#2", quote=Q_A3)])
    base.update(kw)
    return action(**base)


def draft_act(a):
    return good_draft(recommended_actions=[a], mitigation_options=[])


def test_sup003_escalation_survives_validation():
    d = draft_act(esc())
    assert only(problems_for(d), "recommended_actions[0]") == []
    pruned, removed, _, remaining = prune_unsupported(d, LIVE3 + EVIDENCE, FACTS, True, 3, MONEY, CTX3)
    assert not removed and not remaining and len(pruned.recommended_actions) == 1


def test_escalation_citing_an_unrelated_suppliers_assessment_is_still_rejected():
    bad = esc(policy_citations=[PolicyCitation(policy_ref="POL-001#2", quote=Q_P1),
                                PolicyCitation(policy_ref="ASM-001#1", quote="holds the fixed-price contract CT-001 that runs until June 2027")])
    assert any("does not relate" in p for p in only(problems_for(draft_act(bad)), "recommended_actions[0]"))
    only_linked = esc(policy_citations=[PolicyCitation(policy_ref="ASM-003#2", quote=Q_A3)])
    assert only(problems_for(draft_act(only_linked)), "recommended_actions[0]")              # a linked quote alone is not substantive support


def test_sign_off_requirements_must_be_cited_and_must_not_gain_exceptions():
    assert any("approval" in p for p in only(problems_for(draft_act(esc(policy_citations=[]))), "recommended_actions[0]"))
    for text in ("Waive the sign-off for SUP-003 renewal as an exception while the conditions are open.",
                 "Bypass the Procurement Director sign-off for SUP-003 and fast-track the renewal."):
        probs = only(problems_for(draft_act(esc(rationale=text))), "recommended_actions[0]")
        assert any("exception" in p for p in probs), text


def test_sup003_stays_conditional_and_the_agent_does_not_authorise():
    assert any("blocked" in p for p in only(problems_for(draft_act(esc(rationale="SUP-003 is a blocked supplier, so renewal needs Procurement Director sign-off while conditions are tracked."))),
                                            "recommended_actions[0]"))
    assert any("cannot approve" in p for p in only(problems_for(draft_act(esc(action="Approve the renewal of CT-003 for SUP-003 with the Procurement Director."))), "recommended_actions[0]"))


# ───────────── 4. unattributed cadence / process wording ─────────────
def cov_mit(text, quote=Q_COV, ref="POL-002#3"):
    return mitigation(option=text, rationale="Uncovered volume increases across the horizon.", internal_refs=["INT:indicator:coverage"],
                      policy_citations=[PolicyCitation(policy_ref=ref, quote=quote)])


def test_monthly_cadence_and_shortlist_are_not_attributed_to_pol002():
    d = good_draft(mitigation_options=[cov_mit("Run a monthly coverage review and prepare a short-list of contract options for the uncovered volume.")])
    probs = " | ".join(only(problems_for(d), "mitigation_options[0]"))
    assert "monthly" in probs and "short-list" in probs
    plain = good_draft(mitigation_options=[cov_mit("Run a coverage review and consider whether to extend, replace or add contracts for the uncovered volume.")])
    assert only(problems_for(plain), "mitigation_options[0]") == []


def test_a_cadence_the_policy_actually_states_is_allowed():
    d = good_draft(mitigation_options=[mitigation(option="Report the share of conditional-supplier volume in the monthly risk review.",
                                                  rationale="SUP-003 carries a material share of committed volume.", internal_refs=["INT:indicator:supplier_status"],
                                                  policy_citations=[PolicyCitation(policy_ref="POL-001#2", quote="Any material share must be reported in the monthly risk review together with the planned closing date")])])
    assert only(problems_for(d), "mitigation_options[0]") == []


# ───────────── 5. unchanged guarantees ─────────────
def test_unsupported_supplier_leads_remain_excluded():
    bad = candidate(identity_citation=Citation(evidence_id="S1", quote="Example Aceites Iberia S.A. is certified by every authority in Europe"))
    pruned, removed, _, remaining = prune_unsupported(good_draft(supplier_candidates=[bad]), EVIDENCE, FACTS, True, 3, MONEY, CTX3)
    assert any("identity quote" in r for r in removed) and not pruned.supplier_candidates and not remaining


def test_prompt_states_the_new_rules():
    from src.agent import prompts
    s = prompts.DRAFT_SYSTEM
    for needle in ("hypothetical", "price_direction", "price_scope", "other-market", "not a price forecast", "combined", "exception", "monthly"):
        assert needle in s, needle
    assert prompts.PROMPT_VERSION >= "2026-11-11"


def test_repair_cycle_with_the_live003_defect_keeps_figures_unchanged():
    """Graph level: a first draft containing the live-003 R3 is rejected and its problems are returned to the model; the repaired draft passes."""
    bad_first = lambda d: d.model_copy(update={"executive_summary": d.executive_summary.replace("a hypothetical +10% benchmark shock scenario would add", "a +10% benchmark shock is expected to add")})  # noqa: E731
    llm = ScriptedLLM(drafts=[bad_first])
    resp, _ = run_assessment(REQUEST, deps(llm=llm))
    assert resp.status == "completed" and llm.draft_calls == 2
    assert any("forecast" in p for p in llm.draft_args[1][2])
    f = resp.report.financial
    assert f.baseline_modelled_cost_eur == "1,961,496.55" and f.months[0].incremental_exposure_eur["+10%"] == "28,565.04"
    assert next(s for s in f.scenarios if s.shock == "+10%").incremental_exposure_eur == "107,744.17"
