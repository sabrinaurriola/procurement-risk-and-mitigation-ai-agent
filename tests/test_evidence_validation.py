"""Pre-filter, credibility tiers, contact redaction, and every validation guardrail (offline)."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.agent.config import PROFILES
from src.agent.credibility import country_supported, credibility, redact_contacts
from src.agent.evidence import normalise_url, prefilter, to_evidence
from src.agent.validation import ValidationContext, inject_required_risks, prune_unsupported, validate_draft
from src.tools.search import RawResult
from tests.validation_fixtures import (CTX, EVIDENCE, FACTS, INDICATORS, MONEY, Q_CRUDE_REFINED, Q_REFINED, Q_SPAIN, Q_SUPPLY, REFS,
                                       action, candidate, cite, cov_risk, ev, good_draft, mitigation, risk, sup_risk)

TODAY = date(2026, 11, 10)
PROFILE = PROFILES["sunflower_oil"]


def raw(url="https://a.example/1", title="Sunflower oil supply", snippet="", published=TODAY - timedelta(days=5), language=None, kind="risk"):
    return RawResult("tavily", url, title, snippet, published, language, "q", kind)


def pf(items, seen=frozenset()):
    return prefilter(items, PROFILE, set(seen), TODAY, 90)


# ───────────── pre-filter ─────────────
def test_relevant_dated_item_is_accepted():
    acc, rej = pf([raw()])
    assert len(acc) == 1 and not rej


@pytest.mark.parametrize("item,reason", [
    (raw(title="Fuel prices", snippet="fossil fuel news"), "NO_INGREDIENT_MATCH"),
    (raw(published=None), "UNDATED"),
    (raw(published=TODAY + timedelta(days=2)), "FUTURE_DATE"),
    (raw(published=TODAY - timedelta(days=200)), "OUTSIDE_DATE_WINDOW"),
    (raw(language="french"), "UNSUPPORTED_LANGUAGE"),
])
def test_prefilter_rejects_with_a_recorded_reason(item, reason):
    acc, rej = pf([item])
    assert not acc and [r.reason for r in rej] == [reason]


def test_duplicates_are_removed_within_and_across_batches():
    acc, rej = pf([raw("https://a.example/1/"), raw("HTTPS://A.EXAMPLE/1#frag"), raw("https://a.example/2")],
                  seen={normalise_url("https://a.example/2")})
    assert len(acc) == 1 and [r.reason for r in rej] == ["DUPLICATE_URL", "DUPLICATE_URL"]


def test_supplier_discovery_may_be_undated_but_still_needs_the_ingredient():
    acc, rej = pf([raw(url="https://s.example/1", published=None, kind="supplier_discovery"),
                   raw(url="https://s.example/2", title="Bakery news", published=None, kind="supplier_discovery")])
    assert len(acc) == 1 and [r.reason for r in rej] == ["NO_INGREDIENT_MATCH"]


def test_evidence_ids_continue_and_publication_and_retrieval_dates_are_distinct():
    acc, _ = pf([raw(title="Precio del aceite de girasol", language="spanish")])
    items = to_evidence(acc, 4, TODAY, is_mock=False)
    assert items[0].evidence_id == "E4" and items[0].retrieved_on == TODAY and items[0].relevance == "unjudged"
    assert items[0].published_date != items[0].retrieved_on and items[0].domain == "a.example"


# ───────────── credibility tiers and redaction ─────────────
@pytest.mark.parametrize("url,tier", [
    ("https://www.reuters.com/x", "trade_press"), ("https://ec.europa.eu/agriculture/x", "official"),
    ("https://www.imarcgroup.com/report", "commercial"), ("https://www.europages.co.uk/companies/spain/sunflower.html", "commercial"),
    ("https://www.facebook.com/groups/1/posts/2", "social"), ("https://m.facebook.com/x", "social"), ("https://elitagro.es/en", "unrated"),
    ("https://social.example.invalid/1", "social"), ("https://tradepress.example.invalid/1", "trade_press"),
])
def test_source_tiers(url, tier):
    assert credibility(url) == tier


def test_contact_details_are_removed_but_prices_dates_and_tonnages_survive():
    text = ("Natural Foods is a supplier. WhatsApp: +34 611 23 33 82 Email: comercial@naturefoods.pro. Call 0034 600 111 222. "
            "Prices reached 1,351 USD/MT on 2026-03-14 for 400 t; ref 2026-10-07.")
    out = redact_contacts(text)
    assert "@" not in out and "611" not in out and "0034" not in out and "[email removed]" in out and "[phone removed]" in out
    assert "1,351 USD/MT" in out and "2026-03-14" in out and "400 t" in out and "2026-10-07" in out


def test_persisted_evidence_never_contains_third_party_contact_details():
    acc, _ = pf([raw(title="Sunflower oil buyer buyer@example.invalid", snippet="tel +34 600 000 000 or sales@example.invalid", url="https://s.example/9")])
    e = to_evidence(acc, 1, TODAY, False)[0]
    assert "@" not in e.title + e.snippet and "600 000" not in e.snippet


@pytest.mark.parametrize("country,quote,ok", [
    ("Spain", "a refining company based in Spain", True), ("Spain", "supplies buyers across Europe", False),
    ("Spain", "tel +34 611 000 000", False), ("Spain", "company with a .es web domain", False),
    ("Romania", "Romanian producer of sunflower oil", True), ("Atlantis", "based in Atlantis", False)])
def test_country_needs_to_be_named_in_the_quote(country, quote, ok):
    assert country_supported(country, quote) is ok


# ───────────── validation: baseline and basis rules ─────────────
def check(d, evidence=EVIDENCE, financial=True, max_candidates=3, money=MONEY, ctx=CTX):
    return validate_draft(d, evidence, FACTS, financial, max_candidates, money, ctx)


def test_valid_draft_passes():
    assert check(good_draft()) == []


def test_internal_finding_must_not_cite_external_evidence():
    p = check(good_draft(risks=[cov_risk(citations=[cite("E1")]), sup_risk(), risk()]))
    assert any(x.startswith("risks[0]") and "must not cite external evidence" in x for x in p)


def test_internal_finding_needs_internal_references_that_exist():
    assert any("at least one internal reference" in x for x in check(good_draft(risks=[cov_risk(internal_refs=[]), sup_risk(), risk()])))
    assert any("unknown internal reference INT:made:up" in x for x in check(good_draft(risks=[cov_risk(internal_refs=["INT:made:up"]), sup_risk(), risk()])))


def test_external_finding_needs_citations_and_no_internal_refs():
    assert any("must cite evidence" in x for x in check(good_draft(risks=[cov_risk(), sup_risk(), risk(citations=[])])))
    assert any("use basis 'combined'" in x for x in check(good_draft(risks=[cov_risk(), sup_risk(), risk(internal_refs=["INT:indicator:coverage"])])))


def test_combined_finding_must_identify_both_sides():
    ok = risk(risk_id="R4", category="market_price", basis="combined", internal_refs=["INT:indicator:coverage"])
    assert check(good_draft(risks=[cov_risk(), sup_risk(), ok])) == []
    no_int = risk(risk_id="R4", category="market_price", basis="combined", internal_refs=[])
    no_ext = risk(risk_id="R4", category="market_price", basis="combined", citations=[], internal_refs=["INT:indicator:coverage"])
    for bad in (no_int, no_ext):
        assert any("BOTH external evidence and internal references" in x for x in check(good_draft(risks=[cov_risk(), sup_risk(), bad])))


def test_category_and_basis_must_be_consistent():
    assert any("must have basis internal_model" in x for x in check(good_draft(risks=[cov_risk(basis="external_evidence", citations=[cite()], internal_refs=[]), sup_risk()])))
    assert any("needs external evidence" in x for x in check(good_draft(risks=[cov_risk(), sup_risk(), risk(basis="internal_model", citations=[], internal_refs=["INT:indicator:coverage"])])))


# ───────────── validation: does the cited source really support the claim? ─────────────
def ext(**kw):
    return good_draft(risks=[cov_risk(), sup_risk(), risk(**kw)])


def test_quote_must_appear_verbatim_in_the_cited_source():
    p = check(ext(citations=[cite("E1", "pirates have closed every Black Sea port to sunflower oil")]))
    assert any("not found verbatim" in x for x in p)


def test_quote_matching_tolerates_case_whitespace_and_quote_marks():
    assert check(ext(citations=[cite("E1", "  SHIPPING  delays are tightening\nsunflower oil supplies to Europe ")])) == []


def test_short_quotes_are_rejected():
    assert any("too short" in x for x in check(ext(citations=[cite("E1", "tightening supplies")])))


def test_quote_must_relate_to_the_claim():
    p = check(ext(title="Port closures cause fertiliser shortages", description="Closures of inland rail terminals raise fertiliser costs.",
                  citations=[cite("E1", Q_SUPPLY)]))
    assert any("does not relate to the claim" in x for x in p)


def test_irrelevant_unjudged_and_unknown_evidence_cannot_support_a_finding():
    assert any("judged irrelevant" in x for x in check(ext(citations=[cite("E5", "Unrelated fuel market commentary")])))
    assert any("unknown evidence_id E99" in x for x in check(ext(citations=[cite("E99", Q_SUPPLY)])))
    unjudged = [ev("E1", relevance="unjudged", snippet="Black Sea shipping delays are tightening sunflower oil supplies to Europe.")]
    assert any("judged unjudged" in x for x in check(ext(), evidence=unjudged + EVIDENCE[1:]))


def test_social_media_can_never_support_a_finding():
    p = check(ext(citations=[cite("E4", "sunflower oil shortages near the Black Sea ports")]))
    assert any("social media" in x for x in p)


def test_contextual_commercial_or_unrated_sources_cannot_carry_a_finding_alone():
    ctx_only = check(ext(citations=[cite("E2", "palm oil stocks are pressuring vegetable oil prices")], title="Palm oil stocks pressure prices",
                         description="Record palm oil stocks pressure vegetable oil prices."))
    comm_only = check(ext(citations=[cite("E3", "forecasts sunflower oil price movements for the coming quarter")],
                          title="Price forecast", description="A vendor forecasts price movements for the coming quarter."))
    for p in (ctx_only, comm_only):
        assert any("no direct citation from an official or trade-press source" in x for x in p)


def test_weak_sources_may_accompany_a_strong_one():
    assert check(ext(citations=[cite("E1", Q_SUPPLY), cite("E3", "forecasts sunflower oil price movements for the coming quarter")],
                     description="Trade press reports shipping delays tightening supply; a vendor forecasts price movements.")) == []


def test_risk_evidence_must_be_dated():
    undated = [ev("E1", published=None, snippet="Black Sea shipping delays are tightening sunflower oil supplies to Europe.")] + EVIDENCE[1:]
    assert any("not a dated risk source" in x for x in check(ext(), evidence=undated))


# ───────────── validation: severity ─────────────
def test_internal_severity_must_follow_the_documented_indicator():
    p = check(good_draft(risks=[cov_risk(severity="high"), sup_risk(), risk()]))
    assert any("severity must be medium per the documented contract_coverage indicator, not high" in x for x in p)


def test_internal_risk_without_a_supporting_indicator_is_rejected():
    ctx = ValidationContext(set(REFS), [i for i in INDICATORS if i.category != "supplier_status"])
    assert any("no deterministic indicator supports a supplier_status risk" in x for x in check(good_draft(), ctx=ctx))


def test_high_external_severity_needs_two_distinct_strong_sources():
    one = check(ext(severity="high"))
    assert any("HIGH severity needs at least two distinct direct citations" in x for x in one)
    e2 = [ev("E6", snippet="A second trade report says sunflower oil shipping delays continue to tighten supplies in Europe.")]
    two = ext(severity="high", citations=[cite("E1", Q_SUPPLY), cite("E6", "sunflower oil shipping delays continue to tighten supplies")],
              description="Two trade reports say shipping delays are tightening supply.")
    assert check(two, evidence=EVIDENCE + e2) == []


def test_every_risk_needs_an_explainable_severity_basis():
    assert any("severity_basis must explain" in x for x in check(ext(severity_basis="ok")))


# ───────────── validation: mitigations and actions ─────────────
def test_mitigations_and_actions_must_address_identified_risks():
    assert any("must address at least one identified risk" in x for x in check(good_draft(mitigation_options=[mitigation(addresses=[])])))
    assert any("addresses unknown risk R9" in x for x in check(good_draft(recommended_actions=[action(addresses=["R9"])])))


def test_actions_always_require_human_approval():
    assert any("requires_human_approval must be true" in x for x in check(good_draft(recommended_actions=[action(requires_human_approval=False)])))


def test_new_supplier_option_must_be_combined_never_external_search_alone():
    m = mitigation(applies_to="new_supplier_candidate", basis="external_evidence", addresses=["R3"], citations=[cite()], internal_refs=[])
    assert any("must be combined" in x for x in check(good_draft(mitigation_options=[m])))
    ok = mitigation(applies_to="new_supplier_candidate", basis="combined", addresses=["R3"], citations=[cite()], internal_refs=["INT:indicator:coverage"],
                    option="Have procurement verify alternative-supplier leads independently", rationale="Reported shipping delays plus uncovered volume.")
    assert check(good_draft(mitigation_options=[ok])) == []


@pytest.mark.parametrize("text", ["Replace the current supplier", "Switch to a new supplier", "Place an order now", "Purchase additional volume", "Cancel the contract"])
def test_replacement_or_purchasing_is_not_recommended_on_external_results_alone(text):
    a = action(action=text, rationale="Shipping delays are tightening supplies.", basis="external_evidence", addresses=["R3"],
               citations=[cite()], internal_refs=[])
    assert any("replacement/purchasing language" in x for x in check(good_draft(recommended_actions=[a])))


# ───────────── validation: alternative-supplier leads ─────────────
def test_valid_candidates_pass_including_unknown_attributes():
    vague = candidate(name="Example Danubian Oils", country="unknown", country_quote="", product_type="both", product_quote=Q_CRUDE_REFINED,
                      identity_citation=cite("S2", Q_CRUDE_REFINED))
    assert check(good_draft(supplier_candidates=[candidate(), vague])) == []
    assert check(good_draft(supplier_candidates=[])) == []                      # zero candidates is acceptable


def test_country_is_not_accepted_from_vague_regions_phone_prefixes_or_missing_quotes():
    vague = candidate(name="Example Danubian Oils", country="Spain", country_quote="supplies crude and refined sunflower oil to commercial buyers across Europe",
                      product_type="both", product_quote=Q_CRUDE_REFINED, identity_citation=cite("S2", Q_CRUDE_REFINED))
    assert any("not named in the quote" in x for x in check(good_draft(supplier_candidates=[vague])))
    no_quote = candidate(country_quote="")
    assert any("needs a verbatim quote" in x for x in check(good_draft(supplier_candidates=[no_quote])))
    leaked = candidate(country="unknown", country_quote=Q_SPAIN)
    assert any("country_quote must be empty" in x for x in check(good_draft(supplier_candidates=[leaked])))


def test_product_type_must_distinguish_crude_from_refined_and_be_quoted():
    wrong = candidate(product_type="crude")
    assert any("does not support product type 'crude'" in x for x in check(good_draft(supplier_candidates=[wrong])))
    both_on_one_word = candidate(product_type="both")
    assert any("does not support product type 'both'" in x for x in check(good_draft(supplier_candidates=[both_on_one_word])))
    unquoted = candidate(product_quote="")
    assert any("needs a verbatim product quote" in x for x in check(good_draft(supplier_candidates=[unquoted])))


def test_candidate_identity_must_come_from_a_direct_non_social_supplier_source():
    social = candidate(name="Natural Foods", country="unknown", country_quote="", product_type="unknown", product_quote="",
                       identity_citation=cite("S3", "Natural Foods is a trusted supplier of sunflower oils."))
    assert any("social-media source cannot establish" in x for x in check(good_draft(supplier_candidates=[social])))
    directory = candidate(name="Sunflower oil - 88 suppliers", country="unknown", country_quote="", product_type="unknown", product_quote="",
                          identity_citation=cite("S4", "Directory page listing many sunflower oil manufacturers"))
    assert any("direct supplier-discovery source" in x for x in check(good_draft(supplier_candidates=[directory])))
    risk_item = candidate(identity_citation=cite("E1", Q_SUPPLY), name="Black Sea")
    assert any("direct supplier-discovery source" in x for x in check(good_draft(supplier_candidates=[risk_item])))


def test_candidate_name_and_identity_quote_must_be_in_the_source():
    assert any("does not appear in the cited source" in x for x in check(good_draft(supplier_candidates=[candidate(name="Invented Oils Ltd")])))
    assert any("identity quote is missing or not found verbatim" in x for x in check(good_draft(supplier_candidates=[candidate(identity_citation=cite("S1", "an invented sentence about this company"))])))


@pytest.mark.parametrize("phrase", ["an approved supplier", "a qualified producer", "certified to ISO standards", "capacity confirmed", "in stock now",
                                    "offers a competitive price", "guaranteed delivery", "preferred supplier"])
def test_candidates_are_never_described_as_approved_certified_or_capable(phrase):
    c = candidate(rationale=f"Found by search and described as {phrase}.")
    assert any("approved/qualified/certified/confirmed" in x for x in check(good_draft(supplier_candidates=[c])))


def test_candidates_are_capped_unverified_and_need_a_verification_step():
    many = [candidate() for _ in range(4)]
    assert any("at most 3" in x for x in check(good_draft(supplier_candidates=many)))
    assert any("verification" in x for x in check(good_draft(supplier_candidates=[candidate(verification_needed=" ")])))
    assert "unverified" in candidate().status


# ───────────── validation: currency amounts ─────────────
def test_tonnage_labelled_as_euros_is_rejected_and_real_amounts_pass():
    bad = good_draft(executive_summary="Contract coverage leaves EUR 451.290 t uncovered across the three months of the horizon.")
    assert any("451.290" in x and "currency amount" in x for x in check(bad))
    assert check(good_draft(executive_summary="Modelled baseline cost is EUR 1,961,496.55 and 451.290 t of demand remains uncovered overall.")) == []


def test_invented_amounts_are_rejected_and_unavailable_financials_forbid_any_amount():
    assert any("999,999" in x for x in check(good_draft(executive_summary="Exposure will reach EUR 999,999 in the next quarter overall.")))
    p = check(good_draft(executive_summary="The exposure would be EUR 107,744.17 under a rise of ten percent."), financial=False, money=set())
    assert any("financial exposure is unavailable" in x for x in p)


def test_amounts_from_usable_cited_evidence_are_allowed_but_not_from_irrelevant_items():
    e = [ev("E1", snippet="Black Sea shipping delays are tightening sunflower oil supplies to Europe; prices reached $1,500 per tonne last week.")] + EVIDENCE[1:]
    assert check(good_draft(executive_summary="Press reports prices of $1,500 per tonne, which is a market claim only, not a quote."), evidence=e) == []
    irrelevant = [ev("E1", relevance="irrelevant", snippet="prices reached $1,500 per tonne")] + EVIDENCE[1:]
    assert any("1,500" in x for x in check(good_draft(executive_summary="Press reports prices of $1,500 per tonne, which is a market claim."), evidence=irrelevant))


def test_short_summary_rejected():
    assert any("too short" in x for x in check(good_draft(executive_summary="ok")))


# ───────────── removal of unsupported claims instead of decorative citations ─────────────
def prune(d, **kw):
    return prune_unsupported(d, kw.get("evidence", EVIDENCE), FACTS, True, 3, MONEY, kw.get("ctx", CTX))


def test_only_unsupported_items_are_removed_and_reported():
    bad_risk = risk(risk_id="R5", title="Port closures raise fertiliser costs", description="Closed rail terminals raise fertiliser costs.",
                    citations=[cite("E1", Q_SUPPLY)])
    dependent = mitigation(option="Hedge against fertiliser cost rises", addresses=["R5"], basis="internal_model", internal_refs=["INT:benchmark"])
    d = good_draft(risks=[cov_risk(), sup_risk(), risk(), bad_risk], mitigation_options=[mitigation(), dependent])
    out, removed, notes, remaining = prune(d)
    assert remaining == [] and [r.risk_id for r in out.risks] == ["R1", "R2", "R3"]
    assert [m.option for m in out.mitigation_options] == [mitigation().option]          # cascade: dependent mitigation also removed
    assert len(removed) == 2 and all("removed:" in x for x in removed) and "does not relate to the claim" in removed[0]
    assert check(out) == []


def test_unfixable_global_problems_are_not_hidden_by_removal():
    out, removed, _, remaining = prune(good_draft(risks=[cov_risk(), sup_risk(), risk(), risk(title="Second risk with the same id")]))
    assert remaining and any("risk_id R3 is used more than once" in x for x in remaining)


def test_a_missing_or_short_discarded_summary_never_blocks_the_removal_of_unsupported_items():
    bad = risk(risk_id="R9", title="Unsupported claim", citations=[cite("E1", "this quote does not appear in the evidence item at all, so it is unsupported")])
    for summary in ("", "ok"):
        out, removed, _, remaining = prune(good_draft(executive_summary=summary, risks=[cov_risk(), sup_risk(), bad]))
        assert remaining == [] and [r.risk_id for r in out.risks] == ["R1", "R2"] and any("Unsupported claim" in x for x in removed)


def test_required_internal_risks_are_added_by_rule_when_the_draft_omits_them():
    d = good_draft(risks=[risk()], mitigation_options=[], recommended_actions=[])
    out, notes = inject_required_risks(d, CTX)
    cats = {r.category: r for r in out.risks}
    assert {"contract_coverage", "supplier_status"} <= set(cats) and len(notes) == 2 and all("RULE_BASED_RISK_ADDED" in n for n in notes)
    assert cats["supplier_status"].severity == "medium" and cats["supplier_status"].basis == "internal_model"
    assert check(good_draft(risks=out.risks, mitigation_options=[], recommended_actions=[])) == []
    again, notes2 = inject_required_risks(out, CTX)
    assert notes2 == [] and len(again.risks) == len(out.risks)                          # idempotent
