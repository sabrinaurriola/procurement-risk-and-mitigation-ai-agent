"""Safe live-mode controls (13 Nov 2026): the USD 5 budget guard, the settings gate, one paid assessment per session, no automatic archive of a live report,
the live-run record and the no-fallback rule. Offline: no network, no paid call, no database."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.agent.run as run_module
from src.agent import budget
from src.agent.live_record import build_record
from src.agent.run import LiveRefused, build_live_deps
from src.agent.mock import MockSearch
from src.bridge.client import WebhookClient
from src.ui import controller as controller_module
from src.ui.controller import AppController
from tests.agent_fixtures import TODAY, deps
from tests.bridge_sim import ARCHIVE_URL, EMAIL_URL, SECRET, FakeN8n

ALL_SETTINGS = {"OPENAI_API_KEY": "sk-secret-sentinel-0123456789", "OPENAI_MODEL": "gpt-5.4-mini", "TAVILY_API_KEY": "tvly-secret-sentinel", "DATABASE_URL": "postgresql://u:p@h/db"}


def log(path: Path, *rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


# ───────── budget guard ─────────
def test_spend_is_estimated_from_the_usage_log_and_unknown_or_broken_lines_are_pessimistic(tmp_path):
    p = log(tmp_path / "u.jsonl", {"model": "gpt-5.4-mini", "input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert budget.spent_usd(p) == pytest.approx(5.25)
    p = log(tmp_path / "u2.jsonl", {"model": "text-embedding-3-small", "input_tokens": 1_000_000, "output_tokens": 0})
    assert budget.spent_usd(p) == pytest.approx(0.02)
    p = log(tmp_path / "u3.jsonl", {"model": "some-new-model", "input_tokens": 1_000_000, "output_tokens": 0})
    assert budget.spent_usd(p) == pytest.approx(0.75)                                    # highest known input price
    p = tmp_path / "u4.jsonl"
    p.write_text("not json\n", encoding="utf-8")
    assert budget.spent_usd(p) == budget.RUN_RESERVE_USD and budget.spent_usd(tmp_path / "missing.jsonl") == 0.0


def test_a_run_is_refused_when_spend_plus_the_reserve_would_exceed_the_limit(tmp_path, monkeypatch):
    monkeypatch.delenv("LIVE_BUDGET_USD", raising=False)
    budget.enforce_budget(log(tmp_path / "ok.jsonl", {"model": "gpt-5.4-mini", "input_tokens": 100_000, "output_tokens": 100_000}))      # about 0.5 USD: fine
    near = log(tmp_path / "near.jsonl", {"model": "gpt-5.4-mini", "input_tokens": 0, "output_tokens": 1_000_000}, {"model": "gpt-5.4-mini", "input_tokens": 0, "output_tokens": 150_000})
    with pytest.raises(budget.BudgetExceeded) as e:                                       # 4.50 + 0.675 > 5
        budget.enforce_budget(near)
    assert str(e.value).startswith("BUDGET:") and "limit USD 5.00" in str(e.value)


def test_the_env_limit_can_only_lower_the_budget(monkeypatch):
    monkeypatch.setenv("LIVE_BUDGET_USD", "99")
    assert budget.budget_limit() == 5.0
    monkeypatch.setenv("LIVE_BUDGET_USD", "1.5")
    assert budget.budget_limit() == 1.5
    monkeypatch.setenv("LIVE_BUDGET_USD", "nonsense")
    assert budget.budget_limit() == 5.0


# ───────── the gates run BEFORE any client is built ─────────
@pytest.fixture()
def no_clients(monkeypatch):
    """Any constructor of a paid or external client fails the test."""
    def boom(*a, **k):
        raise AssertionError("an external client was created although the live run must have been refused")
    import src.rag.embed as embed
    import src.rag.retrieve as retrieve
    import src.tools.search as search
    for mod, name in ((run_module, "OpenAILLM"), (run_module, "PostgresData"), (search, "TavilyTool"), (search, "GdeltTool"), (embed, "OpenAIEmbedder"), (retrieve, "PostgresPolicyRetriever")):
        monkeypatch.setattr(mod, name, boom)


def test_live_deps_refuse_without_the_paid_flag(monkeypatch, no_clients):
    monkeypatch.delenv("ALLOW_PAID_CALLS", raising=False)
    with pytest.raises(LiveRefused) as e:
        build_live_deps("r-1")
    assert e.value.reason == "ALLOW_PAID_CALLS"


def test_live_deps_refuse_when_settings_are_missing_and_name_them_without_values(monkeypatch, no_clients):
    monkeypatch.setenv("ALLOW_PAID_CALLS", "1")
    for k in ALL_SETTINGS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", ALL_SETTINGS["OPENAI_API_KEY"])
    with pytest.raises(LiveRefused) as e:
        build_live_deps("r-1")
    assert e.value.reason == "SETTINGS_MISSING: OPENAI_MODEL, TAVILY_API_KEY, DATABASE_URL"
    assert ALL_SETTINGS["OPENAI_API_KEY"] not in e.value.reason


def test_live_deps_refuse_when_the_budget_guard_says_no(monkeypatch, tmp_path, no_clients):
    monkeypatch.setenv("ALLOW_PAID_CALLS", "1")
    for k, v in ALL_SETTINGS.items():
        monkeypatch.setenv(k, v)
    full = log(tmp_path / "full.jsonl", {"model": "gpt-5.4-mini", "input_tokens": 0, "output_tokens": 1_200_000})
    monkeypatch.setattr(run_module, "enforce_budget", lambda: budget.enforce_budget(full))
    with pytest.raises(LiveRefused) as e:
        build_live_deps("r-1")
    assert e.value.reason.startswith("BUDGET: estimated spend so far USD 5.40")


def test_the_cli_reports_each_refusal_and_runs_nothing(monkeypatch, capsys, no_clients):
    monkeypatch.setattr(run_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(run_module, "run_assessment", lambda *a, **k: pytest.fail("no assessment may run"))
    monkeypatch.setenv("ALLOW_PAID_CALLS", "1")
    for k in ALL_SETTINGS:
        monkeypatch.delenv(k, raising=False)
    assert run_module.main(["--live"]) == 3
    assert "REFUSED: SETTINGS_MISSING: OPENAI_API_KEY, OPENAI_MODEL, TAVILY_API_KEY, DATABASE_URL" in capsys.readouterr().out


# ───────── the controller: one paid run, no auto-archive, a record, no fallback ─────────
def live_controller(tmp_path, factory=None, sim=None, **kw):
    sim = sim or FakeN8n()
    client = WebhookClient(ARCHIVE_URL, EMAIL_URL, SECRET, transport=sim.transport(), sleep=lambda s: None)
    return AppController(factory or (lambda rid: deps(mock=False)), mode="live", client_factory=lambda: client, runs_dir=tmp_path, **kw), sim


def test_only_one_live_assessment_is_allowed_per_session_by_default(tmp_path):
    calls = []
    c, _ = live_controller(tmp_path, factory=lambda rid: calls.append(rid) or deps(mock=False))
    first = c.run("sunflower_oil", 3, None, live_approved=True)
    assert first.assessment_status.startswith(("COMPLETED", "PARTIAL")) and len(calls) == 1
    second = c.run("sunflower_oil", 3, None, live_approved=True)
    assert "Live run limit reached" in second.message and "1 live assessment" in second.message and len(calls) == 1
    c2, _ = live_controller(tmp_path, max_live_runs=2)
    assert c2.run("sunflower_oil", 3, None, True).report_markdown and c2.run("sunflower_oil", 3, None, True).report_markdown


def test_a_refused_attempt_does_not_use_up_the_session_run(tmp_path):
    state = {"n": 0}

    def factory(rid):
        state["n"] += 1
        if state["n"] == 1:
            raise LiveRefused("BUDGET: estimated spend so far USD 4.90 plus the reserve for one run USD 0.25 would exceed the limit USD 5.00")
        return deps(mock=False)
    c, _ = live_controller(tmp_path, factory=factory)
    refused = c.run("sunflower_oil", 3, None, True)
    assert "refused before any external call" in refused.message and "USD 4.90" in refused.message and not refused.report_markdown
    assert c.run("sunflower_oil", 3, None, True).report_markdown                            # the one run is still available
    setting = live_controller(tmp_path, factory=lambda rid: (_ for _ in ()).throw(LiveRefused("SETTINGS_MISSING: TAVILY_API_KEY")))[0].run("sunflower_oil", 3, None, True)
    assert "required settings are missing (TAVILY_API_KEY)" in setting.message


def test_a_completed_or_partial_live_report_is_archived_automatically_but_never_e_mailed(tmp_path):
    c, sim = live_controller(tmp_path)
    v = c.run("sunflower_oil", 3, None, True)
    assert c.auto_archive is True and sim.archive_calls == 1 and sim.email_calls == 0
    assert "Archived in Notion automatically" in v.message and "VERIFIED" in v.archive_message and v.notion_url and v.email_enabled


def test_a_failed_live_run_is_never_archived_automatically(tmp_path):
    from src.agent.llm import LLMError
    from tests.agent_fixtures import ScriptedLLM
    c, sim = live_controller(tmp_path, factory=lambda rid: deps(mock=False, llm=ScriptedLLM(drafts=[LLMError("LLM_CALL_FAILED:TimeoutError")])))
    v = c.run("sunflower_oil", 3, None, True)
    assert v.assessment_status.startswith("FAILED") and sim.archive_calls == 0 and not v.archive_enabled


def test_the_live_run_leaves_a_record_with_attempts_failures_usage_and_no_secrets_or_text(tmp_path, monkeypatch):
    for k, v in ALL_SETTINGS.items():
        monkeypatch.setenv(k, v)
    c, _ = live_controller(tmp_path, factory=lambda rid: deps(mock=False, gdelt=MockSearch("gdelt", TODAY, fail_with="HTTP_429")))
    v = c.run("sunflower_oil", 3, None, True)
    path = c.live_record_path
    assert path is not None and path.name == f"{v.request_id}-live-record.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["schema"] == "live-record/1" and rec["request_id"] == v.request_id and rec["mode"] == "live" and rec["model"] == "gpt-5.4-mini"
    assert rec["started_at"] <= rec["finished_at"] and rec["assessment_status"] in ("completed", "partial")
    ra = rec["research_attempts"]
    assert ra["calls"] == ra["succeeded"] + ra["failed"] and ra["failed"] >= 1 and set(ra["by_tool"]) == {"tavily", "gdelt"}
    gd = [x for x in ra["calls_detail"] if x["tool"] == "gdelt"]
    assert gd and all(x["ok"] is False and x["error_code"] == "HTTP_429" for x in gd)
    assert rec["source_status"]["gdelt"].startswith("unavailable") and rec["source_status"]["tavily"].startswith("ok")
    assert set(rec["execution"]) == {"elapsed_seconds", "research_rounds", "tool_calls", "llm_calls", "tokens"} and "claims_removed" in rec["validation"]
    assert "RESEARCH_SOURCE_GDELT" in rec["validation"]["warning_codes"] and rec["evidence"]["retained"] > 0 and "usage_log_lines_for_this_request" in rec
    blob = path.read_text(encoding="utf-8")
    for secret in (v for k, v in ALL_SETTINGS.items() if k != "OPENAI_MODEL"):         # the model name is recorded on purpose; keys and the database URL never
        assert secret not in blob
    assert "Executive summary" not in blob and "report_markdown" not in blob and "https://" not in blob


def test_there_is_no_fallback_to_fixtures_when_real_research_is_unavailable(tmp_path):
    def factory(rid):
        d = deps(mock=False, tavily=MockSearch("tavily", TODAY, fail_with="TIMEOUT"), gdelt=MockSearch("gdelt", TODAY, fail_with="HTTP_429"))
        d.require_research = True                                                           # exactly what build_live_deps sets
        return d
    c, sim = live_controller(tmp_path, factory=factory)
    v = c.run("sunflower_oil", 3, None, True)
    assert v.assessment_status.startswith("FAILED") and "RESEARCH_UNAVAILABLE" in v.message and "never archived" in v.message
    assert not v.report_markdown and sim.archive_calls == 0
    rec = json.loads(c.live_record_path.read_text(encoding="utf-8"))                         # the failed attempts are still recorded
    assert rec["assessment_status"] == "failed" and rec["error_code"] == "RESEARCH_UNAVAILABLE" and rec["research_attempts"]["succeeded"] == 0 and rec["research_attempts"]["failed"] >= 2
    assert rec["execution"]["llm_calls"] == 0                                               # stopped before any LLM call


def test_build_live_deps_uses_real_clients_and_requires_research():
    src = Path(run_module.__file__).read_text(encoding="utf-8")
    body = src.split("def build_live_deps")[1].split("def main")[0]
    assert "MockSearch" not in body and "DemoLLM" not in body and "require_research=True" in body and "allow_paid=True" in body
    assert "mock=True" not in body
    assert controller_module.live_deps.__name__ == "live_deps"


def test_a_failed_live_run_still_records_evidence_counts_from_the_trace_and_saves_the_trace(tmp_path, monkeypatch):
    from src.agent.llm import LLMError
    for k, v in ALL_SETTINGS.items():
        monkeypatch.setenv(k, v)
    from tests.agent_fixtures import ScriptedLLM
    c, sim = live_controller(tmp_path, factory=lambda rid: deps(mock=False, llm=ScriptedLLM(drafts=[LLMError("LLM_CALL_FAILED:TimeoutError")])))
    v = c.run("sunflower_oil", 3, None, True)
    assert v.assessment_status.startswith("FAILED") and sim.archive_calls == 0
    rec = json.loads(c.live_record_path.read_text(encoding="utf-8"))
    ev = rec["evidence"]
    assert ev["from"] == "trace" and ev["retrieved"] > 0 and ev["passed_prefilter"] > 0 and ev["usable_risk"] >= 1 and ev["by_relevance"]
    trace = json.loads(c.live_trace_path.read_text(encoding="utf-8"))
    assert c.live_trace_path.name == f"{v.request_id}-live-trace.json" and [t["node"] for t in trace][:2] == ["load_internal", "calculate"]
    for secret in (v for k, v in ALL_SETTINGS.items() if k != "OPENAI_MODEL"):
        assert secret not in c.live_trace_path.read_text(encoding="utf-8")
