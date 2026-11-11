"""Policy RAG inside the LangGraph workflow and the validation of policy citations (offline, mocked retrieval)."""
import json
import re

import pytest

from src.agent.graph import run_assessment
from src.agent.mock import build_mock_policy
from src.agent.schemas import PolicyCitation
from src.agent.validation import ValidationContext, prune_unsupported, validate_draft
from tests.agent_fixtures import REQUEST, ScriptedLLM, deps
from tests.validation_fixtures import EVIDENCE, FACTS, INDICATORS, MONEY, REFS, action, good_draft, mitigation

P_COV = ("POL-002#3", "When uncovered volume increases from one month to the next, for example because a contract ends during the horizon, "
                      "the Raw Materials Procurement Manager performs a coverage review and recommends whether to extend, replace or add contracts.")
P_SUP = ("ASM-003#2", "Until both conditions are closed, any renewal of CT-003 or increase of its volume requires sign-off by the Procurement Director.")
Q_COV = "When uncovered volume increases from one month to the next"
Q_SUP = "any renewal of CT-003 or increase of its volume requires sign-off by the Procurement Director"
CTX = ValidationContext(set(REFS), INDICATORS, dict([P_COV, P_SUP]))


def check(d, ctx=CTX):
    return validate_draft(d, EVIDENCE, FACTS, True, 3, MONEY, ctx)


def run(d=None, **kw):
    return run_assessment(REQUEST, d or deps(**kw))


def pcite(ref=P_COV[0], quote=Q_COV):
    return PolicyCitation(policy_ref=ref, quote=quote)


def cov_mitigation(**kw):
    base = dict(option="Trigger a coverage review for the months where uncovered volume increases",
                rationale="Uncovered volume increases over the horizon, which raises market exposure.", policy_citations=[pcite()])
    base.update(kw)
    return mitigation(**base)


# ───────────── validation of policy citations ─────────────
def test_policy_citation_to_a_retrieved_passage_with_a_verbatim_quote_is_accepted():
    assert check(good_draft(mitigation_options=[cov_mitigation()])) == []
    sup = mitigation(option="Close the audit conditions before renewing the contract with the conditional supplier",
                     rationale="Renewal needs sign-off by the Procurement Director once the conditions are closed.", applies_to="existing_supplier",
                     addresses=["R2"], internal_refs=["INT:indicator:supplier_status"], policy_citations=[pcite(*P_SUP[:1], Q_SUP)])
    assert check(good_draft(mitigation_options=[sup])) == []


def test_policy_citations_are_allowed_on_actions_too():
    a = action(action="Review coverage when uncovered volume increases", rationale="Uncovered volume increases from month to month.",
               policy_citations=[pcite()])
    assert check(good_draft(recommended_actions=[a])) == []


def test_a_policy_passage_that_was_not_retrieved_cannot_be_cited():
    p = check(good_draft(mitigation_options=[cov_mitigation(policy_citations=[pcite("POL-009#0", Q_COV)])]))
    assert any("POL-009#0 was not retrieved" in x and "may not be invented" in x for x in p)


def test_quote_must_be_verbatim_long_enough_and_related():
    assert any("not found verbatim" in x for x in check(good_draft(mitigation_options=[cov_mitigation(policy_citations=[
        pcite(quote="The policy demands a hedge on every ton of uncovered volume")])])))
    assert any("too short" in x for x in check(good_draft(mitigation_options=[cov_mitigation(policy_citations=[pcite(quote="coverage review")])])))
    unrelated = cov_mitigation(option="Approve a new warehouse lease", rationale="Storage space is limited near the factories.")
    assert any("does not relate to the recommendation" in x for x in check(good_draft(mitigation_options=[unrelated])))


def test_a_statement_of_policy_requirements_without_a_citation_is_rejected():
    for text in ("Company policy requires a quarterly hedge review.", "According to the policy, coverage must be extended.",
                 "Policy states that the supplier must be replaced."):
        m = mitigation(rationale=text, policy_citations=[])
        assert any("states what a policy requires without citing" in x for x in check(good_draft(mitigation_options=[m]))), text
    assert check(good_draft(mitigation_options=[mitigation(rationale="Uncovered volume carries full benchmark exposure.", policy_citations=[])])) == []


def test_invented_policy_figures_are_rejected_even_next_to_a_real_citation():
    m = cov_mitigation(rationale="Policy sets a 70% coverage target for uncovered volume increases.")
    assert any("percentage 70%" in x and "no invented requirements" in x for x in check(good_draft(mitigation_options=[m])))


def test_policy_passages_cannot_establish_regulatory_compliance():
    m = cov_mitigation(rationale="Following the coverage review keeps the company compliant with EU regulation on uncovered volume increases.")
    assert any("cannot establish real regulatory compliance" in x for x in check(good_draft(mitigation_options=[m])))


def test_no_policy_can_be_cited_when_retrieval_returned_nothing():
    empty = ValidationContext(set(REFS), INDICATORS, {})
    p = check(good_draft(mitigation_options=[cov_mitigation()]), ctx=empty)
    assert any("was not retrieved" in x for x in p)


def test_unsupported_policy_items_are_removed_visibly_not_kept():
    bad = cov_mitigation(policy_citations=[pcite("POL-009#0", Q_COV)])
    d = good_draft(mitigation_options=[mitigation(), bad])
    out, removed, _, remaining = prune_unsupported(d, EVIDENCE, FACTS, True, 3, MONEY, CTX)
    assert remaining == [] and len(out.mitigation_options) == 1 and len(removed) == 1 and "POL-009#0" in removed[0]


# ───────────── workflow ─────────────
def test_report_uses_retrieved_policy_passages_with_real_citations():
    resp, trace = run()
    r = resp.report
    assert resp.status == "completed" and r.policy.status == "ok" and r.policy.passages
    by = {p.policy_ref: p.content for p in r.policy.passages}
    cited = [(c, x) for x in list(r.draft.mitigation_options) + list(r.draft.recommended_actions) for c in x.policy_citations]
    assert cited, "mitigations/actions should rest on retrieved policy"
    norm = lambda t: " ".join(t.split()).casefold()  # noqa: E731
    assert all(c.policy_ref in by and norm(c.quote) in norm(by[c.policy_ref]) for c, _ in cited)
    refs = {c.policy_ref for c, _ in cited}
    assert any(x.startswith("POL-002") for x in refs) and any(x.startswith(("ASM-003", "POL-001")) for x in refs)   # coverage + conditional supplier
    node = next(n for n in trace if n["node"] == "retrieve_policy")
    assert node["status"] == "ok" and node["passages"] == len(r.policy.passages) and node["embedding_calls"] == 1
    assert r.source_status["policy"].startswith("ok: in_memory_vector")


def test_markdown_shows_policy_context_citations_and_the_not_compliance_disclaimer():
    resp, _ = run()
    md = resp.report_markdown
    assert "## Internal policy context (fictional policy documents)" in md and "not evidence of real regulatory compliance" in md
    assert "[POL-002#" in md and "[ASM-003#" in md or "[POL-001#" in md
    assert "fictional internal documents" in " ".join(resp.limitations)


def test_policy_text_is_given_to_the_llm_and_never_changes_the_numbers():
    llm = ScriptedLLM()
    with_policy, _ = run(deps(llm=llm))
    facts = json.loads(llm.draft_args[0][0])
    assert facts["policy_context"]["status"] == "ok" and facts["policy_context"]["passages"]
    assert {"policy_ref", "document", "type", "text"} <= set(facts["policy_context"]["passages"][0])
    without, _ = run(deps(policy=build_mock_policy(fail_with="TIMEOUT")))
    assert with_policy.report.financial == without.report.financial and with_policy.report.indicators == without.report.indicators


def test_retrieval_failure_surfaces_the_limitation_and_invents_no_policy_guidance():
    resp, trace = run(deps(policy=build_mock_policy(fail_with="TIMEOUT")))
    r = resp.report
    assert resp.status == "partial" and r.policy.status == "unavailable" and not r.policy.passages
    assert any(w.startswith("POLICY_CONTEXT_UNAVAILABLE") for w in resp.warnings)
    assert all(not x.policy_citations for x in list(r.draft.mitigation_options) + list(r.draft.recommended_actions))
    assert "UNAVAILABLE — no policy guidance is given" in resp.report_markdown and "policy guidance is unavailable" in r.draft.executive_summary
    assert next(n for n in trace if n["node"] == "retrieve_policy")["status"] == "unavailable"


def test_policy_retriever_not_configured_is_visible_not_silent():
    resp, _ = run(deps(policy=None))
    assert resp.status == "partial" and resp.report.policy.status == "unavailable" and "not configured" in resp.report.policy.note


def test_retriever_exceptions_are_sanitised():
    class Boom:
        def retrieve(self, q, ing, k):
            raise RuntimeError("connection to postgresql://user:SECRETPW@db.example failed")

    resp, _ = run(deps(policy=Boom()))
    blob = json.dumps(resp.model_dump(mode="json"), default=str)
    assert resp.report.policy.status == "unavailable" and "RuntimeError" in resp.report.policy.note and "SECRETPW" not in blob


def test_full_text_fallback_is_reported_but_does_not_degrade_the_report():
    resp, _ = run(deps(policy=build_mock_policy(fail_embedding=True)))
    assert resp.status == "completed" and resp.report.policy.status == "fallback" and resp.report.policy.passages
    assert any(w.startswith("POLICY_RETRIEVAL_FALLBACK") for w in resp.warnings) and "(fallback)" in resp.report_markdown


def test_an_invented_policy_requirement_from_the_llm_is_rejected_then_repaired():
    def invent(good):
        m = good.mitigation_options[0].model_copy(update={"rationale": "Company policy requires a 30% hedge ratio.", "policy_citations": []})
        return good.model_copy(update={"mitigation_options": [m] + good.mitigation_options[1:]})
    llm = ScriptedLLM(drafts=[invent])
    resp, _ = run(deps(llm=llm))
    assert resp.status == "completed" and llm.draft_calls == 2
    assert any("states what a policy requires without citing" in p for p in llm.draft_args[1][2])
    assert "30% hedge" not in resp.report_markdown


def test_a_fabricated_policy_citation_that_survives_the_repair_is_removed_not_published():
    def fake(good):
        m = good.mitigation_options[0].model_copy(update={"policy_citations": [PolicyCitation(policy_ref="POL-009#0", quote=Q_COV)]})
        return good.model_copy(update={"mitigation_options": [m] + good.mitigation_options[1:]})
    resp, _ = run(deps(llm=ScriptedLLM(drafts=[fake, fake])))
    assert resp.status == "partial" and any("POL-009#0" in x for x in resp.report.removed_claims)
    assert "POL-009" not in resp.report_markdown.split("## Unverified concerns")[0]


def test_retrieval_is_read_only_and_makes_no_paid_call_in_mock_runs():
    retriever = build_mock_policy()
    resp, _ = run(deps(policy=retriever))
    assert resp.execution.mock and resp.execution.llm_calls == 2 and resp.execution.tokens == {"input": 0, "output": 0}


# ───────────── conditional-supplier approval sequence in the workflow (after the POL-001 / ASM-003 consistency correction) ─────────────
def test_sup003_escalation_follows_the_corrected_policy_sequence():
    from src.agent.graph import run_assessment
    from tests.agent_fixtures import REQUEST, deps
    resp, _ = run_assessment(REQUEST, deps())
    assert resp.status == "completed" and not resp.report.removed_claims
    stored = {p.policy_ref: " ".join(p.content.split()) for p in resp.report.policy.passages}
    act = next(a for a in resp.report.draft.recommended_actions if "SUP-003" in a.action)
    assert act.requires_human_approval and act.basis == "internal_model"
    assert "complete and verify its open conditions" in act.action                                  # the outstanding qualification actions
    assert {c.policy_ref for c in act.policy_citations} == {"POL-001#2", "ASM-003#2"}
    for c in act.policy_citations:                                                                  # citations resolve to the updated stored passages
        assert " ".join(c.quote.split()) in stored[c.policy_ref]
    assert "in writing" in stored["POL-001#2"] and "in writing" in stored["ASM-003#2"]
    assert "continue to deliver" in stored["POL-001#2"]
    prose = " ".join([resp.report.draft.executive_summary] + [x.rationale + " " + x.next_step for x in resp.report.draft.recommended_actions]
                     + [m.rationale for m in resp.report.draft.mitigation_options])
    assert not re.search(r"\b(exception|waive\w*|bypass\w*|override\w*)\b", prose, re.I)
    assert "not blocked" in act.rationale and "no renewal or volume increase is committed before" in act.next_step
    f = resp.report.financial                                                                       # deterministic results unchanged
    assert f.baseline_modelled_cost_eur == "1,961,496.55" and f.months[0].incremental_exposure_eur["+10%"] == "28,565.04"
    assert next(s for s in f.scenarios if s.shock == "+10%").incremental_exposure_eur == "107,744.17"


def test_conditional_supplier_policy_is_still_retrieved_offline():
    from src.agent.mock import build_mock_policy
    from src.rag.retrieve import build_queries
    res = build_mock_policy().retrieve(build_queries("sunflower_oil", {"supplier_status"}), "sunflower_oil", 3)
    assert "POL-001#2" in {p.policy_ref for p in res.passages}
