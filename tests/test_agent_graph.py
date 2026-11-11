"""End-to-end LangGraph workflow tests with mocked research and a scripted LLM (no network, no paid calls)."""
import json
from datetime import date
from decimal import Decimal as D

import pytest

from src.agent.config import PROFILES, Limits
from src.agent.data import StaticData
from src.agent.graph import build_graph, run_assessment
from src.agent.llm import LLMError
from src.agent.mock import MockSearch
from src.calculations.exposure import calculate_exposure, round_money
from tests.agent_fixtures import REQUEST, TODAY, EmptySearch, FakeClock, ScriptedLLM, deps, internal
from tests.calc_fixtures import KEY, MONTHS, benchmark_history, contract, contracts, ct002, ct003, forecasts


def run(d, request=None):
    return run_assessment(request or REQUEST, d)


def bad_summary(text):
    return lambda good: good.model_copy(update={"executive_summary": text})


# ── T01: complete assessment ──
def test_t01_complete_assessment_has_sourced_evidence_exact_calculations_and_clear_sections():
    resp, trace = run(deps())
    assert resp.status == "completed" and resp.error is None and resp.report is not None
    r, md = resp.report, resp.report_markdown
    assert r.financial.status == "available" and r.benchmark.status == "available" and not r.removed_claims
    calc = calculate_exposure("sunflower_oil", MONTHS, contracts(), forecasts(), benchmark_history(), KEY, TODAY,
                              PROFILES["sunflower_oil"].assumptions)
    s10 = next(s for s in r.financial.scenarios if s.shock == "+10%")
    assert s10.incremental_exposure_eur == f"{round_money(calc.totals.scenarios[D('0.10')].incremental):,.2f}"
    assert r.financial.baseline_modelled_cost_eur == f"{round_money(calc.totals.baseline_cost):,.2f}"
    assert r.financial.months[0].incremental_exposure_eur["+10%"] == "28,565.04"
    for needle in ("Baseline modelled procurement cost", "Incremental price-shock exposure", "Contract coverage", "Assumptions:",
                   "CRUDE standard sunflower oil", "2026-10-19 to 2026-10-25", "not supplier quotes", "Evidence register", "retrieved:",
                   "published:", "UNVERIFIED", "Severity indicators", "basis `internal_model`", "basis `combined`", "CONDITIONAL",
                   "Alternative-supplier leads", "How to read the findings"):
        assert needle in md, needle
    assert any(w.startswith("MOCK RUN") for w in resp.warnings) and "MOCK RUN" in md
    by_id = {e.evidence_id: e for e in r.evidence}
    for x in r.draft.risks:                                   # citations of external/combined findings are real, usable evidence
        for c in x.citations:
            e = by_id[c.evidence_id]
            assert e.relevance in ("direct", "contextual") and e.published_date and e.credibility != "social"
    assert [n["node"] for n in trace][:4] == ["load_internal", "calculate", "retrieve_policy", "plan_research"] and trace[-1]["node"] == "validate"
    assert all(c.status == "unverified" for c in r.draft.supplier_candidates) and len(r.draft.supplier_candidates) <= 3


def test_every_node_has_a_timing_entry():
    _, trace = run(deps(clock=FakeClock(step=0.5)))
    assert all("elapsed_s" in n for n in trace) and all(n["elapsed_s"] >= 0 for n in trace)


def test_calculation_is_independent_of_the_llm():
    a, _ = run(deps(llm=ScriptedLLM(drafts=[bad_summary("[X] The modelled exposure is moderate and is driven by uncovered demand growth.")])))
    b, _ = run(deps())
    assert a.report.financial == b.report.financial and a.report.benchmark == b.report.benchmark
    assert a.report.indicators == b.report.indicators


def test_envelope_matches_the_n8n_contract():
    resp, _ = run(deps())
    d = resp.model_dump(mode="json")
    assert {"request_id", "status", "report_title", "report_markdown", "warnings", "missing_data", "error"} <= set(d)
    assert d["status"] in ("completed", "partial", "failed") and d["request_id"] == "test-001"


# ── T04 / evidence quality ──
def test_t04_irrelevant_weak_and_unsupported_results_are_excluded_or_labelled():
    resp, _ = run(deps())
    r = resp.report
    titles = " ".join(e.title for e in r.evidence)
    assert "Fuel markets" not in titles and "cosecha" not in titles.lower()               # pre-filtered
    by = {e.title.replace("[MOCK FIXTURE] ", ""): e for e in r.evidence}
    assert by["Regional sunflower festival opens"].relevance == "irrelevant"
    assert by["Palm oil stocks weigh on vegetable oil prices"].relevance == "contextual"
    assert by["Sunflower oil price report and forecast"].relevance == "contextual" and by["Sunflower oil price report and forecast"].credibility == "commercial"
    assert by["Forum thread: sunflower oil shortages"].credibility == "social" and by["Forum thread: sunflower oil shortages"].relevance == "irrelevant"
    cited = {c.evidence_id for x in r.draft.risks for c in x.citations}
    assert cited and cited.isdisjoint({e.evidence_id for e in r.evidence if e.relevance != "direct"})
    assert "NO_INGREDIENT_MATCH" in r.source_status["filtering"] and "UNSUPPORTED_LANGUAGE" in r.source_status["filtering"]


def test_persisted_report_and_trace_contain_no_third_party_contact_details():
    resp, trace = run(deps())
    blob = json.dumps(resp.model_dump(mode="json"), default=str) + json.dumps(trace, default=str) + resp.report_markdown
    assert "@" not in blob.replace("example.invalid", "") or "[email removed]" in blob
    assert "+34 600" not in blob and "+34 611" not in blob and "sales@" not in blob and "buyer@" not in blob
    assert "[phone removed]" in blob


# ── T05: research failures and diagnostics ──
def test_t05_one_research_source_down_gives_a_partial_report_with_a_visible_warning():
    tav = MockSearch("tavily", TODAY, fail_with="TIMEOUT")
    resp, _ = run(deps(tavily=tav))
    assert resp.status == "partial" and resp.report is not None
    assert resp.report.source_status["tavily"].startswith("unavailable: TIMEOUT")
    assert any(w.startswith("RESEARCH_SOURCE_TAVILY") for w in resp.warnings)
    assert tav.calls <= 3


def test_both_research_sources_down_keeps_internal_findings_but_asserts_no_external_risk():
    resp, _ = run(deps(tavily=MockSearch("tavily", TODAY, fail_with="HTTP_503"), gdelt=MockSearch("gdelt", TODAY, fail_with="TIMEOUT")))
    r = resp.report
    assert resp.status == "partial" and r.financial.status == "available"
    assert r.draft.risks and all(x.basis == "internal_model" for x in r.draft.risks)
    assert not r.draft.supplier_candidates and any(w.startswith("NO_RELEVANT_EVIDENCE") for w in resp.warnings)
    assert "No external market or supply risk is asserted" in resp.report_markdown
    assert "No candidate is reported" in resp.report_markdown


def test_http_success_with_zero_articles_is_reported_accurately_not_as_usable_evidence():
    resp, trace = run(deps(gdelt=MockSearch("gdelt", TODAY, empty=True)))
    s = resp.report.source_status["gdelt"]
    assert "HTTP 200" in s and "NO RESULTS" in s and "empty 'articles' list" in s
    assert any(w.startswith("RESEARCH_SOURCE_GDELT") and "no usable results" in w for w in resp.warnings)
    assert resp.status == "completed"                        # Tavily still supplied strong evidence
    rec = next(r for r in next(n for n in trace if n["node"] == "search")["records"] if r["tool"] == "gdelt")
    assert rec["results"] == 0 and rec["note"] and rec["attempt_log"][0]["http_status"] == 200


def test_source_status_separates_http_success_from_usable_evidence():
    resp, _ = run(deps())
    s = resp.report.source_status["tavily"]
    assert "HTTP 200" in s and "results returned" in s and "passed pre-filter" in s and "judged usable" in s and "direct" in s


def test_failed_calls_record_every_attempt_without_secrets():
    _, trace = run(deps(tavily=MockSearch("tavily", TODAY, fail_with="NETWORK_ERROR")))
    recs = [r for r in next(n for n in trace if n["node"] == "search")["records"] if r["tool"] == "tavily"]
    assert recs and all([a["attempt"] for a in r["attempt_log"]] == [1, 2, 3] and r["error_code"] == "NETWORK_ERROR" for r in recs)


def test_research_tool_not_configured_is_visible():
    resp, _ = run(deps(tavily=None))
    assert resp.status == "partial" and resp.report.source_status["tavily"] == "not_configured"


# ── T06: missing or unusable internal data ──
def test_t06_missing_contract_price_gives_unavailable_financials_without_any_invented_figure():
    bad = contract("CT-001", "fixed", "100", unit_price=None)
    resp, _ = run(deps(data=StaticData(internal(contracts=[bad, ct002(), ct003()]))))
    r = resp.report
    assert resp.status == "partial" and r.financial.status == "unavailable" and r.financial.scenarios == []
    assert r.financial.baseline_modelled_cost_eur is None
    assert any("CT-001" in m for m in resp.missing_data) and any(w.startswith("FINANCIAL_UNAVAILABLE") for w in resp.warnings)
    assert "UNAVAILABLE — no financial estimate" in resp.report_markdown and "Incremental price-shock exposure" not in resp.report_markdown
    assert not any(i.category == "contract_coverage" for i in r.indicators)
    assert any(i.category == "supplier_status" for i in r.indicators)     # approval status still matters without the model


def test_stale_benchmark_is_reported_explicitly_and_never_replaced_by_an_older_value():
    resp, _ = run(deps(today=date(2026, 11, 16)))
    r = resp.report
    assert resp.status == "partial" and r.benchmark.status == "stale" and r.benchmark.price_eur_per_tonne is None
    assert r.benchmark.age_days == 22 and r.financial.status == "unavailable"
    assert any("STALE_BENCHMARK" in w for w in resp.warnings) and "STALE" in resp.report_markdown


def test_missing_forecast_month_makes_financials_unavailable():
    resp, _ = run(deps(data=StaticData(internal(forecasts=forecasts(jan=None)))))
    assert resp.status == "partial" and any("demand_forecast missing" in m for m in resp.missing_data)


def test_over_commitment_is_visible_in_warnings_and_report_without_blocking():
    resp, _ = run(deps(data=StaticData(internal(forecasts=forecasts("250", "250", "250")))))
    assert resp.status == "completed" and any("OVER_COMMITMENT" in w for w in resp.warnings)
    flags = {m.month: m.over_commitment for m in resp.report.financial.months}
    assert flags == {"2026-12": True, "2027-01": True, "2027-02": False}


def test_ingredient_assumptions_come_from_the_profile_only():
    assert PROFILES["sunflower_oil"].assumptions.spot_premium == D("150") and set(PROFILES) == {"sunflower_oil"}
    resp, _ = run(deps(), {**REQUEST, "ingredient": "wheat"})
    assert resp.status == "failed" and resp.error.code == "INVALID_REQUEST"


# ── T08: malformed or non-compliant LLM output ──
def test_t08_malformed_draft_is_repaired_once():
    llm = ScriptedLLM(drafts=[{"executive_summary": 7, "risks": "nope"}])
    resp, trace = run(deps(llm=llm))
    assert resp.status == "completed" and llm.draft_calls == 2
    assert llm.draft_args[1][2] and llm.draft_args[1][2][0].startswith("schema:")
    assert [n["node"] for n in trace].count("validate") == 2


def test_t08_second_schema_failure_returns_a_visible_failed_state_without_a_report():
    llm = ScriptedLLM(drafts=[{"nope": 1}, {"nope": 2}])
    resp, _ = run(deps(llm=llm))
    assert resp.status == "failed" and resp.report is None and resp.report_markdown is None
    assert resp.error.code == "REPORT_VALIDATION_FAILED" and llm.draft_calls == 2


def test_invented_amounts_are_rejected_then_repaired():
    llm = ScriptedLLM(drafts=[bad_summary("Exposure will reach EUR 999,999 over the horizon because of uncovered demand.")])
    resp, _ = run(deps(llm=llm))
    assert resp.status == "completed" and llm.draft_calls == 2
    assert any("999,999" in p for p in llm.draft_args[1][2]) and "999,999" not in resp.report_markdown


def test_tonnage_written_as_euros_is_rejected_then_repaired_in_the_workflow():
    bad = bad_summary("Contract coverage leaves EUR 451.290 t uncovered across the horizon, which keeps exposure to the benchmark.")
    llm = ScriptedLLM(drafts=[bad])
    resp, _ = run(deps(llm=llm))
    assert resp.status == "completed" and llm.draft_calls == 2
    assert any("451.290" in p and "currency amount" in p for p in llm.draft_args[1][2]) and "EUR 451.290" not in resp.report_markdown


def decorate(good):
    """An external-looking claim whose citation does not support it (the live-002 failure mode)."""
    fake = good.risks[2].model_copy(update={"risk_id": "R9", "title": "Fertiliser rail closures", "description": "Rail closures raise fertiliser costs."})
    return good.model_copy(update={"risks": good.risks + [fake]})


def test_unsupported_claim_is_repaired_when_the_model_corrects_it():
    llm = ScriptedLLM(drafts=[decorate])
    resp, _ = run(deps(llm=llm))
    assert resp.status == "completed" and llm.draft_calls == 2 and not resp.report.removed_claims
    assert any(p.startswith("risks[") and "does not relate to the claim" in p for p in llm.draft_args[1][2])


def test_unsupported_claims_that_survive_the_repair_are_removed_visibly_not_kept_with_decorative_citations():
    llm = ScriptedLLM(drafts=[decorate, decorate])
    resp, _ = run(deps(llm=llm))
    r = resp.report
    assert resp.status == "partial" and llm.draft_calls == 2
    assert "R9" not in [x.risk_id for x in r.draft.risks] and len(r.removed_claims) == 1 and "Fertiliser rail closures" in r.removed_claims[0]
    assert any(w.startswith("UNSUPPORTED_CLAIMS_REMOVED") for w in resp.warnings)
    assert "Unverified concerns - drafted claims removed" in resp.report_markdown


def test_candidate_with_unsupported_country_or_status_is_never_published():
    def corrupt(good):
        c = good.supplier_candidates[1].model_copy(update={"country": "Spain", "country_quote": good.supplier_candidates[1].product_quote})
        return good.model_copy(update={"supplier_candidates": [good.supplier_candidates[0], c]})
    resp, _ = run(deps(llm=ScriptedLLM(drafts=[corrupt, corrupt])))
    names = {c.name: c.country for c in resp.report.draft.supplier_candidates}
    assert "Example Danubian Oils" not in names and names == {"Example Aceites Iberia S.A.": "Spain"}
    assert resp.status == "partial" and resp.report.removed_claims


def test_omitted_internal_risks_are_added_by_rule_with_a_warning():
    def drop_internal(good):
        return good.model_copy(update={"risks": [x for x in good.risks if x.basis != "internal_model"],
                                       "mitigation_options": [], "recommended_actions": [], "supplier_candidates": []})
    resp, _ = run(deps(llm=ScriptedLLM(drafts=[drop_internal])))
    cats = {x.category for x in resp.report.draft.risks}
    assert {"contract_coverage", "supplier_status"} <= cats
    assert sum(w.startswith("RULE_BASED_RISK_ADDED") for w in resp.warnings) == 2


def test_t09_supplier_candidates_are_unverified_capped_and_attributes_are_supported():
    resp, _ = run(deps())
    cands = {c.name: c for c in resp.report.draft.supplier_candidates}
    assert set(cands) == {"Example Aceites Iberia S.A.", "Example Danubian Oils", "Example Girasol Crudo S.L."}
    assert cands["Example Aceites Iberia S.A."].country == "Spain" and cands["Example Aceites Iberia S.A."].product_type == "refined_food_grade"
    assert cands["Example Danubian Oils"].country == "unknown" and cands["Example Danubian Oils"].product_type == "both"
    assert cands["Example Girasol Crudo S.L."].country == "unknown" and cands["Example Girasol Crudo S.L."].product_type == "crude"
    assert "Natural Foods" not in resp.report_markdown.split("Evidence register")[0]       # social-only lead is not a candidate
    assert "To verify independently" in resp.report_markdown and "UNVERIFIED" in resp.report_markdown


# ── failures ──
def test_database_failure_is_a_sanitised_failed_state():
    resp, _ = run(deps(data=StaticData(error=RuntimeError("could not connect to postgresql://user:SECRETPW@db.example:5432/postgres"))))
    assert resp.status == "failed" and resp.report is None and resp.error.code == "INTERNAL_DATA_UNAVAILABLE"
    assert "SECRETPW" not in json.dumps(resp.model_dump(mode="json"), default=str)


def test_llm_unavailable_at_drafting_is_a_failed_state():
    resp, _ = run(deps(llm=ScriptedLLM(drafts=[LLMError("LLM_CALL_FAILED:APITimeoutError")])))
    assert resp.status == "failed" and resp.error.code == "LLM_UNAVAILABLE" and "LLM_CALL_FAILED" in resp.error.message


def test_evidence_screening_failure_leaves_items_unused_and_flags_it():
    resp, _ = run(deps(llm=ScriptedLLM(judge_error=LLMError("TOKEN_BUDGET_EXCEEDED"))))
    assert resp.status == "partial" and any("EVIDENCE_SCREENING_FAILED" in w for w in resp.warnings)
    assert all(x.basis == "internal_model" for x in resp.report.draft.risks) and not resp.report.draft.supplier_candidates


@pytest.mark.parametrize("req", [{**REQUEST, "horizon_months": 0}, {**REQUEST, "horizon_months": 9}, {**REQUEST, "extra": 1},
                                 {**REQUEST, "request_id": "bad id!"}, {"ingredient": "sunflower_oil", "horizon_months": 3}])
def test_invalid_requests_fail_before_any_work(req):
    d = deps()
    resp, trace = run(d, req)
    assert resp.status == "failed" and resp.error.code == "INVALID_REQUEST" and trace == []
    assert d.tavily.calls == 0 and d.gdelt.calls == 0 and d.llm.usage()["calls"] == 0


# ── bounded research ──
def test_research_is_bounded_by_rounds_and_per_tool_call_budgets():
    tav, gd = EmptySearch("tavily"), EmptySearch("gdelt")
    llm = ScriptedLLM()
    resp, trace = run(deps(llm=llm, tavily=tav, gdelt=gd))
    assert resp.execution.research_rounds == 3
    assert tav.calls <= Limits().max_tavily_calls and gd.calls <= Limits().max_gdelt_calls
    assert [n["node"] for n in trace].count("plan_research") == 3
    assert resp.status == "partial" and any(w.startswith("NO_RELEVANT_EVIDENCE") for w in resp.warnings)
    assert llm.refine_n == 2


def test_research_stops_at_the_wall_clock_deadline_and_discloses_it():
    tav, gd = EmptySearch("tavily"), EmptySearch("gdelt")
    resp, _ = run(deps(tavily=tav, gdelt=gd, clock=FakeClock(step=40.0), limits=Limits(deadline_seconds=100.0)))
    assert any(w.startswith("RESEARCH_DEADLINE") for w in resp.warnings)
    assert tav.calls + gd.calls < Limits().max_tavily_calls + Limits().max_gdelt_calls


def test_no_further_rounds_once_enough_usable_evidence_exists():
    resp, _ = run(deps())
    assert resp.execution.research_rounds == 1 and resp.execution.tool_calls == {"tavily": 3, "gdelt": 1}


def test_a_failed_source_is_not_called_again_in_later_rounds():
    tav = MockSearch("tavily", TODAY, fail_with="HTTP_503")
    run(deps(tavily=tav, gdelt=EmptySearch("gdelt")))
    assert tav.calls == 3


# ── structure / safety ──
def test_graph_has_no_purchasing_approval_contract_or_supplier_monitoring_nodes():
    nodes = set(build_graph(deps()).get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"load_internal", "calculate", "retrieve_policy", "plan_research", "search", "filter_evidence", "draft", "validate", "finalize"}


def test_evidence_is_passed_to_the_llm_as_data_with_only_the_needed_fields():
    llm = ScriptedLLM()
    run(deps(llm=llm))
    assert llm.judged and all(set(i) == {"evidence_id", "kind", "title", "snippet", "published_date", "domain", "source_tier"} for i in llm.judged[0])
    facts_text, evidence, _ = llm.draft_args[0]
    assert "28,565.04" in facts_text and "internal_refs" in facts_text and "severity_indicators" in facts_text
    assert evidence and all(e["relevance"] in ("direct", "contextual") and e["source_tier"] != "social" or e["relevance"] == "contextual" for e in evidence)
    assert not any(e["relevance"] == "irrelevant" for e in evidence)


def test_same_inputs_give_identical_financial_sections_across_runs():
    a, _ = run(deps())
    b, _ = run(deps())
    assert a.report.financial == b.report.financial and a.report.supplier_exposure == b.report.supplier_exposure


def test_supplier_exposure_comes_from_sql_data_and_shows_approval_status():
    resp, _ = run(deps())
    rows = {s.contract_id: s for s in resp.report.supplier_exposure}
    assert set(rows) == {"CT-001", "CT-002", "CT-003"}
    assert rows["CT-002"].ends_within_horizon and not rows["CT-001"].ends_within_horizon
    assert rows["CT-003"].approval_status == "conditional" and rows["CT-003"].supplier_name.endswith("(fictional)")
