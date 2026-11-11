"""Opt-in (RUN_DB_TESTS=1), READ ONLY: scenarios A and B on the real database data (mock research and drafting)."""
import os

import pytest

pytestmark = pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1 to read from Supabase")


@pytest.mark.parametrize("key", ["A", "B"])
def test_scenarios_meet_every_expectation_and_audit_check_on_real_data(key, tmp_path):
    from src.agent.demo import run_scenario
    r = run_scenario(key, out=tmp_path)
    assert [n for n, ok in r["expectations"] if not ok] == [] and [c.id for c in r["audit"] if not c.passed] == []
    assert r["response"].report.benchmark.status == ("available" if key == "A" else "stale")
