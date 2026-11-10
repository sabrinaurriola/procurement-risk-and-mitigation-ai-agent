"""Benchmark eligibility and selection. Pure functions: observations are never modified or removed upstream;
ineligible ones are simply not used here and are counted so reports can say why."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.calculations.models import BenchmarkObservation

# Flags that do not make an observation invalid. DUPLICATE_PERIOD marks marketing-year boundary weeks that are
# reported twice (handled by de-duplication); PERIOD_GAP only says the previous week is missing.
# Every other flag (outlier, non-numeric, non-positive, conflicting duplicate, overlap, odd length, future date,
# and any flag added later) excludes the observation.
BENIGN_FLAGS = frozenset({"DUPLICATE_PERIOD", "PERIOD_GAP"})
DEFAULT_MAX_AGE_DAYS = 21  # provisional; measured from period_end to the assessment date


@dataclass(frozen=True)
class BenchmarkResult:
    available: bool
    series_key: str
    price: Decimal | None
    period_start: date | None
    period_end: date | None
    age_days: int | None
    currency: str | None
    unit: str | None
    max_age_days: int
    considered: int
    excluded: dict[str, int]
    reasons: tuple[str, ...] = ()


def _classify(observations, series_key, as_of, currency, unit):
    eligible: list[BenchmarkObservation] = []
    excluded: Counter = Counter()
    for o in observations:
        if o.series_key != series_key:
            excluded["OTHER_SERIES"] += 1
        elif o.currency != currency or o.unit != unit:
            excluded["INCOMPATIBLE_CURRENCY_OR_UNIT"] += 1
        elif set(o.quality_flags) - BENIGN_FLAGS:
            excluded[sorted(set(o.quality_flags) - BENIGN_FLAGS)[0]] += 1
        elif o.price is None or o.price <= 0:
            excluded["INVALID_PRICE"] += 1
        elif o.period_end > as_of:
            excluded["NOT_YET_COMPLETE"] += 1
        else:
            eligible.append(o)
    by_period: dict[date, list[BenchmarkObservation]] = {}
    for o in eligible:
        by_period.setdefault(o.period_start, []).append(o)
    kept: list[BenchmarkObservation] = []
    for group in by_period.values():
        if len({o.price for o in group}) > 1:           # same week, different prices: cannot choose safely
            excluded["CONFLICTING_DUPLICATES"] += len(group)
        else:
            kept.append(min(group, key=lambda o: o.occurrence_no))
    return sorted(kept, key=lambda o: (o.period_end, o.period_start)), excluded


def eligible_observations(observations: list[BenchmarkObservation], series_key: str, as_of: date,
                          currency: str = "EUR", unit: str = "tonne") -> list[BenchmarkObservation]:
    """Observations usable for calculations and trend analysis, oldest first (one per reporting week)."""
    return _classify(observations, series_key, as_of, currency, unit)[0]


def select_benchmark(observations: list[BenchmarkObservation], series_key: str, as_of: date,
                     max_age_days: int = DEFAULT_MAX_AGE_DAYS, currency: str = "EUR",
                     unit: str = "tonne") -> BenchmarkResult:
    """Latest eligible observation of `series_key`; unavailable (never invented) if none or older than max_age_days."""
    eligible, excluded = _classify(observations, series_key, as_of, currency, unit)
    base = dict(series_key=series_key, max_age_days=max_age_days, considered=len(observations), excluded=dict(excluded))
    if not eligible:
        return BenchmarkResult(False, price=None, period_start=None, period_end=None, age_days=None, currency=None,
                               unit=None, reasons=(f"NO_ELIGIBLE_BENCHMARK: no usable observation of series {series_key} "
                                                   f"as of {as_of} (excluded: {dict(excluded) or 'none'})",), **base)
    latest = eligible[-1]
    age = (as_of - latest.period_end).days
    if age > max_age_days:
        return BenchmarkResult(False, price=None, period_start=latest.period_start, period_end=latest.period_end,
                               age_days=age, currency=latest.currency, unit=latest.unit,
                               reasons=(f"STALE_BENCHMARK: latest eligible observation covers {latest.period_start}.."
                                        f"{latest.period_end} and is {age} days old (maximum {max_age_days})",), **base)
    return BenchmarkResult(True, price=latest.price, period_start=latest.period_start, period_end=latest.period_end,
                           age_days=age, currency=latest.currency, unit=latest.unit, **base)
