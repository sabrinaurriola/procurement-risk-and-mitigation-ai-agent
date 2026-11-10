import copy
import hashlib
import shutil
from pathlib import Path

import pytest

from src.ingestion import load_synthetic as ls
from src.synthetic.validate import collect, load_dir
from tests.schema_utils import schema_columns

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"


@pytest.fixture
def raw():
    return copy.deepcopy(load_dir(DATA))


@pytest.fixture
def rows(raw):
    return ls.build_rows(collect(raw).valid)


def as_existing(rows):
    """Simulate a database that already holds exactly these rows."""
    return {s.table: {tuple(r[k] for k in s.key): dict(r) for r in rows[s.table]} for s in ls.SPECS}


def test_baseline_mapping_counts(rows):
    assert {t: len(r) for t, r in rows.items()} == {
        "ingredients": 2, "suppliers": 5, "supplier_ingredients": 5, "contracts": 5, "demand_forecasts": 6}


def test_supplier_ingredients_exploded_and_consistent(rows):
    pairs = {(r["supplier_id"], r["ingredient_code"]) for r in rows["supplier_ingredients"]}
    assert ("SUP-001", "sunflower_oil") in pairs and ("SUP-004", "wheat") in pairs
    for c in rows["contracts"]:
        assert (c["supplier_id"], c["ingredient_code"]) in pairs


def test_load_order_is_dependency_order():
    order = [s.table for s in ls.SPECS]
    assert order.index("ingredients") < order.index("supplier_ingredients") < order.index("contracts")
    assert order.index("suppliers") < order.index("supplier_ingredients")
    assert order.index("ingredients") < order.index("demand_forecasts")


def test_every_mapped_row_is_synthetic(rows):
    for t in ("suppliers", "contracts", "demand_forecasts"):
        assert all(r["is_synthetic"] is True for r in rows[t])


def test_empty_database_plans_all_inserts(rows):
    plans = ls.plan_changes(rows, {})
    assert sum(p.insert for p in plans) == 2 + 5 + 5 + 5 + 6
    assert sum(p.update + p.unchanged + p.orphans for p in plans) == 0


def test_rerun_is_idempotent(rows):
    plans = ls.plan_changes(rows, as_existing(rows))
    assert sum(p.insert + p.update + p.orphans for p in plans) == 0
    assert sum(p.unchanged for p in plans) == 23


def test_changed_csv_value_is_an_update_not_an_insert(raw):
    existing = as_existing(ls.build_rows(collect(raw).valid))
    raw["contracts"][0]["unit_price"] = "1530.00"
    plans = {p.table: p for p in ls.plan_changes(ls.build_rows(collect(raw).valid), existing)}
    assert plans["contracts"].update == 1 and plans["contracts"].changed_columns == ("unit_price",)
    assert plans["contracts"].insert == 0


def test_database_only_rows_are_reported_never_deleted(rows):
    existing = as_existing(rows)
    existing["suppliers"][("SUP-999",)] = {"supplier_id": "SUP-999"}
    plan = {p.table: p for p in ls.plan_changes(rows, existing)}["suppliers"]
    assert plan.orphans == 1
    assert all("DELETE" not in ls.upsert_sql(s).upper() for s in ls.SPECS)


def test_invalid_records_give_line_numbered_messages(raw):
    raw["contracts"][0]["unit_price"] = ""
    raw["suppliers"][1]["approval_status"] = "maybe"
    c = collect(raw)
    assert c.invalid["contracts"] == 1 and c.invalid["suppliers"] == 1
    assert any(e.startswith("contracts.csv line 2:") and "positive unit_price" in e for e in c.errors)
    assert any(e.startswith("suppliers.csv line 3:") for e in c.errors)
    assert len(c.valid["contracts"]) == 4


def test_referential_integrity_errors(raw):
    raw["contracts"][0]["supplier_id"] = "SUP-999"
    raw["demand_forecasts"][0]["ingredient_code"] = "sugar"
    errs = " ".join(collect(raw).errors)
    assert "unknown supplier SUP-999" in errs and "unknown ingredient sugar" in errs


def test_supplier_ingredient_mismatch_detected(raw):
    raw["contracts"][3]["ingredient_code"] = "sunflower_oil"  # SUP-004 only supplies wheat
    assert any("does not supply" in e for e in collect(raw).errors)


def test_unflagged_synthetic_rejected(raw):
    raw["demand_forecasts"][0]["is_synthetic"] = "false"
    assert any("synthetic" in e for e in collect(raw).errors)


def test_date_and_quantity_rules(raw):
    raw["contracts"][0]["end_date"] = "2026-06-30"       # before start
    raw["contracts"][1]["committed_monthly_volume_t"] = "0"
    raw["demand_forecasts"][0]["period_month"] = "2026-12-15"
    errs = " ".join(collect(raw).errors)
    assert "end_date must be after start_date" in errs and "volume must be > 0" in errs and "first day of a month" in errs


def test_sql_generation_matches_keys():
    sql = {s.table: ls.upsert_sql(s) for s in ls.SPECS}
    assert "ON CONFLICT (supplier_id, ingredient_code) DO NOTHING" in sql["supplier_ingredients"]
    assert "ON CONFLICT (ingredient_code, period_month, forecast_version) DO UPDATE" in sql["demand_forecasts"]
    assert "IS DISTINCT FROM" in sql["contracts"]


def test_loader_columns_exist_in_approved_schema():
    schema = schema_columns()
    for s in ls.SPECS:
        assert set(s.cols) <= schema[s.table], (s.table, set(s.cols) - schema[s.table])
        assert set(s.key) <= set(s.cols)


def test_cli_dry_run_makes_no_changes_and_leaves_csvs_untouched(capsys):
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in DATA.glob("*.csv")}
    assert ls.main(["--no-db"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in DATA.glob("*.csv")}


def test_cli_rejects_invalid_data_and_blocks_apply(tmp_path, capsys):
    for p in DATA.glob("*.csv"):
        shutil.copy(p, tmp_path / p.name)
    bad = (tmp_path / "contracts.csv")
    bad.write_text(bad.read_text(encoding="utf-8").replace("1520.00", "", 1), encoding="utf-8")
    assert ls.main(["--no-db", "--data-dir", str(tmp_path)]) == 2
    out = capsys.readouterr().out
    assert "REJECTED" in out and "contracts.csv line 2" in out
    assert ls.main(["--apply", "--no-db"]) == 2  # apply needs a database
