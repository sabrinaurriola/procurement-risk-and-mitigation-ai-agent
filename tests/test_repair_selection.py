"""Draft-repair regression (live runs of 13 Nov): the LLM repair regenerates the whole draft and sometimes introduces MORE unsupported claims than the
original draft had. Both drafts are now pruned by the same process and the better one is kept. Offline; no research or LLM call is real.

Selection rule under test (src/agent/validation.py::select_candidate):
  1. only a candidate with no unfixable problem is eligible; 2. most complete risk -> mitigation -> action chains; 3. most retained verified items;
  4. a genuine tie goes to the repaired draft. Substantive claim removal still means PARTIAL; nothing is forced to COMPLETED."""
from __future__ import annotations

import json

from src.agent.graph import run_assessment
from src.agent.schemas import Citation
from src.agent.validation import (EXCERPT_MAX, Candidate, _enforce_no_dangling, prune_candidate, retained_chains, retained_total,
                                  select_candidate)
from tests.agent_fixtures import REQUEST, ScriptedLLM, deps
from tests.validation_fixtures import CTX, EVIDENCE, FACTS, MONEY, action, candidate, cite, cov_risk, good_draft, mitigation, risk, sup_risk

BAD_QUOTE = "an invented quote that does not appear in any evidence item at all"


def cand(name, draft, remaining=()):
    return Candidate(name, draft, [], [], list(remaining), [])


def chain_draft(chained: list[str], extra_suppliers: int = 0):
    """Risks R1, R2, R3; a mitigation and an action for each risk id in `chained`; optional extra supplier leads."""
    mits = [mitigation(option=f"Mitigation for {r}", addresses=[r]) for r in chained]
    acts = [action(action=f"Action for {r}", addresses=[r]) for r in chained]
    sups = [candidate(name=f"Example Supplier {i}") for i in range(extra_suppliers)]
    return good_draft(risks=[cov_risk(), sup_risk(), risk()], mitigation_options=mits, recommended_actions=acts, supplier_candidates=sups)


# ───────── the selection rule ─────────
def test_substance_beats_quantity():
    two_chains = chain_draft(["R1", "R3"], extra_suppliers=0)
    one_chain_many_items = chain_draft(["R1"], extra_suppliers=3)
    assert retained_chains(two_chains) == 2 and retained_chains(one_chain_many_items) == 1
    assert retained_total(one_chain_many_items) > retained_total(two_chains)                    # more items, but less substance
    chosen, diag = select_candidate([cand("original", two_chains), cand("repaired", one_chain_many_items)])
    assert chosen.name == "original" and diag["chosen"] == "original"
    chosen, _ = select_candidate([cand("original", one_chain_many_items), cand("repaired", two_chains)])
    assert chosen.name == "repaired"


def test_with_equal_chains_the_draft_with_more_verified_items_wins():
    few, many = chain_draft(["R1"], 0), chain_draft(["R1"], 2)
    assert select_candidate([cand("original", many), cand("repaired", few)])[0].name == "original"
    assert select_candidate([cand("original", few), cand("repaired", many)])[0].name == "repaired"


def test_a_genuine_tie_prefers_the_repaired_draft():
    same = chain_draft(["R1", "R3"], 1)
    assert select_candidate([cand("original", same), cand("repaired", same)])[0].name == "repaired"
    assert select_candidate([cand("repaired", same), cand("original", same)])[0].name == "repaired"


def test_a_draft_with_an_unfixable_problem_is_never_selected_however_many_items_it_has():
    small, huge = chain_draft(["R1"], 0), chain_draft(["R1", "R3"], 3)
    chosen, diag = select_candidate([cand("original", small), cand("repaired", huge, remaining=["risk_id R3 is used more than once"])])
    assert chosen.name == "original" and [r["eligible"] for r in diag["candidates"]] == [True, False]
    chosen, diag = select_candidate([cand("original", huge, remaining=["x"]), cand("repaired", small, remaining=["y"])])
    assert chosen is None and diag["chosen"] is None


# ───────── the same prune process, no dangling references ─────────
def bad_risk():
    return risk(risk_id="R9", title="Unsupported claim", citations=[cite("E1", BAD_QUOTE)])


def test_a_removed_risk_never_leaves_a_mitigation_or_an_action_behind():
    d = good_draft(risks=[cov_risk(), sup_risk(), bad_risk()],
                   mitigation_options=[mitigation(), mitigation(option="Only for the bad risk", addresses=["R9"], basis="internal_model")],
                   recommended_actions=[action(), action(action="Only for the bad risk", addresses=["R9"])])
    c = prune_candidate("original", d, [], EVIDENCE, FACTS, True, 3, MONEY, CTX)
    ids = {r.risk_id for r in c.draft.risks}
    assert not c.remaining and "R9" not in ids
    for x in c.draft.mitigation_options + c.draft.recommended_actions:
        assert x.addresses and set(x.addresses) <= ids
    assert any("Only for the bad risk" in r and "addressed only the removed risk" in r for r in c.removed)


def test_the_no_dangling_post_condition_removes_orphans_and_trims_dead_references():
    d = good_draft(mitigation_options=[mitigation(addresses=["R1", "R9"]), mitigation(option="Orphan", addresses=["R9"])],
                   recommended_actions=[action(addresses=["R8"])])
    out, removed, details = _enforce_no_dangling(d)
    assert [m.option for m in out.mitigation_options] == [mitigation().option] and out.mitigation_options[0].addresses == ["R1"]
    assert out.recommended_actions == [] and len(removed) == 2 and {x["kind"] for x in details} == {"mitigation_options", "recommended_actions"}
    clean, none_removed, _ = _enforce_no_dangling(good_draft())
    assert none_removed == [] and clean == good_draft()


def test_prune_candidate_records_bounded_diagnostics_for_every_discarded_claim():
    long_quote = "this quote is invented and does not appear in the source text. " * 8
    d = good_draft(risks=[cov_risk(), sup_risk(), risk(risk_id="R9", title="Unsupported claim", citations=[cite("E1", long_quote)])],
                   mitigation_options=[mitigation()], recommended_actions=[action()])
    c = prune_candidate("repaired", d, [], EVIDENCE, FACTS, True, 3, MONEY, CTX)
    (detail,) = [x for x in c.details if x["kind"] == "risks"]
    assert detail["ref"] == "R9" and detail["source_ids"] == ["E1"] and "not found verbatim" in detail["reason"]
    cit = detail["citations"][0]
    assert len(long_quote) > EXCERPT_MAX and len(cit["quote"]) <= EXCERPT_MAX and len(cit["source"]) <= EXCERPT_MAX and cit["quote"].endswith("...")
    json.dumps(c.details)                                                                      # plain data, safe to put in a trace file


# ───────── the whole graph ─────────
def one_bad_risk(d):
    risks = [r.model_copy(update={"citations": [Citation(evidence_id="E1", quote=BAD_QUOTE)]}) if r.category == "supply_disruption" else r for r in d.risks]
    return d.model_copy(update={"risks": risks})


def and_broken_suppliers(d):
    d = one_bad_risk(d)
    sups = [c.model_copy(update={"identity_citation": Citation(evidence_id=c.identity_citation.evidence_id,
                                                               quote="an invented identity quote that appears nowhere in the source")})
            for c in d.supplier_candidates]
    return d.model_copy(update={"supplier_candidates": sups})


def run_with(*drafts):
    return run_assessment(dict(REQUEST), deps(llm=ScriptedLLM(drafts=list(drafts))))


def validate_steps(trace):
    return [t for t in trace if t["node"] == "validate"]


def assert_no_dangling(resp):
    ids = {r.risk_id for r in resp.report.draft.risks}
    for x in resp.report.draft.mitigation_options + resp.report.draft.recommended_actions:
        assert set(x.addresses) <= ids


def test_a_repair_that_adds_unsupported_supplier_leads_loses_to_the_original_draft():
    baseline, _ = run_with()
    assert baseline.status == "completed" and baseline.report.draft.supplier_candidates            # precondition: the mock draft has supplier leads
    resp, trace = run_with(one_bad_risk, and_broken_suppliers)
    last = validate_steps(trace)[-1]
    assert last["selection"]["chosen"] == "original"
    rows = {r["name"]: r for r in last["selection"]["candidates"]}
    assert rows["original"]["retained"] > rows["repaired"]["retained"] and rows["original"]["chains"] >= rows["repaired"]["chains"]
    assert resp.status == "partial" and resp.error is None                                         # a removal still means PARTIAL
    assert len(resp.report.draft.supplier_candidates) == len(baseline.report.draft.supplier_candidates)
    assert all("supplier_candidates" not in r for r in resp.report.removed_claims) and len(resp.report.removed_claims) >= 1
    assert any(w.startswith("UNSUPPORTED_CLAIMS_REMOVED") for w in resp.warnings)
    assert_no_dangling(resp)
    assert {d["kind"] for d in last["discarded"]} >= {"risks"} and all(len(d["reason"]) <= EXCERPT_MAX for d in last["discarded"])
    json.dumps(trace, default=str)


def test_when_both_drafts_are_equally_good_the_repaired_one_is_kept():
    resp, trace = run_with(one_bad_risk, one_bad_risk)
    assert validate_steps(trace)[-1]["selection"]["chosen"] == "repaired" and resp.status == "partial"
    assert_no_dangling(resp)


def test_an_unusable_repair_falls_back_to_the_original_draft_pruned():
    resp, trace = run_with(one_bad_risk, {"not": "a report"})
    assert validate_steps(trace)[-1]["selection"]["chosen"] == "original"
    assert resp.status == "partial" and resp.error is None and resp.report is not None and resp.report.removed_claims
    assert_no_dangling(resp)


def duplicate_risk_id(d):
    return d.model_copy(update={"risks": d.risks + [d.risks[-1].model_copy(update={"title": "Same id again"})]})


def test_when_neither_draft_can_be_fixed_by_removal_the_run_still_fails():
    resp, trace = run_with(duplicate_risk_id, duplicate_risk_id)
    assert resp.status == "failed" and resp.error.code == "REPORT_VALIDATION_FAILED"
    assert validate_steps(trace)[-1]["selection"]["chosen"] is None


def test_a_repair_that_fixes_everything_is_used_as_before_without_any_removal():
    resp, trace = run_with(one_bad_risk)                                                           # second draft is the clean mock draft
    assert resp.status == "completed" and resp.report.removed_claims == []
    assert validate_steps(trace)[-1]["selection"] is None
