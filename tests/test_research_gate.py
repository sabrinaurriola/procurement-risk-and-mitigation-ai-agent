"""Research gate for live runs, per-call diagnostics, and HTTP status reporting (all mocked)."""
import httpx

from src.agent.mock import MockSearch
from src.agent.graph import run_assessment
from src.tools.search import GdeltTool, TavilyTool
from tests.agent_fixtures import REQUEST, TODAY, deps


def run(d):
    return run_assessment(REQUEST, d)


def test_require_research_stops_before_any_llm_call_when_both_sources_fail():
    d = deps(tavily=MockSearch("tavily", TODAY, fail_with="NETWORK_ERROR"), gdelt=MockSearch("gdelt", TODAY, fail_with="NETWORK_ERROR"))
    d.require_research = True
    resp, trace = run(d)
    assert resp.status == "failed" and resp.error.code == "RESEARCH_UNAVAILABLE" and resp.report is None
    assert d.llm.usage()["calls"] == 0 and d.llm.draft_calls == 0 and resp.execution.llm_calls == 0
    assert [n["node"] for n in trace][-1] == "search"          # never reached filtering, drafting or validation
    assert resp.execution.tool_calls == {"tavily": 3, "gdelt": 1}   # the connectivity checks count toward the limits


def test_require_research_continues_when_one_source_answers():
    d = deps(tavily=MockSearch("tavily", TODAY, fail_with="HTTP_503"))
    d.require_research = True
    resp, _ = run(d)
    assert resp.status == "partial" and resp.report is not None


def test_without_the_gate_a_report_is_still_produced_when_research_fails():
    d = deps(tavily=MockSearch("tavily", TODAY, fail_with="TIMEOUT"), gdelt=MockSearch("gdelt", TODAY, fail_with="TIMEOUT"))
    resp, _ = run(d)
    assert resp.status == "partial" and resp.report is not None


def test_trace_records_per_call_diagnostics_and_rejections():
    _, trace = run(deps())
    search_node = next(n for n in trace if n["node"] == "search")
    assert len(search_node["records"]) == 4
    assert all({"tool", "query", "ok", "http_status", "attempts", "error_code", "results"} <= set(r) for r in search_node["records"])
    filt = next(n for n in trace if n["node"] == "filter_evidence")
    assert filt["retrieved"] > filt["accepted"] and filt["rejected"] > 0
    assert {r["reason"] for r in filt["rejected_items"]} >= {"NO_INGREDIENT_MATCH", "UNSUPPORTED_LANGUAGE"}
    assert all({"id", "relevance", "reason"} <= set(j) for j in filt["judged"])


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_http_status_is_reported_for_success_failure_and_transport_errors():
    ok = TavilyTool("k", _client(lambda r: httpx.Response(200, json={"results": []})), sleep=lambda s: None)
    assert ok.search("q", kind="risk", lookback_days=90, max_results=3).http_status == 200
    bad = TavilyTool("k", _client(lambda r: httpx.Response(503)), sleep=lambda s: None)
    assert bad.search("q", kind="risk", lookback_days=90, max_results=3).http_status == 503

    def boom(req):
        raise httpx.ConnectError("x", request=req)

    assert TavilyTool("k", _client(boom), sleep=lambda s: None).search("q", kind="risk", lookback_days=90, max_results=3).http_status is None
    g = GdeltTool(_client(lambda r: httpx.Response(200, json={"articles": []})), sleep=lambda s: None, min_interval=0)
    assert g.search("q", kind="risk", lookback_days=90, max_results=3).http_status == 200
