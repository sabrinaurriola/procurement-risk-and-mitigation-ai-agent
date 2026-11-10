import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from src.ingestion import ec_oilseeds as ec
from tests.schema_utils import schema_columns

TODAY = date(2026, 11, 10)
KEY_STAGE = "EC_OILSEEDS|Crude sunflower oil|Standard|ES|Average|{stage}||EUR|tonne"
KEY = KEY_STAGE.format(stage="DEPPROC")
START = date(2026, 1, 5)  # a Monday


def row(i=0, price=1400.0, **kw):
    start = kw.pop("start", START + timedelta(days=7 * i))
    r = {"Product": "Crude sunflower oil", "Product Type": "Standard", "Member State": "Spain", "Marketing Year": "2025/2026",
         "Week": 20 + i, "Week Begin Date": datetime.combine(start, datetime.min.time()),
         "Week End Date": datetime.combine(start + timedelta(days=6), datetime.min.time()), "Market": "Average",
         "Market Stage": "DEPPROC", "Unit of Measure": "€/tonne", "Price (EUR)": price}
    r.update(kw)
    return r


def make(tmp_path, rows, name="wb.xlsx"):
    wb = Workbook()
    ws = wb.active
    ws.title = "MyWorkSheet-1"
    ws.append(ec.EXPECTED_HEADERS)
    for r in rows:
        ws.append([r[h] for h in ec.EXPECTED_HEADERS])
    path = tmp_path / name
    wb.save(path)
    return path


def parse(tmp_path, rows):
    return ec.parse_workbook(make(tmp_path, rows), today=TODAY)


def flags(res):
    return {o.source_row: o.quality_flags for o in res.observations}


def test_valid_rows_parse_and_normalise(tmp_path):
    res = parse(tmp_path, [row(0, 1400.5), row(1, 1410), row(2, 1420)])
    assert (res.read, len(res.observations), len(res.rejected)) == (3, 3, 0)
    o = res.observations[0]
    assert o.series_key == KEY
    assert (o.product, o.product_type, o.member_state, o.market, o.market_stage) == (
        "Crude sunflower oil", "Standard", "ES", "Average", "DEPPROC")
    assert (o.currency, o.unit, o.unit_raw) == ("EUR", "tonne", "€/tonne")
    assert o.price == Decimal("1400.5000") and o.price_raw == "1400.5"
    assert o.period_start == START and o.period_end == START + timedelta(days=6)
    assert (o.source_row, o.source_file, o.source_sheet, o.week_in_my, o.marketing_year) == (
        2, "wb.xlsx", "MyWorkSheet-1", 20, "2025/2026")
    assert len(o.row_hash) == 64 and o.raw_values["Member State"] == "Spain"
    assert all(o.quality_flags == [] for o in res.observations)


def test_date_forms_are_normalised(tmp_path):
    r1 = row(0)
    r2 = row(1, **{"Week Begin Date": 46034, "Week End Date": 46040})  # Excel serials 2026-01-12..18
    r3 = row(2, **{"Week Begin Date": "2026-01-19", "Week End Date": "2026-01-25"})
    res = parse(tmp_path, [r1, r2, r3])
    assert [o.period_start for o in res.observations] == [date(2026, 1, 5), date(2026, 1, 12), date(2026, 1, 19)]
    assert all(o.quality_flags == [] for o in res.observations)


def test_missing_and_invalid_prices_are_flagged_not_dropped(tmp_path):
    res = parse(tmp_path, [row(0, None), row(1, "n/a"), row(2, 0), row(3, -5), row(4, 1400)])
    assert len(res.observations) == 5
    f = flags(res)
    assert f[2] == [ec.F_NONNUM] and f[3] == [ec.F_NONNUM] and f[4] == [ec.F_NONPOS] and f[5] == [ec.F_NONPOS]
    assert [o.price for o in res.observations][:4] == [None] * 4
    assert res.observations[1].price_raw == "n/a" and res.observations[3].price_raw == "-5"


def test_duplicate_weeks_kept_with_occurrence_and_flags(tmp_path):
    a = row(0, 1400, **{"Marketing Year": "2025/2026", "Week": 53})
    b = row(0, 1400, **{"Marketing Year": "2026/2027", "Week": 1})
    c = row(1, 1500)
    d = row(1, 1555, **{"Marketing Year": "2026/2027", "Week": 2})
    res = parse(tmp_path, [a, b, c, d])
    assert len(res.observations) == 4
    assert [o.occurrence_no for o in res.observations] == [1, 2, 1, 2]
    f = flags(res)
    assert f[2] == f[3] == [ec.F_DUP]
    assert f[4] == f[5] == sorted([ec.F_DUP, ec.F_DUP_CONFLICT])
    assert len({o.natural_key for o in res.observations}) == 4 and len({o.row_hash for o in res.observations}) == 4
    assert ec.summarize(res)["duplicate_groups"] == 2


def test_outlier_flagged_but_value_untouched(tmp_path):
    rows = [row(i, 1400 + i) for i in range(12)]
    rows[6] = row(6, 140)
    res = parse(tmp_path, rows)
    out = [o for o in res.observations if ec.F_OUTLIER in o.quality_flags]
    assert len(out) == 1 and out[0].price == Decimal("140.0000") and out[0].source_row == 8


def test_gap_overlap_length_and_future_flags(tmp_path):
    gap = row(0), row(2)                                              # one week missing
    overlap = row(0), row(1, start=START + timedelta(days=3))         # starts inside previous week
    long = (row(0, **{"Week End Date": datetime.combine(START + timedelta(days=9), datetime.min.time())}),)
    future = (row(0, start=TODAY + timedelta(days=30)),)
    assert ec.F_GAP in flags(parse(tmp_path, list(gap)))[3]
    assert ec.F_OVERLAP in flags(parse(tmp_path, list(overlap)))[3]
    assert ec.F_LENGTH in flags(parse(tmp_path, list(long)))[2]
    assert ec.F_FUTURE in flags(parse(tmp_path, list(future)))[2]


def test_invalid_rows_are_rejected_with_reasons_and_valid_rows_still_load(tmp_path):
    bad = [
        row(0, **{"Week Begin Date": "not a date"}),
        row(1, **{"Member State": "Atlantis"}),
        row(2, **{"Unit of Measure": "$/tonne"}),
        row(3, **{"Product": ""}),
        row(4, **{"Week End Date": datetime(2020, 1, 1)}),
        row(5, 1400),
    ]
    res = parse(tmp_path, bad)
    assert res.read == 6 and len(res.observations) == 1 and len(res.rejected) == 5
    reasons = " | ".join(" ".join(r.reasons) for r in res.rejected)
    for expected in ("unparseable date", "unknown Member State", "unrecognised unit", "missing Product", "before"):
        assert expected in reasons
    assert [r.source_row for r in res.rejected] == [2, 3, 4, 5, 6]


def test_distinct_series_are_never_merged(tmp_path):
    res = parse(tmp_path, [row(0, 1400), row(1, 1410, **{"Market Stage": "ex-factory"}), row(2, 1420, **{"Member State": "France"})])
    assert {o.series_key for o in res.observations} == {
        KEY, KEY_STAGE.format(stage="ex-factory"), KEY.replace("|ES|", "|FR|")}


def test_unexpected_headers_rejected(tmp_path):
    wb = Workbook()
    wb.active.append(["a", "b"])
    p = tmp_path / "bad.xlsx"
    wb.save(p)
    with pytest.raises(ValueError, match="unexpected workbook headers"):
        ec.parse_workbook(p, today=TODAY)


def test_blank_rows_skipped_and_counted(tmp_path):
    path = make(tmp_path, [row(0), row(1)])
    wb = __import__("openpyxl").load_workbook(path)
    wb.active.append([None] * 11)
    wb.save(path)
    res = ec.parse_workbook(path, today=TODAY)
    assert res.read == 2 and res.blank_skipped == 1


def test_plan_is_idempotent_and_detects_revisions(tmp_path):
    res = parse(tmp_path, [row(i, 1400 + i) for i in range(5)])
    first = ec.plan_prices(res.observations, {})
    assert (first.insert, first.skipped_existing, first.revised) == (5, 0, [])
    existing = {o.natural_key: o.row_hash for o in res.observations}  # database after a first load
    again = ec.plan_prices(res.observations, existing)
    assert (again.insert, again.skipped_existing, again.revised) == (0, 5, [])
    existing[res.observations[2].natural_key] = "x" * 64            # a source value changed upstream
    changed = ec.plan_prices(res.observations, existing)
    assert changed.insert == 0 and len(changed.revised) == 1
    with pytest.raises(RuntimeError, match="REVISED"):
        ec.apply(None, res, changed)  # refuses before touching the database


def test_row_hash_is_stable_across_parses(tmp_path):
    rows = [row(i, 1400 + i) for i in range(3)]
    a = ec.parse_workbook(make(tmp_path, rows, "a.xlsx"), today=TODAY)
    b = ec.parse_workbook(make(tmp_path, rows, "b.xlsx"), today=TODAY)
    assert [o.row_hash for o in a.observations] == [o.row_hash for o in b.observations]


def test_insert_columns_match_approved_schema():
    schema = schema_columns()["commodity_prices"]
    assert set(ec.PRICE_COLUMNS) <= schema
    assert schema - set(ec.PRICE_COLUMNS) == {"id", "ingested_at"}  # only generated columns are omitted
    assert "ON CONFLICT (series_key, period_start, occurrence_no) DO NOTHING" in ec.insert_prices_sql()
    assert "DELETE" not in ec.insert_prices_sql().upper()
    assert {"name", "publisher", "file_name", "sha256", "licence_note"} <= schema_columns()["data_sources"]


def test_to_row_maps_every_column(tmp_path):
    o = parse(tmp_path, [row(0, 1400)]).observations[0]
    r = ec.to_row(o, 1, 2)
    assert set(r) == set(ec.PRICE_COLUMNS) and r["source_id"] == 1 and r["run_id"] == 2 and r["quality_flags"] == []


def test_cli_dry_run_leaves_workbook_untouched(tmp_path, capsys):
    path = make(tmp_path, [row(0), row(1)])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert ec.main(["--no-db", "--file", str(path)]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert ec.main(["--apply", "--no-db", "--file", str(path)]) == 2


REAL = Path(__file__).resolve().parents[1] / "data" / "raw" / "European_Commission_Oilseeds_Prices.xlsx"


@pytest.mark.skipif(not REAL.exists(), reason="raw workbook is untracked and not present")
def test_real_workbook_smoke():
    res = ec.parse_workbook(REAL, today=TODAY)
    assert (res.read, len(res.observations), len(res.rejected)) == (308, 308, 0)
    assert res.sha256 == "1259519cb22f1e3b53125e87da2a114c3bba11645bf6d98fa7d82cc99c60e407"
    keys = {o.series_key for o in res.observations}
    assert KEY in keys and KEY_STAGE.format(stage="ex-factory") in keys
    latest = max((o for o in res.observations if o.series_key == KEY), key=lambda o: o.period_start)
    assert (latest.period_start, latest.price) == (date(2026, 10, 19), Decimal("1457.4000"))
    assert all(str(o.price) == str(Decimal(o.price_raw).quantize(Decimal("0.0001"))) for o in res.observations if o.price)
    assert len({o.natural_key for o in res.observations}) == 308
