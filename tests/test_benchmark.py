from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from src.calculations.benchmark import eligible_observations, select_benchmark
from tests.calc_fixtures import AS_OF, KEY, OTHER_KEY, benchmark_history, obs


def pick(observations, as_of=AS_OF, **kw):
    return select_benchmark(observations, KEY, as_of, **kw)


def test_latest_valid_benchmark_and_period_are_reported():
    b = pick(benchmark_history())
    assert b.available
    assert b.price == D("1457.40") and (b.period_start, b.period_end) == (date(2026, 10, 19), date(2026, 10, 25))
    assert b.age_days == 16 and b.series_key == KEY and (b.currency, b.unit) == ("EUR", "tonne")


@pytest.mark.parametrize("as_of,ok", [(date(2026, 11, 15), True), (date(2026, 11, 16), False)])
def test_staleness_boundary_is_21_days_from_period_end(as_of, ok):
    b = pick(benchmark_history(), as_of)
    assert b.available is ok
    if not ok:
        assert b.price is None and "STALE_BENCHMARK" in b.reasons[0] and b.age_days == 22


def test_max_age_is_configurable():
    assert pick(benchmark_history(), date(2026, 11, 20), max_age_days=30).available


@pytest.mark.parametrize("flag", ["OUTLIER_VS_MEDIAN", "NON_NUMERIC_PRICE", "NON_POSITIVE_PRICE", "DUPLICATE_CONFLICTING_PRICE",
                                  "OVERLAPPING_PERIOD", "UNUSUAL_PERIOD_LENGTH", "FUTURE_DATE", "SOMETHING_NEW"])
def test_flagged_latest_observation_is_excluded_and_previous_valid_week_used(flag):
    history = [obs(date(2026, 10, 26), "166.00", flags=[flag])] + benchmark_history()
    b = pick(history, date(2026, 11, 10))
    assert b.available and b.price == D("1457.40")
    assert b.excluded[flag if flag != "SOMETHING_NEW" else "SOMETHING_NEW"] == 1


def test_benign_flags_do_not_exclude():
    history = [obs(date(2026, 10, 19), "1457.40", flags=["DUPLICATE_PERIOD", "PERIOD_GAP"])]
    assert pick(history).price == D("1457.40")


def test_exclusion_can_make_the_benchmark_stale_rather_than_inventing_a_value():
    history = [obs(date(2026, 10, 26), "999", flags=["OUTLIER_VS_MEDIAN"]), obs(date(2026, 9, 14), "1400.00")]
    b = pick(history)
    assert not b.available and b.price is None and "STALE_BENCHMARK" in b.reasons[0]


def test_null_and_non_positive_prices_are_invalid():
    history = [obs(date(2026, 10, 26), None), obs(date(2026, 10, 19), "0")] + benchmark_history()[1:]
    assert pick(history, date(2026, 11, 5)).price == D("1450.90")   # 18 days old on 5 Nov, so still fresh


def test_other_series_and_incompatible_currency_or_unit_are_excluded():
    history = [obs(date(2026, 10, 26), "1000", key=OTHER_KEY), obs(date(2026, 10, 26), "1000", currency="USD"),
               obs(date(2026, 10, 26), "1000", unit="kg")] + benchmark_history()
    b = pick(history)
    assert b.price == D("1457.40")
    assert b.excluded["OTHER_SERIES"] == 1 and b.excluded["INCOMPATIBLE_CURRENCY_OR_UNIT"] == 2


def test_only_other_series_present_gives_unavailable():
    b = pick([obs(key=OTHER_KEY)])
    assert not b.available and b.reasons[0].startswith("NO_ELIGIBLE_BENCHMARK")


def test_no_observations_is_unavailable():
    b = pick([])
    assert not b.available and b.price is None and b.reasons[0].startswith("NO_ELIGIBLE_BENCHMARK")


def test_observations_not_yet_complete_at_assessment_date_are_excluded():
    history = [obs(AS_OF - timedelta(days=2), "1600")] + benchmark_history()
    b = pick(history)
    assert b.price == D("1457.40") and b.excluded["NOT_YET_COMPLETE"] == 1


def test_duplicate_boundary_weeks_with_identical_prices_are_deduplicated():
    history = [obs(date(2026, 10, 19), "1457.40", flags=["DUPLICATE_PERIOD"], occ=1),
               obs(date(2026, 10, 19), "1457.40", flags=["DUPLICATE_PERIOD"], occ=2)] + benchmark_history()[1:]
    assert len([o for o in eligible_observations(history, KEY, AS_OF)]) == 3
    assert pick(history).price == D("1457.40")


def test_eligible_observations_support_trend_analysis_and_are_sorted():
    history = [obs(date(2026, 10, 5), "1415.91"), obs(date(2026, 10, 19), "1457.40"),
               obs(date(2026, 10, 12), "10", flags=["OUTLIER_VS_MEDIAN"])]
    assert [o.price for o in eligible_observations(history, KEY, AS_OF)] == [D("1415.91"), D("1457.40")]
