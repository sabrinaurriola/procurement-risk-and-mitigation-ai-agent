"""Server-side controller for the local Gradio app: run -> (archive in Notion via n8n) -> (e-mail via n8n).

Reuses the existing agent unchanged: `run_assessment` (src/agent/graph.py) with the dependencies from `build_live_deps` (src/agent/run.py,
the same wiring as `python -m src.agent.run --live`) or, for offline demos, the fixture dependencies of `--mock`. No validator, prompt,
calculation or report renderer is touched or re-implemented here.

Everything sensitive stays in this process: the view returned to the browser holds only display text, flags and the Notion page URL.
One operation at a time (run, archive, e-mail share one lock), so a double click can never start a second assessment, page or e-mail.
Error messages shown to the user are fixed texts, codes or exception CLASS names, never exception text, URLs or secrets."""
from __future__ import annotations

import logging
import os
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from src.agent.graph import Deps, run_assessment
from src.agent.live_record import build_record, write_record, write_trace
from src.agent.llm import LLMError
from src.agent.run import LiveRefused, build_live_deps
from src.agent.schemas import AssessmentRequest, AssessmentResponse
from src.bridge.client import BridgeConfigError, WebhookClient
from src.bridge.models import ArchiveRequest
from src.bridge.payload import PayloadError, build_archive_request, build_email_request, build_report_pdf
from src.bridge.pdf import PdfDocument, PdfError

log = logging.getLogger("procurement.ui")
RUNS_DIR = Path(__file__).resolve().parents[2] / "runs"
# UI-only demonstration list. Only Sunflower Oil is functional; the others are placeholders and can never reach the agent
# (the agent's own supported list is unchanged: src/agent/config.py).
COMMODITY_CHOICES = [("Sunflower Oil", "sunflower_oil"), ("Wheat Flour", "wheat_flour"), ("Granulated Sugar", "granulated_sugar"), ("Butter", "butter")]
SUPPORTED_COMMODITIES = frozenset({"sunflower_oil"})
UNSUPPORTED_MESSAGE = "Assessment for this commodity is not yet available. Currently, only Sunflower Oil is supported."
DepsFactory = Callable[[str], Deps]
ClientFactory = Callable[[], WebhookClient]


def mock_deps(request_id: str) -> Deps:
    """Offline demo wiring (identical to `src.agent.run --mock`): REAL read-only database data, fixture research, template drafting."""
    from src.agent.data import PostgresData
    from src.agent.mock import DemoLLM, MockSearch, build_mock_policy
    today = date.today()
    return Deps(PostgresData(), DemoLLM(), MockSearch("tavily", today), MockSearch("gdelt", today), policy=build_mock_policy(), mock=True)


def live_deps(request_id: str) -> Deps:
    return build_live_deps(request_id)


@dataclass(frozen=True)
class View:
    """Everything the browser may see. No secrets, no webhook URL."""
    message: str = ""
    assessment_status: str = ""
    warnings: str = ""
    report_markdown: str = ""
    archive_message: str = ""
    notion_url: str = ""
    email_message: str = ""
    run_enabled: bool = True
    archive_enabled: bool = False
    email_enabled: bool = False
    confirm_resend_visible: bool = False
    request_id: str = ""


class AppController:
    def __init__(self, deps_factory: DepsFactory, *, mode: str, client_factory: ClientFactory = WebhookClient.from_env,
                 runs_dir: Path = RUNS_DIR, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), auto_archive: bool | None = None,
                 max_live_runs: int = 1):
        if mode not in ("mock", "live"):
            raise ValueError("mode must be mock or live")
        self.mode, self._deps_factory, self._client_factory, self._runs_dir, self._clock = mode, deps_factory, client_factory, runs_dir, clock
        # A completed/partial report (mock or live) is archived in Notion right after the run (user decision of 13 Nov, reversing the earlier
        # "live is manual" rule). A FAILED assessment is never archived. The e-mail always needs the user.
        self.auto_archive = True if auto_archive is None else auto_archive
        self.max_live_runs, self._live_runs = max_live_runs, 0       # a live session may start only this many paid assessments
        self.live_record_path: Path | None = None
        self.live_trace_path: Path | None = None
        self._lock = threading.Lock()
        self._busy = False
        self._reset()

    # ───────── state ─────────
    def _reset(self) -> None:
        self.request_id = ""
        self.response: AssessmentResponse | None = None
        self.archive_req: ArchiveRequest | None = None
        self.pdf: PdfDocument | None = None
        self.pdf_error = ""
        self.notion_url = ""
        self.archive_verified = False
        self.email_state = "not_sent"          # not_sent | sent | already_sent | failed | unknown
        self._status = self._warnings = self._report = self._archive_msg = self._email_msg = self._msg = ""

    def new_request_id(self) -> str:
        rid = f"ui-{self._clock().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rid)
        return rid

    def view(self, message: str | None = None) -> View:
        if message is not None:
            self._msg = message
        can_email = self.archive_verified and self.pdf is not None and self.email_state in ("not_sent", "failed", "unknown")
        return View(message=self._msg, assessment_status=self._status, warnings=self._warnings, report_markdown=self._report,
                    archive_message=self._archive_msg, notion_url=self.notion_url if self.archive_verified else "", email_message=self._email_msg,
                    run_enabled=True, archive_enabled=self.archive_req is not None and not self.archive_verified,
                    email_enabled=can_email, confirm_resend_visible=self.archive_verified and self.email_state == "unknown",
                    request_id=self.request_id)

    def busy_view(self, message: str) -> View:
        """Interim display while an operation runs: every button disabled."""
        return View(message=message, assessment_status=self._status, warnings=self._warnings, report_markdown=self._report,
                    archive_message=self._archive_msg, notion_url=self.notion_url if self.archive_verified else "", email_message=self._email_msg,
                    run_enabled=False, archive_enabled=False, email_enabled=False, request_id=self.request_id)

    def _enter(self) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            return True

    def _leave(self) -> None:
        with self._lock:
            self._busy = False

    # ───────── 1. run the assessment ─────────
    def run(self, ingredient: str, horizon_months: int, assessment_date: str | None = None, live_approved: bool = False) -> View:
        """The assessment date is always today unless a caller (tests, demos) passes one; the UI does not offer it."""
        if not self._enter():
            return self.busy_view("An operation is already in progress. Wait for it to finish.")
        try:
            view = self._run(ingredient, horizon_months, assessment_date, live_approved)
            if self.auto_archive and self.archive_req is not None:       # only a completed or partial report with a valid payload gets here
                base = self._msg
                self._do_archive()
                return self.view(base + (" Archived in Notion automatically." if self.archive_verified
                                         else " The automatic Notion archive was NOT confirmed (see below): use the retry button."))
            return view
        finally:
            self._leave()

    def _run(self, ingredient: str, horizon: int, date_text: str | None, live_approved: bool) -> View:
        self._reset()
        if ingredient not in SUPPORTED_COMMODITIES:          # before anything else: no request, no dependencies, no paid call, no archive
            return self.view(UNSUPPORTED_MESSAGE)
        if self.mode == "live" and not live_approved:
            return self.view("Live run not started: tick the approval box. A live run makes paid OpenAI and Tavily calls.")
        if self.mode == "live" and self._live_runs >= self.max_live_runs:
            return self.view(f"Live run limit reached: this session allows {self.max_live_runs} live assessment(s). Restart the app to run another.")
        try:
            as_of = date.fromisoformat(date_text.strip()) if date_text and date_text.strip() else None
        except ValueError:
            return self.view("The assessment date must be empty or written YYYY-MM-DD.")
        rid = self.new_request_id()
        try:
            request = AssessmentRequest(request_id=rid, ingredient=ingredient, horizon_months=int(horizon), assessment_date=as_of)
        except (ValidationError, ValueError, TypeError):
            return self.view("The request is invalid: check the ingredient, the horizon (1-6) and the date (within the allowed range).")
        self.request_id = rid
        try:
            deps = self._deps_factory(rid)
        except LiveRefused as exc:
            return self.view(self._refusal_text(exc.reason))
        except LLMError as exc:
            return self.view(f"Live run refused before any external call ({exc.code}).")
        except Exception as exc:  # noqa: BLE001 - class name only
            return self.view(f"The agent could not be prepared ({type(exc).__name__}).")
        started = self._clock()
        if self.mode == "live":
            self._live_runs += 1                                # counted when the paid run actually starts (refusals above do not use it up)
        try:
            response, trace = run_assessment(request, deps)
        except Exception as exc:  # noqa: BLE001 - run_assessment does not raise for business failures; this is a defect guard
            log.error("assessment crashed: %s", type(exc).__name__)
            return self.view(f"The assessment crashed unexpectedly ({type(exc).__name__}). Nothing was archived.")
        if self.mode == "live":
            self._save_live_record(response, trace, started)
        self.response = response
        self._status = response.status.upper() + (" (MOCK)" if self.mode == "mock" else "")
        self._warnings = "\n".join(f"- {w}" for w in response.warnings)
        self._report = response.report_markdown or ""
        if response.status == "failed" or not response.report_markdown:
            err = response.error
            return self.view(f"The assessment FAILED ({err.code if err else 'no report'}): {err.message if err else 'no report was produced'}. "
                             "A failed assessment is never archived.")
        try:
            self._runs_dir.mkdir(parents=True, exist_ok=True)
            (self._runs_dir / f"{rid}.md").write_text(response.report_markdown, encoding="utf-8")
        except OSError as exc:
            log.error("could not save the local report copy (%s)", type(exc).__name__)
        try:
            self.archive_req = build_archive_request(response, mode=self.mode, generated_at=self._clock())
        except PayloadError as exc:
            return self.view(f"Assessment {response.status.upper()}, but the archive payload could not be prepared ({exc.code}).")
        try:
            self.pdf = build_report_pdf(self.archive_req)
            note = f" PDF ready: {self.pdf.pages} pages, {self.pdf.size_bytes // 1024 + 1} KB" + (
                f" ({self.pdf.replaced_chars} character(s) could not be drawn)" if self.pdf.replaced_chars else "") + "."
        except PdfError as exc:
            self.pdf_error = exc.code
            note = f" The PDF could not be produced ({exc.code}); the e-mail will not be available."
        lead = f"Assessment {response.status.upper()}." + ("" if self.auto_archive else " Review the report, then archive it in Notion.")
        return self.view(lead + note)

    @staticmethod
    def _refusal_text(reason: str) -> str:
        if reason == "ALLOW_PAID_CALLS":
            return "Live run refused: ALLOW_PAID_CALLS=1 is not set for this process."
        if reason.startswith("SETTINGS_MISSING"):
            return "Live run refused before any external call: required settings are missing (" + reason.split(": ", 1)[-1] + ")."
        if reason.startswith("BUDGET"):
            return "Live run refused before any external call: " + reason.split(": ", 1)[-1] + "."
        return "Live run refused before any external call."

    def _save_live_record(self, response, trace, started) -> None:
        """What the live run actually did (attempts, successes, failures, usage): counts and codes only."""
        try:
            record = build_record(response=response, trace=trace, mode=self.mode, model=os.getenv("OPENAI_MODEL"), started_at=started, finished_at=self._clock())
            self.live_record_path = write_record(record, self._runs_dir)
            self.live_trace_path = write_trace(trace, response.request_id, self._runs_dir)
        except Exception as exc:  # noqa: BLE001 - the record must never change the assessment
            log.error("could not write the live record (%s)", type(exc).__name__)

    # ───────── 2. archive in Notion ─────────
    def archive(self) -> View:
        """Manual (re)try, used when the automatic archive did not confirm."""
        if not self._enter():
            return self.busy_view("An operation is already in progress. Wait for it to finish.")
        try:
            return self._do_archive()
        finally:
            self._leave()

    def _do_archive(self) -> View:
        """Caller holds the operation lock."""
        if self.archive_req is None:
            return self.view("Nothing to archive: run a completed or partial assessment first.")
        if self.archive_verified:
            return self.view("This report is already archived and verified.")
        try:
            client = self._client_factory()
        except BridgeConfigError as exc:
            self._archive_msg = f"Archive not sent: {exc.message}."
            return self.view()
        result = client.archive(self.archive_req)
        if result.ok:
            self.archive_verified, self.notion_url = True, result.notion_page_url or ""
            self._archive_msg = (f"Archive VERIFIED in Notion: {result.blocks_found} of {result.blocks_expected} blocks found, "
                                 f"hash confirmed{' (page already existed for this request)' if result.duplicate_existing else ''}.")
        else:
            self._archive_msg = f"Archive NOT confirmed [{result.code}]: {result.message}" + (f" ({result.detail})" if result.detail else "")
        return self.view()

    # ───────── 3. e-mail (only after a verified archive; at most once) ─────────
    def email(self, confirm_resend: bool = False) -> View:
        if not self._enter():
            return self.busy_view("An operation is already in progress. Wait for it to finish.")
        try:
            if not self.archive_verified or self.archive_req is None:
                return self.view("E-mail is available only after the report is archived and verified in Notion.")
            if self.email_state in ("sent", "already_sent"):
                return self.view("The e-mail was already sent for this request; it will not be sent again.")
            if self.email_state == "unknown" and not confirm_resend:
                return self.view("The previous e-mail result is unknown. Check the inbox; to send again, tick the confirmation box first.")
            try:
                client = self._client_factory()
            except BridgeConfigError as exc:
                self._email_msg = f"E-mail not sent: {exc.message}."
                return self.view()
            if self.pdf is None:
                self._email_msg = f"E-mail not sent: the PDF report is not available ({self.pdf_error or 'not generated'}). The e-mail must carry the PDF."
                return self.view()
            try:
                request = build_email_request(self.archive_req, self.notion_url, self.pdf)
            except ValidationError:
                self._email_msg = "E-mail not sent: the verified Notion link or the PDF attachment is not usable."
                return self.view()
            result = client.email(request)                     # exactly one attempt
            self.email_state = result.state if result.state != "failed" else "failed"
            if result.ok:
                self._email_msg = ("E-mail sent." if result.state == "sent" else "n8n reports the e-mail was already sent for this request; nothing new was sent.")
            elif result.state == "unknown":
                self._email_msg = f"[{result.code}] {result.message} It was NOT resent automatically." + (f" ({result.detail})" if result.detail else "")
            else:
                self._email_msg = f"E-mail NOT sent [{result.code}]: {result.message}" + (f" ({result.detail})" if result.detail else "")
            return self.view()
        finally:
            self._leave()
