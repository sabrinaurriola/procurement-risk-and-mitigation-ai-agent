"""Day 4 / S1: operational-event derivation and validation. In-memory sink only; no database writer exists yet (migration 002 is not applied)."""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.api.events import InMemoryEventSink, OperationalEvent, events_from_envelope
from tests.api_fixtures import BODY, HEADERS, Runner, client_for, make_app


def ev(rid="e-1", **kw):
    return OperationalEvent(event_key=f"{rid}|x", request_id=rid, event_type="assessment.finished", **kw)


def test_a_real_mock_run_emits_events_that_match_the_envelope(tmp_path):
    app, _, sink = make_app(tmp_path)
    env = client_for(app).post("/assessments", json=BODY, headers=HEADERS).json()
    types = sink.types()
    assert types[0] == "assessment.received" and types[-1] == "assessment.finished"
    sources = {e.detail["source"]: e for e in sink.events if e.event_type == "research.source"}
    assert set(sources) == set(env["report"]["source_status"])
    for name, e in sources.items():
        assert e.detail["status"] == env["report"]["source_status"][name][:200]
    fin = sink.events[-1]
    ex = env["execution"]
    assert fin.assessment_status == env["status"] and fin.duration_ms == int(round(ex["elapsed_seconds"] * 1000))
    assert fin.input_tokens == ex["tokens"]["input"] and fin.output_tokens == ex["tokens"]["output"]
    assert fin.cost_usd is None and fin.detail["cost_estimated"] is False                             # mock runs have no cost
    assert fin.detail["llm_calls"] == ex["llm_calls"] and fin.detail["tool_calls"] == ex["tool_calls"]
    assert fin.detail["warnings"] == len(env["warnings"]) and fin.mode == "mock"
    assert fin.detail["benchmark_status"] == env["report"]["benchmark"]["status"] and fin.detail["financial_status"] == env["report"]["financial"]["status"]


def envelope(**over):
    base = {"request_id": "e-1", "status": "partial", "warnings": ["a", "b"], "missing_data": [], "error": None,
            "execution": {"elapsed_seconds": 72.205, "research_rounds": 1, "tool_calls": {"tavily": 3, "gdelt": 1}, "llm_calls": 3,
                          "tokens": {"input": 22053, "output": 7224}, "mock": False},
            "report": {"source_status": {"tavily": "ok", "gdelt": "unavailable: HTTP_429 (1 failed calls, 2 attempts, HTTP 429)"},
                       "removed_claims": ["risks 'Trade-press context shows falling prices' removed: no citation",
                                          "supplier_candidates 'Elitagro' removed: product quote does not support refined",
                                          "mitigation_options 'Use the benchmark only as a crude indicator' removed: addresses unknown risk R4"],
                       "benchmark": {"status": "available"}, "financial": {"status": "available"}}}
    base.update(over)
    return base


def test_demo_002_shaped_envelope_maps_every_metric_to_a_real_field():
    evs = events_from_envelope(envelope(), model="gpt-5.4-mini")
    by = {}
    for e in evs:
        by.setdefault(e.event_type, []).append(e)
    gd = next(e for e in by["research.source"] if e.detail["source"] == "gdelt")
    assert gd.severity == "warning" and gd.detail["status_class"] == "unavailable" and "429" in gd.detail["status"]
    assert next(e for e in by["research.source"] if e.detail["source"] == "tavily").severity == "info"
    rem = by["validation.removed"][0]
    assert rem.detail == {"count": 3, "kinds": {"risks": 1, "supplier_candidates": 1, "mitigation_options": 1}} and rem.severity == "warning"
    assert "Elitagro" not in rem.model_dump_json() and "Trade-press" not in rem.model_dump_json()      # claim text is never stored
    fin = by["assessment.finished"][0]
    assert fin.duration_ms == 72205 and fin.input_tokens == 22053 and fin.output_tokens == 7224 and fin.severity == "warning"
    assert fin.cost_usd == Decimal("0.049048")                                                        # hand-checked: 22053/1e6*0.75 + 7224/1e6*4.5 = 0.01653975 + 0.032508
    assert fin.detail["cost_estimated"] is True and fin.detail["research_rounds"] == 1 and fin.detail["tool_calls"] == {"tavily": 3, "gdelt": 1}


def test_cost_is_not_invented():
    assert events_from_envelope(envelope(), model=None)[-1].cost_usd is None                           # unknown model: no price
    assert events_from_envelope(envelope(), model="some-other-model")[-1].cost_usd is None
    assert events_from_envelope(envelope(execution={**envelope()["execution"], "mock": True}), model="gpt-5.4-mini")[-1].cost_usd is None
    assert events_from_envelope(envelope(status="failed"), model="gpt-5.4-mini")[-1].cost_usd is None


def test_a_failed_assessment_without_a_report_has_no_invented_research_or_validation_rows():
    env = envelope(status="failed", report=None, error={"code": "RESEARCH_UNAVAILABLE", "message": "x"})
    evs = events_from_envelope(env)
    assert [e.event_type for e in evs] == ["assessment.finished"]
    assert evs[0].severity == "error" and evs[0].error_code == "RESEARCH_UNAVAILABLE" and "benchmark_status" not in evs[0].detail


def test_event_keys_are_deterministic_so_a_retried_write_can_be_a_no_op():
    a = [e.event_key for e in events_from_envelope(envelope())]
    b = [e.event_key for e in events_from_envelope(envelope())]
    assert a == b and len(set(a)) == len(a) and all(k.startswith("e-1|") for k in a)


@pytest.mark.parametrize("kw", [{"request_id": "bad id"}, {"event_type": "assessment.exploded"}, {"error_code": "lowercase"}, {"duration_ms": -1},
                                {"detail": {"report_text": "x"}}, {"detail": {"count": "x" * 5000}}, {"surprise": 1}, {"severity": "fatal"}])
def test_event_validation_rejects_bad_rows(kw):
    base = dict(event_key="k", request_id="e-1", event_type="assessment.finished")
    with pytest.raises(ValidationError):
        OperationalEvent(**{**base, **kw})


def test_timeout_replay_and_interrupted_events_are_emitted(tmp_path):
    import threading
    gate = threading.Event()
    app, runner, sink = make_app(tmp_path, Runner(gate=gate), deadline_seconds=0.2)
    c = client_for(app)
    assert c.post("/assessments", json=BODY, headers=HEADERS).status_code == 504
    t = [e for e in sink.events if e.event_type == "assessment.timeout"][0]
    assert t.severity == "warning" and t.detail["http_wait_ms"] >= 150 and t.duration_ms >= 150
    gate.set()
    from tests.api_fixtures import wait_until
    wait_until(lambda: "assessment.finished" in sink.types())
    c.post("/assessments", json=BODY, headers=HEADERS)
    assert sink.types().count("assessment.replayed") == 1 and sink.types().count("assessment.received") == 1


def test_event_taxonomy_covers_the_planned_archive_and_delivery_types_without_a_writer():
    for t, kw in (("archive.stored", {"archive_status": "stored"}), ("archive.failed", {"archive_status": "failed"}),
                  ("delivery.sent", {"delivery_status": "sent"}), ("delivery.failed", {"delivery_status": "failed"})):
        OperationalEvent(event_key="k", request_id="e-1", component="n8n", event_type=t, **kw)
    assert InMemoryEventSink().types() == []
