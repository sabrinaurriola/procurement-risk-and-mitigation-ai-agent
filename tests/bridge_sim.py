"""In-process simulator of the two n8n webhooks (archive + e-mail) for offline tests. Used through httpx.MockTransport: no network,
no Notion, no Gmail. It implements the CONTRACT the real n8n workflow must honour (dedupe by request_id, already_sent), so it is
simulation evidence only, not evidence about n8n."""
from __future__ import annotations

import base64
import hashlib
import json

import httpx

ARCHIVE_URL = "https://n8n.example.test/webhook/procurement-archive"
EMAIL_URL = "https://n8n.example.test/webhook/procurement-email"
SECRET = "test-secret-value-123"
PAGE_URL = "https://www.notion.so/Assessment-0123456789abcdef0123456789abcdef"


class FakeN8n:
    def __init__(self, secret: str = SECRET):
        self.secret = secret
        self.pages: dict[str, dict] = {}              # request_id -> stored page
        self.sent: dict[str, int] = {}                # request_id -> real e-mails sent
        self.archive_calls = self.email_calls = 0
        self.archive_faults: list[str] = []           # consumed one per archive call
        self.email_faults: list[str] = []
        self.store_only: int | None = None            # simulate a partial append (page keeps only N blocks)
        self.echo_hash: str | None = None             # override report_sha256_echo
        self.echo_request_id: str | None = None
        self.seen_headers: list[dict] = []
        self.seen_bodies: list[dict] = []
        self.attachments: dict[str, bytes] = {}       # request_id -> decoded PDF attached to the one real e-mail
        self.skip_echo = False
        self.wrong_echo = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def _timeout(req: httpx.Request) -> httpx.ReadTimeout:
        return httpx.ReadTimeout("read timed out", request=req)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen_headers.append(dict(request.headers))
        if request.headers.get("x-webhook-secret") != self.secret:
            return httpx.Response(403, json={"message": "Authorization data is wrong!"})
        body = json.loads(request.content)
        self.seen_bodies.append(body)
        if str(request.url) == ARCHIVE_URL:
            return self._archive(request, body)
        if str(request.url) == EMAIL_URL:
            return self._email(request, body)
        return httpx.Response(404)

    def _archive(self, request: httpx.Request, body: dict) -> httpx.Response:
        self.archive_calls += 1
        fault = self.archive_faults.pop(0) if self.archive_faults else ""
        if fault == "timeout_before":
            raise self._timeout(request)
        if fault == "connect_error":
            raise httpx.ConnectError("refused", request=request)
        if fault == "503":
            return httpx.Response(503, text="unavailable")
        if fault == "429":
            return httpx.Response(429, json={})
        if fault == "garbage":
            return httpx.Response(200, text="<html>accepted</html>")
        if fault == "accepted_only":
            return httpx.Response(200, json={"message": "Workflow was started"})
        if fault == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.example.test/"})
        rid = body["request_id"]
        page = self.pages.get(rid)
        status = "duplicate_existing" if page else "stored"
        if page is None:                              # dedupe: a second request with the same id never creates a second page
            kept = body["blocks"] if self.store_only is None else body["blocks"][: self.store_only]
            page = self.pages[rid] = {"url": PAGE_URL, "blocks": list(kept), "sha": body["report_sha256"]}
        found, expected = len(page["blocks"]), body["total_blocks"]
        if found < expected:
            status = "incomplete"
        resp = {"ok": status in ("stored", "duplicate_existing"), "request_id": self.echo_request_id or rid, "archive_status": status,
                "notion_page_url": page["url"], "blocks_expected": expected, "blocks_found": found,
                "report_sha256_echo": self.echo_hash or page["sha"]}
        if fault == "timeout_after_effect":           # the page exists in Notion but the answer is lost
            raise self._timeout(request)
        return httpx.Response(200, json=resp)

    def _email(self, request: httpx.Request, body: dict) -> httpx.Response:
        self.email_calls += 1
        fault = self.email_faults.pop(0) if self.email_faults else ""
        if fault == "timeout_before":
            raise self._timeout(request)
        if fault == "connect_error":
            raise httpx.ConnectError("refused", request=request)
        rid = body["request_id"]
        if fault == "reset_before":
            raise httpx.RemoteProtocolError("connection reset", request=request)
        if fault == "reports_failed":
            return httpx.Response(502, json={"ok": False, "request_id": rid, "email_status": "failed"})
        already = self.sent.get(rid, 0) > 0
        att = body.get("attachment") or {}
        raw = base64.b64decode(att.get("content_base64", ""), validate=True)      # n8n side: Convert to File -> binary PDF
        assert raw.startswith(b"%PDF-") and hashlib.sha256(raw).hexdigest() == att["sha256"] and len(raw) == att["size_bytes"]
        if not already:
            self.sent[rid] = 1                        # the one real Gmail send
            self.attachments[rid] = raw
        if fault == "timeout_after_effect":
            raise self._timeout(request)
        if fault == "500_after_effect":
            return httpx.Response(500, text="boom")
        if fault == "accepted_only":
            return httpx.Response(200, json={"message": "Workflow was started"})
        out = {"ok": True, "request_id": rid, "email_status": "already_sent" if already else "sent", "message_id": None if already else "msg-1"}
        if not already and not self.skip_echo:
            out["attachment_sha256_echo"] = "0" * 64 if self.wrong_echo else att["sha256"]
        return httpx.Response(200, json=out)
