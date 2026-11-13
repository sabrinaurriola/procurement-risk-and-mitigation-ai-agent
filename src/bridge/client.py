"""Authenticated OUTBOUND webhook client (Python -> n8n). httpx only; HTTPS only; redirects are never followed (the secret header
must not travel to another host); the URL and the secret are never logged, printed, put in exceptions or shown in repr().

Reliability rules (docs/day4-architecture.md section 19):
* ARCHIVE may be retried (bounded, with backoff) on transport errors, timeouts, 429 and 5xx, because n8n deduplicates by request_id.
  It is never retried on 401/403, other 4xx, redirects or a malformed 200 body.
* EMAIL is AT-MOST-ONCE: exactly one attempt per call. After an ambiguous outcome (timeout after the request was sent, connection reset,
  5xx or 2xx without a clear valid body) the state is `unknown`; nothing is resent here. The controller requires an explicit user
  confirmation for a manual resend, and n8n must answer `already_sent` if it did send for that request_id.
* HTTP 200 / "accepted" is never success: archive success needs the verified response described in verify_archive_response()."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Literal

import httpx
from pydantic import ValidationError

from src.bridge.models import ArchiveRequest, ArchiveResponse, EmailRequest, EmailResponse, is_notion_url

log = logging.getLogger("procurement.bridge")
for _noisy in ("httpx", "httpcore"):          # httpx logs every request URL at INFO; the webhook URL must never reach a log
    logging.getLogger(_noisy).setLevel(logging.WARNING)
SECRET_HEADER = "X-Webhook-Secret"

# One fixed, safe message per outcome. Nothing here can contain a URL, a secret, report text or exception text.
MESSAGES = {
    "OK": "Done.",
    "NOT_CONFIGURED": "The n8n webhook is not configured correctly (check the webhook settings in .env; HTTPS is required).",
    "AUTH_REJECTED": "n8n rejected the webhook secret (HTTP 401/403). Check N8N_WEBHOOK_SECRET and the n8n Header Auth credential.",
    "TIMEOUT": "n8n did not answer in time.",
    "CONNECTION_FAILED": "Could not connect to the n8n webhook.",
    "SERVER_ERROR": "n8n reported a server error.",
    "RATE_LIMITED": "n8n is rate limiting requests. Try again shortly.",
    "REJECTED": "n8n rejected the request (HTTP 4xx).",
    "INVALID_RESPONSE": "n8n returned a response this app cannot trust (malformed or unexpected). Nothing is confirmed.",
    "URL_UNTRUSTED": "n8n returned a Notion page URL on a host this app does not accept (only https notion.so, notion.site or notion.com). Nothing is confirmed.",
    "ARCHIVE_INCOMPLETE": "The Notion archive is incomplete: not every report block was stored. It is NOT confirmed.",
    "ARCHIVE_FAILED": "n8n reported that the Notion archive failed.",
    "ID_MISMATCH": "n8n answered for a different request_id. Nothing is confirmed.",
    "HASH_MISMATCH": "The report hash echoed by n8n does not match the report that was sent. Nothing is confirmed.",
    "COUNT_MISMATCH": "The number of Notion blocks found does not match the number sent. Nothing is confirmed.",
    "ATTACHMENT_UNCONFIRMED": "n8n did not confirm the PDF attachment it sent. The e-mail may have been sent without it: check the inbox.",
    "EMAIL_UNKNOWN": "The e-mail result is UNKNOWN: it may or may not have been sent. Check the inbox before deciding to resend.",
    "EMAIL_FAILED": "n8n reported that the e-mail was not sent.",
}


class BridgeConfigError(Exception):
    """Webhook settings are missing or unsafe. The message names the setting, never a value."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ArchiveResult:
    ok: bool
    code: str
    message: str
    notion_page_url: str | None = None
    blocks_found: int = 0
    blocks_expected: int = 0
    duplicate_existing: bool = False
    attempts: int = 0
    detail: str = ""             # safe diagnostics only: HTTP status and the SHAPE of an unusable body (field names), never body text


@dataclass(frozen=True)
class EmailResult:
    ok: bool
    code: str
    message: str
    state: Literal["sent", "already_sent", "failed", "unknown"]
    message_id: str | None = None
    detail: str = ""             # safe diagnostics only: the error codes n8n reported (uppercase codes), never free text


def _https_url(name: str, value: str | None) -> str:
    if not value or not value.strip():
        raise BridgeConfigError(f"{name} is not set")
    value = value.strip()
    if not value.startswith("https://") or len(value) <= len("https://") or " " in value:
        raise BridgeConfigError(f"{name} must be an https:// URL")
    return value


def verify_archive_response(req: ArchiveRequest, resp: ArchiveResponse) -> str:
    """Return 'OK' only for a verified, complete archive; otherwise a distinct failure code."""
    if resp.request_id != req.request_id:
        return "ID_MISMATCH"
    if resp.archive_status == "failed":
        return "ARCHIVE_FAILED"
    if resp.archive_status == "incomplete":
        return "ARCHIVE_INCOMPLETE"
    if not resp.ok:
        return "ARCHIVE_FAILED"
    if resp.report_sha256_echo != req.report_sha256:
        return "HASH_MISMATCH"
    if resp.blocks_expected != req.total_blocks or resp.blocks_found != resp.blocks_expected:
        return "COUNT_MISMATCH"
    if not is_notion_url(resp.notion_page_url):
        return "URL_UNTRUSTED"
    return "OK"                                   # archive_status is stored or duplicate_existing, both verified complete


def shape_detail(r: httpx.Response) -> str:
    """Why a response is unusable, without echoing its content: status, JSON type and field NAMES only."""
    base = f"HTTP {r.status_code}"
    if 300 <= r.status_code < 400:
        return f"{base}, redirect not followed"
    try:
        data = json.loads(r.content)
    except ValueError:
        return f"{base}, body is not JSON"
    if isinstance(data, str):
        return f"{base}, body is a JSON string (double-encoded JSON?)"
    if isinstance(data, dict):
        required = ("ok", "request_id", "archive_status", "blocks_expected", "blocks_found")
        missing = [k for k in required if k not in data]
        keys = [k for k in data if re.fullmatch(r"[A-Za-z0-9_]{1,30}", str(k))][:8]
        if missing:
            return f"{base}, JSON object without {', '.join(missing)}; keys present: {', '.join(keys) or 'none'}"
        return f"{base}, JSON fields have unexpected types or values"
    return f"{base}, JSON {type(data).__name__}"


def reported_codes(r: httpx.Response) -> str:
    """The error code(s) an n8n answer carries (`error_code`, `details`), kept only if they look like codes. Never any other text."""
    try:
        data = json.loads(r.content)
    except ValueError:
        return f"HTTP {r.status_code}"
    if not isinstance(data, dict):
        return f"HTTP {r.status_code}"
    code = data.get("error_code")
    extra = data.get("details") if isinstance(data.get("details"), list) else []
    ok = lambda v: isinstance(v, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,50}", v)
    parts = [f"HTTP {r.status_code}"]
    if ok(code):
        parts.append(f"n8n error_code {code}")
    codes = [x for x in extra if ok(x)][:12]
    if codes:
        parts.append("details " + ", ".join(codes))
    return ", ".join(parts)


class WebhookClient:
    def __init__(self, archive_url: str | None, email_url: str | None, secret: str | None, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, archive_timeout: float = 90.0, email_timeout: float = 30.0,
                 max_attempts: int = 3, backoff: tuple[float, ...] = (1.0, 3.0)):
        self._archive_url = _https_url("N8N_ARCHIVE_WEBHOOK_URL", archive_url)
        self._email_url = _https_url("N8N_EMAIL_WEBHOOK_URL", email_url)
        if not secret or not secret.strip():
            raise BridgeConfigError("N8N_WEBHOOK_SECRET is not set")
        self._secret = secret.strip()
        self._transport, self._sleep = transport, sleep
        self._archive_timeout, self._email_timeout = archive_timeout, email_timeout
        self._max_attempts, self._backoff = max(1, min(max_attempts, 5)), backoff

    @classmethod
    def from_env(cls, **kw) -> "WebhookClient":
        return cls(os.getenv("N8N_ARCHIVE_WEBHOOK_URL"), os.getenv("N8N_EMAIL_WEBHOOK_URL"), os.getenv("N8N_WEBHOOK_SECRET"), **kw)

    def __repr__(self) -> str:                    # never reveals URLs or the secret
        return "WebhookClient(<configured>)"

    def _post(self, url: str, body: str, timeout: float) -> httpx.Response:
        with httpx.Client(transport=self._transport, timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False) as client:
            return client.post(url, content=body.encode("utf-8"),
                               headers={"Content-Type": "application/json", SECRET_HEADER: self._secret})

    # ───────── archive (retryable) ─────────
    def archive(self, req: ArchiveRequest) -> ArchiveResult:
        body = req.model_dump_json()
        last, last_detail = "CONNECTION_FAILED", ""
        for attempt in range(1, self._max_attempts + 1):
            retryable, code, parsed, detail = False, "", None, ""
            try:
                r = self._post(self._archive_url, body, self._archive_timeout)
            except httpx.TimeoutException:
                retryable, code = True, "TIMEOUT"
            except httpx.TransportError:
                retryable, code = True, "CONNECTION_FAILED"
            else:
                if r.status_code in (401, 403):
                    code = "AUTH_REJECTED"
                elif r.status_code == 429:
                    retryable, code = True, "RATE_LIMITED"
                elif r.status_code >= 500:
                    retryable, code = True, "SERVER_ERROR"
                elif 300 <= r.status_code < 400:
                    code, detail = "INVALID_RESPONSE", shape_detail(r)
                elif r.status_code >= 400:
                    code, detail = "REJECTED", f"HTTP {r.status_code}"
                else:
                    try:
                        parsed = ArchiveResponse.model_validate_json(r.content)
                    except (ValidationError, ValueError):
                        code, detail = "INVALID_RESPONSE", shape_detail(r)
            log.info("archive attempt %d/%d request_id=%s outcome=%s", attempt, self._max_attempts, req.request_id, code or "response")
            if parsed is not None:
                code = verify_archive_response(req, parsed)
                return ArchiveResult(code == "OK", code, MESSAGES[code], parsed.notion_page_url if code == "OK" else None,
                                     parsed.blocks_found, parsed.blocks_expected, parsed.archive_status == "duplicate_existing" and code == "OK", attempt,
                                     "" if code != "URL_UNTRUSTED" else "the URL host was not notion.so, notion.site or notion.com")
            last, last_detail = code, detail
            if not retryable or attempt == self._max_attempts:
                break
            self._sleep(self._backoff[min(attempt - 1, len(self._backoff) - 1)])
        return ArchiveResult(False, last, MESSAGES[last], attempts=attempt, detail=last_detail)

    # ───────── e-mail (at most once) ─────────
    def email(self, req: EmailRequest) -> EmailResult:
        try:
            r = self._post(self._email_url, req.model_dump_json(), self._email_timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return self._email_done(req, "failed", "CONNECTION_FAILED")          # the request never reached n8n
        except httpx.TransportError:
            return self._email_done(req, "unknown", "EMAIL_UNKNOWN")             # may have reached n8n (timeout, reset): ambiguous
        parsed: EmailResponse | None
        try:
            parsed = EmailResponse.model_validate_json(r.content)
        except (ValidationError, ValueError):
            parsed = None
        if r.status_code in (401, 403):
            return self._email_done(req, "failed", "AUTH_REJECTED")
        if r.status_code == 429:
            return self._email_done(req, "failed", "RATE_LIMITED")
        if (parsed is not None and parsed.request_id == req.request_id and parsed.ok and parsed.email_status == "sent"
                and 200 <= r.status_code < 300 and parsed.attachment_sha256_echo != req.attachment.sha256):
            return self._email_done(req, "unknown", "ATTACHMENT_UNCONFIRMED")        # sent, but the PDF is not confirmed: never claim success
        if parsed is not None and parsed.request_id == req.request_id and parsed.email_status == "unknown" and not parsed.ok:
            return self._email_done(req, "unknown", "EMAIL_UNKNOWN", reported_codes(r))            # n8n itself reports an uncertain send: never resend automatically
        if parsed is not None and parsed.request_id == req.request_id and (
                (parsed.email_status in ("sent", "already_sent") and parsed.ok and 200 <= r.status_code < 300)
                or (parsed.email_status == "failed" and not parsed.ok)):
            if parsed.email_status == "failed":
                return self._email_done(req, "failed", "EMAIL_FAILED", reported_codes(r))
            return EmailResult(True, "OK", MESSAGES["OK"], parsed.email_status, parsed.message_id)
        if 300 <= r.status_code < 500:
            return self._email_done(req, "failed", "REJECTED" if r.status_code >= 400 else "INVALID_RESPONSE", reported_codes(r))
        return self._email_done(req, "unknown", "EMAIL_UNKNOWN")                 # 5xx or 2xx without a clear, consistent body

    @staticmethod
    def _email_done(req: EmailRequest, state: Literal["failed", "unknown"], code: str, detail: str = "") -> EmailResult:
        log.info("email request_id=%s outcome=%s", req.request_id, code)
        return EmailResult(False, code, MESSAGES[code], state, detail=detail)
