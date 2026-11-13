"""Paid-call budget guard (CLAUDE.md: OpenAI budget max USD 5 in total, every call logged in runs/usage.jsonl).

Before ANY live run the cumulative ESTIMATED spend recorded in the usage log plus a worst-case reserve for the coming run must stay under the limit,
otherwise the run is refused before an external call is made. The log holds counts only (no text). A log line for a model without a price entry is
counted at the highest known price (pessimistic); an unreadable line is counted as the reserve. The numbers are estimates from list prices, not billing."""
from __future__ import annotations

import json
import os
from pathlib import Path

TOTAL_BUDGET_USD = 5.00
RUN_RESERVE_USD = 0.25      # worst case of one run: the 40,000-token cap entirely as output at the highest known output price (about 0.18) + embeddings
USAGE_LOG = Path(__file__).resolve().parents[2] / "runs" / "usage.jsonl"
# USD per million tokens (input, output), from OpenAI's model pages checked 10 Nov 2026; verify before relying on them.
PRICE_PER_M = {"gpt-5.4-mini": (0.75, 4.50), "text-embedding-3-small": (0.02, 0.0)}


class BudgetExceeded(Exception):
    """The budget guard refused a paid run (the message states the estimated numbers only)."""


def budget_limit() -> float:
    """USD 5 unless LIVE_BUDGET_USD sets a LOWER limit (it can never raise it)."""
    try:
        return min(TOTAL_BUDGET_USD, float(os.getenv("LIVE_BUDGET_USD", TOTAL_BUDGET_USD)))
    except ValueError:
        return TOTAL_BUDGET_USD


def spent_usd(log: Path = USAGE_LOG) -> float:
    if not log.exists():
        return 0.0
    worst_in, worst_out = max(p[0] for p in PRICE_PER_M.values()), max(p[1] for p in PRICE_PER_M.values())
    total = 0.0
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            pin, pout = PRICE_PER_M.get(row.get("model"), (worst_in, worst_out))
            total += int(row.get("input_tokens") or 0) / 1e6 * pin + int(row.get("output_tokens") or 0) / 1e6 * pout
        except (ValueError, TypeError, AttributeError):
            total += RUN_RESERVE_USD
    return total


def enforce_budget(log: Path = USAGE_LOG, reserve: float = RUN_RESERVE_USD) -> None:
    limit, spent = budget_limit(), spent_usd(log)
    if spent + reserve > limit:
        raise BudgetExceeded(f"BUDGET: estimated spend so far USD {spent:.2f} plus the reserve for one run USD {reserve:.2f} would exceed the limit USD {limit:.2f}")


def estimate_cost_usd(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    """Estimated USD for one model's tokens; None for a model without a price entry."""
    if model not in PRICE_PER_M:
        return None
    pin, pout = PRICE_PER_M[model]
    return round(input_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6)
