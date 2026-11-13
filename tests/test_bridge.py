"""Offline tests for the Python-to-n8n bridge: payloads, hashing, summary extraction, response verification, retries, at-most-once e-mail.
Everything runs against tests/bridge_sim.py through httpx.MockTransport. No network, no Notion, no Gmail."""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from pydantic import ValidationError

from src.agent.graph import run_assessment
from src.api import notion_blocks
from src.bridge.client import BridgeConfigError, WebhookClient, verify_archive_response
from src.bridge.fonts import fonts_available
from tests.pdf_fonts import needs_fonts
from src.bridge.models import ArchiveRequest, ArchiveResponse, EmailAttachment, EmailRequest, is_notion_url
from src.bridge.payload import PayloadError, build_archive_request, build_email_request, build_report_pdf, extract_executive_summary
from tests.agent_fixtures import REQUEST, deps
from tests.bridge_sim import ARCHIVE_URL, EMAIL_URL, PAGE_URL, SECRET, FakeN8n


@pytest.fixture(scope="module")
def response():
    resp, _ = run_assessment(dict(REQUEST), deps())
    assert resp.status in ("completed", "partial")
    return resp


@pytest.fixture()
def archive_req(response) -> ArchiveRequest:
    return build_archive_request(response, mode="mock")


def make_client(sim: FakeN8n, sleeps: list | None = None, **kw) -> WebhookClient:
    sl = sleeps if sleeps is not None else []
    return WebhookClient(ARCHIVE_URL, EMAIL_URL, SECRET, transport=sim.transport(), sleep=sl.append, **kw)


# ───────── payload construction ─────────
def test_hash_is_sha256_of_exact_utf8_bytes(response, archive_req):
    assert archive_req.report_markdown == response.report_markdown
    assert archive_req.report_sha256 == hashlib.sha256(response.report_markdown.encode("utf-8")).hexdigest()


def test_hash_covers_non_ascii_text():
    md = "# T\n\n## Executive summary\nPrecio: 1.457,40 € · aceite de girasol ñ\n\n_The summary is assembled by code_"
    assert notion_blocks.sha256_text(md) == hashlib.sha256(md.encode("utf-8")).hexdigest()
    assert extract_executive_summary(md) == "Precio: 1.457,40 € · aceite de girasol ñ"


def test_summary_equals_the_reports_own_summary_never_regenerated(response, archive_req):
    assert archive_req.executive_summary == response.report.draft.executive_summary.strip()
    assert archive_req.executive_summary in response.report_markdown


def test_summary_extraction_stops_at_the_generated_note_or_next_heading():
    assert extract_executive_summary("# T\n## Executive summary\nA\nB\n\n## Next\nC") == "A\nB"
    with pytest.raises(PayloadError):
        extract_executive_summary("# T\n## Something else\nx")
    with pytest.raises(PayloadError):
        extract_executive_summary("# T\n## Executive summary\n\n## Next\nx")


def test_a_summary_that_disagrees_with_the_structured_report_is_refused(response):
    tampered = response.model_copy(update={"report_markdown": response.report_markdown.replace(
        response.report.draft.executive_summary.strip(), "A paraphrased summary.", 1)})
    with pytest.raises(PayloadError) as e:
        build_archive_request(tampered, mode="mock")
    assert e.value.code == "SUMMARY_MISMATCH"


def test_blocks_and_hashes_come_from_notion_blocks(response, archive_req):
    blocks = notion_blocks.render_blocks(response.report_markdown)
    assert archive_req.blocks == blocks and archive_req.total_blocks == len(blocks)
    assert archive_req.canonical_sha256 == notion_blocks.sha256_text(notion_blocks.canonical(response.report_markdown))
    assert all(len(t["text"]["content"]) <= 2000 for b in blocks for t in b[b["type"]]["rich_text"])


def test_payload_fields_and_no_secret_or_credential_names(archive_req):
    d = json.loads(archive_req.model_dump_json())
    assert d["action"] == "archive" and d["mode"] == "mock" and d["assessment_status"] in ("completed", "partial")
    assert set(d["metadata"]) == {"ingredient", "horizon_months", "assessment_date", "schema_version", "generated_at"}
    text = archive_req.model_dump_json().lower()
    for forbidden in ("database_url", "api_key", "webhook_secret", "postgresql://", "sk-"):
        assert forbidden not in text


def test_failed_assessment_is_never_archivable(response):
    failed = response.model_copy(update={"status": "failed", "report_markdown": None, "report": None})
    with pytest.raises(PayloadError) as e:
        build_archive_request(failed, mode="mock")
    assert e.value.code == "NOT_ARCHIVABLE"


def test_model_rejects_inconsistent_hash_count_and_bad_request_id(archive_req):
    d = json.loads(archive_req.model_dump_json())
    for patch in ({"report_sha256": "0" * 64}, {"total_blocks": archive_req.total_blocks + 1}, {"request_id": "bad id!"},
                  {"assessment_status": "failed"}, {"unexpected": 1}):
        with pytest.raises(ValidationError):
            ArchiveRequest.model_validate({**d, **patch})


@needs_fonts
def test_email_request_has_no_recipient_and_takes_url_from_verified_archive(archive_req):
    em = build_email_request(archive_req, PAGE_URL, build_report_pdf(archive_req))
    assert set(json.loads(em.model_dump_json())) == {"action", "request_id", "report_sha256", "notion_page_url", "executive_summary", "mode", "attachment"}
    assert em.request_id == archive_req.request_id and em.report_sha256 == archive_req.report_sha256
    with pytest.raises(ValidationError):
        EmailRequest.model_validate({**json.loads(em.model_dump_json()), "to": "someone@example.com"})
    for bad in ("http://www.notion.so/x", "https://evil.example.test/x", "https://notion.so.evil.test/x", "https://notion.com.evil.test/x", "https://user@www.notion.so/x", ""):
        assert not is_notion_url(bad)
    assert is_notion_url("https://www.notion.so/x") and is_notion_url("https://team.notion.site/x") and is_notion_url("https://app.notion.com/p/MOCK-Page-" + "a" * 32)


# ───────── configuration and secrecy ─────────
@pytest.mark.parametrize("a,e,s", [("http://n8n.example.test/a", EMAIL_URL, SECRET), (ARCHIVE_URL, "http://n8n.example.test/e", SECRET),
                                   (None, EMAIL_URL, SECRET), (ARCHIVE_URL, EMAIL_URL, ""), ("https://", EMAIL_URL, SECRET)])
def test_http_urls_and_missing_settings_are_rejected(a, e, s):
    with pytest.raises(BridgeConfigError) as exc:
        WebhookClient(a, e, s)
    assert "n8n.example.test" not in exc.value.message and SECRET not in exc.value.message


def test_from_env_and_repr_never_reveal_url_or_secret(monkeypatch):
    monkeypatch.setenv("N8N_ARCHIVE_WEBHOOK_URL", ARCHIVE_URL)
    monkeypatch.setenv("N8N_EMAIL_WEBHOOK_URL", EMAIL_URL)
    monkeypatch.setenv("N8N_WEBHOOK_SECRET", SECRET)
    c = WebhookClient.from_env()
    assert SECRET not in repr(c) and "example.test" not in repr(c) and SECRET not in str(c)


def test_secret_is_sent_only_in_the_header_and_not_logged(archive_req, caplog):
    sim = FakeN8n()
    caplog.set_level("DEBUG")
    make_client(sim).archive(archive_req)
    assert sim.seen_headers[0]["x-webhook-secret"] == SECRET
    assert SECRET not in json.dumps(sim.seen_bodies[0])
    assert SECRET not in caplog.text and "example.test" not in caplog.text and archive_req.report_markdown[:40] not in caplog.text


# ───────── archive: success only when verified ─────────
def test_archive_success_is_verified_and_creates_one_page(archive_req):
    sim = FakeN8n()
    r = make_client(sim).archive(archive_req)
    assert r.ok and r.code == "OK" and r.notion_page_url == PAGE_URL and r.blocks_found == r.blocks_expected == archive_req.total_blocks
    assert len(sim.pages) == 1 and sim.archive_calls == 1


def test_http_200_accepted_or_garbage_is_not_success(archive_req):
    for fault in ("accepted_only", "garbage"):
        sim = FakeN8n()
        sim.archive_faults = [fault]
        r = make_client(sim).archive(archive_req)
        assert not r.ok and r.code == "INVALID_RESPONSE" and sim.archive_calls == 1      # not retried: a malformed 200 is deterministic


@pytest.mark.parametrize("status", [401, 403])
def test_auth_rejection_is_distinct_and_not_retried(archive_req, status):
    sim = FakeN8n()
    c = WebhookClient(ARCHIVE_URL, EMAIL_URL, "wrong-secret", transport=sim.transport(), sleep=lambda s: None)
    r = c.archive(archive_req)
    assert r.code == "AUTH_REJECTED" and sim.archive_calls == 0 and len(sim.seen_headers) == 1


def test_timeout_is_retried_with_backoff_then_reported(archive_req):
    sim, sleeps = FakeN8n(), []
    sim.archive_faults = ["timeout_before"] * 3
    r = make_client(sim, sleeps).archive(archive_req)
    assert not r.ok and r.code == "TIMEOUT" and r.attempts == 3 and sim.archive_calls == 3 and sleeps == [1.0, 3.0] and not sim.pages


def test_transient_failure_then_success_creates_exactly_one_page(archive_req):
    sim = FakeN8n()
    sim.archive_faults = ["503", "429"]
    r = make_client(sim).archive(archive_req)
    assert r.ok and r.attempts == 3 and len(sim.pages) == 1


def test_lost_response_after_the_page_was_created_is_deduplicated_on_retry(archive_req):
    sim = FakeN8n()
    sim.archive_faults = ["timeout_after_effect"]
    r = make_client(sim).archive(archive_req)
    assert r.ok and r.duplicate_existing and r.attempts == 2 and len(sim.pages) == 1


def test_archiving_twice_never_creates_a_second_page(archive_req):
    sim = FakeN8n()
    c = make_client(sim)
    first, second = c.archive(archive_req), c.archive(archive_req)
    assert first.ok and second.ok and second.duplicate_existing and len(sim.pages) == 1


def test_connection_error_retried_and_reported(archive_req):
    sim = FakeN8n()
    sim.archive_faults = ["connect_error"] * 3
    assert make_client(sim).archive(archive_req).code == "CONNECTION_FAILED"


def test_redirect_is_not_followed_and_not_success(archive_req):
    sim = FakeN8n()
    sim.archive_faults = ["redirect"]
    r = make_client(sim).archive(archive_req)
    assert r.code == "INVALID_RESPONSE" and len(sim.seen_headers) == 1            # the secret was never sent to the redirect target


def test_incomplete_archive_is_not_success(archive_req):
    sim = FakeN8n()
    sim.store_only = archive_req.total_blocks - 1
    r = make_client(sim).archive(archive_req)
    assert not r.ok and r.code == "ARCHIVE_INCOMPLETE" and r.notion_page_url is None


def _resp(req, **kw):
    base = dict(ok=True, request_id=req.request_id, archive_status="stored", notion_page_url=PAGE_URL, blocks_expected=req.total_blocks,
                blocks_found=req.total_blocks, report_sha256_echo=req.report_sha256)
    return ArchiveResponse(**{**base, **kw})


@pytest.mark.parametrize("patch,code", [
    ({}, "OK"), ({"archive_status": "duplicate_existing"}, "OK"),
    ({"archive_status": "incomplete", "ok": False}, "ARCHIVE_INCOMPLETE"), ({"archive_status": "failed", "ok": False}, "ARCHIVE_FAILED"),
    ({"ok": False}, "ARCHIVE_FAILED"), ({"request_id": "other-id"}, "ID_MISMATCH"), ({"report_sha256_echo": "0" * 64}, "HASH_MISMATCH"),
    ({"report_sha256_echo": None}, "HASH_MISMATCH"), ({"blocks_found": 1}, "COUNT_MISMATCH"), ({"blocks_expected": 1, "blocks_found": 1}, "COUNT_MISMATCH"),
    ({"archive_status": "duplicate_existing", "blocks_found": 1}, "COUNT_MISMATCH"),
    ({"notion_page_url": None}, "URL_UNTRUSTED"), ({"notion_page_url": "https://evil.example.test/x"}, "URL_UNTRUSTED")])
def test_verification_matrix(archive_req, patch, code):
    assert verify_archive_response(archive_req, _resp(archive_req, **patch)) == code


def test_hash_and_id_mismatch_from_the_server_are_reported_not_trusted(archive_req):
    for attr, value, code in (("echo_hash", "f" * 64, "HASH_MISMATCH"), ("echo_request_id", "someone-else", "ID_MISMATCH")):
        sim = FakeN8n()
        setattr(sim, attr, value)
        r = make_client(sim).archive(archive_req)
        assert not r.ok and r.code == code and r.notion_page_url is None


# ───────── e-mail: at most once ─────────
@pytest.fixture()
def email_req(archive_req):
    if not fonts_available():
        pytest.skip("PDF fonts not installed: run python -m src.bridge.fonts --fetch")
    return build_email_request(archive_req, PAGE_URL, build_report_pdf(archive_req))


def test_email_success_sends_one_message(email_req):
    sim = FakeN8n()
    r = make_client(sim).email(email_req)
    assert r.ok and r.state == "sent" and r.message_id == "msg-1" and sim.sent == {email_req.request_id: 1}


@pytest.mark.parametrize("fault", ["timeout_after_effect", "timeout_before", "reset_before", "500_after_effect", "accepted_only"])
def test_ambiguous_email_outcomes_are_unknown_and_never_resent(email_req, fault):
    sim, sleeps = FakeN8n(), []
    sim.email_faults = [fault]
    r = make_client(sim, sleeps).email(email_req)
    assert not r.ok and r.state == "unknown" and r.code == "EMAIL_UNKNOWN"
    assert sim.email_calls == 1 and sleeps == []                                   # one attempt, no automatic retry


def test_definite_failures_are_failed_not_unknown(email_req):
    sim = FakeN8n()
    sim.email_faults = ["connect_error"]
    assert make_client(sim).email(email_req).state == "failed"
    sim.email_faults = ["reports_failed"]
    r = make_client(sim).email(email_req)
    assert r.state == "failed" and r.code == "EMAIL_FAILED"
    bad = WebhookClient(ARCHIVE_URL, EMAIL_URL, "wrong", transport=sim.transport(), sleep=lambda s: None).email(email_req)
    assert bad.state == "failed" and bad.code == "AUTH_REJECTED"


def test_manual_resend_after_a_lost_answer_gets_already_sent_and_one_real_email(email_req):
    sim = FakeN8n()
    c = make_client(sim)
    sim.email_faults = ["timeout_after_effect"]
    assert c.email(email_req).state == "unknown"
    again = c.email(email_req)
    assert again.ok and again.state == "already_sent" and sim.sent[email_req.request_id] == 1 and sim.email_calls == 2


def test_email_response_for_another_request_is_not_trusted(email_req):
    sim = FakeN8n()
    sim.handle = lambda r: __import__("httpx").Response(200, json={"ok": True, "request_id": "other", "email_status": "sent"})
    r = make_client(sim).email(email_req)
    assert not r.ok and r.state == "unknown"


# ───────── diagnostics for an unusable answer: status and shape only, never content ─────────
def archive_with_answer(archive_req, handler):
    client = WebhookClient(ARCHIVE_URL, EMAIL_URL, SECRET, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    return client.archive(archive_req)


def test_unusable_archive_answers_say_what_is_wrong_without_echoing_content(archive_req):
    cases = [
        (lambda r: httpx.Response(200, json={"message": "Workflow was started"}), "JSON object without ok, request_id, archive_status, blocks_expected, blocks_found; keys present: message"),
        (lambda r: httpx.Response(200, content=json.dumps(json.dumps({"ok": True})).encode(), headers={"content-type": "application/json"}), "body is a JSON string (double-encoded JSON?)"),
        (lambda r: httpx.Response(200, text="<html>SECRET-BODY-TEXT</html>"), "body is not JSON"),
        (lambda r: httpx.Response(302, headers={"location": "https://elsewhere.example.test/"}), "redirect not followed"),
        (lambda r: httpx.Response(200, json={"ok": True, "request_id": "x", "archive_status": "stored", "blocks_expected": 1, "blocks_found": "many"}), "JSON fields have unexpected types or values"),
    ]
    for handler, expected in cases:
        r = archive_with_answer(archive_req, handler)
        assert not r.ok and r.code == "INVALID_RESPONSE" and expected in r.detail, r.detail
        assert "SECRET-BODY-TEXT" not in r.detail and "elsewhere" not in r.detail


def test_untrusted_notion_host_has_its_own_code(archive_req):
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "request_id": body["request_id"], "archive_status": "stored", "notion_page_url": "https://app.notion.example/p/abc",
                                         "blocks_expected": body["total_blocks"], "blocks_found": body["total_blocks"], "report_sha256_echo": body["report_sha256"]})
    r = archive_with_answer(archive_req, handler)
    assert not r.ok and r.code == "URL_UNTRUSTED" and "notion.so, notion.site or notion.com" in r.detail and r.notion_page_url is None


def test_email_failure_reports_the_n8n_error_codes_and_nothing_else(email_req):
    sim = FakeN8n()
    body = {"ok": False, "request_id": email_req.request_id, "email_status": "failed", "error_code": "ARCHIVE_NOT_READY", "details": ["ARCHIVE_NOT_STORED", "free text must not appear"],
            "message": "SECRET-TEXT"}
    client = WebhookClient(ARCHIVE_URL, EMAIL_URL, SECRET, transport=httpx.MockTransport(lambda r: httpx.Response(409, json=body)), sleep=lambda s: None)
    r = client.email(email_req)
    assert r.state == "failed" and r.code == "EMAIL_FAILED"
    assert r.detail == "HTTP 409, n8n error_code ARCHIVE_NOT_READY, details ARCHIVE_NOT_STORED"
    assert "SECRET-TEXT" not in r.detail and "free text" not in r.detail and sim.email_calls == 0
    unknown = WebhookClient(ARCHIVE_URL, EMAIL_URL, SECRET, sleep=lambda s: None,
                            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": False, "request_id": email_req.request_id, "email_status": "unknown", "error_code": "GMAIL_SEND_UNKNOWN"}))).email(email_req)
    assert unknown.state == "unknown" and "GMAIL_SEND_UNKNOWN" in unknown.detail
