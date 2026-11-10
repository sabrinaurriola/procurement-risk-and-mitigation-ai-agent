from datetime import date
from decimal import Decimal as D
from fractions import Fraction as Fr

import pytest

from src.calculations.exposure import Assumptions, calculate_exposure, round_money
from tests.calc_fixtures import (AS_OF, DEC, FEB, JAN, KEY, MONTHS, benchmark_history, contract, contracts, ct001, ct002,
                                 ct003, forecasts, frac, obs, to_dec)

P10, M10, P20, M20, ZERO = D("0.10"), D("-0.10"), D("0.20"), D("-0.20"), D("0")
B0 = Fr("1457.40")


def run(cs=None, fs=None, history=None, months=MONTHS, as_of=AS_OF, assumptions=None):
    return calculate_exposure("sunflower_oil", months, cs if cs is not None else contracts(),
                              fs if fs is not None else forecasts(), history if history is not None else benchmark_history(),
                              KEY, as_of, assumptions or Assumptions())


def month(res, m):
    return next(x for x in res.months if x.month == m)


def line(mres, kind, cid=None):
    return next(l for l in mres.lines if l.kind == kind and (cid is None or l.contract_id == cid))


def close(a: D, b: Fr, tol="0.0001"):
    assert abs(a - to_dec(b)) < D(tol), (a, float(b))


# ── 1. approved December worked example ──
def test_december_worked_example_matches_approved_figures():
    res = run(months=[DEC], fs=forecasts())
    dec = month(res, DEC)
    assert res.status == "available" and dec.status == "available"
    assert round_money(dec.baseline_cost) == D("636650.40")
    assert round_money(dec.scenarios[P10].incremental) == D("28565.04")
    assert round_money(dec.scenarios[P10].incremental / dec.baseline_cost * 100) == D("4.49")
    assert round_money(line(dec, "fixed", "CT-001").baseline_cost) == D("152000.00")
    assert round_money(line(dec, "fixed", "CT-002").baseline_cost) == D("126400.00")
    assert round_money(line(dec, "index_linked").baseline_cost) == D("197510.40")
    assert round_money(line(dec, "uncovered").baseline_cost) == D("160740.00")
    assert round_money(line(dec, "index_linked").incremental[P10]) == D("13991.04")
    assert round_money(line(dec, "uncovered").incremental[P10]) == D("14574.00")
    assert round_money(line(dec, "index_linked").baseline_price) == D("1645.92")
    assert round_money(line(dec, "uncovered").baseline_price) == D("1607.40")
    assert round_money(sum(l.mark_to_market for l in dec.lines if l.kind == "fixed")) == D("10932.00")


# ── 2. fixed contracts are insulated ──
@pytest.mark.parametrize("shock", [M20, M10, ZERO, P10, P20])
def test_fixed_contracts_are_insulated_from_benchmark_shocks(shock):
    for m in MONTHS:
        for l in (x for x in month(run(), m).lines if x.kind == "fixed"):
            assert l.scenario_price[shock] == l.baseline_price
            assert l.scenario_cost[shock] == l.baseline_cost and l.incremental[shock] == 0


# ── 3. index-linked pass-through ──
@pytest.mark.parametrize("pt", ["0", "0.5", "0.8", "1"])
def test_index_linked_applies_the_contract_pass_through(pt):
    res = run(cs=[ct003(pass_through=D(pt))], fs=forecasts("120", "120", "120"))
    l = line(month(res, DEC), "index_linked")
    dB = B0 * Fr("0.10")
    close(l.incremental[P10], 120 * Fr(pt) * dB)
    close(l.scenario_price[P10] - l.baseline_price, Fr(pt) * dB)
    assert l.scenario_price[ZERO] == l.baseline_price


def test_index_linked_price_formula_uses_base_index_and_premium():
    res = run(cs=[ct003(fixed_premium=D("12.5"))], fs=forecasts("120", "120", "120"))
    l = line(month(res, DEC), "index_linked")
    close(l.baseline_price, Fr("1600") + Fr("0.8") * (B0 - Fr("1400")) + Fr("12.5"))


# ── 4. uncovered demand ──
def test_uncovered_cost_is_benchmark_plus_labelled_spot_premium_and_premium_cancels_in_exposure():
    res = run(months=[DEC])
    u = line(month(res, DEC), "uncovered")
    assert u.volume_t == D("100") and u.baseline_price == D("1457.40") + D("150")
    assert res.assumptions.spot_premium == D("150") and "assumption" in res.assumptions.description.lower()
    res0 = run(months=[DEC], assumptions=Assumptions(spot_premium=D("0")))
    u0 = line(month(res0, DEC), "uncovered")
    assert u0.baseline_price == D("1457.40") and u0.incremental[P10] == u.incremental[P10]


# ── 5. January partial month and February uncovered exposure ──
def test_january_partial_month_expiry_of_ct002_is_prorated_by_days():
    jan = month(run(), JAN)
    ct2 = line(jan, "fixed", "CT-002")
    close(ct2.volume_t, Fr(80 * 15, 31), "0.000000000001")            # 15 active days of 31, rounded to 1e-12 t
    assert ct2.volume_t == (D("80") * 15 / 31).quantize(D("1e-12")) and ct2.contracted_volume_t == ct2.volume_t
    committed = 100 + Fr(80 * 15, 31) + 120
    close(jan.committed_t, committed)
    close(jan.uncovered_t, 420 - committed)
    dB = B0 * Fr("0.10")
    close(jan.scenarios[P10].incremental, (120 * Fr("0.8") + (420 - committed)) * dB)
    expected_base = 100 * Fr("1520") + Fr(80 * 15, 31) * Fr("1580") + 120 * (Fr("1600") + Fr("0.8") * (B0 - 1400)) \
        + (420 - committed) * (B0 + 150)
    close(jan.baseline_cost, expected_base, "0.001")


def test_february_has_no_ct002_and_larger_uncovered_exposure():
    feb = month(run(), FEB)
    assert not any(l.contract_id == "CT-002" for l in feb.lines)
    assert feb.committed_t == D("220") and feb.uncovered_t == D("190")
    assert round_money(feb.baseline_cost) == D("654916.40")
    assert round_money(feb.scenarios[P10].incremental) == D("41681.64")


def test_coverage_change_is_reported_separately_from_price_shock():
    res = run()
    assert month(res, DEC).coverage_change_vs_prior_t is None
    jan, feb = month(res, JAN), month(res, FEB)
    assert jan.coverage_change_vs_prior_t == jan.uncovered_t - month(res, DEC).uncovered_t
    assert feb.coverage_change_vs_prior_t == feb.uncovered_t - jan.uncovered_t
    # coverage (volumes) and baseline cost do not depend on the shock; only exposure does
    assert all(month(res, m).scenarios[ZERO].incremental == 0 for m in MONTHS)


def test_three_month_aggregation():
    res = run()
    assert res.status == "available"
    assert res.totals.baseline_cost == sum(m.baseline_cost for m in res.months)
    for s in (M20, M10, ZERO, P10, P20):
        assert res.totals.scenarios[s].incremental == sum(m.scenarios[s].incremental for m in res.months)
        assert res.totals.scenarios[s].cost == res.totals.baseline_cost + res.totals.scenarios[s].incremental


# ── zero shock and symmetry ──
def test_zero_shock_produces_zero_exposure_everywhere():
    res = run()
    for m in res.months:
        assert m.scenarios[ZERO].incremental == 0 and m.scenarios[ZERO].cost == m.baseline_cost
        assert all(l.incremental[ZERO] == 0 and l.scenario_price[ZERO] == l.baseline_price for l in m.lines)
    assert res.totals.scenarios[ZERO].incremental == 0


def test_shock_symmetry_and_scaling():
    res = run()
    t = res.totals.scenarios
    assert t[P10].incremental == -t[M10].incremental and t[P20].incremental == -t[M20].incremental
    assert abs(t[P20].incremental - 2 * t[P10].incremental) <= D("0.00001")   # per-line rounding at 1e-6 EUR


# ── 6. no double counting / no negative uncovered ──
@pytest.mark.parametrize("dec", ["1", "50", "99.999", "100", "250", "299.5", "300", "300.001", "400", "1000", "123456.789"])
def test_allocated_plus_uncovered_equals_forecast_and_uncovered_never_negative(dec):
    m = month(run(fs=forecasts(dec, dec, dec)), DEC)
    assert m.uncovered_t >= 0
    assert m.allocated_t + m.uncovered_t == D(dec)
    assert sum(l.volume_t for l in m.lines) == D(dec)


@pytest.mark.parametrize("mm", MONTHS)
def test_invariant_holds_in_every_month_of_the_baseline(mm):
    m = month(run(), mm)
    assert sum(l.volume_t for l in m.lines) == m.forecast_t and m.uncovered_t >= 0


# ── 7. over-commitment ──
def test_over_commitment_warns_and_keeps_contractual_obligations_visible():
    res = run(fs=forecasts("250", "250", "250"), months=[DEC])
    m = month(res, DEC)
    assert m.over_commitment and m.uncovered_t == 0
    assert m.committed_t == D("300") and m.surplus_committed_t == D("50") and m.allocated_t == D("250")
    assert any("OVER_COMMITMENT" in w for w in m.warnings) and any("OVER_COMMITMENT" in w for w in res.warnings)
    assert all(l.contracted_volume_t > l.volume_t for l in m.lines)          # obligations are not reduced
    assert not any(l.kind == "uncovered" and l.volume_t > 0 for l in m.lines)
    assert m.surplus_commitment_baseline_cost > 0                            # informational, shown not hidden
    assert sum(l.volume_t for l in m.lines) == D("250")


# ── 8. missing inputs ──
def test_missing_fixed_contract_price_makes_the_month_unavailable_without_a_number():
    bad = contract("CT-001", "fixed", "100", unit_price=None)
    res = run(cs=[bad, ct002(), ct003()])
    assert res.status == "unavailable" and res.totals is None
    m = month(res, DEC)
    assert m.status == "unavailable" and m.baseline_cost is None and not m.scenarios
    assert any("CT-001" in r and "unit_price" in r for r in m.reasons) and any("CT-001" in r for r in res.unavailable_reasons)


@pytest.mark.parametrize("field", ["index_series_key", "base_price", "index_base_value", "pass_through", "fixed_premium"])
def test_missing_index_fields_make_the_month_unavailable(field):
    res = run(cs=[ct001(), ct002(), ct003(**{field: None})])
    assert res.status == "unavailable" and any(field in r for r in month(res, DEC).reasons)


def test_missing_forecast_for_a_month_is_unavailable_but_other_months_are_still_calculated():
    res = run(fs=forecasts(jan=None))
    assert month(res, JAN).status == "unavailable" and any("demand_forecast" in r for r in month(res, JAN).reasons)
    assert month(res, DEC).status == "available" and month(res, FEB).status == "available"
    assert res.status == "unavailable" and res.totals is None


def test_no_contracts_means_all_demand_is_uncovered_and_flagged():
    res = run(cs=[], months=[DEC])
    m = month(res, DEC)
    assert m.uncovered_t == D("400") and any("NO_ACTIVE_CONTRACTS" in w for w in m.warnings)


def test_missing_benchmark_is_unavailable():
    res = run(history=[])
    assert res.status == "unavailable" and res.totals is None and not res.benchmark.available
    assert all(m.status == "unavailable" for m in res.months)


def test_contract_for_another_ingredient_is_ignored():
    wheat = contract("CT-004", "fixed", "600", ingredient="wheat", unit_price=D("265"))
    assert round_money(month(run(cs=contracts() + [wheat], months=[DEC]), DEC).baseline_cost) == D("636650.40")


def test_invalid_contract_not_active_in_the_month_does_not_block_other_months():
    late = contract("CT-009", "fixed", "10", date(2027, 2, 1), date(2027, 6, 30), unit_price=None)
    res = run(cs=contracts() + [late])
    assert month(res, DEC).status == "available" and month(res, FEB).status == "unavailable"


# ── 9. stale benchmark ──
def test_stale_benchmark_returns_unavailable_not_a_stale_number():
    res = run(as_of=date(2026, 11, 16))
    assert res.status == "unavailable" and res.totals is None
    assert any("STALE_BENCHMARK" in r for r in res.unavailable_reasons)
    assert all(m.baseline_cost is None for m in res.months)


# ── 10. outliers and incompatible series ──
def test_flagged_outlier_never_drives_the_calculation():
    history = [obs(date(2026, 10, 26), "166.00", flags=["OUTLIER_VS_MEDIAN"])] + benchmark_history()
    res = run(history=history, months=[DEC])
    assert res.benchmark.price == D("1457.40") and round_money(month(res, DEC).baseline_cost) == D("636650.40")


def test_index_contract_linked_to_another_series_is_unavailable():
    other = "EC_OILSEEDS|Crude sunflower oil|Standard|ES|Average|ex-factory||EUR|tonne"
    res = run(cs=[ct001(), ct002(), ct003(index_series_key=other)])
    assert res.status == "unavailable" and any("index series" in r for r in month(res, DEC).reasons)


def test_contract_currency_must_match_benchmark_currency():
    res = run(cs=[ct001(), ct002(), ct003(currency="USD")])
    assert res.status == "unavailable" and any("currency" in r for r in month(res, DEC).reasons)


def test_incompatible_benchmark_series_is_not_used():
    history = [obs(date(2026, 10, 26), "9999", currency="USD")] + benchmark_history()
    assert run(history=history, months=[DEC]).benchmark.price == D("1457.40")


# ── assumptions, presentation, no hidden state ──
def test_assumptions_are_configurable_and_labelled_as_modelled_not_quotes():
    res = run(assumptions=Assumptions(spot_premium=D("200"), shocks=(D("0.05"),)))
    assert set(res.months[0].scenarios) == {D("0.05")}
    assert res.disclaimer and "not supplier quotes" in res.disclaimer.lower()
    assert res.benchmark.period_start == date(2026, 10, 19) and res.benchmark.period_end == date(2026, 10, 25)


def test_calculation_is_deterministic_and_does_not_mutate_inputs():
    cs, fs, hist = contracts(), forecasts(), benchmark_history()
    a, b = run(cs, fs, hist), run(cs, fs, hist)
    assert a == b and cs == contracts() and fs == forecasts() and hist == benchmark_history()


def test_results_use_decimal_not_float():
    res = run()
    for m in res.months:
        assert isinstance(m.baseline_cost, D) and isinstance(m.uncovered_t, D)
        assert all(isinstance(l.baseline_cost, D) for l in m.lines)
