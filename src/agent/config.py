"""Static configuration: research budgets and ingredient-specific profiles/assumptions.

Assumptions are INGREDIENT-SPECIFIC: the 150 EUR/tonne spot premium is approved for the fictional sunflower-oil
scenario only. An ingredient without an entry here is unsupported and is rejected, never given a borrowed premium."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.calculations.exposure import Assumptions


@dataclass(frozen=True)
class Limits:
    max_rounds: int = 3
    max_tavily_calls: int = 6          # credit guard (one basic search is typically one credit)
    max_gdelt_calls: int = 3           # GDELT asks for at most one request every ~5 s
    deadline_seconds: float = 180.0
    min_relevant_evidence: int = 3     # below this, another research round is attempted (within budgets)
    lookback_days: int = 90
    max_results_per_call: int = 6
    max_candidates: int = 3            # unverified supplier candidates
    max_repairs: int = 1               # one bounded report-repair attempt
    policy_top_k: int = 3              # passages retrieved per theme query (merged, so up to 4 x 3)


@dataclass(frozen=True)
class IngredientProfile:
    code: str
    display_name: str
    keywords: tuple[str, ...]                 # an item must mention one of these to be considered
    risk_queries: tuple[str, ...]             # Tavily (news) queries, round 1
    gdelt_query: str
    supplier_query: str                       # Tavily (general) query for preliminary, unverified candidates
    assumptions: Assumptions
    benchmark_caveat: str


PROFILES: dict[str, IngredientProfile] = {
    "sunflower_oil": IngredientProfile(
        code="sunflower_oil",
        display_name="Sunflower oil",
        keywords=("sunflower", "girasol", "sunflower oil", "aceite de girasol"),
        risk_queries=("sunflower oil supply disruption Europe", "sunflower oil price Spain Black Sea exports"),
        gdelt_query='"sunflower oil"',
        supplier_query="refined sunflower oil bulk suppliers Spain Europe food industry",
        assumptions=Assumptions(spot_premium=Decimal("150")),
        benchmark_caveat=("The benchmark is the European Commission price of CRUDE standard sunflower oil in Spain "
                          "(market stage DEPPROC, whose definition has not been independently confirmed). It is not the "
                          "company's refined-oil procurement price."),
    ),
}

SUPPORTED_INGREDIENTS = tuple(PROFILES)
