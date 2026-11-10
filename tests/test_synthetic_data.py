import copy
from pathlib import Path

import pytest

from src.synthetic.validate import load_dir, validate

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"


@pytest.fixture
def raw():
    return copy.deepcopy(load_dir(DATA))


def test_baseline_is_valid_and_complete(raw):
    data = validate(raw)
    assert len(data["suppliers"]) == 5
    assert len(data["contracts"]) == 5
    assert len(data["demand_forecasts"]) == 6
    assert {c.contract_type for c in data["contracts"]} == {"fixed", "index_linked"}


def test_every_record_is_flagged_synthetic(raw):
    for table in ("suppliers", "contracts", "demand_forecasts"):
        assert all(r["is_synthetic"] == "true" for r in raw[table])


def test_missing_fixed_price_rejected(raw):
    raw["contracts"][0]["unit_price"] = ""
    with pytest.raises(ValueError, match="positive unit_price"):
        validate(raw)


def test_incomplete_index_contract_rejected(raw):
    raw["contracts"][2]["pass_through"] = ""
    with pytest.raises(ValueError, match="all index fields"):
        validate(raw)


def test_supplier_must_supply_contract_ingredient(raw):
    raw["contracts"][0]["ingredient_code"] = "wheat"
    with pytest.raises(ValueError, match="does not supply"):
        validate(raw)


def test_unknown_supplier_rejected(raw):
    raw["contracts"][0]["supplier_id"] = "SUP-999"
    with pytest.raises(ValueError, match="unknown supplier"):
        validate(raw)


def test_missing_forecast_month_rejected(raw):
    raw["demand_forecasts"].pop(0)
    with pytest.raises(ValueError, match="expected 3 monthly forecasts"):
        validate(raw)


def test_index_currency_mismatch_rejected(raw):
    raw["contracts"][2]["currency"] = "USD"
    with pytest.raises(ValueError, match="currency/unit"):
        validate(raw)


def test_unflagged_synthetic_record_rejected(raw):
    raw["suppliers"][0]["is_synthetic"] = "false"
    with pytest.raises(ValueError, match="synthetic"):
        validate(raw)
