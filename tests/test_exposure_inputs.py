"""Mapping of the real synthetic CSV data into the model, repository SQL review, and the opt-in database check."""
import os
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from src.calculations import repository as repo
from src.calculations.exposure import calculate_exposure, round_money
from src.synthetic.validate import collect, load_dir
from tests.calc_fixtures import AS_OF, DEC, FEB, JAN, KEY, MONTHS, benchmark_history
from tests.schema_utils import schema_columns

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"


def csv_inputs():
    valid = collect(load_dir(DATA)).valid
    contracts = [repo.contract_from_mapping(c.model_dump()) for c in valid["contracts"]]
    forecasts = [repo.forecast_from_mapping(f.model_dump()) for f in valid["demand_forecasts"]]
    return contracts, forecasts


def test_real_synthetic_csv_reproduces_the_approved_december_example():
    contracts, forecasts = csv_inputs()
    res = calculate_exposure("sunflower_oil", MONTHS, contracts, forecasts, benchmark_history(), KEY, AS_OF)
    assert res.status == "available"
    dec = res.months[0]
    assert round_money(dec.baseline_cost) == D("636650.40") and round_money(dec.scenarios[D("0.10")].incremental) == D("28565.04")
    assert res.months[1].uncovered_t > res.months[0].uncovered_t < res.months[2].uncovered_t   # CT-002 expiry widens the gap


def test_wheat_is_unavailable_without_a_loaded_benchmark():
    contracts, forecasts = csv_inputs()
    wheat_key = "EC_CEREALS|Breadmaking common wheat|BLTPAN|ES||DEPSILO|Navarra|EUR|tonne"
    res = calculate_exposure("wheat", MONTHS, contracts, forecasts, [], wheat_key, AS_OF)
    assert res.status == "unavailable" and res.totals is None


def test_repository_sql_is_read_only_and_columns_exist_in_schema():
    schema = schema_columns()
    for sql in (repo.CONTRACT_SQL, repo.FORECAST_SQL, repo.BENCHMARK_KEY_SQL, repo.OBSERVATION_SQL):
        assert sql.lstrip().upper().startswith("SELECT")
        assert not any(w in sql.upper() for w in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE"))
    assert "committed_monthly_volume_t" in schema["contracts"] and "quality_flags" in schema["commodity_prices"]


@pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to read from Supabase")
def test_database_inputs_reproduce_the_same_results_read_only():
    from src.db.preflight import connect
    with connect() as conn:
        key, contracts, forecasts, observations = repo.load_inputs(conn, "sunflower_oil")
    assert key == KEY and len(contracts) == 3 and len(forecasts) == 3 and len(observations) == 293
    res = calculate_exposure("sunflower_oil", MONTHS, contracts, forecasts, observations, key, date(2026, 11, 10))
    assert res.status == "available" and res.benchmark.price == D("1457.40")
    assert round_money(res.months[0].scenarios[D("0.10")].incremental) == D("28565.04")
    assert res.benchmark.excluded.get("OUTLIER_VS_MEDIAN") == 2   # the two 2022 outliers are excluded, not deleted
