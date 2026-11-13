"""Construction of webhook payloads from a finished assessment. Pure functions: no network, no randomness.

The Executive Summary is EXTRACTED from the exact report Markdown (never regenerated or paraphrased). When the structured report is at hand the
extraction is cross-checked against it, so a rendering change that breaks the extraction fails loudly instead of sending a wrong summary."""
from __future__ import annotations

import base64
from datetime import datetime, timezone

from src.agent.schemas import AssessmentResponse
from src.api import notion_blocks
from src.bridge.models import ArchiveMetadata, ArchiveRequest, EmailAttachment, EmailRequest
from src.bridge.pdf import PdfDocument, markdown_to_pdf

SUMMARY_HEADING = "## Executive summary"
SUMMARY_NOTE_START = "_The summary is assembled by code"     # the italic note render.py writes right after the summary


class PayloadError(Exception):
    """The payload cannot be built safely (message is fixed text; it never echoes report content)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def extract_executive_summary(report_markdown: str) -> str:
    """Text of the report's '## Executive summary' section, up to the code-generated note (or the next '## ' heading)."""
    lines = report_markdown.replace("\r\n", "\n").split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if ln.rstrip() == SUMMARY_HEADING) + 1
    except StopIteration:
        raise PayloadError("SUMMARY_NOT_FOUND", "The report has no Executive summary section.") from None
    body: list[str] = []
    for ln in lines[start:]:
        if ln.startswith(SUMMARY_NOTE_START) or ln.startswith("## "):
            break
        body.append(ln.rstrip())
    text = "\n".join(body).strip()
    if not text:
        raise PayloadError("SUMMARY_EMPTY", "The report's Executive summary section is empty.")
    return text


def build_archive_request(response: AssessmentResponse, *, mode: str, generated_at: datetime | None = None) -> ArchiveRequest:
    if response.status not in ("completed", "partial") or not response.report_markdown or response.report is None:
        raise PayloadError("NOT_ARCHIVABLE", "Only a completed or partial assessment with a report can be archived.")
    md = response.report_markdown
    summary = extract_executive_summary(md)
    if summary != response.report.draft.executive_summary.strip():
        raise PayloadError("SUMMARY_MISMATCH", "The extracted summary differs from the report's own summary; nothing was prepared.")
    blocks = notion_blocks.render_blocks(md)
    stamp = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    rep = response.report
    return ArchiveRequest(
        request_id=response.request_id, assessment_status=response.status, mode=mode, report_markdown=md,
        report_sha256=notion_blocks.sha256_text(md), executive_summary=summary, warnings=list(response.warnings),
        metadata=ArchiveMetadata(ingredient=rep.ingredient, horizon_months=rep.horizon_months,
                                 assessment_date=str(response.assessment_date or rep.assessment_date), schema_version=response.schema_version,
                                 generated_at=stamp),
        total_blocks=len(blocks), canonical_sha256=notion_blocks.sha256_text(notion_blocks.canonical(md)), blocks=blocks)


def build_report_pdf(archive: ArchiveRequest) -> PdfDocument:
    """PDF of the EXACT report Markdown that was archived (warnings, tables, citations included). Deterministic: same report, same bytes."""
    created = datetime.fromisoformat(archive.metadata.generated_at)
    return markdown_to_pdf(archive.report_markdown, title=f"Procurement risk assessment {archive.request_id}", footer_label=archive.request_id,
                           created=created)


def build_email_request(archive: ArchiveRequest, notion_page_url: str, pdf: PdfDocument) -> EmailRequest:
    """The page URL must be the one returned by a VERIFIED archive; the summary, hash and PDF come from the archived payload."""
    return EmailRequest(request_id=archive.request_id, report_sha256=archive.report_sha256, notion_page_url=notion_page_url,
                        executive_summary=archive.executive_summary, mode=archive.mode,
                        attachment=EmailAttachment(filename=f"procurement-risk-assessment-{archive.request_id}.pdf", size_bytes=pdf.size_bytes,
                                                   sha256=pdf.sha256, pages=pdf.pages, content_base64=base64.b64encode(pdf.data).decode("ascii")))
