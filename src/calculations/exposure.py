"""Deterministic procurement price-exposure model (Decimal only; no LLM, no database, no framework).

For each forecast month and benchmark shock s (e.g. +10 %), with B0 the latest eligible benchmark (EUR/tonne) and
B_s = B0 * (1 + s):

* Coverage (independent of s): committed = sum over active contracts of monthly_volume * active_days / days_in_month.
  If committed <= forecast: allocated = committed, uncovered = forecast - committed.
  If committed > forecast: OVER_COMMITMENT. Allocation is scaled pro-rata ONLY as a costing convention; the contractual
  obligation is still reported (contracted volume, surplus, surplus cost). allocated + uncovered == forecast exactly.
* Fixed contract:   price = unit_price in every scenario  -> incremental exposure 0 while the contract is valid.
  (mark_to_market = volume * (spot_0 - unit_price) is informational and never added to exposure.)
* Index-linked:     price_s = base_price + pass_through * (B_s - index_base_value) + fixed_premium
                    incremental = volume * pass_through * (B_s - B0)
* Uncovered demand: price_s = B_s + spot_premium (a modelled ASSUMPTION)  incremental = volume * (B_s - B0)
* baseline cost = sum(volume * baseline price); scenario cost = baseline cost + incremental exposure.

Anything essential that is missing or invalid makes the affected month 'unavailable' with explicit reasons; no value is
estimated. Modelled costs are NOT supplier quotes, and the benchmark is a crude-oil price, not a refined-oil price.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext

from src.calculations.benchmark import DEFAULT_MAX_AGE_DAYS, BenchmarkResult, select_benchmark
from src.calculations.models import BenchmarkObservation, ContractInput, ForecastInput

D = Decimal
VOLUME_Q = D("0.000000000001")   # volumes rounded to 1e-12 tonne so that all sums are exact
MONEY_Q = D("0.000001")          # internal money precision (1e-6 EUR); round_money() gives cents for display
DEFAULT_SHOCKS = (D("-0.20"), D("-0.10"), D("0"), D("0.10"), D("0.20"))
DISCLAIMER = ("Modelled procurement costs under stated assumptions; not supplier quotes. The benchmark is a market "
              "price for the commodity (for sunflower oil: crude oil), not the company's contract price for the "
              "refined or processed ingredient.")


@dataclass(frozen=True)
class Assumptions:
    """Explicit, configurable scenario assumptions (fictional defaults)."""
    spot_premium: Decimal = D("150")                     # EUR/tonne added to the benchmark for uncovered volume
    shocks: tuple[Decimal, ...] = DEFAULT_SHOCKS         # relative benchmark shocks
    max_benchmark_age_days: int = DEFAULT_MAX_AGE_DAYS   # measured from the observation's period_end

    @property
    def description(self) -> str:
        return (f"Modelled assumption: spot premium of {self.spot_premium} EUR/tonne over the benchmark for uncovered "
                f"volume; benchmark shocks {[str(s) for s in self.shocks]}; benchmark older than "
                f"{self.max_benchmark_age_days} days (from period_end) is rejected.")


@dataclass(frozen=True)
class ScenarioTotals:
    cost: Decimal
    incremental: Decimal


@dataclass(frozen=True)
class Line:
    kind: str                                   # 'fixed' | 'index_linked' | 'uncovered'
    contract_id: str | None
    supplier_id: str | None
    volume_t: Decimal                           # volume allocated to forecast demand (costing basis)
    contracted_volume_t: Decimal                # contractual commitment for the month (0 for uncovered)
    baseline_price: Decimal
    baseline_cost: Decimal
    scenario_price: dict[Decimal, Decimal]
    scenario_cost: dict[Decimal, Decimal]
    incremental: dict[Decimal, Decimal]
    mark_to_market: Decimal | None = None       # fixed contracts only; informational


@dataclass(frozen=True)
class MonthResult:
    month: date
    status: str                                 # 'available' | 'unavailable'
    forecast_t: Decimal | None = None
    forecast_version: str | None = None
    committed_t: Decimal | None = None
    allocated_t: Decimal | None = None
    uncovered_t: Decimal | None = None
    surplus_committed_t: Decimal | None = None
    surplus_commitment_baseline_cost: Decimal | None = None
    over_commitment: bool = False
    coverage_change_vs_prior_t: Decimal | None = None   # change in uncovered volume vs previous month (coverage, not price)
    lines: tuple[Line, ...] = ()
    baseline_cost: Decimal | None = None
    scenarios: dict[Decimal, ScenarioTotals] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Totals:
    forecast_t: Decimal
    committed_t: Decimal
    uncovered_t: Decimal
    baseline_cost: Decimal
    scenarios: dict[Decimal, ScenarioTotals]


@dataclass(frozen=True)
class ExposureResult:
    ingredient: str
    assessment_date: date
    status: str
    benchmark: BenchmarkResult
    assumptions: Assumptions
    months: tuple[MonthResult, ...]
    totals: Totals | None
    warnings: tuple[str, ...]
    unavailable_reasons: tuple[str, ...]
    disclaimer: str = DISCLAIMER


def round_money(value: Decimal) -> Decimal:
    return value.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def _q(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_EVEN)


def _active_days(c: ContractInput, first: date, last: date) -> int:
    return max(0, (min(c.end_date, last) - max(c.start_date, first)).days + 1)


def _contract_problems(c: ContractInput, benchmark: BenchmarkResult, series_key: str) -> list[str]:
    p: list[str] = []
    need = lambda name, v: p.append(f"{c.contract_id}: missing {name}") if v is None else None  # noqa: E731
    need("monthly_volume_t", c.monthly_volume_t)
    if c.monthly_volume_t is not None and c.monthly_volume_t <= 0:
        p.append(f"{c.contract_id}: monthly_volume_t must be > 0")
    if c.currency != benchmark.currency:
        p.append(f"{c.contract_id}: contract currency {c.currency} does not match benchmark currency {benchmark.currency}")
    if c.contract_type == "fixed":
        need("unit_price", c.unit_price)
        if c.unit_price is not None and c.unit_price <= 0:
            p.append(f"{c.contract_id}: unit_price must be > 0")
        if any(v is not None for v in (c.index_series_key, c.base_price, c.index_base_value, c.pass_through, c.fixed_premium)):
            p.append(f"{c.contract_id}: fixed contract must not carry index fields")
    elif c.contract_type == "index_linked":
        for name in ("index_series_key", "base_price", "index_base_value", "pass_through", "fixed_premium"):
            need(name, getattr(c, name))
        if c.unit_price is not None:
            p.append(f"{c.contract_id}: index-linked contract must not carry unit_price")
        if c.index_series_key is not None and c.index_series_key != series_key:
            p.append(f"{c.contract_id}: index series {c.index_series_key} does not match the benchmark series {series_key}")
        for name in ("base_price", "index_base_value"):
            v = getattr(c, name)
            if v is not None and v <= 0:
                p.append(f"{c.contract_id}: {name} must be > 0")
        if c.pass_through is not None and not (D(0) <= c.pass_through <= D(1)):
            p.append(f"{c.contract_id}: pass_through must be within [0, 1]")
    else:
        p.append(f"{c.contract_id}: unknown contract_type {c.contract_type!r}")
    return p


def _calc_month(m: date, contracts: list[ContractInput], forecasts: list[ForecastInput], ingredient: str,
                benchmark: BenchmarkResult, series_key: str, a: Assumptions) -> MonthResult:
    reasons: list[str] = []
    if m.day != 1:
        return MonthResult(m, "unavailable", reasons=(f"month {m} is not the first day of a month",))
    candidates = [f for f in forecasts if f.ingredient_code == ingredient and f.period_month == m]
    forecast = max(candidates, key=lambda f: (f.generated_on, f.version)) if candidates else None
    if forecast is None:
        reasons.append(f"demand_forecast missing for {ingredient} {m:%Y-%m}")
    elif forecast.volume_t is None or forecast.volume_t <= 0:
        reasons.append(f"demand_forecast for {m:%Y-%m} must be > 0")
    if not benchmark.available:
        reasons += list(benchmark.reasons)

    first, last = m, date(m.year, m.month, monthrange(m.year, m.month)[1])
    days_in_month = last.day
    active: list[tuple[ContractInput, Decimal]] = []
    for c in contracts:
        if c.ingredient_code != ingredient:
            continue
        if c.start_date is None or c.end_date is None or c.end_date < c.start_date:
            reasons.append(f"{c.contract_id}: missing or invalid contract dates")
            continue
        days = _active_days(c, first, last)
        if days == 0:
            continue
        if not benchmark.available:
            continue
        problems = _contract_problems(c, benchmark, series_key)
        if problems:
            reasons += problems
            continue
        volume = c.monthly_volume_t if days == days_in_month else _q(c.monthly_volume_t * days / days_in_month, VOLUME_Q)
        active.append((c, volume))
    if reasons:
        return MonthResult(m, "unavailable", forecast_t=forecast.volume_t if forecast else None,
                           forecast_version=forecast.version if forecast else None, reasons=tuple(reasons))

    F = forecast.volume_t
    committed = sum((v for _, v in active), D(0))
    warnings: list[str] = []
    over = committed > F
    if over:
        allocations = []
        for _, v in active[:-1]:
            allocations.append(_q(v * F / committed, VOLUME_Q))
        allocations.append(max(D(0), F - sum(allocations, D(0))))
        uncovered = D(0)
        warnings.append(f"OVER_COMMITMENT: {m:%Y-%m} contracts commit {committed} t against a forecast of {F} t "
                        f"(surplus {committed - F} t). Costs use pro-rata allocation as a convention only; "
                        f"the contractual obligations remain.")
    else:
        allocations = [v for _, v in active]
        uncovered = F - committed
    if not active:
        warnings.append(f"NO_ACTIVE_CONTRACTS: no contract covers {ingredient} in {m:%Y-%m}; all demand is uncovered")

    B0 = benchmark.price
    shocks = a.shocks
    B = {s: B0 * (1 + s) for s in shocks}
    lines: list[Line] = []
    surplus_cost = D(0)
    for (c, contracted), alloc in zip(active, allocations):
        if c.contract_type == "fixed":
            p0 = c.unit_price
            price_s = {s: p0 for s in shocks}
            inc = {s: D(0) for s in shocks}
            mtm = _q(alloc * (B0 + a.spot_premium - p0), MONEY_Q)
        else:
            p0 = c.base_price + c.pass_through * (B0 - c.index_base_value) + c.fixed_premium
            price_s = {s: c.base_price + c.pass_through * (B[s] - c.index_base_value) + c.fixed_premium for s in shocks}
            inc = {s: _q(alloc * c.pass_through * (B[s] - B0), MONEY_Q) for s in shocks}
            mtm = None
        if p0 <= 0 or any(p <= 0 for p in price_s.values()):
            return MonthResult(m, "unavailable", forecast_t=F, forecast_version=forecast.version,
                               reasons=(f"{c.contract_id}: computed contract price is not positive",))
        base_cost = _q(alloc * p0, MONEY_Q)
        surplus_cost += _q((contracted - alloc) * p0, MONEY_Q)
        lines.append(Line(c.contract_type, c.contract_id, c.supplier_id, alloc, contracted, p0, base_cost, price_s,
                          {s: base_cost + inc[s] for s in shocks}, inc, mtm))
    if uncovered > 0:
        p0 = B0 + a.spot_premium
        base_cost = _q(uncovered * p0, MONEY_Q)
        inc = {s: _q(uncovered * (B[s] - B0), MONEY_Q) for s in shocks}
        lines.append(Line("uncovered", None, None, uncovered, D(0), p0, base_cost, {s: B[s] + a.spot_premium for s in shocks},
                          {s: base_cost + inc[s] for s in shocks}, inc))
    baseline = sum((l.baseline_cost for l in lines), D(0))
    scenarios = {s: ScenarioTotals(baseline + sum((l.incremental[s] for l in lines), D(0)),
                                   sum((l.incremental[s] for l in lines), D(0))) for s in shocks}
    return MonthResult(m, "available", F, forecast.version, committed, sum(allocations, D(0)), uncovered,
                       max(D(0), committed - F), surplus_cost, over, None, tuple(lines), baseline, scenarios,
                       (), tuple(warnings))


def calculate_exposure(ingredient: str, months: list[date], contracts: list[ContractInput],
                       forecasts: list[ForecastInput], observations: list[BenchmarkObservation],
                       benchmark_series_key: str, assessment_date: date,
                       assumptions: Assumptions | None = None) -> ExposureResult:
    a = assumptions or Assumptions()
    with localcontext() as ctx:
        ctx.prec = 50
        benchmark = select_benchmark(observations, benchmark_series_key, assessment_date, a.max_benchmark_age_days)
        results: list[MonthResult] = []
        prior_uncovered: Decimal | None = None
        for m in months:
            r = _calc_month(m, contracts, forecasts, ingredient, benchmark, benchmark_series_key, a)
            if r.uncovered_t is not None:
                if prior_uncovered is not None and r.status == "available":
                    r = _with_coverage_change(r, r.uncovered_t - prior_uncovered)
                prior_uncovered = r.uncovered_t
            results.append(r)
        warnings = tuple(w for r in results for w in r.warnings)
        reasons = tuple(f"{r.month:%Y-%m}: {x}" for r in results for x in r.reasons)
        totals = None
        if results and all(r.status == "available" for r in results):
            totals = Totals(
                sum((r.forecast_t for r in results), D(0)), sum((r.committed_t for r in results), D(0)),
                sum((r.uncovered_t for r in results), D(0)), sum((r.baseline_cost for r in results), D(0)),
                {s: ScenarioTotals(sum((r.scenarios[s].cost for r in results), D(0)),
                                   sum((r.scenarios[s].incremental for r in results), D(0))) for s in a.shocks})
        status = "available" if totals is not None else "unavailable"
        if not benchmark.available and not reasons:
            reasons = benchmark.reasons
        return ExposureResult(ingredient, assessment_date, status, benchmark, a, tuple(results), totals, warnings, reasons)


def _with_coverage_change(r: MonthResult, change: Decimal) -> MonthResult:
    from dataclasses import replace
    return replace(r, coverage_change_vs_prior_t=change)
