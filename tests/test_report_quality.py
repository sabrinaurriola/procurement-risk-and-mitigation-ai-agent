"""Report-quality regressions of the live-readiness work (13 Nov 2026): no silent truncation, three source classes kept apart, a decision-oriented
summary, less repetition, the removal cascade, the optional-GDELT rule and the supplier-product-claim guidance. Offline."""
from __future__ import annotations

import re
from datetime import date

import pytest

from src.agent import prompts
from src.agent.graph import run_assessment
from src.agent.mock import MockSearch
from src.agent.validation import prune_unsupported
from src.agent.summary import approval_needed, priority_actions
from tests.agent_fixtures import REQUEST, TODAY, EmptySearch, ScriptedLLM, deps
from tests.validation_fixtures import CTX, EVIDENCE, FACTS, MONEY, action, cite, cov_risk, good_draft, mitigation, risk, sup_risk


@pytest.fixture(scope="module")
def mock_run():
    return run_assessment(dict(REQUEST), deps())[0]


def squash(s: str) -> str:
    return " ".join(s.split())


# ───────── A1/A2: nothing is silently truncated ─────────
def test_the_benchmark_caveat_is_never_cut_in_the_uncertainties(mock_run):
    caveat = mock_run.report.benchmark.caveat
    unc = " ".join(mock_run.report.draft.uncertainties)
    assert caveat in unc and not unc.rstrip().endswith("(m")
    assert "Spain (m\n" not in mock_run.report_markdown and "Spain (m " not in mock_run.report_markdown


def test_policy_passages_are_printed_in_full_and_no_text_is_replaced_by_an_ellipsis(mock_run):
    md = squash(mock_run.report_markdown)
    assert "…" not in mock_run.report_markdown
    for p in mock_run.report.policy.passages:
        body = squash(" ".join(ln.strip() for ln in p.content.strip().splitlines()[1:])) if p.content.lstrip().startswith("#") else squash(p.content).lstrip("# ")
        assert body in md, p.policy_ref
        assert f"**[{p.policy_ref}]**" in mock_run.report_markdown


def test_removed_claims_and_concerns_keep_their_full_text():
    long_title = "Supply disruption risk from Black Sea export pressure " + "with a deliberately long title " * 6
    bad = risk(risk_id="R9", title=long_title, citations=[cite("E1", "this quote does not appear in the evidence item at all, so it is unsupported")])
    d, removed, notes, remaining = prune_unsupported(good_draft(risks=[cov_risk(), sup_risk(), bad]), EVIDENCE, FACTS, True, 3, MONEY, CTX)
    assert any(long_title in x for x in removed) and not remaining
    assert all("…" not in x for x in removed)


# ───────── A3: three kinds of source are distinguished; fixtures are never presented as real ─────────
def test_mock_leads_are_labelled_as_fixtures_not_real_companies(mock_run):
    md = mock_run.report_markdown
    assert "## Alternative-supplier leads — MOCK FIXTURE EXAMPLES (not real companies" in md and "## Alternative-supplier leads — UNVERIFIED (real companies" not in md
    assert "fixture data, NOT real companies" in md and "candidates are real companies found" not in md.split("## Scope and limitations")[1].split("In a live run")[0]
    lead_lines = [ln for ln in md.splitlines() if "UNVERIFIED" in ln and ln.startswith("- ") and "country:" in ln]
    assert lead_lines and all(ln.startswith("- [MOCK FIXTURE - not a real company] ") for ln in lead_lines)
    s = mock_run.report.draft.executive_summary
    assert "MOCK FIXTURE" in s and "NOT real companies" in s and "found by web search" not in s


def test_a_non_mock_run_labels_leads_as_real_but_unverified():
    resp, _ = run_assessment(dict(REQUEST), deps(mock=False))
    md = resp.report_markdown
    assert "## Alternative-supplier leads — UNVERIFIED (real companies found by search; not approved, qualified or capacity-confirmed)" in md
    assert "[MOCK FIXTURE" not in md.split("## Alternative-supplier leads")[1].split("## Recommended next steps")[0]
    assert "found by web search" in resp.report.draft.executive_summary and "MOCK" not in resp.report.draft.executive_summary


def test_the_report_explains_the_three_source_classes(mock_run):
    block = mock_run.report_markdown.split("## How to read the findings")[1].split("## Market benchmark")[0]
    for needle in ("Fictional internal data", "External evidence", "Sourcing leads", "not a fact-check", "NOT real companies", "never attributed"[:0]):
        assert needle in block, needle
    assert "no news is ever attributed" in block


# ───────── A4: a decision-oriented summary ─────────
def test_priority_actions_are_ranked_by_severity_with_owner_approval_and_evidence(mock_run):
    s = mock_run.report.draft.executive_summary
    lines = [ln for ln in s.splitlines() if ln.startswith("- **Priority ")]
    assert len(lines) == len(mock_run.report.draft.recommended_actions) >= 4
    assert [int(re.match(r"- \*\*Priority (\d+)", ln).group(1)) for ln in lines] == list(range(1, len(lines) + 1))
    for ln in lines:
        assert "Owner: Raw Materials Procurement Manager." in ln and "Approval needed:" in ln and "Review before acting:" in ln and "Addresses: R" in ln
    sup = next(ln for ln in lines if "SUP-003" in ln)
    assert "written sign-off by the Procurement Director" in sup and "POL-001" in sup
    sev = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    ranks = [sev[re.search(r"Priority \d+ · (HIGH|MEDIUM|LOW|UNRATED)", ln).group(1)] if "UNRATED" not in ln else 0 for ln in lines]
    assert ranks == sorted(ranks, reverse=True)
    assert "Decisions requested, in priority order" in s and "this report only recommends" in s


def test_priority_order_follows_risk_severity_and_approval_text_claims_only_what_is_cited():
    low_risk = cov_risk(severity="low")
    high_risk = sup_risk(severity="high")
    d = good_draft(risks=[low_risk, high_risk], recommended_actions=[action(addresses=["R1"], action="Low priority thing"),
                                                                     action(addresses=["R2"], action="High priority thing")])
    ordered = priority_actions(d)
    assert [a.action for _, a, _ in ordered] == ["High priority thing", "Low priority thing"] and [s for s, _, _ in ordered] == ["HIGH", "LOW"]
    assert "no approver is named" in approval_needed(action())
    assert "Procurement Director" not in approval_needed(action())                      # never invents an approver the policy did not give


def test_a_run_without_actions_says_so():
    resp, _ = run_assessment(dict(REQUEST), deps(tavily=MockSearch("tavily", TODAY, fail_with="TIMEOUT"), gdelt=MockSearch("gdelt", TODAY, fail_with="TIMEOUT")))
    assert "Decisions requested" in resp.report.draft.executive_summary


# ───────── A5/A6: less repetition, everything important kept ─────────
def test_every_quote_is_printed_once_and_later_mentions_use_references(mock_run):
    md = mock_run.report_markdown
    d = mock_run.report.draft
    for item in d.risks + d.mitigation_options + d.recommended_actions:
        for c in item.citations:
            assert md.count(f'"{c.quote}"') <= 3                                       # first use, plus the lead identity line at most
    for item in d.mitigation_options + d.recommended_actions:
        for pc in item.policy_citations:
            assert f'"{pc.quote}"' not in md                                           # policy quotes live in the printed passages; findings cite [POL-x#n]
            assert f"[{pc.policy_ref}]" in md
    raw = len(re.findall(r"Policy context: ##", md))
    assert raw == 0
    assert md.count("Independently verify identity, product specification") <= len(mock_run.report.draft.supplier_candidates) + 0


def test_all_sections_tables_ids_and_human_approval_statements_survive(mock_run):
    md = mock_run.report_markdown
    for heading in ("## Warnings", "## Executive summary", "## How to read the findings", "## Market benchmark (external data)", "## Financial exposure (modelled; not supplier quotes)",
                    "### Incremental price-shock exposure vs baseline", "### Contract coverage and monthly detail", "## Existing supplier and contract exposure (fictional internal data)",
                    "## Severity indicators", "## Internal policy context", "## Risks", "## Mitigation options", "## Alternative-supplier leads", "## Recommended next steps (all require human review and approval)",
                    "## Uncertainties", "## Research status", "## Evidence register", "## Scope and limitations"):
        assert heading in md, heading
    assert md.count("|---|") >= 4 and all(f"**{x.risk_id} ·" in md for x in mock_run.report.draft.risks)
    assert "1,961,496.55" in md and "107,744.17" in md and "Advisory decision support only" in md and "human review" in md.lower()
    assert all(f"[{e.evidence_id}]" in md for e in mock_run.report.evidence)             # the full evidence register is intact


# ───────── B: removal propagation (a removed risk takes down what addressed only it) ─────────
def test_a_removed_risk_removes_items_that_addressed_only_it_and_trims_shared_references():
    bad = risk(risk_id="R4", title="Unsupported market claim", citations=[cite("E1", "this quote does not appear in the evidence item at all, so it is unsupported")])
    d = good_draft(risks=[cov_risk(), sup_risk(), bad],
                   mitigation_options=[mitigation(option="Only about R4", addresses=["R4"]), mitigation(option="About R1 and R4", addresses=["R1", "R4"])],
                   recommended_actions=[action(action="Act only on R4", addresses=["R4"]), action(action="Act on R1 and R4", addresses=["R1", "R4"])])
    out, removed, notes, remaining = prune_unsupported(d, EVIDENCE, FACTS, True, 3, MONEY, CTX)
    assert not remaining
    assert [x.option for x in out.mitigation_options] == ["About R1 and R4"] and out.mitigation_options[0].addresses == ["R1"]
    assert [x.action for x in out.recommended_actions] == ["Act on R1 and R4"] and out.recommended_actions[0].addresses == ["R1"]
    text = " | ".join(removed)
    assert "risks 'Unsupported market claim' removed" in text
    assert "mitigation_options 'Only about R4' removed: it addressed only the removed risk(s) R4" in text
    assert "recommended_actions 'Act only on R4' removed: it addressed only the removed risk(s) R4" in text
    assert any(n.startswith("REMOVED_RISK_REFERENCES_TRIMMED: 2 item(s)") and "R4" in n for n in notes)


def test_removal_cascade_is_visible_in_the_final_status_and_never_leaves_dangling_references():
    llm = ScriptedLLM()

    def break_market_risk(d):
        risks = [r.model_copy(update={"citations": [cite("E1", "an invented quote that is not in any evidence item")]}) if r.category == "supply_disruption" else r
                 for r in d.risks]
        return d.model_copy(update={"risks": risks})
    llm.drafts = [break_market_risk, break_market_risk]
    resp, _ = run_assessment(dict(REQUEST), deps(llm=llm))
    d = resp.report.draft
    ids = {r.risk_id for r in d.risks}
    assert resp.status == "partial" and any("removed" in x for x in resp.report.removed_claims)
    assert all(set(x.addresses) <= ids for x in d.mitigation_options + d.recommended_actions)
    assert not any(a.addresses == [] for a in d.recommended_actions)


# ───────── B: optional GDELT (D7) ─────────
def test_a_gdelt_failure_alone_does_not_make_the_report_partial_but_stays_visible():
    resp, _ = run_assessment(dict(REQUEST), deps(gdelt=MockSearch("gdelt", TODAY, fail_with="HTTP_429")))
    assert resp.status == "completed"
    assert resp.report.source_status["gdelt"].startswith("unavailable")
    w = [x for x in resp.warnings if x.startswith("RESEARCH_SOURCE_GDELT")]
    assert w and "optional source" in w[0] and "HTTP_429" in w[0]
    assert "RESEARCH_SOURCE_GDELT" in resp.report_markdown                                 # visible in the report warnings


def test_gdelt_is_not_optional_when_tavily_failed_or_evidence_is_insufficient():
    down = run_assessment(dict(REQUEST), deps(tavily=MockSearch("tavily", TODAY, fail_with="TIMEOUT")))[0]
    assert down.status == "partial" and any(w.startswith("RESEARCH_SOURCE_TAVILY") for w in down.warnings)
    thin = run_assessment(dict(REQUEST), deps(tavily=EmptySearch("tavily"), gdelt=MockSearch("gdelt", TODAY, fail_with="TIMEOUT")))[0]
    assert thin.status == "partial"
    gd = [w for w in thin.warnings if w.startswith("RESEARCH_SOURCE_GDELT")]
    assert gd and "optional source" not in gd[0]


def test_other_partial_causes_still_win_even_when_only_gdelt_failed():
    llm = ScriptedLLM()

    def bad(d):
        return d.model_copy(update={"risks": [r.model_copy(update={"citations": [cite("E1", "an invented quote that is not in any evidence item")]})
                                              if r.category == "supply_disruption" else r for r in d.risks]})
    llm.drafts = [bad, bad]
    resp, _ = run_assessment(dict(REQUEST), deps(llm=llm, gdelt=MockSearch("gdelt", TODAY, fail_with="TIMEOUT")))
    assert resp.status == "partial" and any(w.startswith("UNSUPPORTED_CLAIMS_REMOVED") for w in resp.warnings)


# ───────── B: supplier-product claims ─────────
def test_the_draft_prompt_guards_supplier_product_claims_and_dependent_items():
    p = prompts.DRAFT_SYSTEM
    assert prompts.PROMPT_VERSION >= "2026-11-13" and "16. SUPPLIER PRODUCT CLAIMS" in p and "17. DEPENDENT ITEMS" in p
    assert "literally says refined or food-grade" in p and "Never infer refined food-grade" in p and "cite it by reference" in p


def test_a_lead_claiming_refined_food_grade_without_a_supporting_quote_is_removed_visibly():
    from tests.validation_fixtures import Q_CRUDE_REFINED, candidate
    bad = candidate(name="Example Danubian Oils", identity_citation=cite("S2", Q_CRUDE_REFINED), country="unknown", country_quote="",
                    product_type="refined_food_grade", product_quote="refined food-grade oil for bakeries")                 # the quote is NOT in S2's text
    out, removed, _, remaining = prune_unsupported(good_draft(supplier_candidates=[bad]), EVIDENCE, FACTS, True, 3, MONEY, CTX)
    assert not remaining and out.supplier_candidates == [] and any("supplier_candidates 'Example Danubian Oils' removed" in x for x in removed)


# ───────── live run of 13 Nov: an empty LLM summary stopped the removal of prunable items and failed the whole run ─────────
def test_an_empty_llm_summary_with_unsupported_items_gives_a_partial_report_not_a_failed_run():
    llm = ScriptedLLM()

    def broken(d):
        risks = [r.model_copy(update={"citations": [cite("E1", "an invented quote that is not in any evidence item")]}) if r.category == "supply_disruption" else r
                 for r in d.risks]
        return d.model_copy(update={"executive_summary": "", "risks": risks})
    llm.drafts = [broken, broken]
    resp, _ = run_assessment(dict(REQUEST), deps(llm=llm))
    assert resp.status == "partial" and resp.error is None and resp.report is not None
    assert any(w.startswith("UNSUPPORTED_CLAIMS_REMOVED") for w in resp.warnings) and resp.report.removed_claims
    assert resp.report.draft.executive_summary.startswith("- **Exposure over the next")              # the summary shown is the code-built one


def test_the_draft_prompt_still_asks_for_a_plain_summary_sentence():
    assert "one plain, neutral sentence of at least 40 characters (never empty)" in prompts.DRAFT_SYSTEM and prompts.PROMPT_VERSION >= "2026-11-13.2"
