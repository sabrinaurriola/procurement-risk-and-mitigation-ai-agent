"""Day 4 / S1 API contract tests (mock mode, offline): A01-A08 of docs/api-specification.md plus authentication, safe errors and persistence."""
import json
import logging
import os
import threading

import pytest

from src.agent.llm import LLMError
from src.api.app import create_app
from src.api.events import InMemoryEventSink
from src.api.store import FileStore
from tests.agent_fixtures import ScriptedLLM, deps
from tests.api_fixtures import BODY, HEADERS, KEY, Runner, client_for, make_app

SECRETS = ["hunter2", "C:\\secret", "db.example.supabase.co", "postgresql://"]


def post(client, **over):
    return client.post("/assessments", json={**BODY, **over}, headers=HEADERS)


# ───────── A01 / A02: envelope and replay ─────────
def test_a01_valid_mock_request_returns_the_envelope(tmp_path):
    app, runner, _ = make_app(tmp_path)
    r = post(client_for(app))
    assert r.status_code == 200 and runner.calls == 1
    env = r.json()
    assert env["status"] in {"completed", "partial", "failed"} and env["status"] == env["assessment_status"]
    assert env["run_state"] == "finished" and env["idempotent_replay"] is False and env["schema_version"] == "1.1"
    assert env["report_markdown"] and env["report_location"] == {"markdown": "/assessments/api-001/report.md", "json": "/assessments/api-001"}
    assert env["error"] is None and env["execution"]["mock"] is True
    assert runner.requests[0] == {"request_id": "api-001", "ingredient": "sunflower_oil", "horizon_months": 3}     # API-only fields are not passed on


def test_a02_replay_returns_the_stored_envelope_with_zero_new_calls(tmp_path):
    app, runner, sink = make_app(tmp_path)
    c = client_for(app)
    first, second = post(c).json(), post(c).json()
    assert runner.calls == 1 and second["idempotent_replay"] is True
    assert {k: v for k, v in first.items() if k != "idempotent_replay"} == {k: v for k, v in second.items() if k != "idempotent_replay"}
    assert "assessment.replayed" in sink.types()


def test_assessment_date_override_is_passed_and_disclosed(tmp_path):
    app, runner, _ = make_app(tmp_path)
    r = post(client_for(app), request_id="api-date", assessment_date="2026-11-20")
    assert runner.requests[0]["assessment_date"] == "2026-11-20"
    assert any("ASSESSMENT_DATE_OVERRIDE" in w for w in r.json()["warnings"])


# ───────── A03: conflicts ─────────
@pytest.mark.parametrize("over", [{"horizon_months": 4}, {"assessment_date": "2026-11-20"}, {"mode": "live"}])
def test_a03_same_id_with_different_parameters_conflicts(tmp_path, over):
    app, runner, _ = make_app(tmp_path)
    c = client_for(app)
    post(c)
    r = post(c, **over)
    assert r.status_code == 409 and r.json()["error"]["code"] == "REQUEST_ID_CONFLICT" and runner.calls == 1


# ───────── A04: validation ─────────
@pytest.mark.parametrize("over", [{"ingredient": "wheat"}, {"horizon_months": 0}, {"horizon_months": 7}, {"horizon_months": "three"},
                                  {"assessment_date": "2030-01-01"}, {"assessment_date": "not-a-date"}, {"request_id": "bad id!"},
                                  {"request_id": "../escape"}, {"request_id": "a" * 65}, {"mode": "turbo"}, {"surprise": 1}])
def test_a04_invalid_input_is_rejected_with_a_safe_422(tmp_path, over):
    app, runner, _ = make_app(tmp_path)
    r = post(client_for(app), **over)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_REQUEST" and runner.calls == 0
    assert "Traceback" not in r.text and str(tmp_path) not in r.text
    assert list((tmp_path / "store").iterdir()) == []                                  # nothing was recorded for a rejected request


def test_a04_missing_and_malformed_bodies(tmp_path):
    app, runner, _ = make_app(tmp_path)
    c = client_for(app)
    assert c.post("/assessments", json={}, headers=HEADERS).status_code == 422
    r = c.post("/assessments", content=b"{not json", headers={**HEADERS, "content-type": "application/json"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_REQUEST" and runner.calls == 0


# ───────── A05: live gate ─────────
def test_a05_live_without_the_server_flag_is_refused_before_any_call(tmp_path):
    app, runner, _ = make_app(tmp_path)
    c = client_for(app)
    r = post(c, mode="live")
    assert r.status_code == 403 and r.json()["error"]["code"] == "LIVE_NOT_AUTHORISED" and runner.calls == 0
    body = {k: v for k, v in BODY.items() if k != "mode"}                              # an omitted mode is `live` and is refused too
    assert c.post("/assessments", json=body, headers=HEADERS).status_code == 403
    assert list((tmp_path / "store").iterdir()) == []


def test_live_is_not_wired_in_s1_even_when_the_flag_is_on(tmp_path):
    from src.api.runners import default_runner_factory
    app = create_app(store_dir=tmp_path / "s", api_key=KEY, runner_factory=default_runner_factory, live_allowed=lambda: True, db_check=lambda: True)
    r = client_for(app).post("/assessments", json={**BODY, "mode": "live"}, headers=HEADERS)
    assert r.status_code == 501 and r.json()["error"]["code"] == "LIVE_NOT_AVAILABLE"
    assert list((tmp_path / "s").iterdir()) == []


# ───────── A07: failed results ─────────
def test_a07_failed_assessment_has_no_report_and_a_sanitised_error(tmp_path):
    llm = ScriptedLLM(drafts=[LLMError("LLM_DOWN"), LLMError("LLM_DOWN")])
    app, _, sink = make_app(tmp_path, Runner(deps_factory=lambda: deps(llm=llm)))
    c = client_for(app)
    r = post(c)
    env = r.json()
    assert r.status_code == 200 and env["status"] == "failed" and env["assessment_status"] == "failed"
    assert env["report"] is None and env["report_markdown"] is None and env["report_location"]["markdown"] is None
    assert env["error"]["code"] == "LLM_UNAVAILABLE" and env["error"]["message"] == "Report drafting failed (LLM_DOWN)"      # the agent exposes an error CODE only
    rep = c.get("/assessments/api-001/report.md", headers=HEADERS)
    assert rep.status_code == 404 and rep.json()["error"]["code"] == "REPORT_NOT_AVAILABLE"
    fin = [e for e in sink.events if e.event_type == "assessment.finished"][0]
    assert fin.severity == "error" and fin.assessment_status == "failed" and fin.error_code == "LLM_UNAVAILABLE"


# ───────── A08 / auth ─────────
def test_a08_health_reports_only_booleans_and_a_version(tmp_path):
    app, _, _ = make_app(tmp_path)
    r = client_for(app).get("/health")
    assert r.status_code == 200 and set(r.json()) == {"status", "version", "live_enabled", "db"}
    assert r.json()["db"] == "ok" and r.json()["live_enabled"] is False
    app2, _, _ = make_app(tmp_path / "x", db_check=lambda: False)
    j = client_for(app2).get("/health").json()
    assert j["db"] == "error" and not any(s in json.dumps(j).lower() for s in ("postgres", "supabase", "host", "password"))


@pytest.mark.parametrize("exc", [SystemExit("FAIL: DATABASE_URL not set"), RuntimeError("password=hunter2 host=db.example.supabase.co")])
def test_db_check_failures_return_a_bool_and_never_raise(monkeypatch, exc):
    from src.api import app as appmod

    def broken():
        raise exc
    monkeypatch.setattr("src.db.preflight.connect", broken)
    assert appmod._db_ok() is False


@pytest.mark.parametrize("method,path", [("post", "/assessments"), ("get", "/assessments/api-001"), ("get", "/assessments/api-001/report.md")])
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
def test_protected_endpoints_require_the_api_key(tmp_path, method, path, headers):
    app, runner, _ = make_app(tmp_path)
    c = client_for(app)
    r = c.post(path, json=BODY, headers=headers) if method == "post" else c.get(path, headers=headers)
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED" and runner.calls == 0
    assert KEY not in r.text


def test_the_app_refuses_to_start_without_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ASSESSMENT_API_KEY", raising=False)
    monkeypatch.setattr("src.api.app.load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError) as exc:
        create_app(store_dir=tmp_path)
    assert "ASSESSMENT_API_KEY" in str(exc.value)


def test_unknown_routes_ids_and_methods_get_safe_errors(tmp_path):
    app, _, _ = make_app(tmp_path)
    c = client_for(app)
    r = c.get("/assessments/never-ran", headers=HEADERS)
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert c.get("/assessments/bad id", headers=HEADERS).status_code in (404, 422)
    assert c.get("/assessments/..%2Fsecret", headers=HEADERS).status_code in (404, 422)
    assert c.get("/nope", headers=HEADERS).status_code == 404
    assert c.put("/assessments", json=BODY, headers=HEADERS).status_code == 405
    assert c.get("/openapi.json").status_code == 404                                            # no schema/docs endpoints exposed


def test_report_markdown_endpoint_serves_the_stored_report(tmp_path):
    app, _, _ = make_app(tmp_path)
    c = client_for(app)
    env = post(c).json()
    r = c.get("/assessments/api-001/report.md", headers=HEADERS)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown") and r.text == env["report_markdown"]


# ───────── safe errors ─────────
def test_an_unexpected_exception_is_stored_as_a_sanitised_failure(tmp_path, caplog):
    boom = RuntimeError("password=hunter2 file C:\\secret\\x.py host=db.example.supabase.co postgresql://u:p@h/db")
    app, runner, sink = make_app(tmp_path, Runner(fail=boom))
    c = client_for(app)
    with caplog.at_level(logging.DEBUG):
        r = post(c)
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR" and r.json()["status"] == "failed"
    blob = r.text + (tmp_path / "store" / "api-001.json").read_text(encoding="utf-8") + caplog.text
    assert not [s for s in SECRETS if s in blob]
    again = post(c)                                                                            # the stored failure is replayed, never re-run (a new id retries)
    assert again.status_code == 200 and again.json()["idempotent_replay"] is True and runner.calls == 1
    assert not [s for s in SECRETS if s in again.text]


def test_unhandled_errors_outside_the_runner_are_generic(tmp_path):
    app, _, _ = make_app(tmp_path)
    def explode(rid):
        raise ValueError("secret C:\\secret hunter2")
    app.state.service.get = explode
    r = client_for(app).get("/assessments/x1", headers=HEADERS)
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR" and not [s for s in SECRETS if s in r.text]


def test_a_corrupt_record_gives_a_generic_error_without_paths(tmp_path):
    app, _, _ = make_app(tmp_path)
    (tmp_path / "store" / "bad-1.json").write_text("{truncated", encoding="utf-8")
    c = client_for(app)
    for r in (c.get("/assessments/bad-1", headers=HEADERS), post(c, request_id="bad-1")):
        assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR" and str(tmp_path) not in r.text and "truncated" not in r.text


def test_a_failing_event_sink_never_changes_the_assessment(tmp_path):
    class Broken:
        def emit(self, event):
            raise RuntimeError("sink down hunter2")
    app, runner, _ = make_app(tmp_path, sink=Broken())
    r = post(client_for(app))
    assert r.status_code == 200 and r.json()["status"] in {"completed", "partial"} and runner.calls == 1


def test_run_state_and_assessment_status_are_distinct(tmp_path):
    gate = threading.Event()
    app, runner, _ = make_app(tmp_path, Runner(gate=gate), deadline_seconds=0.2)
    c = client_for(app)
    assert post(c).status_code == 504
    running = c.get("/assessments/api-001", headers=HEADERS)
    assert running.status_code == 202 and running.json()["run_state"] == "running" and running.json()["assessment_status"] is None
    assert "status" not in running.json() and running.headers["retry-after"]
    gate.set()
    from tests.api_fixtures import wait_until
    done = wait_until(lambda: (lambda r: r if r.status_code == 200 else None)(c.get("/assessments/api-001", headers=HEADERS)))
    assert done.json()["run_state"] == "finished" and done.json()["assessment_status"] == done.json()["status"]
    assert runner.calls == 1


# ───────── store ─────────
def test_atomic_write_leaves_no_partial_or_temporary_file_when_the_replace_fails(tmp_path, monkeypatch):
    store = FileStore(tmp_path)
    store.write_final("r1", {"v": 1})
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        store.write_final("r1", {"v": 2})
    monkeypatch.undo()
    assert store.read_final("r1") == {"v": 1} and [p.name for p in tmp_path.iterdir()] == ["r1.json"]


def test_orphaned_temporary_files_are_ignored(tmp_path):
    store = FileStore(tmp_path)
    (tmp_path / ".r2.json.deadbeef.tmp").write_text('{"half":', encoding="utf-8")
    assert store.read_final("r2") is None
    store.write_final("r2", {"ok": True})
    assert store.read_final("r2") == {"ok": True}


def test_a_reader_never_sees_a_partial_record(tmp_path):
    store = FileStore(tmp_path)
    big = {"blob": "x" * 400_000}
    store.write_final("r3", big)
    stop, seen = threading.Event(), []

    def writer():
        while not stop.is_set():
            store.write_final("r3", big)

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(300):
            seen.append(store.read_final("r3"))
    finally:
        stop.set()
        t.join()
    assert all(s == big for s in seen)


def test_the_store_rejects_unsafe_ids(tmp_path):
    store = FileStore(tmp_path)
    for bad in ("../x", "a/b", "a\\b", "", "x" * 65, "a b"):
        with pytest.raises(ValueError):
            store.read_final(bad)


def test_a_failed_file_write_is_served_from_memory_for_this_process(tmp_path, monkeypatch):
    app, runner, _ = make_app(tmp_path)
    monkeypatch.setattr(app.state.service.store, "write_final", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    c = client_for(app)
    assert post(c).status_code == 200
    assert c.get("/assessments/api-001", headers=HEADERS).status_code == 200 and post(c).json()["idempotent_replay"] is True and runner.calls == 1
