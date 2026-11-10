"""Tavily and GDELT research tools.

* One retry owner: the retry loop lives HERE (tenacity), nowhere else. Initial call + up to two retries on timeouts,
  transport errors, HTTP 429 and 5xx (Retry-After honoured up to a cap). Anything else fails immediately.
* Failures never raise to the caller: they return ToolResult(ok=False, error_code=...), so the workflow can report an
  unavailable source instead of crashing or inventing evidence.
* Every attempt is logged (outcome, HTTP status, result count) so a "successful" call that retried or returned nothing
  is visible. A successful HTTP response is NOT the same thing as usable evidence.
* API keys are only used in the Authorization header; they never appear in results, errors, notes or logs.
"""
from __future__ import annotations

import email.utils
import re
import ssl
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Protocol

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt

MAX_ATTEMPTS = 3               # initial call + two retries
RETRY_AFTER_CAP_SECONDS = 10.0


@dataclass(frozen=True)
class RawResult:
    source: str                 # 'tavily' | 'gdelt'
    url: str
    title: str
    snippet: str
    published_date: date | None
    language: str | None
    query: str
    kind: str                   # 'risk' | 'supplier_discovery'


@dataclass
class ToolResult:
    ok: bool
    items: list[RawResult] = field(default_factory=list)
    error_code: str | None = None
    attempts: int = 0
    http_status: int | None = None          # last HTTP status seen (None for transport-level failures)
    attempt_log: list[dict] = field(default_factory=list)   # per attempt: outcome, code, http_status, results
    note: str | None = None                 # e.g. why an HTTP 200 returned no items
    params: dict = field(default_factory=dict)              # non-secret request parameters (for diagnosis)


class SearchTool(Protocol):
    name: str

    def search(self, query: str, *, kind: str, lookback_days: int, max_results: int) -> ToolResult: ...


class _Transient(Exception):
    def __init__(self, code: str, retry_after: float | None = None):
        super().__init__(code)
        self.code, self.retry_after = code, retry_after


class _Permanent(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def make_client(timeout: float = 30.0) -> httpx.Client:
    """HTTP client that verifies TLS against the operating-system trust store.

    httpx's default (bundled certifi list) cannot verify TLS on some Windows setups where a locally trusted root sits
    in the system store; verification is never disabled."""
    return httpx.Client(timeout=timeout, verify=ssl.create_default_context())


def parse_any_date(value) -> date | None:
    """Tolerant date parsing for API fields; returns None when the value cannot be interpreted."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return email.utils.parsedate_to_datetime(v).date()
    except (TypeError, ValueError):
        return None


def _retry_wait(sleep: Callable[[float], None]):
    def wait(state: RetryCallState) -> float:
        exc = state.outcome.exception()
        delay = min(2.0 ** (state.attempt_number - 1), 8.0)
        if isinstance(exc, _Transient) and exc.retry_after is not None:
            delay = min(exc.retry_after, RETRY_AFTER_CAP_SECONDS)
        return delay
    return wait


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, (_Transient, httpx.TimeoutException, httpx.TransportError))


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, (_Transient, _Permanent)):
        return exc.code
    if isinstance(exc, httpx.TimeoutException):
        return "TIMEOUT"
    return "NETWORK_ERROR"          # class of failure only; never the raw message (it could echo a URL)


def _call_with_retry(fn: Callable[[], list[RawResult]], sleep: Callable[[float], None],
                     status: Callable[[], int | None] = lambda: None) -> ToolResult:
    attempts = 0
    log: list[dict] = []
    retrying = Retrying(stop=stop_after_attempt(MAX_ATTEMPTS), retry=retry_if_exception(_is_transient),
                        wait=_retry_wait(sleep), sleep=sleep, reraise=True)
    try:
        for attempt in retrying:
            with attempt:
                attempts += 1
                try:
                    items = fn()
                except Exception as exc:  # noqa: BLE001 - recorded, then re-raised for the retry policy
                    log.append({"attempt": attempts, "outcome": "error", "code": _error_code(exc),
                                "retried": _is_transient(exc) and attempts < MAX_ATTEMPTS, "http_status": status()})
                    raise
                log.append({"attempt": attempts, "outcome": "ok", "code": None, "retried": False,
                            "http_status": status(), "results": len(items)})
        return ToolResult(True, items, None, attempts, attempt_log=log)
    except Exception as exc:  # noqa: BLE001 - converted to a visible, sanitised failure
        return ToolResult(False, [], _error_code(exc), attempts, attempt_log=log)


def _status_error(resp: httpx.Response) -> Exception | None:
    if resp.status_code == 429 or resp.status_code >= 500:
        ra = resp.headers.get("Retry-After")
        try:
            retry_after = float(ra) if ra is not None else None
        except ValueError:
            retry_after = None
        return _Transient(f"HTTP_{resp.status_code}", retry_after)
    if resp.status_code >= 400:
        return _Permanent(f"HTTP_{resp.status_code}")
    return None


class TavilyTool:
    name = "tavily"
    URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str | None, client: httpx.Client | None = None, timeout: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep):
        self._key = api_key
        self._client = client or make_client(timeout)
        self._sleep = sleep
        self._status: int | None = None
        self._note: str | None = None

    @staticmethod
    def _time_range(days: int) -> str:
        return "day" if days <= 1 else "week" if days <= 7 else "month" if days <= 31 else "year"

    def search(self, query: str, *, kind: str, lookback_days: int, max_results: int) -> ToolResult:
        if not self._key:
            return ToolResult(False, [], "MISSING_CREDENTIALS", 0)
        payload = {"query": query, "topic": "news" if kind == "risk" else "general", "search_depth": "basic",
                   "max_results": max_results, "include_published_date": True, "include_answer": False}
        if kind == "risk":
            payload["time_range"] = self._time_range(lookback_days)
        self._status, self._note = None, None

        def call() -> list[RawResult]:
            self._status = None
            resp = self._client.post(self.URL, json=payload, headers={"Authorization": f"Bearer {self._key}"})
            self._status = resp.status_code
            err = _status_error(resp)
            if err:
                raise err
            try:
                data = resp.json()
            except ValueError as exc:
                raise _Permanent("BAD_RESPONSE") from exc
            items = [RawResult("tavily", r.get("url", ""), r.get("title", "") or "", (r.get("content", "") or "")[:600],
                               parse_any_date(r.get("published_date")), None, query, kind)
                     for r in data.get("results", []) if r.get("url")]
            self._note = None if items else "HTTP 200 but the response contained no results"
            return items

        res = _call_with_retry(call, self._sleep, lambda: self._status)
        res.http_status, res.note = self._status, (self._note if res.ok else None)
        res.params = {"topic": payload["topic"], "time_range": payload.get("time_range"), "max_results": max_results}
        return res


class GdeltTool:
    name = "gdelt"
    URL = "https://api.gdeltproject.org/api/v2/doc/doc"
    MIN_INTERVAL = 5.5          # GDELT asks for no more than one request per ~5 seconds

    def __init__(self, client: httpx.Client | None = None, timeout: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 min_interval: float | None = None):
        self._client = client or make_client(timeout)
        self._sleep, self._clock = sleep, clock
        self._min_interval = self.MIN_INTERVAL if min_interval is None else min_interval
        self._last: float | None = None
        self._status: int | None = None
        self._note: str | None = None

    def search(self, query: str, *, kind: str, lookback_days: int, max_results: int) -> ToolResult:
        if self._last is not None:
            wait = self._min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        params = {"query": query, "mode": "artlist", "format": "json", "maxrecords": max_results,
                  "timespan": f"{max(1, round(lookback_days / 30))}months" if lookback_days > 30 else f"{lookback_days}d"}
        self._status, self._note = None, None

        def call() -> list[RawResult]:
            self._status = None
            self._last = self._clock()
            resp = self._client.get(self.URL, params=params)
            self._status = resp.status_code
            err = _status_error(resp)
            if err:
                raise err
            try:
                data = resp.json()
            except ValueError as exc:
                if re.search(r"limit requests", resp.text or "", re.I):
                    raise _Transient("RATE_LIMITED") from exc
                if not (resp.text or "").strip():
                    self._note = "HTTP 200 with an empty body (GDELT can return this when nothing matches or a query is rejected)"
                    return []
                raise _Permanent("BAD_RESPONSE") from exc
            if not isinstance(data, dict) or "articles" not in data:
                keys = sorted(data)[:6] if isinstance(data, dict) else type(data).__name__
                self._note = f"HTTP 200 JSON without an 'articles' key (keys: {keys})"
                return []
            items = [RawResult("gdelt", a.get("url", ""), a.get("title", "") or "", "", parse_any_date(a.get("seendate")),
                               (a.get("language") or "").lower() or None, query, kind)
                     for a in data["articles"] if a.get("url")]
            self._note = None if items else "HTTP 200 with an empty 'articles' list"
            return items

        res = _call_with_retry(call, self._sleep, lambda: self._status)
        res.http_status, res.note = self._status, (self._note if res.ok else None)
        res.params = {"timespan": params["timespan"], "maxrecords": max_results}
        return res
