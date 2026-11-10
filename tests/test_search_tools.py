"""Tavily and GDELT tools against httpx.MockTransport: no network, no keys, no sleeping."""
from datetime import date

import httpx

from src.tools.search import GdeltTool, TavilyTool, parse_any_date

SECRET = "tvly-SECRET-KEY-123"


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def tavily(handler, key=SECRET, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    return TavilyTool(key, client(handler), sleep=sleeps.append), sleeps


def ok_body():
    return {"results": [{"url": "https://a.example/x", "title": "Sunflower oil supply", "content": "text " * 300,
                         "published_date": "2026-11-03"}, {"url": "", "title": "no url"}]}


def search(tool, query="sunflower oil", kind="risk"):
    return tool.search(query, kind=kind, lookback_days=90, max_results=5)


def test_tavily_success_normalises_results_and_sends_expected_request():
    seen = {}

    def handler(req):
        seen["auth"], seen["body"] = req.headers["Authorization"], req.read().decode()
        return httpx.Response(200, json=ok_body())

    tool, _ = tavily(handler)
    res = search(tool)
    assert res.ok and res.attempts == 1 and len(res.items) == 1
    item = res.items[0]
    assert (item.source, item.url, item.published_date, item.kind) == ("tavily", "https://a.example/x", date(2026, 11, 3), "risk")
    assert len(item.snippet) <= 600
    assert seen["auth"] == f"Bearer {SECRET}" and '"topic":"news"' in seen["body"].replace(" ", "") and "time_range" in seen["body"]
    assert "include_answer" in seen["body"]


def test_tavily_supplier_discovery_uses_general_topic_without_time_range():
    seen = {}

    def handler(req):
        seen["body"] = req.read().decode().replace(" ", "")
        return httpx.Response(200, json={"results": []})

    tool, _ = tavily(handler)
    assert search(tool, kind="supplier_discovery").ok
    assert '"topic":"general"' in seen["body"] and "time_range" not in seen["body"]


def test_429_then_success_retries_and_honours_retry_after_with_a_cap():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "600"}) if len(calls) == 1 else httpx.Response(200, json=ok_body())

    tool, sleeps = tavily(handler)
    res = search(tool)
    assert res.ok and res.attempts == 2 and len(calls) == 2
    assert sleeps == [10.0]                      # Retry-After 600 s is capped at 10 s


def test_persistent_5xx_gives_initial_call_plus_two_retries_then_visible_failure():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503)

    tool, sleeps = tavily(handler)
    res = search(tool)
    assert not res.ok and res.error_code == "HTTP_503" and res.attempts == 3 and len(calls) == 3 and len(sleeps) == 2
    assert res.items == []


def test_timeout_is_retried_then_reported_as_timeout():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    tool, _ = tavily(handler)
    res = search(tool)
    assert not res.ok and res.error_code == "TIMEOUT" and res.attempts == 3


def test_network_error_is_reported_without_the_raw_message():
    def handler(req):
        raise httpx.ConnectError(f"cannot reach {req.url} with {SECRET}", request=req)

    tool, _ = tavily(handler)
    res = search(tool)
    assert res.error_code == "NETWORK_ERROR" and SECRET not in repr(res)


def test_non_transient_http_error_is_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, json={"detail": "bad key " + SECRET})

    tool, sleeps = tavily(handler)
    res = search(tool)
    assert res.error_code == "HTTP_401" and res.attempts == 1 and len(calls) == 1 and not sleeps
    assert SECRET not in repr(res)


def test_invalid_json_is_a_non_retried_bad_response():
    tool, _ = tavily(lambda req: httpx.Response(200, text="<html>nope</html>"))
    res = search(tool)
    assert res.error_code == "BAD_RESPONSE" and res.attempts == 1


def test_missing_key_makes_no_request():
    called = []
    tool, _ = tavily(lambda req: called.append(1) or httpx.Response(200, json={}), key=None)
    res = search(tool)
    assert res.error_code == "MISSING_CREDENTIALS" and not called and res.attempts == 0


def test_key_never_appears_in_results_on_success():
    tool, _ = tavily(lambda req: httpx.Response(200, json=ok_body()))
    assert SECRET not in repr(search(tool))


# ── GDELT ──
def gdelt(handler, **kw):
    sleeps, now = [], [100.0]
    tool = GdeltTool(client(handler), sleep=lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)),
                     clock=lambda: now[0], **kw)
    return tool, sleeps, now


def test_gdelt_success_normalises_articles():
    body = {"articles": [{"url": "https://g.example/1", "title": "Sunflower oil prices", "seendate": "20261105T120000Z",
                          "language": "English"}, {"title": "no url"}]}
    tool, _, _ = gdelt(lambda req: httpx.Response(200, json=body))
    res = search(tool, '"sunflower oil"')
    assert res.ok and len(res.items) == 1
    i = res.items[0]
    assert (i.source, i.published_date, i.language, i.snippet) == ("gdelt", date(2026, 11, 5), "english", "")


def test_gdelt_text_rate_limit_message_is_retried_as_transient():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, text="Please limit requests to one every 5 seconds.")
        return httpx.Response(200, json={"articles": []})

    tool, _, _ = gdelt(handler)
    res = search(tool)
    assert res.ok and res.attempts == 2


def test_gdelt_requests_are_throttled_to_the_minimum_interval():
    tool, sleeps, now = gdelt(lambda req: httpx.Response(200, json={"articles": []}))
    search(tool)
    now[0] += 1.0                                  # only 1 s passes before the next call
    search(tool)
    assert len(sleeps) == 1 and 4.4 < sleeps[0] <= 5.5


def test_gdelt_persistent_failure_is_visible():
    tool, _, _ = gdelt(lambda req: httpx.Response(502))
    res = search(tool)
    assert not res.ok and res.error_code == "HTTP_502" and res.attempts == 3


def test_parse_any_date_variants():
    assert parse_any_date("20261105T120000Z") == date(2026, 11, 5)
    assert parse_any_date("2026-11-03") == date(2026, 11, 3)
    assert parse_any_date("2026-11-03T10:00:00Z") == date(2026, 11, 3)
    assert parse_any_date("Tue, 03 Nov 2026 10:00:00 GMT") == date(2026, 11, 3)
    assert parse_any_date("") is None and parse_any_date(None) is None and parse_any_date("not a date") is None


def test_default_clients_verify_tls_against_the_system_store():
    import ssl
    from src.tools.search import make_client
    c = make_client()
    ctx = c._transport._pool._ssl_context
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    assert TavilyTool("k")._client._transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert GdeltTool()._client._transport._pool._ssl_context.check_hostname is True


# ── per-attempt diagnostics (live-002: GDELT succeeded after 3 attempts with zero articles, and nothing recorded why) ──
def test_retry_outcomes_are_recorded_per_attempt_with_http_status_and_no_secret():
    codes = iter([503, 429, 200])
    tool, _ = tavily(lambda req: httpx.Response(next(codes), json=ok_body() if False else {"results": []}))
    res = search(tool)
    assert res.ok and res.attempts == 3
    log = res.attempt_log
    assert [a["outcome"] for a in log] == ["error", "error", "ok"]
    assert [a["http_status"] for a in log] == [503, 429, 200] and [a["code"] for a in log] == ["HTTP_503", "HTTP_429", None]
    assert [a["retried"] for a in log] == [True, True, False] and log[-1]["results"] == 0
    assert res.note == "HTTP 200 but the response contained no results" and SECRET not in repr(res)


def test_transport_failure_attempts_are_logged_without_exposing_the_message():
    def boom(req):
        raise httpx.ConnectError(f"cannot reach {req.url} using {SECRET}", request=req)

    tool, _ = tavily(boom)
    res = search(tool)
    assert [a["code"] for a in res.attempt_log] == ["NETWORK_ERROR"] * 3 and all(a["http_status"] is None for a in res.attempt_log)
    assert SECRET not in repr(res) and "cannot reach" not in repr(res)


def test_gdelt_zero_article_responses_explain_themselves():
    for body, expect in (({"articles": []}, "empty 'articles' list"), ({"status": "ok"}, "without an 'articles' key")):
        tool, _, _ = gdelt(lambda req, b=body: httpx.Response(200, json=b))
        res = search(tool)
        assert res.ok and res.items == [] and res.http_status == 200 and expect in res.note and res.params["timespan"] == "3months"
    tool, _, _ = gdelt(lambda req: httpx.Response(200, text=""))
    res = search(tool)
    assert res.ok and "empty body" in res.note


def test_gdelt_attempt_log_distinguishes_a_rate_limit_message_from_the_final_empty_success():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(200, text="Please limit requests to one every 5 seconds.")
        return httpx.Response(200, json={"articles": []})

    tool, _, _ = gdelt(handler)
    res = search(tool)
    assert res.ok and res.attempts == 3
    assert [a["code"] for a in res.attempt_log] == ["RATE_LIMITED", "RATE_LIMITED", None] and res.attempt_log[-1]["results"] == 0
    assert "empty 'articles' list" in res.note


def test_requested_parameters_are_recorded_without_credentials():
    tool, _ = tavily(lambda req: httpx.Response(200, json=ok_body()))
    res = search(tool)
    assert res.params == {"topic": "news", "time_range": "year", "max_results": 5} and SECRET not in repr(res.params)
