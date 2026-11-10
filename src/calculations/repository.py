"""Read-only data access for the exposure model (parameterised SELECTs only; never writes).

Kept apart from the pure calculation modules so they stay testable without a database."""
from __future__ import annotations

from typing import Any, Mapping

from src.calculations.models import BenchmarkObservation, ContractInput, ForecastInput


def contract_from_mapping(m: Mapping[str, Any]) -> ContractInput:
    return ContractInput(
        contract_id=m["contract_id"], supplier_id=m["supplier_id"], ingredient_code=m["ingredient_code"],
        contract_type=m["contract_type"], currency=m["currency"], monthly_volume_t=m["committed_monthly_volume_t"],
        start_date=m["start_date"], end_date=m["end_date"], unit_price=m.get("unit_price"),
        index_series_key=m.get("index_series_key"), base_price=m.get("base_price"),
        index_base_value=m.get("index_base_value"), pass_through=m.get("pass_through"),
        fixed_premium=m.get("fixed_premium"))


def forecast_from_mapping(m: Mapping[str, Any]) -> ForecastInput:
    return ForecastInput(m["ingredient_code"], m["period_month"], m["forecast_volume_t"], m["forecast_version"],
                         m["generated_on"])


CONTRACT_SQL = ("SELECT contract_id, supplier_id, ingredient_code, contract_type, currency, committed_monthly_volume_t, "
                "start_date, end_date, unit_price, index_series_key, base_price, index_base_value, pass_through, "
                "fixed_premium FROM procurement.contracts WHERE ingredient_code = %s ORDER BY contract_id")
FORECAST_SQL = ("SELECT ingredient_code, period_month, forecast_volume_t, forecast_version, generated_on "
                "FROM procurement.demand_forecasts WHERE ingredient_code = %s ORDER BY period_month, generated_on")
BENCHMARK_KEY_SQL = "SELECT benchmark_series_key FROM procurement.ingredients WHERE ingredient_code = %s"
OBSERVATION_SQL = ("SELECT series_key, period_start, period_end, price, currency, unit, quality_flags, occurrence_no "
                   "FROM procurement.commodity_prices WHERE series_key = %s ORDER BY period_start, occurrence_no")


def _rows(conn, sql: str, param: str) -> list[dict[str, Any]]:
    cur = conn.execute(sql, (param,))
    names = [d.name for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def load_inputs(conn, ingredient: str):
    """Returns (benchmark_series_key | None, contracts, forecasts, observations). `conn` must be a read-only connection."""
    row = conn.execute(BENCHMARK_KEY_SQL, (ingredient,)).fetchone()
    key = row[0] if row else None
    contracts = [contract_from_mapping(r) for r in _rows(conn, CONTRACT_SQL, ingredient)]
    forecasts = [forecast_from_mapping(r) for r in _rows(conn, FORECAST_SQL, ingredient)]
    observations: list[BenchmarkObservation] = []
    if key:
        for r in _rows(conn, OBSERVATION_SQL, key):
            observations.append(BenchmarkObservation(r["series_key"], r["period_start"], r["period_end"], r["price"],
                                                     r["currency"], r["unit"], tuple(r["quality_flags"] or ()),
                                                     r["occurrence_no"]))
    return key, contracts, forecasts, observations


SUPPLIER_SQL = ("SELECT s.supplier_id, s.name, s.country, s.approval_status FROM procurement.suppliers s "
                "JOIN procurement.supplier_ingredients si USING (supplier_id) WHERE si.ingredient_code = %s "
                "ORDER BY s.supplier_id")


def load_suppliers(conn, ingredient: str) -> dict[str, dict[str, Any]]:
    """Suppliers (read-only) that supply the ingredient, keyed by supplier_id."""
    return {r["supplier_id"]: r for r in _rows(conn, SUPPLIER_SQL, ingredient)}
