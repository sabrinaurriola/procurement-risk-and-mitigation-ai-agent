"""Demonstrate the exposure model on data read (read-only) from PostgreSQL.

Usage: python -m src.calculations.run_demo [--ingredient sunflower_oil] [--as-of 2026-11-10]
                                           [--months 2026-12 2027-01 2027-02] [--spot-premium 150]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from decimal import Decimal

from src.calculations.exposure import Assumptions, ExposureResult, calculate_exposure, round_money
from src.calculations.repository import load_inputs

money = lambda d: f"{round_money(d):>14,.2f}" if d is not None else f"{'unavailable':>14}"  # noqa: E731
tonnes = lambda d: f"{d:>10,.3f}" if d is not None else f"{'-':>10}"  # noqa: E731


def render(r: ExposureResult) -> str:
    b = r.benchmark
    out = [f"EXPOSURE ASSESSMENT — {r.ingredient} — as of {r.assessment_date} — status: {r.status.upper()}", ""]
    if b.available:
        out.append(f"Benchmark: {b.price} {b.currency}/{b.unit} | series {b.series_key}")
        out.append(f"           observation period {b.period_start} to {b.period_end} ({b.age_days} days old at assessment date; "
                   f"limit {b.max_age_days} from period_end)")
    else:
        out.append(f"Benchmark UNAVAILABLE: {'; '.join(b.reasons)}")
    if b.excluded:
        out.append(f"           observations excluded from calculations (kept in database): {b.excluded}")
    out += [f"Assumptions: {r.assumptions.description}", f"Note: {r.disclaimer}", ""]
    shocks = list(r.assumptions.shocks)
    for m in r.months:
        out.append(f"── {m.month:%B %Y} — {m.status}" + (f" (forecast {m.forecast_version})" if m.forecast_version else ""))
        if m.status != "available":
            out += [f"   UNAVAILABLE: {x}" for x in m.reasons]
            continue
        out.append(f"   forecast {tonnes(m.forecast_t)} t | contract commitments {tonnes(m.committed_t)} t | "
                   f"allocated {tonnes(m.allocated_t)} t | uncovered {tonnes(m.uncovered_t)} t"
                   + (f" | coverage change vs prior month (uncovered) {m.coverage_change_vs_prior_t:+,.3f} t"
                      if m.coverage_change_vs_prior_t is not None else ""))
        out.append(f"   {'line':<22}{'volume t':>12}{'base price':>13}{'baseline cost':>16}" + "".join(f"{s:>+10.0%}" for s in shocks))
        for l in m.lines:
            name = f"{l.kind} {l.contract_id or ''}".strip()
            out.append(f"   {name:<22}{l.volume_t:>12,.3f}{l.baseline_price:>13,.2f}{money(l.baseline_cost):>16}"
                       + "".join(f"{round_money(l.incremental[s]):>10,.0f}" for s in shocks))
        out.append(f"   {'TOTAL':<22}{tonnes(m.allocated_t + m.uncovered_t):>12}{'':>13}{money(m.baseline_cost):>16}"
                   + "".join(f"{round_money(m.scenarios[s].incremental):>10,.0f}" for s in shocks) + "   (incremental EUR)")
        out += [f"   WARNING: {w}" for w in m.warnings]
    out.append("")
    if r.totals:
        t = r.totals
        out.append(f"THREE-MONTH TOTAL: forecast {t.forecast_t:,.3f} t | commitments {t.committed_t:,.3f} t | uncovered {t.uncovered_t:,.3f} t "
                   f"| baseline modelled cost EUR {round_money(t.baseline_cost):,.2f}")
        for s in shocks:
            sc = t.scenarios[s]
            pct = (sc.incremental / t.baseline_cost * 100) if t.baseline_cost else Decimal(0)
            out.append(f"   benchmark {s:+.0%}: scenario cost EUR {round_money(sc.cost):>14,.2f} | incremental exposure EUR "
                       f"{round_money(sc.incremental):>12,.2f} ({pct:+.2f}% of baseline)")
    else:
        out.append("THREE-MONTH TOTAL: UNAVAILABLE")
        out += [f"   - {x}" for x in r.unavailable_reasons]
    out += [f"WARNING: {w}" for w in r.warnings]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingredient", default="sunflower_oil")
    ap.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    ap.add_argument("--months", nargs="+", default=["2026-12", "2027-01", "2027-02"])
    ap.add_argument("--spot-premium", type=Decimal, default=Assumptions().spot_premium)
    args = ap.parse_args(argv)
    from src.db.preflight import connect
    months = [date(int(x[:4]), int(x[5:7]), 1) for x in args.months]
    try:
        with connect() as conn:  # READ ONLY session
            key, contracts, forecasts, observations = load_inputs(conn, args.ingredient)
    except Exception as exc:
        print(f"FAIL: database read error {type(exc).__name__} sqlstate={getattr(exc, 'sqlstate', None)}")
        return 1
    if not key:
        print(f"UNAVAILABLE: no benchmark series is defined for ingredient {args.ingredient!r}")
        return 2
    result = calculate_exposure(args.ingredient, months, contracts, forecasts, observations, key, args.as_of,
                                Assumptions(spot_premium=args.spot_premium))
    print(render(result))
    return 0 if result.status == "available" else 2


if __name__ == "__main__":
    sys.exit(main())
