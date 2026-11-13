"""Webhook contracts between the local Python app and n8n (documented in docs/day4-architecture.md section 19).

ARCHIVE  request -> ArchiveRequest   response -> ArchiveResponse
EMAIL    request -> EmailRequest     response -> EmailResponse
Nothing here carries credentials, database details or the webhook secret (the secret travels only in the X-Webhook-Secret header)."""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.api.notion_blocks import sha256_text
from src.bridge.pdf import MAX_PDF_BYTES

REQUEST_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
_NOTION_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*(?:notion\.so|notion\.site|notion\.com)$")


class ArchiveMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ingredient: str
    horizon_months: int = Field(ge=1, le=6)
    assessment_date: str
    schema_version: str
    generated_at: str


class ArchiveRequest(BaseModel):
    """Sent to the archive webhook. Only completed/partial assessments are ever archived; a failed one has no report."""
    model_config = ConfigDict(extra="forbid")
    action: Literal["archive"] = "archive"
    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    assessment_status: Literal["completed", "partial"]
    mode: Literal["mock", "live"]
    report_markdown: str = Field(min_length=1)
    report_sha256: str = Field(pattern=SHA256_PATTERN)           # SHA-256 of the exact UTF-8 bytes of report_markdown
    executive_summary: str = Field(min_length=1)                  # the report's own Executive summary section, extracted by code
    warnings: list[str]
    metadata: ArchiveMetadata
    total_blocks: int = Field(ge=1)
    canonical_sha256: str = Field(pattern=SHA256_PATTERN)         # SHA-256 of the layout-normalised Markdown the blocks reproduce
    blocks: list[dict[str, Any]] = Field(min_length=1)            # flat Notion blocks (src/api/notion_blocks.render_blocks)

    @model_validator(mode="after")
    def _consistent(self) -> "ArchiveRequest":
        if sha256_text(self.report_markdown) != self.report_sha256:
            raise ValueError("report_sha256 does not match report_markdown")
        if len(self.blocks) != self.total_blocks:
            raise ValueError("total_blocks does not match blocks")
        return self


class ArchiveResponse(BaseModel):
    """What n8n must answer. Unknown extra fields are ignored; every field below is required except the two nullable ones."""
    model_config = ConfigDict(extra="ignore")
    ok: bool
    request_id: str
    archive_status: Literal["stored", "incomplete", "failed", "duplicate_existing"]
    notion_page_url: str | None = None
    blocks_expected: int = Field(ge=0)
    blocks_found: int = Field(ge=0)
    report_sha256_echo: str | None = None


class EmailAttachment(BaseModel):
    """The complete report as a PDF, carried as base64 inside the (authenticated) webhook JSON. n8n turns it into a binary file for Gmail."""
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(pattern=r"^procurement-risk-assessment-[A-Za-z0-9_-]{1,64}\.pdf$")
    content_type: Literal["application/pdf"] = "application/pdf"
    encoding: Literal["base64"] = "base64"
    size_bytes: int = Field(ge=1, le=MAX_PDF_BYTES)               # size of the DECODED PDF
    sha256: str = Field(pattern=SHA256_PATTERN)                   # SHA-256 of the decoded PDF bytes
    pages: int = Field(ge=1)
    content_base64: str = Field(min_length=1, max_length=(MAX_PDF_BYTES * 4) // 3 + 8)

    @model_validator(mode="after")
    def _is_the_declared_pdf(self) -> "EmailAttachment":
        try:
            raw = base64.b64decode(self.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("content_base64 is not valid base64") from None
        if len(raw) != self.size_bytes:
            raise ValueError("size_bytes does not match the decoded attachment")
        if hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("sha256 does not match the decoded attachment")
        if not raw.startswith(b"%PDF-") or not raw.rstrip().endswith(b"%%EOF"):
            raise ValueError("the attachment is not a PDF")
        return self


class EmailRequest(BaseModel):
    """Sent to the e-mail webhook. The recipient is FIXED inside n8n and is never part of this payload.
    `mode` is a label for the subject line (mock output must never look like live output)."""
    model_config = ConfigDict(extra="forbid")
    action: Literal["email"] = "email"
    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    report_sha256: str = Field(pattern=SHA256_PATTERN)
    notion_page_url: str
    executive_summary: str = Field(min_length=1)
    mode: Literal["mock", "live"]
    attachment: EmailAttachment

    @model_validator(mode="after")
    def _attachment_belongs_to_request(self) -> "EmailRequest":
        if self.attachment.filename != f"procurement-risk-assessment-{self.request_id}.pdf":
            raise ValueError("attachment filename does not match request_id")
        return self

    @field_validator("notion_page_url")
    @classmethod
    def _notion_url(cls, v: str) -> str:
        if not is_notion_url(v):
            raise ValueError("notion_page_url must be an https Notion URL")
        return v


class EmailResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ok: bool
    request_id: str
    email_status: Literal["sent", "failed", "already_sent", "unknown"]
    message_id: str | None = None
    attachment_sha256_echo: str | None = None                 # required for "sent": the SHA-256 of the PDF n8n actually attached


def is_notion_url(url: str | None) -> bool:
    """https only, host notion.so, notion.site or notion.com (or a subdomain; app.notion.com is what Notion returns today). An unexpected host is rejected, never trusted."""
    if not url or not url.startswith("https://"):
        return False
    rest = url[len("https://"):]
    host = re.split(r"[/?#]", rest, maxsplit=1)[0].lower()
    return "@" not in host and ":" not in host and bool(_NOTION_HOST.match(host))
