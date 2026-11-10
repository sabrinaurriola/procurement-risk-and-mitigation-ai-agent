"""Opt-in (RUN_DB_TESTS=1): the full workflow on REAL read-only database data with mocked research and a template LLM.
No writes, no external API calls, no paid calls."""
import os
from datetime import date

import pytest

pytestmark = pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to read from Supabase")


def test_workflow_on_real_database_data_with_mocked_research():
    from src.agent.data import PostgresData
    from src.agent.graph import Deps, run_assessment
    from src.agent.mock import DemoLLM, MockSearch
    today = date(2026, 11, 10)
    deps = Deps(PostgresData(), DemoLLM(), MockSearch("tavily", today), MockSearch("gdelt", today), today=lambda: today, mock=True)
    resp, _ = run_assessment({"request_id": "db-test", "ingredient": "sunflower_oil", "horizon_months": 3}, deps)
    assert resp.status == "completed", resp.error
    r = resp.report
    assert r.benchmark.price_eur_per_tonne == "1,457.40" and str(r.benchmark.period_end) == "2026-10-25"
    assert r.benchmark.excluded_observations.get("OUTLIER_VS_MEDIAN") == 2
    assert r.financial.months[0].incremental_exposure_eur["+10%"] == "28,565.04"
    assert {s.contract_id for s in r.supplier_exposure} == {"CT-001", "CT-002", "CT-003"}
    assert any(w.startswith("MOCK RUN") for w in resp.warnings)
