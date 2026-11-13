"""Offline tests for the fpdf2 PDF export, the font handling and the e-mail attachment contract. No network (the font download is mocked)."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.agent.graph import run_assessment
from src.bridge import fonts as fontmod
from src.bridge import pdf as pdfmod
from src.bridge.client import WebhookClient
from src.bridge.models import EmailAttachment, EmailRequest
from src.bridge.payload import build_archive_request, build_email_request, build_report_pdf
from src.bridge.pdf import FOOTER_TEXT, PdfError, markdown_to_pdf, verify_pdf_structure
from src.bridge.pdf_text import extract_pdf_text
from tests.agent_fixtures import REQUEST, deps
from tests.bridge_sim import ARCHIVE_URL, EMAIL_URL, PAGE_URL, SECRET, FakeN8n
from tests.pdf_fonts import needs_fonts

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
STATUS_LINE = "**Request:** req-1 · **Assessment date:** 2026-11-12 · **Status:** {status}"


def make(md: str):
    return markdown_to_pdf(md, title="T", footer_label="req-1", created=NOW)


def body_text(data: bytes) -> str:
    """All body strings of the PDF (footers removed), whitespace removed."""
    return re.sub(r"\s+", "", "".join("".join(pdfmod.body_lines(p)) for p in extract_pdf_text(data)))


def all_lines(data: bytes) -> list[str]:
    return [ln for p in extract_pdf_text(data) for ln in pdfmod.body_lines(p)]


@pytest.fixture(scope="module")
def response():
    resp, _ = run_assessment(dict(REQUEST), deps())
    return resp


@pytest.fixture(scope="module")
def archive(response):
    return build_archive_request(response, mode="mock")


@pytest.fixture(scope="module")
def doc(archive):
    if not fontmod.fonts_available():
        pytest.skip("PDF fonts not installed: run python -m src.bridge.fonts --fetch")
    return build_report_pdf(archive)


def plain(md: str) -> str:
    return re.sub(r"\s+", "", md)


# ───────── integrity ─────────
@needs_fonts
def test_pdf_is_well_formed_and_deterministic(archive, doc):
    assert doc.data.startswith(b"%PDF-") and doc.data.rstrip().endswith(b"%%EOF")
    assert verify_pdf_structure(doc.data) == doc.pages >= 2
    assert doc.sha256 == hashlib.sha256(doc.data).hexdigest() and doc.size_bytes == len(doc.data)
    again = build_report_pdf(archive)
    assert again.data == doc.data and again.sha256 == doc.sha256                       # same report, same bytes, same hash


@needs_fonts
def test_structure_check_detects_corruption(doc):
    for bad in (doc.data.replace(b"2 0 obj", b"9 0 obj", 1), doc.data[:-30], b"not a pdf", doc.data.replace(b"/Count", b"/Cxunt", 1)):
        with pytest.raises(PdfError):
            verify_pdf_structure(bad)


@needs_fonts
def test_complete_report_text_is_in_the_pdf_including_warnings_tables_and_citations(response, doc):
    text = body_text(doc.data)
    md = response.report_markdown
    assert "## Warnings" in md and "|---" in md and "## Executive summary" in md
    for line in md.splitlines():
        if not line.strip() or re.fullmatch(r"\s*(-{3,}|\*{3,}|_{3,})\s*", line):
            continue
        if line.lstrip().startswith("|"):                                 # a table cell may wrap, so check its words
            cells = [c.strip() for c in line.strip().strip("|").split("|") if not set(c.strip()) <= set("-: ")]
            for cell in cells:
                for token in cell.replace("**", "").split():
                    if len(token) <= 24:
                        assert plain(token) in text, token
            continue
        body = re.sub(r"^(#{1,6} +|\s*[-*] +|> ?)", "", line.strip())
        body = re.sub(r"(?<!\w)_(?=\S)|(?<=\S)_(?!\w)", "", body.replace("**", "").replace("`", ""))
        assert plain(body) in text, body[:70]                             # headings, paragraphs, bullets, quotes: complete and in order
    for w in response.warnings:
        assert plain(w) in text
    quotes = [c.quote for f in (response.report.draft.risks or []) for c in f.citations if getattr(c, "quote", None)]
    assert quotes, "the fixture report must contain cited quotes"
    for q in quotes:
        assert plain(q) in text
    assert doc.text_pieces > 100 and doc.replaced_chars == 0


@needs_fonts
def test_financial_values_survive_exactly(response, doc):
    lines = " ".join(all_lines(doc.data))
    for value in ("1,961,496.55", "107,744.17", "636,650.40", "451.290", "778.710", "65,771.05", "41,973.12", "+5.49%", "-215,488.34"):
        assert value in response.report_markdown and value in lines, value


@needs_fonts
def test_numbers_and_ids_in_table_cells_are_not_split_across_lines(response, doc):
    lines = all_lines(doc.data)
    tokens = {t for ln in response.report_markdown.splitlines() if ln.startswith("|") for c in ln.strip("|").split("|")
              for t in c.replace("**", "").split() if re.fullmatch(r"[-+]?\d[\d.,]*\d%?|\d{4}-\d{2}|CT-\d{3}|SUP-\d{3}", t)}
    assert len(tokens) > 20
    for t in tokens:
        assert any(t in ln for ln in lines), t


@needs_fonts
def test_every_page_has_a_footer_with_request_id_and_page_numbers(archive, doc):
    pages = extract_pdf_text(doc.data)
    for n, lines in enumerate(pages, start=1):
        tail = re.sub(r"\s+", "", "".join(lines[-4:]))
        assert re.sub(r"\s+", "", f"{archive.request_id}|{FOOTER_TEXT}") in tail and f"Pageof{n}" not in tail or f"Page{n}of{len(pages)}" in tail


@needs_fonts
def test_no_page_is_nearly_empty_except_possibly_the_last(doc):
    pages = extract_pdf_text(doc.data)
    assert all(len(pdfmod.body_lines(p)) >= 25 for p in pages[:-1])


# ───────── special characters ─────────
@needs_fonts
def test_unicode_text_is_preserved():
    text = ("Δ benchmark → cost ≥ 107.744,17 (≈ 5,5%) · € ñ señor «Цена на подсолнечное масло» zażółć α β Ω "
            "“curly” ‘single’ – — … ™ ½ ± °")
    d = make(f"# T\n\n{text}\n")
    assert d.replaced_chars == 0
    assert plain(text) in body_text(d.data)


@needs_fonts
def test_glyphs_outside_the_font_become_question_marks_and_are_announced():
    d = make("# T\n\nKanji " + "".join(chr(c) for c in (0x65E5, 0x672C, 0x8A9E)) + " and emoji " + chr(0x1F642) + " here.\n")
    t = body_text(d.data)
    assert d.replaced_chars == 4 and plain("Kanji ??? and emoji ? here.") in t
    assert plain("NOTICE: 4 character(s)") in t and plain("The Notion page holds the exact text.") in t


@needs_fonts
def test_inline_markup_is_applied_and_literal_inside_words():
    t = body_text(make("# T\n\n**Bold** and _italic_ and `code_x` and snake_case_name and price_exposure.\n").data)
    for word in ("Bold", "italic", "code_x", "snake_case_name", "price_exposure"):
        assert word in t
    assert "**Bold**" not in t and "_italic_" not in t and "`code_x`" not in t


@needs_fonts
def test_an_unpaired_marker_stays_literal():
    t = body_text(make("# T\n\nfive ** three and unpaired ` tick and 2 _ 3.\n").data)
    assert plain("five ** three and unpaired ` tick and 2 _ 3.") in t


@needs_fonts
def test_long_url_is_not_lost_and_irregular_table_keeps_every_cell():
    url = "https://example.invalid/" + "a" * 300
    d = make(f"# T\n\n- see {url}\n\n| a | b |\n|---|---|\n| only one |\n")
    t = body_text(d.data)
    assert url in t and "onlyone" in t and "|a|b|" in t


@needs_fonts
def test_partial_status_gets_a_banner_and_completed_does_not():
    partial = " ".join(all_lines(make("# T\n\n" + STATUS_LINE.format(status="PARTIAL") + "\n\n## Warnings\n- one\n- two\n").data))
    completed = " ".join(all_lines(make("# T\n\n" + STATUS_LINE.format(status="COMPLETED") + "\n\n## Warnings\n- one\n").data))
    assert "PARTIAL ASSESSMENT" in partial and "2 warning(s) listed" in partial and "Human review is required" in partial
    assert "ASSESSMENT" not in completed.replace("Assessment date", "")


@needs_fonts
def test_table_header_repeats_after_a_page_break():
    rows = "\n".join(f"| row{i} | value {i} |" for i in range(120))
    d = make(f"# T\n\n| Name | Value |\n|---|---|\n{rows}\n")
    pages = extract_pdf_text(d.data)
    assert len(pages) >= 2 and all("Name" in pdfmod.body_lines(p) for p in pages)


# ───────── limits and failures ─────────
def test_empty_and_oversized_inputs_are_refused_with_codes():
    for md, code in (("   \n ", "PDF_EMPTY"), ("x" * (pdfmod.MAX_MARKDOWN_BYTES + 1), "PDF_INPUT_TOO_LARGE")):
        with pytest.raises(PdfError) as e:
            make(md)
        assert e.value.code == code


@needs_fonts
def test_page_and_size_limits(monkeypatch):
    big = "\n\n".join(f"paragraph {i} " + "word " * 40 for i in range(200))
    monkeypatch.setattr(pdfmod, "MAX_PAGES", 2)
    with pytest.raises(PdfError) as e:
        make(big)
    assert e.value.code == "PDF_TOO_MANY_PAGES"
    monkeypatch.setattr(pdfmod, "MAX_PAGES", 200)
    monkeypatch.setattr(pdfmod, "MAX_PDF_BYTES", 1000)
    with pytest.raises(PdfError) as e:
        make("# T\n\nhello")
    assert e.value.code == "PDF_TOO_LARGE"


@needs_fonts
def test_render_failure_is_reported_with_a_fixed_message_and_no_content(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret text SECRET-REPORT-CONTENT")
    monkeypatch.setattr(pdfmod, "_render", boom)
    with pytest.raises(PdfError) as e:
        make("# T\n\nSECRET-REPORT-CONTENT")
    assert e.value.code == "PDF_RENDER_FAILED" and "SECRET" not in e.value.message and e.value.__cause__ is None


@needs_fonts
def test_incomplete_output_is_never_returned(monkeypatch):
    monkeypatch.setattr("src.bridge.pdf_text.extract_pdf_text", lambda data: [["only a footer " + FOOTER_TEXT]])
    with pytest.raises(PdfError) as e:
        make("# T\n\nsome body text that must be found again")
    assert e.value.code == "PDF_INCOMPLETE"


@needs_fonts
def test_real_report_size_is_far_below_the_limit(doc):
    assert doc.size_bytes < 400_000 < pdfmod.MAX_PDF_BYTES and doc.pages < 20


# ───────── fonts ─────────
def test_missing_or_modified_fonts_fail_closed_with_codes(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_FONT_DIR", str(tmp_path))
    with pytest.raises(PdfError) as e:
        make("# T\n\nhello")
    assert e.value.code == "PDF_FONT_MISSING" and str(tmp_path) not in e.value.message
    for name in fontmod.FILES:
        (tmp_path / name).write_bytes(b"not a font")
    with pytest.raises(PdfError) as e:
        make("# T\n\nhello")
    assert e.value.code == "PDF_FONT_INVALID"
    assert fontmod.fonts_available() is False


def test_font_fetch_refuses_an_archive_with_the_wrong_hash(tmp_path, monkeypatch):
    class R:
        content = b"not the pinned archive"
    monkeypatch.setattr("httpx.get", lambda *a, **k: R())
    with pytest.raises(fontmod.FontError) as e:
        fontmod.fetch(tmp_path)
    assert e.value.code == "PDF_FONT_INVALID" and list(tmp_path.iterdir()) == []


def test_font_pins_match_the_manifest():
    manifest = (fontmod.DEFAULT_DIR / "MANIFEST.md").read_text(encoding="utf-8")
    assert fontmod.ARCHIVE_SHA256 in manifest and fontmod.RELEASE_URL in manifest
    for name, digest in fontmod.FILES.items():
        assert name in manifest and digest in manifest


# ───────── attachment contract ─────────
@needs_fonts
def test_email_attachment_is_the_declared_pdf(archive, doc):
    em = build_email_request(archive, PAGE_URL, doc)
    a = em.attachment
    raw = base64.b64decode(a.content_base64, validate=True)
    assert raw == doc.data and a.size_bytes == len(raw) and a.sha256 == doc.sha256 and a.pages == doc.pages
    assert a.filename == f"procurement-risk-assessment-{archive.request_id}.pdf" and a.content_type == "application/pdf" and a.encoding == "base64"
    d = json.loads(em.model_dump_json())
    assert set(d) == {"action", "request_id", "report_sha256", "notion_page_url", "executive_summary", "mode", "attachment"}
    assert set(d["attachment"]) == {"filename", "content_type", "encoding", "size_bytes", "sha256", "pages", "content_base64"}
    assert em.report_sha256 == archive.report_sha256 and em.executive_summary == archive.executive_summary    # unchanged by the PDF


def _att(doc, **kw):
    base = dict(filename="procurement-risk-assessment-req-1.pdf", size_bytes=doc.size_bytes, sha256=doc.sha256, pages=doc.pages,
                content_base64=base64.b64encode(doc.data).decode())
    return {**base, **kw}


@needs_fonts
@pytest.mark.parametrize("patch", [
    {"content_base64": "***not base64***"}, {"size_bytes": 5}, {"sha256": "0" * 64}, {"filename": "../evil.pdf"}, {"filename": "x.exe"},
    {"content_type": "text/html"}, {"encoding": "hex"}, {"pages": 0}, {"unexpected": 1}])
def test_attachment_validation_rejects_tampering(doc, patch):
    with pytest.raises(ValidationError):
        EmailAttachment(**_att(doc, **patch))


@needs_fonts
def test_attachment_that_is_not_a_pdf_truncated_or_too_big_is_rejected(doc):
    junk = b"<html>hello</html>"
    with pytest.raises(ValidationError):
        EmailAttachment(**_att(doc, content_base64=base64.b64encode(junk).decode(), size_bytes=len(junk), sha256=hashlib.sha256(junk).hexdigest()))
    with pytest.raises(ValidationError):
        EmailAttachment(**_att(doc, size_bytes=pdfmod.MAX_PDF_BYTES + 1))
    truncated = doc.data[:-30]
    with pytest.raises(ValidationError):
        EmailAttachment(**_att(doc, content_base64=base64.b64encode(truncated).decode(), size_bytes=len(truncated),
                               sha256=hashlib.sha256(truncated).hexdigest()))


@needs_fonts
def test_attachment_filename_must_match_the_request_id(archive, doc):
    em = json.loads(build_email_request(archive, PAGE_URL, doc).model_dump_json())
    em["request_id"] = "another-id"
    with pytest.raises(ValidationError):
        EmailRequest.model_validate(em)


@needs_fonts
def test_simulated_n8n_receives_and_attaches_exactly_the_pdf(archive, doc):
    sim = FakeN8n()
    client = WebhookClient(ARCHIVE_URL, EMAIL_URL, SECRET, transport=sim.transport(), sleep=lambda s: None)
    r = client.email(build_email_request(archive, PAGE_URL, doc))
    assert r.ok and r.state == "sent" and sim.attachments[archive.request_id] == doc.data


@needs_fonts
def test_sent_without_a_matching_attachment_echo_is_not_reported_as_success(archive, doc):
    for attr in ("skip_echo", "wrong_echo"):
        sim = FakeN8n()
        setattr(sim, attr, True)
        client = WebhookClient(ARCHIVE_URL, EMAIL_URL, SECRET, transport=sim.transport(), sleep=lambda s: None)
        r = client.email(build_email_request(archive, PAGE_URL, doc))
        assert not r.ok and r.state == "unknown" and r.code == "ATTACHMENT_UNCONFIRMED" and sim.sent[archive.request_id] == 1
