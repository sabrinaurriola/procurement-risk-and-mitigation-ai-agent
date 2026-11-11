"""Helpers for the API tests: a controllable runner, an app builder and a real uvicorn server on a free local port. No network beyond 127.0.0.1,
no database, no paid call."""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi.testclient import TestClient

from src.agent.graph import run_assessment
from src.api.app import create_app
from src.api.events import InMemoryEventSink
from tests.agent_fixtures import deps

KEY = "test-key-for-offline-tests"
HEADERS = {"X-API-Key": KEY}
BODY = {"request_id": "api-001", "ingredient": "sunflower_oil", "horizon_months": 3, "mode": "mock"}


class Runner:
    """Stands in for the LangGraph runner. Counts calls; can block on a gate, sleep, raise, or use a custom deps()."""

    def __init__(self, gate: threading.Event | None = None, sleep: float = 0.0, fail: Exception | None = None, deps_factory=None):
        self.gate, self.sleep, self.fail, self.deps_factory = gate, sleep, fail, deps_factory
        self.calls = 0
        self.requests: list[dict] = []
        self.started = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, request):
        with self._lock:
            self.calls += 1
            self.requests.append(dict(request))
        self.started.set()
        if self.gate is not None and not self.gate.wait(20):
            raise TimeoutError("test gate never released")
        if self.sleep:
            time.sleep(self.sleep)
        if self.fail is not None:
            raise self.fail
        return run_assessment(request, (self.deps_factory or deps)())

    def factory(self, mode: str):
        return self


def make_app(tmp_path: Path, runner: Runner | None = None, sink=None, **kw):
    runner = runner or Runner()
    sink = sink if sink is not None else InMemoryEventSink()
    kw.setdefault("live_allowed", lambda: False)
    kw.setdefault("db_check", lambda: True)
    app = create_app(store_dir=tmp_path / "store", api_key=KEY, runner_factory=runner.factory, sink=sink, **kw)
    return app, runner, sink


def client_for(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


class LiveServer:
    """A real uvicorn server in a thread (needed to observe true concurrency and a real restart)."""

    def __init__(self, app):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        assert self.server.started, "uvicorn did not start"
        self.base = f"http://127.0.0.1:{self.port}"

    def client(self, timeout: float = 30.0) -> httpx.Client:
        return httpx.Client(base_url=self.base, headers=HEADERS, timeout=timeout, trust_env=False)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(15)


def wait_until(predicate, timeout: float = 15.0, step: float = 0.05):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not reached in time")
