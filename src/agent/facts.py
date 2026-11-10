"""Deterministic report sections built from the calculation result and SQL data (never from the LLM)."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from src.agent.config import IngredientProfile
from src.agent.data import InternalData
from src.agent.schemas import (BenchmarkSection, FinancialSection, Indicator, MonthRow, ScenarioRow, SupplierExposure)
from src.calculations.exposure import ExposureResult, round_money


def horizon_months(as_of: date, n: int) -> list[date]:
    """The n full calendar months after the assessment month (10 Nov 2026, n=3 -> Dec 2026, Jan 2027, Feb 2027)."""
    y, m = as_of.year, as_of.month
    out = []
    for _ in range(n):
        m += 1
        if m == 13:
            y, m = y + 1, 1
        out.append(date(y, m, 1))
    return out


money = lambda d: f"{round_money(d):,.2f}"  # noqa: E731
tonnes = lambda d: f"{d:,.3f}"  # noqa: E731
label = lambda s: f"{s:+.0%}"  # noqa: E731


def benchmark_section(calc: ExposureResult, profile: IngredientProfile) -> BenchmarkSection:
    b = calc.benchmark
    status = "available" if b.available else ("stale" if b.reasons and b.reasons[0].startswith("STALE") else "unavailable")
    return BenchmarkSection(status=status, series_key=b.series_key,
                            price_eur_per_tonne=f"{b.price:,.2f}" if b.price is not None else None,
                            period_start=b.period_start, period_end=b.period_end, age_days=b.age_days,
                            max_age_days=b.max_age_days, excluded_observations=dict(b.excluded),
                            reasons=list(b.reasons), caveat=profile.benchmark_caveat)


def financial_section(calc: ExposureResult) -> FinancialSection:
    shocks = list(calc.assumptions.shocks)
    rows = [MonthRow(month=f"{m.month:%Y-%m}", forecast_t=tonnes(m.forecast_t), contract_commitments_t=tonnes(m.committed_t),
                     uncovered_t=tonnes(m.uncovered_t),
                     coverage_change_vs_prior_month_t=(f"{m.coverage_change_vs_prior_t:+,.3f}"
                                                       if m.coverage_change_vs_prior_t is not None else None),
                     over_commitment=m.over_commitment, baseline_modelled_cost_eur=money(m.baseline_cost),
                     incremental_exposure_eur={label(s): money(m.scenarios[s].incremental) for s in shocks})
            for m in calc.months if m.status == "available"]
    t = calc.totals
    scenarios = []
    if t:
        scenarios = [ScenarioRow(shock=label(s), scenario_cost_eur=money(t.scenarios[s].cost),
                                 incremental_exposure_eur=money(t.scenarios[s].incremental),
                                 pct_of_baseline=f"{(t.scenarios[s].incremental / t.baseline_cost * 100):+.2f}%"
                                 if t.baseline_cost else "n/a") for s in shocks]
    return FinancialSection(status="available" if t else "unavailable", months=rows, scenarios=scenarios,
                            baseline_modelled_cost_eur=money(t.baseline_cost) if t else None,
                            total_forecast_t=tonnes(t.forecast_t) if t else None,
                            total_contract_commitments_t=tonnes(t.committed_t) if t else None,
                            total_uncovered_t=tonnes(t.uncovered_t) if t else None,
                            assumptions=calc.assumptions.description, unavailable_reasons=list(calc.unavailable_reasons),
                            disclaimer=calc.disclaimer)


def supplier_exposure(internal: InternalData, ingredient: str, months: list[date]) -> list[SupplierExposure]:
    out = []
    first, last = months[0], months[-1]
    end_of_horizon = date(last.year + (last.month == 12), last.month % 12 + 1, 1)
    for c in internal.contracts:
        if c.ingredient_code != ingredient or c.start_date is None or c.end_date is None:
            continue
        if c.end_date < first or c.start_date >= end_of_horizon:
            continue
        s = internal.suppliers.get(c.supplier_id)
        out.append(SupplierExposure(
            contract_id=c.contract_id, supplier_id=c.supplier_id, supplier_name=s.name if s else "unknown supplier",
            country=s.country if s else "??", approval_status=s.approval_status if s else "unknown",
            contract_type=c.contract_type, monthly_volume_t=tonnes(c.monthly_volume_t) if c.monthly_volume_t is not None else "missing",
            valid_from=c.start_date, valid_to=c.end_date, ends_within_horizon=c.end_date < end_of_horizon))
    return out


SEVERITY_RULES = {
    "contract_coverage": "peak monthly uncovered share of forecast: >= 50% high; >= 25% medium; > 0% low",
    "supplier_status": "any active contract with a blocked supplier: high; with a conditional or unverified supplier: medium",
    "price_exposure": "a +10% benchmark shock as % of baseline modelled cost: >= 10% high; >= 3% medium; > 0% low",
}
_ORDER = {"low": 0, "medium": 1, "high": 2}


def indicators(calc: ExposureResult, internal: InternalData, ingredient: str) -> list[Indicator]:
    """Documented, deterministic severity indicators for the internal risk categories (computed, not judged)."""
    out: list[Indicator] = []
    months = [m for m in calc.months if m.status == "available"]
    if not months or calc.status != "available":
        return _supplier_status_fallback(calc, internal, ingredient)
    shares = [(m.uncovered_t / m.forecast_t, m) for m in months if m.forecast_t]
    if shares and max(s for s, _ in shares) > 0:
        share, m = max(shares, key=lambda x: x[0])
        sev = "high" if share >= Decimal("0.5") else "medium" if share >= Decimal("0.25") else "low"
        out.append(Indicator(ref="INT:indicator:coverage", category="contract_coverage", severity=sev,
                             value=f"peak uncovered share {share:.1%} in {m.month:%Y-%m} ({tonnes(m.uncovered_t)} t of {tonnes(m.forecast_t)} t forecast)",
                             rule=SEVERITY_RULES["contract_coverage"]))
    status_of = {k: v.approval_status for k, v in internal.suppliers.items()}
    supplier_of = {c.contract_id: c.supplier_id for c in internal.contracts}
    flagged: dict[str, Decimal] = {}
    total = Decimal(0)
    for m in months:
        for l in m.lines:
            if l.kind == "uncovered":
                continue
            total += l.volume_t
            sid = supplier_of.get(l.contract_id)
            if status_of.get(sid, "unknown") != "approved":
                flagged[sid] = flagged.get(sid, Decimal(0)) + l.volume_t
    if flagged and total:
        worst = "high" if any(status_of.get(s) == "blocked" for s in flagged) else "medium"
        desc = ", ".join(f"{s} ({status_of.get(s, 'unknown')})" for s in sorted(flagged))
        out.append(Indicator(ref="INT:indicator:supplier_status", category="supplier_status", severity=worst,
                             value=f"{desc}: {sum(flagged.values()) / total:.1%} of contract-committed volume over the horizon",
                             rule=SEVERITY_RULES["supplier_status"]))
    if calc.totals and calc.totals.baseline_cost:
        plus = next((s for s in calc.assumptions.shocks if s == Decimal("0.10")), None)
        if plus is not None:
            pct = calc.totals.scenarios[plus].incremental / calc.totals.baseline_cost * 100
            sev = "high" if pct >= 10 else "medium" if pct >= 3 else "low"
            out.append(Indicator(ref="INT:indicator:price_exposure", category="price_exposure", severity=sev,
                                 value=f"+10% benchmark shock = {pct:+.2f}% of baseline modelled cost (EUR {money(calc.totals.scenarios[plus].incremental)})",
                                 rule=SEVERITY_RULES["price_exposure"]))
    return out


def _supplier_status_fallback(calc: ExposureResult, internal: InternalData, ingredient: str) -> list[Indicator]:
    """When the financial model is unavailable, supplier approval status still matters: derive it from the contracts that
    overlap the horizon (no volume shares, which need the calculation)."""
    if not calc.months:
        return []
    first = min(m.month for m in calc.months)
    last = max(m.month for m in calc.months)
    end = date(last.year + (last.month == 12), last.month % 12 + 1, 1)
    flagged: dict[str, str] = {}
    for c in internal.contracts:
        if c.ingredient_code != ingredient or c.start_date is None or c.end_date is None:
            continue
        if c.end_date < first or c.start_date >= end:
            continue
        s = internal.suppliers.get(c.supplier_id)
        if s and s.approval_status != "approved":
            flagged[c.supplier_id] = s.approval_status
    if not flagged:
        return []
    worst = "high" if "blocked" in flagged.values() else "medium"
    desc = ", ".join(f"{k} ({v})" for k, v in sorted(flagged.items()))
    return [Indicator(ref="INT:indicator:supplier_status", category="supplier_status", severity=worst,
                      value=f"{desc} hold active contracts in the horizon (volume share not available: financial model unavailable)",
                      rule=SEVERITY_RULES["supplier_status"])]


def internal_refs(calc: ExposureResult, internal: InternalData, ingredient: str, inds: list[Indicator],
                  suppliers: list[SupplierExposure]) -> dict[str, str]:
    """Identifiers of internal records and calculation outputs that findings may cite as their internal basis."""
    refs: dict[str, str] = {}
    if calc.benchmark.available:
        refs["INT:benchmark"] = (f"latest eligible benchmark {calc.benchmark.price:,.2f} EUR/tonne, period "
                                 f"{calc.benchmark.period_start} to {calc.benchmark.period_end}")
    if calc.status == "available":
        refs["INT:calc:baseline"] = "baseline modelled procurement cost for the horizon"
        for s in calc.assumptions.shocks:
            refs[f"INT:calc:scenario:{label(s)}"] = f"scenario cost and incremental exposure for a {label(s)} benchmark shock"
    for m in calc.months:
        if m.status == "available":
            refs[f"INT:coverage:{m.month:%Y-%m}"] = f"contract coverage and uncovered volume for {m.month:%Y-%m}"
    for c in internal.contracts:
        if c.ingredient_code == ingredient:
            refs[f"INT:contract:{c.contract_id}"] = f"synthetic contract {c.contract_id} ({c.contract_type})"
    for sid, s in internal.suppliers.items():
        refs[f"INT:supplier:{sid}"] = f"synthetic supplier {sid}, approval status {s.approval_status}"
    for i in inds:
        refs[i.ref] = f"deterministic indicator: {i.value}"
    return refs


def facts_for_llm(benchmark: BenchmarkSection, financial: FinancialSection, suppliers: list[SupplierExposure],
                  warnings: list[str], limitations: list[str], inds: list[Indicator] | None = None,
                  refs: dict[str, str] | None = None) -> dict:
    """The only numbers the LLM may quote. Everything is pre-formatted text; the LLM does no arithmetic."""
    return {
        "benchmark": benchmark.model_dump(mode="json"),
        "financial": financial.model_dump(mode="json"),
        "supplier_exposure": [s.model_dump(mode="json") for s in suppliers],
        "severity_indicators": [i.model_dump(mode="json") for i in (inds or [])],
        "internal_refs": [{"ref": k, "description": v} for k, v in sorted((refs or {}).items())],
        "warnings": warnings, "limitations": limitations,
    }


def facts_text(facts: dict) -> str:
    return json.dumps(facts, ensure_ascii=False, indent=1, sort_keys=True)


def monetary_values(benchmark: BenchmarkSection, financial: FinancialSection, spot_premium: Decimal) -> set[Decimal]:
    """Every figure that is genuinely an amount of money (EUR) in the deterministic facts. Tonnages are excluded on
    purpose so that a volume written with a currency label ("EUR 451.29 t") is caught."""
    raw: list[str] = [str(spot_premium)]
    if benchmark.price_eur_per_tonne:
        raw.append(benchmark.price_eur_per_tonne)
    if financial.baseline_modelled_cost_eur:
        raw.append(financial.baseline_modelled_cost_eur)
    for s in financial.scenarios:
        raw += [s.scenario_cost_eur, s.incremental_exposure_eur]
    for m in financial.months:
        raw.append(m.baseline_modelled_cost_eur)
        raw += list(m.incremental_exposure_eur.values())
    out: set[Decimal] = set()
    for r in raw:
        out.add(abs(Decimal(r.replace(",", ""))).normalize())
    return out
