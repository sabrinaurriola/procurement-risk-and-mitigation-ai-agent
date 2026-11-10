"""Shared builders for calculation tests (plain dataclasses; no database)."""
from datetime import date, timedelta
from decimal import Decimal as D
from fractions import Fraction

from src.calculations.models import BenchmarkObservation, ContractInput, ForecastInput

KEY = "EC_OILSEEDS|Crude sunflower oil|Standard|ES|Average|DEPPROC||EUR|tonne"
OTHER_KEY = "EC_OILSEEDS|Crude sunflower oil|Standard|ES|Average|ex-factory||EUR|tonne"
AS_OF = date(2026, 11, 10)
DEC, JAN, FEB = date(2026, 12, 1), date(2027, 1, 1), date(2027, 2, 1)
MONTHS = [DEC, JAN, FEB]


def contract(cid, ctype="fixed", vol="100", start=date(2026, 7, 1), end=date(2027, 6, 30), ingredient="sunflower_oil", **kw):
    base = dict(contract_id=cid, supplier_id="SUP-001", ingredient_code=ingredient, contract_type=ctype, currency="EUR",
                monthly_volume_t=D(vol), start_date=start, end_date=end)
    base.update(kw)
    return ContractInput(**base)


def ct001():
    return contract("CT-001", "fixed", "100", unit_price=D("1520.00"))


def ct002():
    return contract("CT-002", "fixed", "80", date(2026, 9, 1), date(2027, 1, 15), supplier_id="SUP-002", unit_price=D("1580.00"))


def ct003(**kw):
    base = dict(index_series_key=KEY, base_price=D("1600.00"), index_base_value=D("1400.00"),
                pass_through=D("0.80"), fixed_premium=D("0"))
    base.update(kw)
    base.setdefault("supplier_id", "SUP-003")
    return contract("CT-003", "index_linked", "120", date(2026, 9, 1), date(2027, 8, 31), **base)


def contracts():
    return [ct001(), ct002(), ct003()]


def forecasts(dec="400", jan="420", feb="410", ingredient="sunflower_oil"):
    return [ForecastInput(ingredient, m, D(v), "F-2026-11", date(2026, 11, 10))
            for m, v in zip(MONTHS, (dec, jan, feb)) if v is not None]


def obs(start=date(2026, 10, 19), price="1457.40", key=KEY, flags=(), currency="EUR", unit="tonne", occ=1):
    return BenchmarkObservation(key, start, start + timedelta(days=6), None if price is None else D(price),
                                currency, unit, tuple(flags), occ)


def benchmark_history():
    """Latest valid week 19-25 Oct 2026 at 1457.40 plus earlier weeks."""
    return [obs(date(2026, 10, 19), "1457.40"), obs(date(2026, 10, 12), "1450.90"), obs(date(2026, 10, 5), "1415.91")]


def frac(d: D) -> Fraction:
    return Fraction(str(d))


def to_dec(f: Fraction) -> D:
    return D(f.numerator) / D(f.denominator)
