"""Day 4 / S1: concurrency, deadline/recovery and restart behaviour, over REAL HTTP (uvicorn on 127.0.0.1, mock mode, offline)."""
import json
import threading
import time

import pytest

from src.api.store import FileStore
from tests.api_fixtures import BODY, HEADERS, LiveServer, Runner, make_app, wait_until


@pytest.fixture
def servers():
    started = []

    def start(app):
        s = LiveServer(app)
        started.append(s)
        return s

    yield start
    for s in started:
        s.stop()


def first_post_in_background(client, **over):
    out = {}

    def go():
        out["r"] = client.post("/assessments", json={**BODY, **over}, headers=HEADERS)

    t = threading.Thread(target=go)
    t.start()
    return t, out


def test_duplicate_and_second_request_while_one_is_running(tmp_path, servers):
    gate = threading.Event()
    app, runner, sink = make_app(tmp_path, Runner(gate=gate))
    srv = servers(app)
    with srv.client() as c, srv.client() as c2:
        t, out = first_post_in_background(c)
        assert runner.started.wait(10)

        dup = c2.post("/assessments", json=BODY, headers=HEADERS)                                   # same id, same hash, running
        assert dup.status_code == 409 and dup.json()["error"]["code"] == "ASSESSMENT_IN_PROGRESS" and dup.headers["retry-after"]
        assert dup.json()["run_state"] == "running" and dup.json()["assessment_status"] is None

        other = c2.post("/assessments", json={**BODY, "request_id": "api-002"}, headers=HEADERS)    # another id: single-flight limit
        assert other.status_code == 429 and other.json()["error"]["code"] == "BUSY" and other.headers["retry-after"]

        clash = c2.post("/assessments", json={**BODY, "horizon_months": 5}, headers=HEADERS)         # same id, other parameters
        assert clash.status_code == 409 and clash.json()["error"]["code"] == "REQUEST_ID_CONFLICT"

        polled = c2.get("/assessments/api-001", headers=HEADERS)
        assert polled.status_code == 202 and polled.json()["run_state"] == "running" and polled.json()["assessment_status"] is None
        assert c2.get("/assessments/api-002", headers=HEADERS).status_code == 404                    # the refused request left no trace

        gate.set()
        t.join(20)
        assert out["r"].status_code == 200 and out["r"].json()["run_state"] == "finished"
        assert runner.calls == 1                                                                      # exactly one execution for three attempts
        assert c2.get("/assessments/api-001", headers=HEADERS).json()["idempotent_replay"] is False
        assert c2.post("/assessments", json={**BODY, "request_id": "api-002"}, headers=HEADERS).status_code == 200      # capacity is free again
        assert runner.calls == 2


def test_simultaneous_identical_posts_run_the_assessment_once(tmp_path, servers):
    app, runner, _ = make_app(tmp_path, Runner(sleep=0.4))
    srv = servers(app)
    barrier, results = threading.Barrier(6), []

    def go():
        with srv.client() as c:
            barrier.wait()
            results.append(c.post("/assessments", json=BODY, headers=HEADERS))

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    codes = sorted(r.status_code for r in results)
    assert len(results) == 6 and set(codes) <= {200, 409} and 200 in codes and runner.calls == 1
    assert all(r.json()["error"]["code"] == "ASSESSMENT_IN_PROGRESS" for r in results if r.status_code == 409)


def test_simultaneous_different_ids_admit_exactly_one(tmp_path, servers):
    app, runner, _ = make_app(tmp_path, Runner(sleep=0.4))
    srv = servers(app)
    barrier, results = threading.Barrier(4), []

    def go(i):
        with srv.client() as c:
            barrier.wait()
            results.append(c.post("/assessments", json={**BODY, "request_id": f"par-{i}"}, headers=HEADERS))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) >= 1 and set(codes) <= {200, 429} and runner.calls == codes.count(200)


def test_deadline_returns_504_without_cancelling_failing_or_duplicating(tmp_path, servers):
    gate = threading.Event()
    app, runner, sink = make_app(tmp_path, Runner(gate=gate), deadline_seconds=0.4)
    srv = servers(app)
    with srv.client() as c:
        t0 = time.monotonic()
        r = c.post("/assessments", json=BODY, headers=HEADERS)
        assert r.status_code == 504 and time.monotonic() - t0 < 5
        assert r.json()["error"]["code"] == "ASSESSMENT_TIMEOUT" and r.json()["run_state"] == "running" and r.headers["retry-after"]
        assert r.json()["assessment_status"] is None and "report" not in r.json()

        for _ in range(3):                                                                            # polling and re-posting never start a second run
            assert c.get("/assessments/api-001", headers=HEADERS).status_code == 202
            assert c.post("/assessments", json=BODY, headers=HEADERS).status_code == 409
        assert runner.calls == 1
        assert not (tmp_path / "store" / "api-001.json").exists()                                     # still running: no result, and certainly no failed record
        assert "assessment.finished" not in sink.types() and "assessment.timeout" in sink.types()

        gate.set()                                                                                    # the worker was never cancelled; it finishes and persists
        done = wait_until(lambda: (lambda x: x if x.status_code == 200 else None)(c.get("/assessments/api-001", headers=HEADERS)))
        assert done.json()["status"] == "completed" and done.json()["run_state"] == "finished" and done.json()["error"] is None
        replay = c.post("/assessments", json=BODY, headers=HEADERS)
        assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True and runner.calls == 1
        assert sink.types().count("assessment.finished") == 1


def test_a_run_past_the_overdue_threshold_is_reported_but_not_killed(tmp_path, servers):
    gate = threading.Event()
    app, runner, _ = make_app(tmp_path, Runner(gate=gate), deadline_seconds=0.2, overdue_after_seconds=0.3)
    srv = servers(app)
    with srv.client() as c:
        assert c.post("/assessments", json=BODY, headers=HEADERS).status_code == 504
        time.sleep(0.5)
        body = c.get("/assessments/api-001", headers=HEADERS).json()
        assert body["run_state"] == "running" and body["overdue"] is True
        gate.set()
        wait_until(lambda: c.get("/assessments/api-001", headers=HEADERS).status_code == 200)
        assert runner.calls == 1


def test_replay_after_a_restart_makes_no_new_calls(tmp_path, servers):
    app1, runner1, _ = make_app(tmp_path)
    s1 = servers(app1)
    with s1.client() as c:
        first = c.post("/assessments", json=BODY, headers=HEADERS).json()
    s1.stop()

    app2, runner2, sink2 = make_app(tmp_path)                                                         # new process state, same store directory
    s2 = servers(app2)
    with s2.client() as c:
        got = c.get("/assessments/api-001", headers=HEADERS)
        again = c.post("/assessments", json=BODY, headers=HEADERS)
        clash = c.post("/assessments", json={**BODY, "horizon_months": 2}, headers=HEADERS)
    assert got.status_code == 200 and got.json() == first
    assert again.status_code == 200 and again.json()["idempotent_replay"] is True and runner2.calls == 0
    assert {k: v for k, v in again.json().items() if k != "idempotent_replay"} == {k: v for k, v in first.items() if k != "idempotent_replay"}
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "REQUEST_ID_CONFLICT"
    assert sink2.types() == ["assessment.replayed"]


def test_a_run_whose_process_died_is_marked_interrupted_and_never_re_run(tmp_path):
    store = FileStore(tmp_path / "store")                                                              # the previous process left only a running marker
    store.write_run("api-001", {"request_hash": "abc", "mode": "mock", "started_at": "2026-11-11T10:00:00+00:00", "boot_id": "old-boot"})
    app, runner, sink = make_app(tmp_path)
    from tests.api_fixtures import client_for
    c = client_for(app)
    got = c.get("/assessments/api-001", headers=HEADERS)
    assert got.status_code == 200 and got.json()["status"] == "failed" and got.json()["error"]["code"] == "ASSESSMENT_INTERRUPTED"
    assert got.json()["run_state"] == "finished" and got.json()["report"] is None
    assert store.read_run("api-001") is None and store.read_final("api-001")["request_hash"] == "abc"
    assert runner.calls == 0
    post_same = c.post("/assessments", json=BODY, headers=HEADERS)                                     # hash differs from the marker's, so it conflicts; never re-runs
    assert post_same.status_code == 409 and runner.calls == 0
    fresh = c.post("/assessments", json={**BODY, "request_id": "api-new"}, headers=HEADERS)            # a new id works
    assert fresh.status_code == 200 and runner.calls == 1
    assert "assessment.interrupted" in sink.types()


def test_interrupted_marker_with_a_matching_hash_replays_the_failure(tmp_path):
    from src.api.models import ApiRequest, request_hash
    h = request_hash(ApiRequest(**BODY))
    FileStore(tmp_path / "store").write_run("api-001", {"request_hash": h, "mode": "mock", "started_at": "x", "boot_id": "old"})
    app, runner, _ = make_app(tmp_path)
    from tests.api_fixtures import client_for
    r = client_for(app).post("/assessments", json=BODY, headers=HEADERS)
    assert r.status_code == 200 and r.json()["error"]["code"] == "ASSESSMENT_INTERRUPTED" and r.json()["idempotent_replay"] is True and runner.calls == 0


def test_the_stored_record_is_complete_json_with_the_hash_and_no_secrets(tmp_path, servers):
    app, _, _ = make_app(tmp_path)
    srv = servers(app)
    with srv.client() as c:
        c.post("/assessments", json=BODY, headers=HEADERS)
    rec = json.loads((tmp_path / "store" / "api-001.json").read_text(encoding="utf-8"))
    assert set(rec) == {"request_hash", "finished_at", "envelope", "trace"} and len(rec["request_hash"]) == 64
    assert not (tmp_path / "store" / "api-001.run.json").exists()
    assert not [p for p in (tmp_path / "store").iterdir() if p.name.endswith(".tmp")]
