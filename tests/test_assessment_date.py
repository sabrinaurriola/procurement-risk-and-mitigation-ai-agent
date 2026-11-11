"""assessment_date in the request/response contract (needed for reproducible demos such as the stale-benchmark scenario)."""
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from src.agent.graph import run_assessment
from src.agent.schemas import AssessmentRequest
from tests.agent_fixtures import REQUEST, TODAY, deps


def test_assessment_date_is_optional_and_validated_to_a_sane_range():
    assert AssessmentRequest.model_validate(REQUEST).assessment_date is None
    ok = AssessmentRequest.model_validate({**REQUEST, "assessment_date": "2026-11-20"})
    assert ok.assessment_date == date(2026, 11, 20)
    for bad in (date.today() - timedelta(days=400), date.today() + timedelta(days=200), "not-a-date", "2026-13-40"):
        with pytest.raises(ValidationError):
            AssessmentRequest.model_validate({**REQUEST, "assessment_date": bad})


def test_default_date_is_today_and_is_echoed_without_an_override_warning():
    resp, _ = run_assessment(REQUEST, deps())
    assert resp.assessment_date == TODAY and resp.report.assessment_date == TODAY and resp.schema_version == "1.1"
    assert not any(w.startswith("ASSESSMENT_DATE_OVERRIDE") for w in resp.warnings)


def test_overridden_date_drives_freshness_and_is_flagged():
    resp, _ = run_assessment({**REQUEST, "assessment_date": "2026-11-20"}, deps())
    assert resp.assessment_date == date(2026, 11, 20) and resp.report.assessment_date == date(2026, 11, 20)
    assert any(w.startswith("ASSESSMENT_DATE_OVERRIDE") and "2026-11-20" in w for w in resp.warnings)
    b = resp.report.benchmark
    assert resp.status == "partial" and b.status == "stale" and b.age_days == 26 and resp.report.financial.status == "unavailable"


def test_failed_responses_still_echo_the_date_and_invalid_requests_have_none():
    from src.agent.data import StaticData
    resp, _ = run_assessment({**REQUEST, "assessment_date": "2026-11-12"}, deps(data=StaticData(error=RuntimeError("x"))))
    assert resp.status == "failed" and resp.assessment_date == date(2026, 11, 12)
    bad, _ = run_assessment({**REQUEST, "horizon_months": 0}, deps())
    assert bad.status == "failed" and bad.assessment_date is None
