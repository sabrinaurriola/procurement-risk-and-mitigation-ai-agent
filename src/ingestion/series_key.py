"""Canonical commodity series keys.

series_key = source|product|product_type|member_state|market|market_stage|quotation_basis|currency|unit

Distinct quotations (different stage, quotation place/basis, currency or unit) must never share a key.
"""
from __future__ import annotations

import re
from typing import NamedTuple

KNOWN_SOURCES = {"EC_OILSEEDS", "EC_CEREALS", "FR_CEREALS"}
ALLOWED_UNITS = {"tonne"}
FIELDS = ("source", "product", "product_type", "member_state", "market", "market_stage",
          "quotation_basis", "currency", "unit")
REQUIRED = ("source", "product", "member_state", "market_stage", "currency", "unit")


class SeriesKey(NamedTuple):
    source: str
    product: str
    product_type: str
    member_state: str
    market: str
    market_stage: str
    quotation_basis: str
    currency: str
    unit: str

    def __str__(self) -> str:
        return "|".join(self)


def build_series_key(**fields: str) -> str:
    """Build a key from named components; empty optional components are allowed, required ones are not."""
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown series_key fields: {sorted(unknown)}")
    return str(parse_series_key("|".join(fields.get(f, "") or "" for f in FIELDS)))


def parse_series_key(key: str) -> SeriesKey:
    """Validate the complete key (not just a suffix); raise ValueError describing every problem."""
    if not isinstance(key, str):
        raise ValueError("series_key must be a string")
    parts = key.split("|")
    if len(parts) != len(FIELDS):
        raise ValueError(f"series_key must have {len(FIELDS)} '|'-separated fields, found {len(parts)}")
    sk = SeriesKey(*parts)
    errors = []
    for name, value in zip(FIELDS, parts):
        if value != value.strip():
            errors.append(f"{name} has leading/trailing whitespace")
    for name in REQUIRED:
        if not getattr(sk, name):
            errors.append(f"{name} is required")
    if sk.source and sk.source not in KNOWN_SOURCES:
        errors.append(f"unknown source {sk.source!r}")
    if sk.member_state and not re.fullmatch(r"[A-Z]{2}", sk.member_state):
        errors.append("member_state must be a 2-letter upper-case code")
    if sk.currency and not re.fullmatch(r"[A-Z]{3}", sk.currency):
        errors.append("currency must be a 3-letter upper-case code")
    if sk.unit and sk.unit not in ALLOWED_UNITS:
        errors.append(f"unit must be one of {sorted(ALLOWED_UNITS)}")
    if errors:
        raise ValueError("invalid series_key: " + "; ".join(errors))
    return sk
