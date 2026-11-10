"""Offline tests: mandatory contract fields, series_key validation, SQL static review, migration planning."""
import copy
import re
from pathlib import Path

import pytest

from src.db.migrate import migration_files, plan
from src.ingestion.series_key import build_series_key, parse_series_key
from src.synthetic.validate import load_dir, validate

ROOT = Path(__file__).resolve().parents[1]
SQL = (ROOT / "sql" / "001_schema.sql").read_text(encoding="utf-8")
GOOD_KEY = "EC_OILSEEDS|Crude sunflower oil|Standard|ES|Average|DEPPROC||EUR|tonne"


@pytest.fixture
def raw():
    return copy.deepcopy(load_dir(ROOT / "data" / "synthetic"))


# ── mandatory contract fields (validator side) ──
@pytest.mark.parametrize("bad", ["", "0", "-5"])
def test_fixed_contract_requires_positive_price(raw, bad):
    raw["contracts"][0]["unit_price"] = bad
    with pytest.raises(ValueError):
        validate(raw)


@pytest.mark.parametrize("field", ["index_series_key", "base_price", "index_base_value", "pass_through", "fixed_premium"])
def test_index_contract_requires_every_index_field(raw, field):
    raw["contracts"][2][field] = ""
    with pytest.raises(ValueError):
        validate(raw)


@pytest.mark.parametrize("field,value", [("base_price", "0"), ("index_base_value", "-1"), ("pass_through", "1.2"), ("pass_through", "-0.1")])
def test_index_contract_rejects_out_of_range(raw, field, value):
    raw["contracts"][2][field] = value
    with pytest.raises(ValueError):
        validate(raw)


@pytest.mark.parametrize("field,value", [("index_series_key", GOOD_KEY), ("base_price", "1"), ("pass_through", "0.5"), ("fixed_premium", "0")])
def test_fixed_contract_rejects_index_fields(raw, field, value):
    raw["contracts"][0][field] = value
    with pytest.raises(ValueError):
        validate(raw)


def test_index_contract_rejects_unit_price(raw):
    raw["contracts"][2]["unit_price"] = "1500"
    with pytest.raises(ValueError):
        validate(raw)


# ── SQL static review: the CHECK must not be NULL-permissive ──
def _contract_check() -> str:
    start = SQL.index("CONSTRAINT contracts_type_fields_ck CHECK (")
    depth, i = 0, SQL.index("(", start)
    for j in range(i, len(SQL)):
        depth += SQL[j] == "("
        depth -= SQL[j] == ")"
        if depth == 0:
            return SQL[i:j + 1]
    raise AssertionError("unbalanced CHECK")


def test_sql_contract_check_guards_every_mandatory_field():
    check = _contract_check()
    for col in ("unit_price", "index_series_key", "base_price", "index_base_value", "pass_through", "fixed_premium"):
        assert f"{col} IS NOT NULL" in check, f"{col} lacks an explicit IS NOT NULL guard"
        assert f"{col} IS NULL" in check, f"{col} is not required to be NULL on the other contract type"
    # the old NULL-permissive pattern must be gone
    assert "contract_type <>" not in check
    assert "BETWEEN" not in check


def test_sql_comparisons_always_follow_a_not_null_guard():
    check = _contract_check()
    for col in ("unit_price", "base_price", "index_base_value", "pass_through"):
        for m in re.finditer(rf"{col} (>|>=|<=) ", check):
            assert f"{col} IS NOT NULL" in check[:m.start()].split(" OR ")[-1] or f"{col} IS NOT NULL" in check[:m.start()]


def test_sql_migration_description_and_vector_location():
    assert "10 tables" in SQL
    assert len(re.findall(r"^CREATE TABLE ", SQL, re.M)) == 10
    assert "extensions.vector(1536)" in SQL
    assert "WITH SCHEMA extensions" in SQL
    assert "CREATE TABLE IF NOT EXISTS" not in SQL


# ── series_key: full-key validation ──
def test_valid_series_key_roundtrip():
    sk = parse_series_key(GOOD_KEY)
    assert (sk.source, sk.member_state, sk.currency, sk.unit, sk.quotation_basis) == ("EC_OILSEEDS", "ES", "EUR", "tonne", "")
    assert build_series_key(source="EC_OILSEEDS", product="Crude sunflower oil", product_type="Standard", member_state="ES",
                            market="Average", market_stage="DEPPROC", currency="EUR", unit="tonne") == GOOD_KEY


@pytest.mark.parametrize("bad", [
    GOOD_KEY + "|extra",                                                      # too many fields
    "EC_OILSEEDS|Crude sunflower oil|ES|EUR|tonne",                           # too few fields
    GOOD_KEY.replace("EC_OILSEEDS", "UNKNOWN_SRC"),                           # unknown source
    GOOD_KEY.replace("|ES|", "|Spain|"),                                      # bad member state
    GOOD_KEY.replace("|EUR|", "|eur|"),                                       # bad currency
    GOOD_KEY.replace("|tonne", "|kg"),                                        # bad unit
    GOOD_KEY.replace("Crude sunflower oil", ""),                              # missing product
    GOOD_KEY.replace("DEPPROC", ""),                                          # missing market stage
    GOOD_KEY.replace("Crude sunflower oil", " Crude sunflower oil"),          # whitespace
])
def test_invalid_series_keys_rejected(bad):
    with pytest.raises(ValueError):
        parse_series_key(bad)


def test_contract_with_malformed_index_key_rejected(raw):
    raw["contracts"][2]["index_series_key"] = "EC_OILSEEDS|Crude sunflower oil|EUR|tonne"
    with pytest.raises(ValueError, match="series_key"):
        validate(raw)


def test_malformed_ingredient_benchmark_key_rejected(raw):
    raw["ingredients"][0]["benchmark_series_key"] = "garbage|EUR|tonne"
    with pytest.raises(ValueError):
        validate(raw)


# ── migration planning (pure function over inspected state) ──
def _info(**kw):
    base = {"schemas": ["public", "extensions"], "procurement_tables": [], "vector_installed": [],
            "vector_available": [("0.8.2",)]}
    base.update(kw)
    return base


def test_fresh_database_plans_001_without_blockers():
    pending, problems = plan(_info(), set(), migration_files())
    assert [v for v, _ in pending] == ["001"] and problems == []


def test_recorded_version_is_skipped():
    pending, problems = plan(_info(schemas=["public", "extensions", "procurement"], procurement_tables=["schema_migrations"]),
                             {"001"}, migration_files())
    assert pending == [] and problems == []


def test_unrecorded_existing_tables_block():
    _, problems = plan(_info(schemas=["public", "extensions", "procurement"], procurement_tables=["contracts"]), set(), migration_files())
    assert any("unrecorded" in p or "no schema_migrations record" in p for p in problems)


def test_vector_in_unexpected_schema_blocks():
    _, problems = plan(_info(vector_installed=[("0.8.2", "public")]), set(), migration_files())
    assert any("pgvector is installed in schema" in p for p in problems)


def test_vector_unavailable_blocks():
    _, problems = plan(_info(vector_available=[]), set(), migration_files())
    assert any("not available" in p for p in problems)
