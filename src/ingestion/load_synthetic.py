"""Synthetic procurement CSVs -> validation -> PostgreSQL (schema `procurement`).

DRY-RUN BY DEFAULT. `--apply` writes (one transaction, dependency order, idempotent upserts) and needs explicit
user approval; it refuses to run if any record is invalid. The CSV files are never modified, and rows that exist in
the database but not in the CSV are reported as orphans and never deleted.

Usage:
    python -m src.ingestion.load_synthetic            # dry-run: validate + read-only comparison with the database
    python -m src.ingestion.load_synthetic --no-db    # dry-run without any database access
    python -m src.ingestion.load_synthetic --apply    # writes (approval required)
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.synthetic.validate import Collected, collect, load_dir

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "synthetic"


@dataclass(frozen=True)
class Spec:
    table: str
    key: tuple[str, ...]
    cols: tuple[str, ...]  # all columns written, key columns first

    @property
    def value_cols(self) -> tuple[str, ...]:
        return tuple(c for c in self.cols if c not in self.key)


# Dependency order: parents before children.
SPECS: tuple[Spec, ...] = (
    Spec("ingredients", ("ingredient_code",),
         ("ingredient_code", "display_name", "unit", "benchmark_series_key", "benchmark_note")),
    Spec("suppliers", ("supplier_id",),
         ("supplier_id", "name", "country", "approval_status", "notes", "is_synthetic")),
    Spec("supplier_ingredients", ("supplier_id", "ingredient_code"), ("supplier_id", "ingredient_code")),
    Spec("contracts", ("contract_id",),
         ("contract_id", "supplier_id", "ingredient_code", "contract_type", "currency", "committed_monthly_volume_t",
          "start_date", "end_date", "unit_price", "index_series_key", "base_price", "index_base_value",
          "pass_through", "fixed_premium", "notes", "is_synthetic")),
    Spec("demand_forecasts", ("ingredient_code", "period_month", "forecast_version"),
         ("ingredient_code", "period_month", "forecast_version", "forecast_volume_t", "generated_on", "is_synthetic")),
)

Row = dict[str, Any]


def build_rows(valid: dict[str, list]) -> dict[str, list[Row]]:
    """Map validated models to schema rows (one dict per row, keyed by column name)."""
    rows: dict[str, list[Row]] = {s.table: [] for s in SPECS}
    for i in valid["ingredients"]:
        rows["ingredients"].append(i.model_dump())
    for s in valid["suppliers"]:
        d = s.model_dump()
        rows["supplier_ingredients"] += [{"supplier_id": s.supplier_id, "ingredient_code": code} for code in d.pop("ingredients")]
        rows["suppliers"].append(d)
    rows["contracts"] = [c.model_dump() for c in valid["contracts"]]
    rows["demand_forecasts"] = [f.model_dump() for f in valid["demand_forecasts"]]
    spec_by_table = {s.table: s for s in SPECS}
    return {t: [{c: r[c] for c in spec_by_table[t].cols} for r in rs] for t, rs in rows.items()}


# ───────────── planning (pure; no database access) ─────────────
@dataclass
class TablePlan:
    table: str
    read: int
    insert: int = 0
    update: int = 0
    unchanged: int = 0
    orphans: int = 0
    changed_columns: tuple[str, ...] = ()


def plan_changes(rows: dict[str, list[Row]], existing: dict[str, dict[tuple, Row]]) -> list[TablePlan]:
    """Compare CSV rows with existing database rows (key -> row dict). Orphans are only reported."""
    plans = []
    for spec in SPECS:
        have = existing.get(spec.table, {})
        p = TablePlan(spec.table, read=len(rows[spec.table]))
        seen = set()
        changed: set[str] = set()
        for r in rows[spec.table]:
            key = tuple(r[k] for k in spec.key)
            seen.add(key)
            if key not in have:
                p.insert += 1
                continue
            diff = [c for c in spec.value_cols if have[key].get(c) != r[c]]
            if diff:
                p.update += 1
                changed.update(diff)
            else:
                p.unchanged += 1
        p.orphans = len([k for k in have if k not in seen])
        p.changed_columns = tuple(sorted(changed))
        plans.append(p)
    return plans


# ───────────── SQL generation ─────────────
def upsert_sql(spec: Spec) -> str:
    cols = ", ".join(spec.cols)
    marks = ", ".join(f"%({c})s" for c in spec.cols)
    head = f"INSERT INTO procurement.{spec.table} AS t ({cols}) VALUES ({marks}) ON CONFLICT ({', '.join(spec.key)})"
    if not spec.value_cols:
        return head + " DO NOTHING"
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in spec.value_cols)
    old = ", ".join(f"t.{c}" for c in spec.value_cols)
    new = ", ".join(f"EXCLUDED.{c}" for c in spec.value_cols)
    return f"{head} DO UPDATE SET {sets} WHERE ({old}) IS DISTINCT FROM ({new})"


def select_sql(spec: Spec) -> str:
    return f"SELECT {', '.join(spec.cols)} FROM procurement.{spec.table}"


def fetch_existing(conn) -> dict[str, dict[tuple, Row]]:
    out: dict[str, dict[tuple, Row]] = {}
    for spec in SPECS:
        cur = conn.execute(select_sql(spec))
        names = [d.name for d in cur.description]
        out[spec.table] = {}
        for rec in cur.fetchall():
            row = dict(zip(names, rec))
            out[spec.table][tuple(row[k] for k in spec.key)] = row
    return out


def apply(conn, rows: dict[str, list[Row]]) -> None:
    """Write all tables in dependency order inside ONE transaction (all-or-nothing)."""
    with conn.transaction():
        for spec in SPECS:
            with conn.cursor() as cur:
                cur.executemany(upsert_sql(spec), rows[spec.table])


# ───────────── CLI ─────────────
def render(c: Collected, plans: list[TablePlan] | None) -> str:
    lines = ["Synthetic data dry-run summary", f"{'file':<18}{'read':>6}{'valid':>7}{'invalid':>9}"]
    for t in c.read:
        lines.append(f"{t + '.csv':<18}{c.read[t]:>6}{len(c.valid[t]):>7}{c.invalid[t]:>9}")
    lines.append(f"errors: {len(c.errors)}")
    lines += [f"  - {e}" for e in c.errors]
    if plans is not None:
        lines += ["", f"{'table':<22}{'rows':>5}{'insert':>8}{'update':>8}{'same':>6}{'orphan':>8}"]
        for p in plans:
            lines.append(f"{p.table:<22}{p.read:>5}{p.insert:>8}{p.update:>8}{p.unchanged:>6}{p.orphans:>8}")
        lines.append(f"totals: insert={sum(p.insert for p in plans)} update={sum(p.update for p in plans)} "
                     f"unchanged={sum(p.unchanged for p in plans)} orphans(not deleted)={sum(p.orphans for p in plans)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to the database (requires approval)")
    ap.add_argument("--no-db", action="store_true", help="validate only; no database access")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = ap.parse_args(argv)

    collected = collect(load_dir(args.data_dir))
    rows = build_rows(collected.valid)
    plans = None
    if not args.no_db:
        from src.db.preflight import connect
        try:
            with connect() as conn:  # read-only session
                plans = plan_changes(rows, fetch_existing(conn))
        except Exception as exc:
            print(f"FAIL: database read error {type(exc).__name__} sqlstate={getattr(exc, 'sqlstate', None)}")
            return 1
    print(render(collected, plans))
    if collected.errors:
        print("REJECTED: fix the CSV records above before loading.")
        return 2
    if not args.apply:
        print("DRY RUN: no changes made. Use --apply only after explicit approval.")
        return 0
    if args.no_db:
        print("--apply cannot be combined with --no-db")
        return 2
    import os
    import psycopg
    from dotenv import load_dotenv
    load_dotenv()
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15, sslmode="require") as conn:
        apply(conn, rows)
    print("APPLIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
