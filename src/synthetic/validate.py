"""Offline validation of the synthetic Bakery Foods Inc. CSV files (no database access)."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from src.ingestion.series_key import parse_series_key


class Ingredient(BaseModel):
    ingredient_code: str
    display_name: str
    unit: str
    benchmark_series_key: str
    benchmark_note: str

    @field_validator("benchmark_series_key")
    @classmethod
    def _full_key(cls, v):
        parse_series_key(v)
        return v


class Supplier(BaseModel):
    supplier_id: str
    name: str
    country: str
    ingredients: list[str]
    approval_status: Literal["approved", "conditional", "unverified", "blocked"]
    notes: str
    is_synthetic: bool

    @field_validator("ingredients", mode="before")
    @classmethod
    def _split(cls, v):
        return [x.strip() for x in v.split("|") if x.strip()] if isinstance(v, str) else v

    @model_validator(mode="after")
    def _fictional(self):
        if not self.is_synthetic or "(fictional)" not in self.name:
            raise ValueError(f"{self.supplier_id}: must be flagged synthetic and named '(fictional)'")
        if not self.ingredients:
            raise ValueError(f"{self.supplier_id}: no ingredients")
        return self


class Contract(BaseModel):
    contract_id: str
    supplier_id: str
    ingredient_code: str
    contract_type: Literal["fixed", "index_linked"]
    currency: str
    committed_monthly_volume_t: Decimal
    start_date: date
    end_date: date
    unit_price: Optional[Decimal] = None
    index_series_key: Optional[str] = None
    base_price: Optional[Decimal] = None
    index_base_value: Optional[Decimal] = None
    pass_through: Optional[Decimal] = None
    fixed_premium: Optional[Decimal] = None
    notes: str = ""
    is_synthetic: bool

    @field_validator("unit_price", "index_series_key", "base_price", "index_base_value",
                     "pass_through", "fixed_premium", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        return None if v == "" else v

    @model_validator(mode="after")
    def _rules(self):
        cid = self.contract_id
        if not self.is_synthetic:
            raise ValueError(f"{cid}: must be flagged synthetic")
        if self.committed_monthly_volume_t <= 0:
            raise ValueError(f"{cid}: volume must be > 0")
        if self.end_date <= self.start_date:
            raise ValueError(f"{cid}: end_date must be after start_date")
        index_fields = [self.index_series_key, self.base_price, self.index_base_value,
                        self.pass_through, self.fixed_premium]
        if self.contract_type == "fixed":
            if self.unit_price is None or self.unit_price <= 0:
                raise ValueError(f"{cid}: fixed contract needs a positive unit_price")
            if any(f is not None for f in index_fields):
                raise ValueError(f"{cid}: fixed contract must not carry index fields")
        else:
            if self.unit_price is not None or any(f is None for f in index_fields):
                raise ValueError(f"{cid}: index_linked contract needs all index fields and no unit_price")
            if self.base_price <= 0 or self.index_base_value <= 0:
                raise ValueError(f"{cid}: base_price and index_base_value must be > 0")
            if not (Decimal(0) <= self.pass_through <= Decimal(1)):
                raise ValueError(f"{cid}: pass_through must be within [0, 1]")
            try:
                sk = parse_series_key(self.index_series_key)
            except ValueError as exc:
                raise ValueError(f"{cid}: {exc}") from exc
            if sk.currency != self.currency or sk.unit != "tonne":
                raise ValueError(f"{cid}: index series currency/unit does not match contract currency")
        return self


class Forecast(BaseModel):
    ingredient_code: str
    period_month: date
    forecast_volume_t: Decimal
    forecast_version: str
    generated_on: date
    is_synthetic: bool

    @model_validator(mode="after")
    def _rules(self):
        if self.period_month.day != 1:
            raise ValueError(f"{self.ingredient_code}: period_month must be the first day of a month")
        if self.forecast_volume_t <= 0 or not self.is_synthetic:
            raise ValueError(f"{self.ingredient_code} {self.period_month}: volume > 0 and synthetic flag required")
        return self


def _read(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_dir(directory: Path) -> dict[str, list[dict]]:
    return {n: _read(directory / f"{n}.csv") for n in ("ingredients", "suppliers", "contracts", "demand_forecasts")}


MODELS: dict[str, type[BaseModel]] = {
    "ingredients": Ingredient, "suppliers": Supplier, "contracts": Contract, "demand_forecasts": Forecast,
}


@dataclass
class Collected:
    valid: dict[str, list[BaseModel]]
    errors: list[str]
    read: dict[str, int]
    invalid: dict[str, int]


def _clean(msg: str) -> str:
    return msg.removeprefix("Value error, ")


def collect(raw: dict[str, list[dict]]) -> Collected:
    """Validate every row independently (CSV line numbers start at 2) and then cross-file consistency.
    Never raises for bad data; returns every problem found."""
    errors: list[str] = []
    valid: dict[str, list[BaseModel]] = {}
    invalid: dict[str, int] = {}
    for table, model in MODELS.items():
        ok: list[BaseModel] = []
        bad = 0
        for line, row in enumerate(raw[table], start=2):
            try:
                ok.append(model(**row))
            except ValidationError as exc:
                bad += 1
                for e in exc.errors():
                    where = ".".join(str(p) for p in e["loc"])
                    errors.append(f"{table}.csv line {line}: {(where + ': ') if where else ''}{_clean(e['msg'])}")
            except TypeError as exc:  # e.g. extra/unnamed CSV columns
                bad += 1
                errors.append(f"{table}.csv line {line}: malformed row ({exc})")
        valid[table], invalid[table] = ok, bad
    ingredients, suppliers = valid["ingredients"], valid["suppliers"]
    contracts, forecasts = valid["contracts"], valid["demand_forecasts"]
    codes = {i.ingredient_code for i in ingredients}
    keys = {i.benchmark_series_key for i in ingredients}
    by_supplier = {s.supplier_id: s for s in suppliers}
    if len(by_supplier) != len(suppliers):
        errors.append("duplicate supplier_id")
    if len({c.contract_id for c in contracts}) != len(contracts):
        errors.append("duplicate contract_id")
    for s in suppliers:
        errors += [f"{s.supplier_id}: unknown ingredient {i}" for i in s.ingredients if i not in codes]
    for c in contracts:
        sup = by_supplier.get(c.supplier_id)
        if sup is None:
            errors.append(f"{c.contract_id}: unknown supplier {c.supplier_id}")
        elif c.ingredient_code not in sup.ingredients:
            errors.append(f"{c.contract_id}: supplier {c.supplier_id} does not supply {c.ingredient_code}")
        if c.ingredient_code not in codes:
            errors.append(f"{c.contract_id}: unknown ingredient {c.ingredient_code}")
        if c.contract_type == "index_linked" and c.index_series_key not in keys:
            errors.append(f"{c.contract_id}: index series is not a declared ingredient benchmark")
    seen = set()
    for f in forecasts:
        if f.ingredient_code not in codes:
            errors.append(f"forecast: unknown ingredient {f.ingredient_code}")
        k = (f.ingredient_code, f.period_month, f.forecast_version)
        if k in seen:
            errors.append(f"duplicate forecast {k}")
        seen.add(k)
    for code in codes:
        months = sorted(f.period_month for f in forecasts if f.ingredient_code == code)
        if len(months) != 3:
            errors.append(f"{code}: expected 3 monthly forecasts, found {len(months)}")
        elif any((b.year * 12 + b.month) - (a.year * 12 + a.month) != 1 for a, b in zip(months, months[1:])):
            errors.append(f"{code}: forecast months are not consecutive")
    return Collected(valid, errors, {t: len(raw[t]) for t in MODELS}, invalid)


def validate(raw: dict[str, list[dict]]) -> dict[str, list[BaseModel]]:
    """Validate rows and cross-file consistency; raise ValueError listing every problem."""
    result = collect(raw)
    if result.errors:
        raise ValueError("; ".join(result.errors))
    return result.valid


if __name__ == "__main__":
    data = validate(load_dir(Path(__file__).resolve().parents[2] / "data" / "synthetic"))
    print({k: len(v) for k, v in data.items()}, "-> synthetic CSV validation PASS")
