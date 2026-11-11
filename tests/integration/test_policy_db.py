"""Opt-in (RUN_DB_TESTS=1), READ ONLY. Verifies the policy-RAG SQL against the live schema with PREPARE (parsed and planned,
never executed) and runs real retrieval against the currently empty policy_chunks table. No writes, no paid calls."""
import os
from datetime import date

import pytest
from dotenv import load_dotenv

from src.agent.data import PostgresData
from src.agent.graph import Deps, run_assessment
from src.agent.mock import DemoLLM, MockSearch
from src.db.preflight import connect
from src.rag import load_policies as lp
from src.rag.retrieve import COUNT_SQL, FULLTEXT_SQL, VECTOR_SQL, PostgresPolicyRetriever, build_queries
from tests.integration.test_mapping_prepare import to_positional

pytestmark = pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to read from Supabase")

STATEMENTS = {"existing": lp.SELECT_EXISTING_SQL, "upsert": lp.UPSERT_SQL, "pending": lp.SELECT_PENDING_SQL, "embed_update": lp.EMBED_UPDATE_SQL,
              "count": COUNT_SQL, "vector": VECTOR_SQL, "fulltext": FULLTEXT_SQL}


@pytest.mark.parametrize("name", sorted(STATEMENTS))
def test_policy_statement_prepares_against_the_live_schema(name):
    load_dotenv()
    with connect() as conn:                       # READ ONLY session
        conn.execute(f"PREPARE pol_{name} AS {to_positional(STATEMENTS[name])}")
        conn.rollback()


def test_retrieval_against_the_empty_table_reports_the_limitation():
    res = PostgresPolicyRetriever(None).retrieve(build_queries("sunflower_oil", {"contract_coverage"}), "sunflower_oil", 3)
    with connect() as conn:
        loaded = conn.execute("SELECT count(*) FROM procurement.policy_chunks").fetchone()[0]
    if loaded == 0:
        assert res.status == "unavailable" and "no policy chunks are loaded" in res.note and not res.passages
    else:
        assert res.status in ("ok", "fallback", "unavailable")


def test_the_workflow_surfaces_unavailable_policy_context_on_real_data():
    today = date(2026, 11, 10)
    deps = Deps(PostgresData(), DemoLLM(), MockSearch("tavily", today), MockSearch("gdelt", today), policy=PostgresPolicyRetriever(None),
                today=lambda: today, mock=True)
    resp, _ = run_assessment({"request_id": "db-policy", "ingredient": "sunflower_oil", "horizon_months": 3}, deps)
    with connect() as conn:
        loaded = conn.execute("SELECT count(*) FROM procurement.policy_chunks").fetchone()[0]
    assert resp.report is not None and resp.report.financial.months[0].incremental_exposure_eur["+10%"] == "28,565.04"
    if loaded == 0:
        assert resp.status == "partial" and resp.report.policy.status == "unavailable"
        assert any(w.startswith("POLICY_CONTEXT_UNAVAILABLE") for w in resp.warnings)
        assert all(not x.policy_citations for x in list(resp.report.draft.mitigation_options) + list(resp.report.draft.recommended_actions))
