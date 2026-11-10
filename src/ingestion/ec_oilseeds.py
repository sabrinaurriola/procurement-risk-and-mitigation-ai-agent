"""European Commission oilseeds price workbook -> Python transformation -> PostgreSQL (`procurement.commodity_prices`).

DRY-RUN BY DEFAULT. `--apply` writes and needs explicit user approval. The workbook is only read, never modified.
Observations are never altered or deleted: anomalies are *flagged* in `quality_flags`; rows that cannot be stored
(missing required fields, unparseable dates, unknown state/unit) are *rejected and reported*, not silently dropped.

Usage:
    python -m src.ingestion.ec_oilseeds            # dry-run: parse + read-only comparison with the database
    python -m src.ingestion.ec_oilseeds --no-db    # dry-run without any database access
    python -m src.ingestion.ec_oilseeds --apply    # writes (approval required)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel

from src.ingestion.series_key import build_series_key

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FILE = ROOT / "data" / "raw" / "European_Commission_Oilseeds_Prices.xlsx"
SOURCE = "EC_OILSEEDS"
PUBLISHER = "European Commission"
EXPECTED_HEADERS = ["Product", "Product Type", "Member State", "Marketing Year", "Week", "Week Begin Date",
                    "Week End Date", "Market", "Market Stage", "Unit of Measure", "Price (EUR)"]

OUTLIER_THRESHOLD = Decimal("0.35")  # |price / local median - 1| above this is flagged (documented rule)
OUTLIER_WINDOW = 8                   # up to 8 neighbouring observations on each side
OUTLIER_MIN_NEIGHBOURS = 3

MEMBER_STATES = {
    "Austria": "AT", "Belgium": "BE", "Bulgaria": "BG", "Croatia": "HR", "Cyprus": "CY", "Czechia": "CZ",
    "Czech Republic": "CZ", "Denmark": "DK", "Estonia": "EE", "Finland": "FI", "France": "FR", "Germany": "DE",
    "Greece": "GR", "Hungary": "HU", "Ireland": "IE", "Italy": "IT", "Latvia": "LV", "Lithuania": "LT",
    "Luxembourg": "LU", "Malta": "MT", "Netherlands": "NL", "Poland": "PL", "Portugal": "PT", "Romania": "RO",
    "Slovakia": "SK", "Slovenia": "SI", "Spain": "ES", "Sweden": "SE",
}

F_DUP = "DUPLICATE_PERIOD"
F_DUP_CONFLICT = "DUPLICATE_CONFLICTING_PRICE"
F_NONNUM = "NON_NUMERIC_PRICE"
F_NONPOS = "NON_POSITIVE_PRICE"
F_OUTLIER = "OUTLIER_VS_MEDIAN"
F_GAP = "PERIOD_GAP"
F_OVERLAP = "OVERLAPPING_PERIOD"
F_LENGTH = "UNUSUAL_PERIOD_LENGTH"
F_FUTURE = "FUTURE_DATE"

PRICE_COLUMNS = ("source_id", "run_id", "series_key", "product", "product_type", "member_state", "market",
                 "market_stage", "quotation_basis", "currency", "unit", "unit_raw", "price", "price_raw",
                 "period_start", "period_end", "marketing_year", "week_in_my", "source_file", "source_sheet",
                 "source_row", "occurrence_no", "row_hash", "quality_flags", "raw_values")


@dataclass
class Observation:
    source_row: int
    series_key: str
    product: str
    product_type: str | None
    member_state: str
    market: str | None
    market_stage: str
    quotation_basis: str | None
    currency: str
    unit: str
    unit_raw: str
    price: Decimal | None
    price_raw: str
    period_start: date
    period_end: date
    marketing_year: str | None
    week_in_my: int | None
    source_file: str
    source_sheet: str
    row_hash: str
    raw_values: dict[str, str]
    occurrence_no: int = 1
    quality_flags: list[str] = field(default_factory=list)

    @property
    def natural_key(self) -> tuple:
        return (self.series_key, self.period_start, self.occurrence_no)


@dataclass
class Rejected:
    source_row: int
    reasons: list[str]


@dataclass
class ParseResult:
    file_name: str
    sha256: str
    sheet: str
    read: int
    blank_skipped: int
    observations: list[Observation]
    rejected: list[Rejected]


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v).strip()


def parse_date(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, bool):
        raise ValueError("not a date")
    if isinstance(v, (int, float)):
        return from_excel(v).date()
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.strip()):
        return date.fromisoformat(v.strip())
    raise ValueError(f"unparseable date {v!r}")


def parse_unit(raw: str, currency: str) -> str:
    m = re.fullmatch(r"\s*(€|EUR)\s*/\s*(tonne|t)\s*", raw, re.IGNORECASE)
    if not m:
        raise ValueError(f"unrecognised unit {raw!r}")
    if currency != "EUR":
        raise ValueError(f"unit {raw!r} inconsistent with currency {currency}")
    return "tonne"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _convert_row(idx: int, values: list[Any], currency: str, file_name: str, sheet: str) -> Observation:
    raw = {h: _text(v) for h, v in zip(EXPECTED_HEADERS, values)}
    errors = []
    product = raw["Product"]
    product_type = raw["Product Type"] or None
    state_name = raw["Member State"]
    market_stage = raw["Market Stage"]
    if not product:
        errors.append("missing Product")
    if not market_stage:
        errors.append("missing Market Stage")
    state = MEMBER_STATES.get(state_name)
    if not state:
        errors.append(f"unknown Member State {state_name!r}")
    try:
        start, end = parse_date(values[5]), parse_date(values[6])
        if end < start:
            errors.append("Week End Date before Week Begin Date")
    except ValueError as exc:
        errors.append(str(exc))
        start = end = None
    try:
        unit = parse_unit(raw["Unit of Measure"], currency)
    except ValueError as exc:
        errors.append(str(exc))
        unit = ""
    week = None
    if raw["Week"]:
        try:
            week = int(Decimal(raw["Week"]))
        except (InvalidOperation, ValueError):
            errors.append(f"invalid Week {raw['Week']!r}")
    if errors:
        raise ValueError("; ".join(errors))

    flags: list[str] = []
    price: Decimal | None = None
    cell = values[10]
    if isinstance(cell, bool) or cell is None or (isinstance(cell, str) and not cell.strip()):
        flags.append(F_NONNUM)
    else:
        try:
            price = Decimal(str(cell).strip()).quantize(Decimal("0.0001"))
        except (InvalidOperation, ValueError):
            flags.append(F_NONNUM)
        else:
            if not price.is_finite():
                price = None
                flags.append(F_NONNUM)
            elif price <= 0:
                price = None
                flags.append(F_NONPOS)
    key = build_series_key(source=SOURCE, product=product, product_type=product_type or "", member_state=state,
                           market=raw["Market"], market_stage=market_stage, quotation_basis="",
                           currency=currency, unit=unit)
    row_hash = hashlib.sha256(json.dumps([raw[h] for h in EXPECTED_HEADERS], ensure_ascii=False).encode()).hexdigest()
    return Observation(idx, key, product, product_type, state, raw["Market"] or None, market_stage, None, currency,
                       unit, raw["Unit of Measure"], price, raw["Price (EUR)"], start, end,
                       raw["Marketing Year"] or None, week, file_name, sheet, row_hash, raw, quality_flags=flags)


def flag_observations(obs: list[Observation], today: date) -> None:
    """Assign occurrence numbers and data-quality flags in place. Never changes or removes an observation."""
    groups: dict[tuple, list[Observation]] = defaultdict(list)
    for o in obs:  # file order
        groups[(o.series_key, o.period_start)].append(o)
    for members in groups.values():
        for n, o in enumerate(members, start=1):
            o.occurrence_no = n
        if len(members) > 1:
            conflict = len({m.price for m in members if m.price is not None}) > 1
            for m in members:
                m.quality_flags.append(F_DUP)
                if conflict:
                    m.quality_flags.append(F_DUP_CONFLICT)
    by_series: dict[str, list[Observation]] = defaultdict(list)
    for o in obs:
        by_series[o.series_key].append(o)
    for series in by_series.values():
        series.sort(key=lambda o: (o.period_start, o.source_row))
        prev: Observation | None = None
        for o in series:
            if (o.period_end - o.period_start).days != 6:
                o.quality_flags.append(F_LENGTH)
            if o.period_start > today:
                o.quality_flags.append(F_FUTURE)
            if prev is not None and o.period_start != prev.period_start:
                if o.period_start < prev.period_end:
                    o.quality_flags.append(F_OVERLAP)
                elif (o.period_start - prev.period_start) > timedelta(days=7):
                    o.quality_flags.append(F_GAP)
            if prev is None or o.period_start != prev.period_start:
                prev = o
        for i, o in enumerate(series):
            if o.price is None:
                continue
            lo, hi = max(0, i - OUTLIER_WINDOW), min(len(series), i + OUTLIER_WINDOW + 1)
            neighbours = [n.price for j, n in enumerate(series[lo:hi], start=lo) if j != i and n.price is not None]
            if len(neighbours) < OUTLIER_MIN_NEIGHBOURS:
                continue
            median = statistics.median(neighbours)
            if median > 0 and abs(o.price / median - 1) > OUTLIER_THRESHOLD:
                o.quality_flags.append(F_OUTLIER)
    for o in obs:
        o.quality_flags = sorted(set(o.quality_flags))


def parse_workbook(path: Path, today: date | None = None) -> ParseResult:
    today = today or date.today()
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = [_text(c) for c in next(rows)][: len(EXPECTED_HEADERS)]
        if header != EXPECTED_HEADERS:
            raise ValueError(f"unexpected workbook headers: {header}")
        currency = re.search(r"\(([A-Z]{3})\)", header[-1]).group(1)
        obs: list[Observation] = []
        rejected: list[Rejected] = []
        read = blank = 0
        for idx, row in enumerate(rows, start=2):
            values = list(row[: len(EXPECTED_HEADERS)]) + [None] * (len(EXPECTED_HEADERS) - len(row))
            if all(v is None or (isinstance(v, str) and not v.strip()) for v in values):
                blank += 1
                continue
            read += 1
            try:
                obs.append(_convert_row(idx, values, currency, path.name, ws.title))
            except ValueError as exc:
                rejected.append(Rejected(idx, str(exc).split("; ")))
        sheet = ws.title
    finally:
        wb.close()
    flag_observations(obs, today)
    return ParseResult(path.name, sha256_file(path), sheet, read, blank, obs, rejected)


# ───────────── planning (pure) ─────────────
@dataclass
class PricePlan:
    insert: int = 0
    skipped_existing: int = 0
    revised: list[tuple] = field(default_factory=list)  # natural keys whose row_hash differs from the database


def plan_prices(obs: list[Observation], existing: dict[tuple, str]) -> PricePlan:
    """existing maps (series_key, period_start, occurrence_no) -> row_hash."""
    plan = PricePlan()
    for o in obs:
        have = existing.get(o.natural_key)
        if have is None:
            plan.insert += 1
        elif have == o.row_hash:
            plan.skipped_existing += 1
        else:
            plan.revised.append(o.natural_key)
    return plan


# ───────────── SQL ─────────────
def insert_prices_sql() -> str:
    cols = ", ".join(PRICE_COLUMNS)
    marks = ", ".join(f"%({c})s" for c in PRICE_COLUMNS)
    return (f"INSERT INTO procurement.commodity_prices ({cols}) VALUES ({marks}) "
            f"ON CONFLICT (series_key, period_start, occurrence_no) DO NOTHING")


SOURCE_SQL = ("INSERT INTO procurement.data_sources (name, publisher, file_name, sha256, licence_note) "
              "VALUES (%(name)s, %(publisher)s, %(file_name)s, %(sha256)s, %(licence_note)s) "
              "ON CONFLICT (file_name, sha256) DO NOTHING")
RUN_SQL = ("INSERT INTO procurement.ingest_runs (source_id, rows_read, inserted, skipped_existing, flagged, status) "
           "VALUES (%s, %s, %s, %s, %s, 'applied') RETURNING run_id")


def to_row(o: Observation, source_id: Any, run_id: Any) -> dict[str, Any]:
    from psycopg.types.json import Jsonb
    row = {c: getattr(o, c, None) for c in PRICE_COLUMNS}
    row.update(source_id=source_id, run_id=run_id, quality_flags=list(o.quality_flags), raw_values=Jsonb(o.raw_values))
    return row


def fetch_existing_prices(conn, series_keys: list[str]) -> dict[tuple, str]:
    cur = conn.execute("SELECT series_key, period_start, occurrence_no, row_hash FROM procurement.commodity_prices "
                       "WHERE series_key = ANY(%s)", (series_keys,))
    return {(k, d, n): h for k, d, n, h in cur.fetchall()}


def apply(conn, result: ParseResult, plan: PricePlan) -> None:
    if plan.revised:
        raise RuntimeError(f"{len(plan.revised)} existing rows differ from the workbook (REVISED); refusing to overwrite")
    flagged = sum(1 for o in result.observations if o.quality_flags)
    with conn.transaction():
        conn.execute(SOURCE_SQL, {"name": SOURCE, "publisher": PUBLISHER, "file_name": result.file_name,
                                  "sha256": result.sha256, "licence_note": "Redistribution terms not yet confirmed"})
        source_id = conn.execute("SELECT source_id FROM procurement.data_sources WHERE file_name=%s AND sha256=%s",
                                 (result.file_name, result.sha256)).fetchone()[0]
        run_id = conn.execute(RUN_SQL, (source_id, len(result.observations) + len(result.rejected), plan.insert,
                                        plan.skipped_existing, flagged)).fetchone()[0]
        with conn.cursor() as cur:
            cur.executemany(insert_prices_sql(), [to_row(o, source_id, run_id) for o in result.observations])


# ───────────── reporting / CLI ─────────────
def summarize(result: ParseResult) -> dict[str, Any]:
    obs = result.observations
    flag_counts = Counter(f for o in obs for f in o.quality_flags)
    dup_groups = len({(o.series_key, o.period_start) for o in obs if F_DUP in o.quality_flags})
    return {
        "file": result.file_name, "sha256": result.sha256[:16] + "…", "sheet": result.sheet,
        "rows_read": result.read, "blank_rows_skipped": result.blank_skipped, "loadable": len(obs),
        "rejected": len(result.rejected), "series": len({o.series_key for o in obs}),
        "duplicate_groups": dup_groups, "duplicate_rows": sum(1 for o in obs if F_DUP in o.quality_flags),
        "rows_with_any_flag": sum(1 for o in obs if o.quality_flags), "flag_counts": dict(sorted(flag_counts.items())),
        "oldest_period": min((o.period_start for o in obs), default=None),
        "newest_period": max((o.period_start for o in obs), default=None),
    }


def render(result: ParseResult, plan: PricePlan | None, samples: int = 6) -> str:
    s = summarize(result)
    lines = ["EC sunflower oil dry-run summary"]
    lines += [f"  {k}: {v}" for k, v in s.items() if k != "flag_counts"]
    lines.append("  flags:")
    lines += [f"    {k}: {v}" for k, v in s["flag_counts"].items()] or ["    none"]
    for flag in s["flag_counts"]:
        sample = [o for o in result.observations if flag in o.quality_flags][:samples]
        lines.append(f"  samples of {flag}:")
        lines += [f"    row {o.source_row}: {o.period_start}..{o.period_end} price={o.price if o.price is not None else o.price_raw!r}"
                  f" occ={o.occurrence_no}" for o in sample]
    if result.rejected:
        lines.append("  rejected rows:")
        lines += [f"    row {r.source_row}: {'; '.join(r.reasons)}" for r in result.rejected[:20]]
    if plan is not None:
        lines.append(f"  expected database effect: insert={plan.insert} skipped_existing={plan.skipped_existing} "
                     f"revised={len(plan.revised)}")
    return "\n".join(lines)


def check_benchmark_key(result: ParseResult) -> str:
    import csv
    path = ROOT / "data" / "synthetic" / "ingredients.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        want = {r["ingredient_code"]: r["benchmark_series_key"] for r in csv.DictReader(fh)}.get("sunflower_oil")
    keys = {o.series_key for o in result.observations}
    return f"benchmark key of ingredient sunflower_oil present in workbook series: {want in keys}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to the database (requires approval)")
    ap.add_argument("--no-db", action="store_true", help="parse only; no database access")
    ap.add_argument("--file", type=Path, default=DEFAULT_FILE)
    ap.add_argument("--samples", type=int, default=6)
    args = ap.parse_args(argv)

    result = parse_workbook(args.file)
    plan = None
    if not args.no_db:
        from src.db.preflight import connect
        try:
            with connect() as conn:  # read-only session
                plan = plan_prices(result.observations,
                                   fetch_existing_prices(conn, sorted({o.series_key for o in result.observations})))
        except Exception as exc:
            print(f"FAIL: database read error {type(exc).__name__} sqlstate={getattr(exc, 'sqlstate', None)}")
            return 1
    print(render(result, plan, args.samples))
    print(" ", check_benchmark_key(result))
    if plan and plan.revised:
        print("BLOCKER: existing rows differ from the workbook; investigate before loading.")
        return 2
    if not args.apply:
        print("DRY RUN: no changes made. Use --apply only after explicit approval.")
        return 0
    if args.no_db or plan is None:
        print("--apply cannot be combined with --no-db")
        return 2
    import os
    import psycopg
    from dotenv import load_dotenv
    load_dotenv()
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15, sslmode="require") as conn:
        apply(conn, result, plan)
    print("APPLIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
