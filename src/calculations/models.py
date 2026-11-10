"""Plain, immutable inputs for the deterministic exposure model (no database, no LLM, no framework imports)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class ContractInput:
    """A supply contract. Optional price fields are None when missing so that data gaps surface as 'unavailable'."""
    contract_id: str
    supplier_id: str
    ingredient_code: str
    contract_type: str                      # 'fixed' | 'index_linked'
    currency: str
    monthly_volume_t: Decimal | None
    start_date: date | None
    end_date: date | None
    unit_price: Decimal | None = None       # fixed contracts
    index_series_key: str | None = None     # index-linked contracts
    base_price: Decimal | None = None
    index_base_value: Decimal | None = None
    pass_through: Decimal | None = None
    fixed_premium: Decimal | None = None


@dataclass(frozen=True)
class ForecastInput:
    ingredient_code: str
    period_month: date                      # first day of the month
    volume_t: Decimal
    version: str
    generated_on: date


@dataclass(frozen=True)
class BenchmarkObservation:
    """One market price observation, with the quality flags stored by the ingestion pipeline."""
    series_key: str
    period_start: date
    period_end: date
    price: Decimal | None
    currency: str
    unit: str
    quality_flags: tuple[str, ...] = ()
    occurrence_no: int = 1
