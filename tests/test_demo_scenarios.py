"""Scenario A (normal) and B (controlled limitation) as executable acceptance tests (offline: static data, mock research)."""
import pytest

from src.agent.data import StaticData
from src.agent.demo import render_summary, run_scenario
from tests.agent_fixtures import internal


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    out = tmp_path_factory.mktemp("demo")
    data = StaticData(internal())
    return {"A": run_scenario("A", out=out, data=data), "B": run_scenario("B", out=out, data=data),
            "A-fault": run_scenario("A", inject=True, out=out, data=data), "B-fault": run_scenario("B", inject=True, out=out, data=data), "out": out}


@pytest.mark.parametrize("key", ["A", "B", "A-fault", "B-fault"])
def test_every_expectation_and_every_audit_check_passes(results, key):
    r = results[key]
    assert [n for n, ok in r["expectations"] if not ok] == []
    assert len(r["audit"]) == 10 and [c.id for c in r["audit"] if not c.passed] == []


def test_scenario_a_is_a_complete_assessment(results):
    resp = results["A"]["response"]
    assert resp.status == "completed" and resp.report.financial.baseline_modelled_cost_eur == "1,961,496.55"
    assert resp.report.policy.status == "ok" and "MOCK RUN" in resp.report_markdown


def test_scenario_b_withholds_figures_explains_why_and_keeps_what_it_can_support(results):
    resp = results["B"]["response"]
    r, md = resp.report, resp.report_markdown
    assert resp.status == "partial" and r.benchmark.status == "stale" and r.benchmark.age_days == 26 and r.benchmark.price_eur_per_tonne is None
    assert r.financial.status == "unavailable" and r.financial.scenarios == [] and r.financial.baseline_modelled_cost_eur is None
    assert "UNAVAILABLE — no financial estimate is given" in md and "STALE" in md and "1,961,496" not in md and "107,744" not in md
    assert {x.category for x in r.draft.risks} == {"supplier_status", "supply_disruption"}            # no coverage figure without the model
    assert r.policy.status == "ok" and r.draft.supplier_candidates and all(c.status == "unverified" for c in r.draft.supplier_candidates)


def test_the_two_scenarios_are_complementary_not_two_shocks_of_the_same_run(results):
    a, b = results["A"]["response"], results["B"]["response"]
    assert (a.status, b.status) == ("completed", "partial") and a.report.financial.status != b.report.financial.status
    assert a.report.benchmark.status == "available" and b.report.benchmark.status == "stale"


def test_unsupported_claim_is_removed_visibly_in_both_scenarios(results):
    for k in ("A-fault", "B-fault"):
        resp = results[k]["response"]
        assert resp.status == "partial" and len(resp.report.removed_claims) == 1 and "Fertiliser" in resp.report.removed_claims[0]
        assert "Unverified concerns - drafted claims removed" in resp.report_markdown
        assert "R9" not in [x.risk_id for x in resp.report.draft.risks]


def test_outputs_are_written_and_summary_renders(results):
    out = results["out"]
    assert (out / "demo-A.md").exists() and (out / "demo-B-audit.md").exists()
    text = render_summary([results["A"], results["B"]])
    assert "Scenario A" in text and "Scenario B" in text and "**FAIL**" not in text
