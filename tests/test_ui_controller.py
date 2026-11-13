"""Offline tests for the Gradio path: controller wiring to the unchanged agent, gating of archive/e-mail, duplicate prevention.
Gradio itself is not needed (the controller is plain Python); no network, no database, no paid call."""
from __future__ import annotations

from pathlib import Path

import pytest

import src.agent.graph as graph_module
import src.agent.run as run_module
import src.ui.controller as controller_module
from src.agent.llm import LLMError
from src.agent.schemas import AssessmentResponse
from src.bridge.client import BridgeConfigError, WebhookClient
from src.ui.controller import AppController
from tests.agent_fixtures import REQUEST, deps
from tests.bridge_sim import ARCHIVE_URL, EMAIL_URL, SECRET, FakeN8n
from tests.pdf_fonts import needs_fonts


def make(tmp_path: Path, sim: FakeN8n | None = None, mode: str = "mock", factory=None, auto: bool = False) -> tuple[AppController, FakeN8n]:
    sim = sim or FakeN8n()
    client = WebhookClient(ARCHIVE_URL, EMAIL_URL, SECRET, transport=sim.transport(), sleep=lambda s: None)
    return AppController(factory or (lambda rid: deps()), mode=mode, client_factory=lambda: client, runs_dir=tmp_path, auto_archive=auto), sim


def run_ok(c: AppController):
    return c.run("sunflower_oil", 3, "", live_approved=True)


# ───────── proof that the Gradio path uses the unchanged agent ─────────
def test_controller_calls_the_real_run_assessment_and_the_real_graph_runs_its_validators(tmp_path, monkeypatch):
    assert controller_module.run_assessment is graph_module.run_assessment            # same function object, no wrapper, no copy
    seen = {}

    def spy(request, d):
        seen["request"], seen["deps"] = request, d
        resp, trace = graph_module.run_assessment(request, d)                           # the genuine function
        seen["trace"], seen["response"] = trace, resp
        return resp, trace
    monkeypatch.setattr(controller_module, "run_assessment", spy)
    fixture = deps()
    c, _ = make(tmp_path, factory=lambda rid: fixture)
    v = run_ok(c)
    nodes = [t["node"] for t in seen["trace"]]
    assert seen["deps"] is fixture and seen["request"].ingredient == "sunflower_oil" and seen["request"].horizon_months == 3
    assert {"load_internal", "calculate", "retrieve_policy", "search", "filter_evidence", "draft", "validate"} <= set(nodes) and seen["response"].status in ("completed", "partial")
    assert v.report_markdown == seen["response"].report_markdown                          # the report is shown exactly as produced
    assert (tmp_path / f"{v.request_id}.md").read_text(encoding="utf-8") == v.report_markdown


def test_live_dependencies_are_wired_like_run_py_live(monkeypatch, tmp_path):
    """The Gradio live path builds Deps through the same function as `python -m src.agent.run --live`."""
    monkeypatch.setenv("ALLOW_PAID_CALLS", "1")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4-mini")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
    import src.agent.budget as budget_module
    monkeypatch.setattr(budget_module, "USAGE_LOG", tmp_path / "no-usage.jsonl")
    monkeypatch.setattr(run_module, "enforce_budget", lambda: budget_module.enforce_budget(tmp_path / "no-usage.jsonl"))
    made = {}

    def rec(name):
        class Fake:
            def __init__(self, *a, **kw):
                made[name] = (a, kw)
        Fake.__name__ = name
        return Fake
    monkeypatch.setattr(run_module, "OpenAILLM", rec("llm"))
    monkeypatch.setattr(run_module, "PostgresData", rec("data"))
    import src.rag.embed as embed
    import src.rag.retrieve as retrieve
    import src.tools.search as search
    monkeypatch.setattr(search, "TavilyTool", rec("tavily"))
    monkeypatch.setattr(search, "GdeltTool", rec("gdelt"))
    monkeypatch.setattr(embed, "OpenAIEmbedder", rec("embedder"))
    monkeypatch.setattr(retrieve, "PostgresPolicyRetriever", rec("policy"))
    captured = {}
    canned, _ = graph_module.run_assessment(dict(REQUEST), deps())

    def spy(request, d):
        captured["deps"] = d
        return canned, []
    monkeypatch.setattr(controller_module, "run_assessment", spy)
    c, _ = make(tmp_path, mode="live", factory=controller_module.live_deps)
    c.run("sunflower_oil", 3, "", live_approved=True)
    d = captured["deps"]
    assert d.require_research is True and d.mock is False                                  # real research is required, nothing mocked
    assert type(d.llm).__name__ == "llm" and made["llm"][1]["allow_paid"] is True and made["llm"][1]["max_total_tokens"] == 40_000
    assert type(d.tavily).__name__ == "tavily" and made["tavily"][0] == ("tvly-test",)
    assert type(d.gdelt).__name__ == "gdelt" and made["gdelt"][1] == {"max_attempts": 2}
    assert type(d.policy).__name__ == "policy" and type(d.data).__name__ == "data"
    assert "MockSearch" not in {type(d.tavily).__name__, type(d.gdelt).__name__}
    assert controller_module.live_deps.__module__ == "src.ui.controller" and run_module.build_live_deps is not None


def test_live_run_is_refused_without_paid_flag_or_approval_and_makes_no_call(monkeypatch, tmp_path):
    monkeypatch.delenv("ALLOW_PAID_CALLS", raising=False)
    calls = []
    monkeypatch.setattr(controller_module, "run_assessment", lambda *a: calls.append(a))
    c, _ = make(tmp_path, mode="live", factory=controller_module.live_deps)
    assert "approval box" in c.run("sunflower_oil", 3, "", live_approved=False).message
    v = c.run("sunflower_oil", 3, "", live_approved=True)
    assert "ALLOW_PAID_CALLS" in v.message and not calls and v.archive_enabled is False


def test_llm_refusal_and_unexpected_errors_show_safe_messages_only(tmp_path):
    def refuse(rid):
        raise LLMError("OPENAI_MODEL_NOT_SET")

    def boom(rid):
        raise RuntimeError("password=hunter2 host=db.internal")
    for factory, expect in ((refuse, "OPENAI_MODEL_NOT_SET"), (boom, "RuntimeError")):
        c, _ = make(tmp_path, factory=factory)
        msg = run_ok(c).message
        assert expect in msg and "hunter2" not in msg and "sk-abc" not in msg and "postgresql://" not in msg


# ───────── run behaviour ─────────
def test_invalid_inputs_are_rejected_with_safe_messages(tmp_path):
    c, _ = make(tmp_path)
    assert "YYYY-MM-DD" in c.run("sunflower_oil", 3, "not-a-date").message
    assert c.run("wheat", 3, "").message == controller_module.UNSUPPORTED_MESSAGE
    assert "invalid" in c.run("sunflower_oil", 9, "").message
    assert "invalid" in c.run("sunflower_oil", 3, "1999-01-01").message


def test_a_run_produces_status_report_and_gates_archive_and_email(tmp_path):
    c, _ = make(tmp_path)
    v = run_ok(c)
    assert v.assessment_status.startswith(("COMPLETED", "PARTIAL")) and "(MOCK)" in v.assessment_status
    assert v.report_markdown.startswith("# ") and v.archive_enabled and not v.email_enabled and v.notion_url == ""
    assert c.email().message.startswith("E-mail is available only after")                   # no archive yet -> refused, no call


def test_failed_assessment_is_never_archivable(tmp_path):
    c, sim = make(tmp_path, factory=lambda rid: deps(data=type("Boom", (), {"load": lambda self, i: (_ for _ in ()).throw(RuntimeError("x"))})()))
    v = run_ok(c)
    assert v.assessment_status.startswith("FAILED") and not v.archive_enabled and "never archived" in v.message
    assert c.archive().message.startswith("Nothing to archive") and sim.archive_calls == 0


@needs_fonts
def test_request_id_is_created_once_per_run_and_kept_for_archive_and_email(tmp_path):
    c, sim = make(tmp_path)
    rid = run_ok(c).request_id
    c.archive()
    c.email()
    assert {b["request_id"] for b in sim.seen_bodies} == {rid} and len(sim.seen_bodies) == 2
    assert run_ok(c).request_id != rid                                                      # a new run gets a new id and clears old state


# ───────── archive, then e-mail ─────────
@needs_fonts
def test_verified_archive_unlocks_email_and_both_happen_once(tmp_path):
    c, sim = make(tmp_path)
    run_ok(c)
    v = c.archive()
    assert "VERIFIED" in v.archive_message and v.notion_url.startswith("https://www.notion.so/") and v.email_enabled and not v.archive_enabled
    assert "already archived" in c.archive().message and sim.archive_calls == 1            # second click: no call, no second page
    v = c.email()
    assert "E-mail sent" in v.email_message and not v.email_enabled and sim.sent == {v.request_id: 1}
    assert "will not be sent again" in c.email().message and sim.email_calls == 1


@pytest.mark.parametrize("setup,code", [(lambda s, n: setattr(s, "store_only", n - 1), "ARCHIVE_INCOMPLETE"),
                                        (lambda s, n: setattr(s, "echo_hash", "e" * 64), "HASH_MISMATCH"),
                                        (lambda s, n: s.archive_faults.extend(["accepted_only"]), "INVALID_RESPONSE"),
                                        (lambda s, n: s.archive_faults.extend(["timeout_before"] * 3), "TIMEOUT")])
def test_unverified_archive_keeps_email_disabled_and_shows_the_code(tmp_path, setup, code):
    c, sim = make(tmp_path)
    run_ok(c)
    setup(sim, c.archive_req.total_blocks)
    v = c.archive()
    assert f"[{code}]" in v.archive_message and "NOT confirmed" in v.archive_message
    assert not v.email_enabled and v.notion_url == "" and c.email().message.startswith("E-mail is available only after")
    assert sim.email_calls == 0


def test_missing_webhook_configuration_blocks_with_a_safe_message(tmp_path):
    def no_client():
        raise BridgeConfigError("N8N_ARCHIVE_WEBHOOK_URL must be an https:// URL")
    c = AppController(lambda rid: deps(), mode="mock", client_factory=no_client, runs_dir=tmp_path, auto_archive=False)
    run_ok(c)
    assert "N8N_ARCHIVE_WEBHOOK_URL" in c.archive().archive_message


@needs_fonts
def test_ambiguous_email_is_never_resent_automatically_and_needs_explicit_confirmation(tmp_path):
    c, sim = make(tmp_path)
    run_ok(c)
    c.archive()
    sim.email_faults = ["timeout_after_effect"]
    v = c.email()
    assert "UNKNOWN" in v.email_message and "NOT resent automatically" in v.email_message and v.confirm_resend_visible and sim.email_calls == 1
    assert "unknown" in c.email(confirm_resend=False).message and sim.email_calls == 1       # no network without the confirmation
    v = c.email(confirm_resend=True)
    assert "already sent" in v.email_message and sim.email_calls == 2 and sim.sent[v.request_id] == 1 and not v.email_enabled


@needs_fonts
def test_definite_email_failure_can_be_retried_without_confirmation(tmp_path):
    c, sim = make(tmp_path)
    run_ok(c)
    c.archive()
    sim.email_faults = ["connect_error"]
    v = c.email()
    assert "NOT sent" in v.email_message and v.email_enabled and not v.confirm_resend_visible
    assert "E-mail sent" in c.email().email_message and sim.sent[v.request_id] == 1


# ───────── no double start ─────────
def test_a_second_operation_while_one_runs_is_refused(tmp_path):
    import threading
    gate, started = threading.Event(), threading.Event()
    calls = []

    def slow(request, d):
        calls.append(1)
        started.set()
        gate.wait(5)
        return graph_module.run_assessment(request, d)
    mp = pytest.MonkeyPatch()
    mp.setattr(controller_module, "run_assessment", slow)
    try:
        c, _ = make(tmp_path)
        t = threading.Thread(target=lambda: run_ok(c))
        t.start()
        assert started.wait(5)
        v = run_ok(c)                                                                         # the "double click"
        assert "already in progress" in v.message and not v.run_enabled and not v.archive_enabled and len(calls) == 1
        gate.set()
        t.join(10)
        assert run_ok(c).run_enabled and len(calls) == 2
    finally:
        gate.set()
        mp.undo()


# ───────── the Gradio module ─────────
def test_gradio_module_imports_without_gradio_and_reads_no_credentials():
    src = (Path(controller_module.__file__).parent / "gradio_app.py").read_text(encoding="utf-8")
    for name in ("DATABASE_URL", "OPENAI_API_KEY", "TAVILY_API_KEY", "N8N_WEBHOOK_SECRET", "N8N_ARCHIVE_WEBHOOK_URL", "N8N_EMAIL_WEBHOOK_URL", "ASSESSMENT_API_KEY"):
        assert name not in src
    assert "share=False" in src and 'HOST = "127.0.0.1"' in src and "analytics_enabled=False" in src
    import src.ui.gradio_app as app                                                             # noqa: F401  (must not need gradio)


def test_gradio_ui_builds_when_gradio_is_installed(tmp_path):
    pytest.importorskip("gradio")
    from src.ui.gradio_app import build_ui
    c, _ = make(tmp_path)
    assert build_ui(c) is not None


# ───────── PDF attachment through the controller ─────────
@needs_fonts
def test_email_carries_the_pdf_of_the_archived_report_and_the_view_announces_it(tmp_path):
    c, sim = make(tmp_path)
    v = run_ok(c)
    assert "PDF ready:" in v.message and c.pdf is not None and c.pdf.pages >= 2
    c.archive()
    c.email()
    rid = c.request_id
    assert sim.attachments[rid] == c.pdf.data and sim.seen_bodies[-1]["attachment"]["sha256"] == c.pdf.sha256
    assert sim.seen_bodies[-1]["report_sha256"] == c.archive_req.report_sha256                 # same report as the Notion archive


def test_without_a_pdf_the_email_is_not_sent_and_nothing_is_sent_without_attachment(tmp_path, monkeypatch):
    from src.bridge.pdf import PdfError

    def boom(_):
        raise PdfError("PDF_INCOMPLETE", "x")
    monkeypatch.setattr(controller_module, "build_report_pdf", boom)
    c, sim = make(tmp_path)
    v = run_ok(c)
    assert "PDF could not be produced (PDF_INCOMPLETE)" in v.message and v.archive_enabled
    v = c.archive()
    assert "VERIFIED" in v.archive_message and not v.email_enabled                               # archive still works, e-mail does not
    assert "PDF report is not available" in c.email().email_message and sim.email_calls == 0


# ───────── automatic archive after a successful run (the e-mail still needs the user) ─────────
def test_a_completed_run_is_archived_in_notion_automatically_and_the_email_waits_for_the_user(tmp_path):
    c, sim = make(tmp_path, auto=True)
    v = c.run("sunflower_oil", 3)                                                        # no date argument: the date is today
    assert sim.archive_calls == 1 and len(sim.pages) == 1 and v.request_id in sim.pages
    assert "Archived in Notion automatically" in v.message and "VERIFIED" in v.archive_message and v.notion_url.startswith("https://www.notion.so/")
    assert v.email_enabled and not v.archive_enabled and sim.email_calls == 0 and c.email_state == "not_sent"      # nothing is e-mailed without the click


@needs_fonts
def test_the_email_is_sent_only_after_the_button_even_in_auto_mode(tmp_path):
    c, sim = make(tmp_path, auto=True)
    c.run("sunflower_oil", 3)
    assert sim.email_calls == 0
    v = c.email()
    assert "E-mail sent" in v.email_message and sim.email_calls == 1 and sim.archive_calls == 1


def test_a_failed_assessment_is_not_archived_automatically(tmp_path):
    boom = type("Boom", (), {"load": lambda self, i: (_ for _ in ()).throw(RuntimeError("x"))})()
    c, sim = make(tmp_path, factory=lambda rid: deps(data=boom), auto=True)
    v = c.run("sunflower_oil", 3)
    assert v.assessment_status.startswith("FAILED") and sim.archive_calls == 0 and not v.archive_enabled


def test_an_unconfirmed_automatic_archive_leaves_the_retry_button_and_the_email_locked(tmp_path):
    sim = FakeN8n()
    c, sim = make(tmp_path, sim=sim, auto=True)
    sim.store_only = 3                                                                   # Notion keeps only 3 blocks: incomplete
    v = c.run("sunflower_oil", 3)
    assert "NOT confirmed" in v.message and "[ARCHIVE_INCOMPLETE]" in v.archive_message and v.archive_enabled and not v.email_enabled and v.notion_url == ""
    sim.store_only = None
    sim.pages.clear()                                                                    # the operator fixed Notion; the retry button re-sends the same request
    v = c.archive()
    assert "VERIFIED" in v.archive_message and v.email_enabled and sim.email_calls == 0


def test_missing_webhook_settings_at_the_automatic_archive_keep_the_report_and_allow_a_retry(tmp_path):
    def no_client():
        raise BridgeConfigError("N8N_WEBHOOK_SECRET is not set")
    c = AppController(lambda rid: deps(), mode="mock", client_factory=no_client, runs_dir=tmp_path, auto_archive=True)
    v = c.run("sunflower_oil", 3)
    assert v.assessment_status.startswith(("COMPLETED", "PARTIAL")) and v.report_markdown.startswith("# ")
    assert "Archive not sent: N8N_WEBHOOK_SECRET is not set" in v.archive_message and v.archive_enabled and not v.email_enabled


def test_the_interface_has_no_date_field_and_always_assesses_today():
    src = (Path(controller_module.__file__).parent / "gradio_app.py").read_text(encoding="utf-8")
    assert "Assessment date" not in src and "adate" not in src and "controller.run(ing, int(hz), None, True)" in src and "gr.Checkbox(label=\"I approve" not in src
    assert '"Retry archive in Notion" if controller.auto_archive else "Archive in Notion"' in src and "--max-live-runs" in src


# ───────── commodity list (UI-only demonstration): only Sunflower Oil may reach the agent ─────────
def test_the_dropdown_offers_four_commodities_with_sunflower_oil_first_and_the_only_supported_one():
    assert controller_module.COMMODITY_CHOICES == [("Sunflower Oil", "sunflower_oil"), ("Wheat Flour", "wheat_flour"),
                                                   ("Granulated Sugar", "granulated_sugar"), ("Butter", "butter")]
    assert controller_module.SUPPORTED_COMMODITIES == {"sunflower_oil"}
    src = (Path(controller_module.__file__).parent / "gradio_app.py").read_text(encoding="utf-8")
    assert 'gr.Dropdown(COMMODITY_CHOICES, value="sunflower_oil"' in src


@pytest.mark.parametrize("commodity", ["wheat_flour", "granulated_sugar", "butter", "wheat", "", "SUNFLOWER_OIL", "sunflower_oil ", None])
@pytest.mark.parametrize("mode,auto", [("mock", True), ("mock", False), ("live", True)])
def test_unsupported_commodities_never_trigger_an_assessment_dependencies_archive_or_email(tmp_path, monkeypatch, commodity, mode, auto):
    calls = []
    monkeypatch.setattr(controller_module, "run_assessment", lambda *a, **k: calls.append("run_assessment"))
    monkeypatch.setenv("ALLOW_PAID_CALLS", "1")                                            # even with every gate open, nothing may start
    factory_calls = []
    c, sim = make(tmp_path, mode=mode, auto=auto, factory=lambda rid: factory_calls.append(rid))
    v = c.run(commodity, 3, None, live_approved=True)
    assert v.message == "Assessment for this commodity is not yet available. Currently, only Sunflower Oil is supported."
    assert calls == [] and factory_calls == [] and sim.archive_calls == 0 and sim.email_calls == 0
    assert v.report_markdown == "" and v.request_id == "" and not v.archive_enabled and not v.email_enabled and v.run_enabled


def test_choosing_an_unsupported_commodity_clears_a_previous_report_and_sunflower_oil_still_runs(tmp_path):
    c, sim = make(tmp_path, auto=True)
    first = c.run("sunflower_oil", 3, None)
    assert first.report_markdown and sim.archive_calls == 1
    blocked = c.run("butter", 3, None)
    assert blocked.report_markdown == "" and sim.archive_calls == 1                        # no stale sunflower report shown under another commodity
    again = c.run("sunflower_oil", 3, None)
    assert again.report_markdown and sim.archive_calls == 2
