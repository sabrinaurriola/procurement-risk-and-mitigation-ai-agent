"""Internal data access for the agent: read-only PostgreSQL, or static data for tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from src.calculations.models import BenchmarkObservation, ContractInput, ForecastInput


@dataclass(frozen=True)
class SupplierInfo:
    supplier_id: str
    name: str
    country: str
    approval_status: str


@dataclass(frozen=True)
class InternalData:
    benchmark_key: str | None
    contracts: list[ContractInput]
    forecasts: list[ForecastInput]
    observations: list[BenchmarkObservation]
    suppliers: dict[str, SupplierInfo] = field(default_factory=dict)


class DataSource(Protocol):
    def load(self, ingredient: str) -> InternalData: ...


class PostgresData:
    """Loads inputs through a READ ONLY session (see src.db.preflight.connect); never writes."""

    def load(self, ingredient: str) -> InternalData:
        from src.calculations.repository import load_inputs, load_suppliers
        from src.db.preflight import connect
        with connect() as conn:
            key, contracts, forecasts, observations = load_inputs(conn, ingredient)
            suppliers = {k: SupplierInfo(v["supplier_id"], v["name"], v["country"], v["approval_status"])
                         for k, v in load_suppliers(conn, ingredient).items()}
        return InternalData(key, contracts, forecasts, observations, suppliers)


class StaticData:
    def __init__(self, data: InternalData | None = None, error: Exception | None = None):
        self._data, self._error = data, error

    def load(self, ingredient: str) -> InternalData:
        if self._error:
            raise self._error
        return self._data
