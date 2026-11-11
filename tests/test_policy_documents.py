"""Governance checks on the six fictional policy documents (the approval gate before they are loaded into the database).

They verify what can be verified mechanically: fictional labelling, no real legal/regulatory claims, human approval
language, no invented numeric thresholds, and consistency with the synthetic supplier and contract data."""
import csv
import re
from pathlib import Path

import pytest

from src.rag.chunker import load_documents

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "data" / "synthetic" / "documents"
FILES = {p.name[:7]: p for p in sorted(DOCS.glob("*.md"))}


def text(doc_id):
    return FILES[doc_id].read_text(encoding="utf-8")


def rows(name):
    with (ROOT / "data" / "synthetic" / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def chunks():
    cs, errors = load_documents(DOCS, {"sunflower_oil", "wheat"})
    assert errors == []
    return cs


def test_there_are_exactly_the_six_approved_documents():
    assert sorted(FILES) == ["ASM-001", "ASM-003", "POL-001", "POL-002", "POL-003", "POL-004"]


def test_every_document_is_explicitly_fictional_and_belongs_to_bakery_foods(chunks):
    for d in FILES:
        first = next(c for c in chunks if c.doc_id == d and c.chunk_index == 0).content.lower()
        assert "fictional" in first and "bakery foods inc." in first, d
        assert "synthetic: true" in text(d)
        assert "portfolio simulation" in first, d


def test_no_document_presents_itself_as_a_real_legal_regulatory_or_industry_requirement():
    forbidden = re.compile(r"\b(regulations?|regulatory|directive|legislation|gdpr|haccp|fssc|codex|iso\s?\d*|eu law|statutory|by law)\b|"
                           r"\b(must|shall) comply with\b", re.I)
    for d in FILES:
        body = text(d).split("---", 2)[2].replace("legal entity", "company entity")
        for sentence in re.split(r"(?<=[.!?])\s+|\n", body):
            if forbidden.search(sentence) or re.search(r"\blaw\b", sentence, re.I):
                assert re.search(r"\bnot\b|\bno real\b", sentence, re.I), f"{d}: unqualified legal/regulatory wording: {sentence!r}"


def test_no_numeric_thresholds_percentages_or_amounts_are_invented():
    for d in FILES:
        body = text(d).split("---", 2)[2]
        assert not re.search(r"\d\s?%|€|EUR|USD|\$\s?\d|\b\d+\s?(t|tonnes?|kg)\b", body), f"{d}: contains a figure that could be mistaken for a threshold"


def test_decisions_remain_with_named_people_and_tools_never_approve():
    approval_words = re.compile(r"\b(approv\w*|qualif\w*|renewal|sign-off)\b", re.I)
    people = re.compile(r"Procurement Director|Raw Materials Procurement Manager|people (who hold|with) approval authority|Legal", re.I)
    for d in ("POL-001", "POL-002", "POL-003", "ASM-003"):
        assert approval_words.search(text(d)) and people.search(text(d)), d
    assert re.search(r"decision-support tools, including the risk assessment agent, can recommend but never approve", text("POL-001"))
    assert re.search(r"agent may suggest leads, but it cannot qualify or approve a supplier", text("POL-003"))
    assert re.search(r"any decision remains with the people who hold approval authority", text("POL-002"))
    for d in FILES:
        assert not re.search(r"\b(automatically|auto-)(approve|qualify|order|purchase)|agent (approves|orders|purchases|qualifies)\b", text(d), re.I), d


def test_documents_agree_with_the_synthetic_supplier_and_contract_data():
    sup = {r["supplier_id"]: r for r in rows("suppliers.csv")}
    con = {r["contract_id"]: r for r in rows("contracts.csv")}
    for d in FILES:
        for sid in re.findall(r"SUP-\d{3}", text(d)):
            assert sid in sup, (d, sid)
        for cid in re.findall(r"CT-\d{3}", text(d)):
            assert cid in con, (d, cid)
    a1, a3 = text("ASM-001"), text("ASM-003")
    assert sup["SUP-001"]["approval_status"] == "approved" and "approved supplier" in a1
    assert con["CT-001"]["contract_type"] == "fixed" and "fixed-price contract, CT-001" in a1
    assert con["CT-001"]["end_date"].startswith("2027-06") and "June 2027" in a1
    assert sup["SUP-003"]["approval_status"] == "conditional" and "conditional" in a3
    assert con["CT-003"]["contract_type"] == "index_linked" and "index-linked contract CT-003" in a3
    assert sup["SUP-003"]["country"] == "RO" and "Romania" in a3
    assert con["CT-003"]["start_date"].startswith("2026-09") and "since September 2026" in a3
    assert "SUP-001" in a1 and "Aceites del Sur S.A." in a1 and "Danube Agri Oils SRL" in a3


def test_no_document_makes_a_comparative_claim_the_data_contradicts():
    """live review finding: ASM-001 once called SUP-001 the 'largest' contract holder although CT-003 (120 t) exceeds CT-001 (100 t)."""
    for d in FILES:
        assert not re.search(r"\b(largest|biggest|main|primary|sole|only)\b[^.]{0,40}\bsupplier\b|\blargest single\b", text(d), re.I), d
    vol = {r["contract_id"]: float(r["committed_monthly_volume_t"]) for r in rows("contracts.csv")}
    assert vol["CT-003"] > vol["CT-001"]


def test_policies_do_not_define_numeric_objectives_so_a_breach_cannot_be_inferred_by_arithmetic():
    """POL-002's coverage objective is qualitative ('a majority'); the agent must not claim it is breached or met."""
    assert "a majority of the forecast demand" in text("POL-002") and "a large share" in text("POL-001")


def test_scope_matches_the_company_profile_and_the_ingredients(chunks):
    assert "three factories" in text("POL-004")                                   # company profile: 3 factories in Spain
    by_doc = {c.doc_id: set(c.ingredient_codes) for c in chunks}
    assert by_doc["POL-001"] == {"sunflower_oil", "wheat"} and by_doc["POL-003"] == by_doc["POL-004"] == {"sunflower_oil"}
    sup = {r["supplier_id"]: r["ingredients"] for r in rows("suppliers.csv")}
    assert "sunflower_oil" in sup["SUP-001"] and "sunflower_oil" in sup["SUP-003"]


def test_policy_text_is_separate_from_financial_and_external_evidence_by_construction():
    """Three independent citation channels exist, and policy text can only back mitigations/actions, never a risk."""
    from src.agent.schemas import Action, Mitigation, Risk
    for model in (Mitigation, Action):
        assert {"citations", "internal_refs", "policy_citations"} <= set(model.model_fields)
    assert "policy_citations" not in Risk.model_fields


def test_policy_figures_can_never_justify_a_currency_amount():
    from decimal import Decimal
    from src.agent.validation import ValidationContext, validate_draft
    from tests.validation_fixtures import EVIDENCE, FACTS, INDICATORS, MONEY, REFS, good_draft
    ctx = ValidationContext(set(REFS), INDICATORS, {"POL-009#0": "## Rule" + chr(10) + "A review is held every 2026 quarter and covers 3 factories."})
    d = good_draft(executive_summary="Exposure of EUR 2,026 is expected over the horizon because uncovered demand grows.")
    problems = validate_draft(d, EVIDENCE, FACTS, True, 3, MONEY, ctx)
    assert any("currency amount 2,026" in p for p in problems)


def test_roles_are_declared_fictional_and_review_is_separated_from_approval_authority():
    for d in ("POL-001", "POL-002", "POL-003"):
        t = " ".join(text(d).split())
        assert "fictional organizational assumptions" in t, d
        assert "reviews and recommends; final approval authority rests with the Procurement Director" in t, d
    assert "and, for blocked suppliers, with Legal" in " ".join(text("POL-001").split())
    t2 = " ".join(text("POL-002").split())
    assert "performs a coverage review and recommends whether" in t2 and "contract commitments are approved by the Procurement Director" in t2
    assert "decides whether to extend" not in t2                       # review responsibility is not final approval


def test_pol003_allows_non_binding_qualification_activity_but_requires_approval_for_commitments():
    t = " ".join(text("POL-003").split())
    assert "may request non-binding samples, carry out technical evaluation and verify the lead's claims" in t
    assert "must not receive an order, a contract, a purchasing commitment or a supply-volume commitment until it has been qualified and the appropriate approval has been given" in t
    assert "binding sample requests" not in t and "volume indications" not in t         # superseded wording
    assert "cannot qualify or approve a supplier" in t


def test_assessments_identify_their_details_as_synthetic_scenario_information():
    for d in ("ASM-001", "ASM-003"):
        t = " ".join(text(d).split())
        assert "synthetic scenario information" in t and "not independently verified facts about any supplier" in t, d
        assert "audit" in t.lower()


# ───────────── conditional-supplier approval sequence: POL-001 and ASM-003 must say the same thing ─────────────
def _section(doc_id, heading):
    body = text(doc_id).split(heading, 1)[1].split("\n## ", 1)[0]
    return " ".join(body.split())


@pytest.fixture(scope="module")
def pol1():
    return _section("POL-001", "## Conditional suppliers")


@pytest.fixture(scope="module")
def asm3():
    return _section("ASM-003", "## Conditions to close")


def test_both_documents_state_the_same_approval_sequence(pol1, asm3):
    for t in (pol1, asm3):
        assert re.search(r"cannot be committed until (?:all open|both) conditions are closed", t), t
        assert "sign-off by the Procurement Director in writing" in t
    assert "POL-001" in asm3                                               # the assessment defers to the policy


def test_existing_deliveries_are_distinguished_from_renewals_and_increases(pol1, asm3):
    for t in (pol1, asm3):
        assert re.search(r"continue to deliver under (?:its existing valid contract|CT-003)", t)
        assert "subject to internal monitoring" in t
        assert re.search(r"renewal", t, re.I) and re.search(r"volume", t, re.I)
    assert "completed and verified" in pol1 and "verified" in asm3


def test_roles_are_split_and_no_exception_exists(pol1, asm3):
    assert "may initiate a review" in pol1 and "coordinate corrective actions" in pol1 and "recommend escalation" in pol1
    assert "cannot approve suppliers or authorize contractual changes" in pol1
    assert "may initiate a review" in asm3 and "decisions remain with the Procurement Director" in asm3
    assert "Nothing in this policy permits a renewal or volume increase while any condition remains open" in pol1
    for t in (pol1, asm3):
        assert not re.search(r"\b(exception|waive\w*|waiver|bypass\w*|override\w*)\b", t, re.I)
        assert not re.search(r"\bafter the open conditions are closed\b", t)    # the old, differently-ordered wording is gone
