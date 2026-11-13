"""Report validation: schema plus business guardrails. Returns a list of problems (empty = valid).

Item-level problems start with `risks[i]`, `mitigation_options[i]`, `supplier_candidates[i]` or `recommended_actions[i]`
so that `prune_unsupported` can REMOVE just the unsupported item (and say so) instead of keeping it with a decorative citation.

Evidence rules (proportionate, rule-based):
* basis internal_model  -> cites internal_refs only; external citations are rejected.
* basis external_evidence -> cites evidence only. basis combined -> both.
* A citation = evidence_id + a VERBATIM quote from that item's title/snippet that shares a distinctive word with the claim.
  The cited item must be judged direct or contextual (never irrelevant/unjudged), must not be social media, and a finding
  needs at least one DIRECT citation from an official/trade-press source. Contextual, commercial, unrated sources may
  accompany a finding but never carry it alone.
* HIGH severity needs an explainable basis: for internal categories the deterministic indicator; for external/combined
  findings at least two distinct strong direct sources.
* Supplier candidates: real-company leads only; country, product type and identity must be supported by a quote from a
  non-social, direct source; unsupported attributes must be 'unknown'.
* The LLM may not introduce currency amounts that are not genuine monetary facts or in cited evidence.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from src.agent import claims as cl
from src.agent.credibility import STRONG_TIERS, country_supported
from src.agent.schemas import Citation, DraftReport, EvidenceItem, Indicator, PolicyCitation, Risk

NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
AMOUNT_RE = re.compile(r"(?:€|EUR|USD|\$)\s?(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s?(?:€|EUR|USD|euros?|dollars?)", re.I)
BREACH_RE = re.compile(r"\b(approved|pre-?qualified|qualified|capacity[- ]confirmed|certified|verified supplier|preferred supplier|"
                       r"guarantee[sd]?|in stock|confirmed (?:capacity|price|availability)|competitive price)\b", re.I)
REPLACE_RE = re.compile(r"\b(replace|replacing|switch|switching|terminate|cancel|place (?:an? )?orders?|purchase|buy)\b", re.I)
POLICY_CLAIM_RE = re.compile(r"\bpolic(?:y|ies)\b[^.]{0,60}\b(?:requires?|states?|mandates?|stipulates?|specifies|says|prescribes|obliges?)\b|"
                             r"\b(?:according to|per|under|as required by)\b[^.]{0,25}\bpolic(?:y|ies)\b", re.I)
REGULATORY_RE = re.compile(r"\b(?:complian(?:t|ce)|comply|complies)\b[^.]{0,60}\b(?:regulations?|regulatory|law|laws|legal|directive|HACCP|ISO)\b|"
                           r"\b(?:regulations?|regulatory|legal|legally|law)\b[^.]{0,40}\b(?:complian(?:t|ce)|comply|complies|requires?)\b", re.I)
PCT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?%")
ITEM_RE = re.compile(r"^(risks|mitigation_options|supplier_candidates|recommended_actions)\[(\d+)\]")
RISK_ID_RE = re.compile(r"^R\w{1,9}$")
STOP = set("""about above after again against could their there these those which while would should where being between
because before during other since through under until within without across around among whether market markets prices price
supply sunflower oils edible vegetable global world trade demand report reports according current future""".split())
MIN_QUOTE = 25
INTERNAL_CATEGORY_INDICATOR = {"contract_coverage": "contract_coverage", "supplier_status": "supplier_status"}
INDICATOR_FOR = {**INTERNAL_CATEGORY_INDICATOR, "market_price": "price_exposure"}   # market_price is internal only as a pure sensitivity finding
PRICE_INDICATOR_REF = "INT:indicator:price_exposure"


@dataclass
class ValidationContext:
    internal_refs: set[str] = field(default_factory=set)
    indicators: list[Indicator] = field(default_factory=list)
    policy: dict[str, str] = field(default_factory=dict)   # retrieved passages: policy_ref -> text

    def severity_for(self, category: str) -> str | None:
        ind = next((i for i in self.indicators if i.category == INDICATOR_FOR.get(category)), None)
        return ind.severity if ind else None


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("…", "...")
    return re.sub(r"\s+", " ", s).strip().casefold()


def _tokens(s: str) -> set[str]:
    """Distinctive word stems (first 5 letters), so 'tight' and 'tightening' match; generic market words are ignored."""
    return {t[:5] for t in re.findall(r"[a-zà-ÿ]{5,}", s.casefold()) if t not in STOP}


def _numbers(text: str) -> set[Decimal]:
    out = set()
    for tok in NUM_RE.findall(text):
        try:
            out.add(Decimal(tok.replace(",", "")).normalize())
        except InvalidOperation:
            pass
    return out


def _texts(d: DraftReport) -> list[tuple[str, str]]:
    t = [("executive_summary", d.executive_summary)]
    t += [(f"risks[{i}]", f"{r.title} {r.description} {r.severity_basis} {r.time_relevance}") for i, r in enumerate(d.risks)]
    t += [(f"mitigation_options[{i}]", f"{m.option} {m.rationale}") for i, m in enumerate(d.mitigation_options)]
    t += [(f"supplier_candidates[{i}]", f"{c.name} {c.country} {c.rationale} {c.verification_needed}") for i, c in enumerate(d.supplier_candidates)]
    t += [(f"recommended_actions[{i}]", f"{a.action} {a.rationale} {a.next_step}") for i, a in enumerate(d.recommended_actions)]
    t += [(f"uncertainties[{i}]", u) for i, u in enumerate(d.uncertainties)]
    return t


def _check_citations(where: str, claim: str, cites: list[Citation], by_id: dict[str, EvidenceItem], *, need_risk_kind: bool,
                     problems: list[str], category: str = "") -> int:
    """Validate citations; return the number of DISTINCT strong direct sources among them."""
    strong: set[str] = set()
    claim_tokens = _tokens(claim)
    for c in cites:
        e = by_id.get(c.evidence_id)
        if e is None:
            problems.append(f"{where}: unknown evidence_id {c.evidence_id}")
            continue
        if e.relevance not in ("direct", "contextual"):
            problems.append(f"{where}: evidence {c.evidence_id} was judged {e.relevance} and cannot support a finding")
            continue
        if e.credibility == "social":
            problems.append(f"{where}: evidence {c.evidence_id} is social media (low confidence) and cannot support a finding")
            continue
        q = _norm(c.quote)
        if len(q) < MIN_QUOTE:
            problems.append(f"{where}: quote for {c.evidence_id} is too short (min {MIN_QUOTE} characters)")
            continue
        if q not in _norm(f"{e.title} {e.snippet}"):
            problems.append(f"{where}: quote for {c.evidence_id} is not found verbatim in that source")
            continue
        if not (claim_tokens & _tokens(c.quote)):
            problems.append(f"{where}: quote for {c.evidence_id} does not relate to the claim (no shared distinctive wording)")
            continue
        if need_risk_kind and (e.kind != "risk" or e.published_date is None):
            problems.append(f"{where}: evidence {c.evidence_id} is not a dated risk source")
            continue
        markers = cl.other_market_markers(c.quote)
        if markers:               # a price quoted for another market/delivery basis: context at most, never a strong source
            naming = markers - cl.NAMING_MARKERS_EXCLUDED
            if cl.PRICE_WORD.search(claim) and not any(m in claim.casefold() for m in naming):
                problems.append(f"{where}: quote for {c.evidence_id} is a price quotation from another market ({', '.join(sorted(markers))}) and cannot "
                                f"support a price claim about the Spanish benchmark unless the claim names that market and treats it as context")
            continue
        logistics = category == "supply_disruption" and cl.logistics_support(e.domain, c.quote)   # shipping press: supply-disruption claims only
        if e.relevance == "direct" and (e.credibility in STRONG_TIERS or logistics):
            strong.add(c.evidence_id)
    return len(strong)


def _check_policy(where: str, claim: str, pcites: list[PolicyCitation], ctx: ValidationContext, problems: list[str],
                  refs: list[str] | None = None) -> None:
    """A policy citation must name a RETRIEVED passage and quote it verbatim; a statement about what policy requires
    without any citation is an invented policy requirement.

    At least one quote must share distinctive wording with the recommendation. A further quote that does not may stay only when
    its passage is about the same supplier the recommendation concerns (e.g. ASM-003 for SUP-003), so a recommendation can cite the
    conditions of its own supplier without weakening the check for unrelated passages."""
    claim_tokens = _tokens(claim)
    claim_entities = cl.entity_ids(claim) | cl.entity_ids(" ".join(refs or []))
    valid, related = [], 0
    for c in pcites:
        text = ctx.policy.get(c.policy_ref)
        if text is None:
            problems.append(f"{where}: policy passage {c.policy_ref} was not retrieved (policy requirements may not be invented)")
            continue
        q = _norm(c.quote)
        if len(q) < MIN_QUOTE:
            problems.append(f"{where}: policy quote for {c.policy_ref} is too short (min {MIN_QUOTE} characters)")
        elif q not in _norm(text):
            problems.append(f"{where}: policy quote for {c.policy_ref} is not found verbatim in the retrieved passage")
        else:
            valid.append(c)
            if claim_tokens & _tokens(c.quote):
                related += 1
            elif not (claim_entities & cl.passage_entities(c.policy_ref, text)):
                problems.append(f"{where}: policy quote for {c.policy_ref} does not relate to the recommendation")
    if valid and not related and not any("does not relate" in p and p.startswith(where) for p in problems):
        problems.append(f"{where}: no cited policy quote substantively supports the recommendation (a quote about the same supplier is not enough alone)")
    quotes = " ".join(c.quote for c in valid)
    needs_approval_cite = bool(cl.SIGN_OFF.search(claim) or (cl.ESCALATE.search(claim) and cl.SUPPLIER_CONTEXT.search(claim)))
    if needs_approval_cite and not pcites:
        problems.append(f"{where}: refers to approval/sign-off authority without citing a retrieved policy passage (approval rules may not be invented)")
    elif needs_approval_cite and valid and not cl.APPROVAL_QUOTE.search(quotes):
        problems.append(f"{where}: no cited policy quote states the approval or review requirement the recommendation relies on")
    if not pcites and POLICY_CLAIM_RE.search(claim):
        problems.append(f"{where}: states what a policy requires without citing a retrieved policy passage")
    if REGULATORY_RE.search(claim):
        problems.append(f"{where}: fictional internal policy passages cannot establish real regulatory compliance")
    if valid:                 # cadence or process wording must be in the cited policy text, otherwise it is the model's own idea
        low = _norm(quotes).replace("-", "")
        for m in cl.CADENCE.finditer(claim):
            term = m.group(0).casefold()
            key = term if term.split()[0] in ("within", "every") else re.split(r"[\s]", term)[0].replace("-", "")
            if key not in low:
                problems.append(f"{where}: '{m.group(0)}' is not in the cited policy quote and must not be attributed to the policy")
    if cl.SUPPLIER_CONTEXT.search(claim):
        if cl.EXCEPTION.search(claim):
            problems.append(f"{where}: implies an approval exception or bypass; the policies define none (sign-off by the Procurement Director is required)")
        if cl.BLOCKED.search(claim):
            problems.append(f"{where}: a conditional supplier is not blocked; do not describe it as blocked")
        if cl.AGENT_DECISION.search(claim):
            problems.append(f"{where}: decision support cannot approve, renew or order; recommend escalation to the people with approval authority")


def _is_lead_only(cites: list[Citation], by_id) -> bool:
    return bool(cites) and all(by_id.get(c.evidence_id) is not None and by_id[c.evidence_id].kind == "supplier_discovery" for c in cites)


def _lead_problems(where: str, claim: str, cites: list[Citation], pcites: list[PolicyCitation], by_id, reported: dict[str, str], problems: list[str]) -> None:
    """A supplier-verification recommendation rests on a REPORTED lead plus the qualification policy (POL-003); it needs no trade-press evidence,
    and it still may not call the lead approved, qualified, certified or capacity-confirmed."""
    if not _is_lead_only(cites, by_id):
        return
    for c in cites:
        if c.evidence_id not in reported:
            problems.append(f"{where}: evidence {c.evidence_id} is not a reported lead; verification recommendations may cite only leads listed in the report")
        elif _norm(reported[c.evidence_id]) not in _norm(claim):
            problems.append(f"{where}: name the lead ('{reported[c.evidence_id]}') that the verification recommendation concerns")
    if not any(p.policy_ref.startswith("POL-003") for p in pcites):
        problems.append(f"{where}: a supplier-verification recommendation must cite the retrieved qualification policy (POL-003)")
    own_words = claim
    for p in pcites:                  # a quoted policy sentence ("until it has been qualified") is not a claim about the lead
        own_words = own_words.replace(p.quote, " ")
    if BREACH_RE.search(own_words):
        problems.append(f"{where}: must not be described as approved/qualified/certified/confirmed or with price/capacity claims")


def _check_basis(where: str, basis: str, claim: str, cites: list[Citation], refs: list[str], by_id, ctx: ValidationContext,
                 problems: list[str], *, need_risk_kind: bool, category: str = "", allow_lead: bool = False) -> int:
    for r in refs:
        if r not in ctx.internal_refs:
            problems.append(f"{where}: unknown internal reference {r}")
    strong = 0
    if basis == "internal_model":
        if cites:
            problems.append(f"{where}: an internal_model finding must not cite external evidence (cite internal_refs instead)")
        if not refs:
            problems.append(f"{where}: an internal_model finding must cite at least one internal reference")
    elif basis == "external_evidence":
        if cl.INTERNAL_EXPOSURE.search(claim):
            problems.append(f"{where}: the text uses internal exposure figures or records; a finding that uses both external evidence and "
                            f"internal exposure must have basis 'combined' and cite both")
        if refs:
            problems.append(f"{where}: basis is external_evidence but internal references were given (use basis 'combined')")
        if not cites:
            problems.append(f"{where}: an external_evidence finding must cite evidence with verbatim quotes")
    else:  # combined
        if not cites or not refs:
            problems.append(f"{where}: a combined finding must cite BOTH external evidence and internal references")
    if basis != "internal_model":
        strong = _check_citations(where, claim, cites, by_id, need_risk_kind=need_risk_kind, problems=problems, category=category)
        if cites and strong < 1 and not (allow_lead and _is_lead_only(cites, by_id)):
            problems.append(f"{where}: no direct citation from an official or trade-press source supports this finding")
    return strong


def _direction_problems(where: str, text: str, observed: str, only_contradictions: bool = False) -> list[str]:
    """Market-price text must follow the OBSERVED price direction of the direct evidence (sources describing falling prices cannot
    justify a claim of rising prices) and, for market-price findings, must state that direction."""
    out = []
    sentences = re.split(r"(?<=[.;])\s+", text)
    if observed == "falling":
        if any(cl.UP_CLAIM.search(s) and not cl.FALL_WORD.search(s) for s in sentences):
            out.append(f"{where}: claims rising/upward price pressure although the direct evidence predominantly reports falling prices")
        elif not only_contradictions and not cl.FALL_WORD.search(text):
            out.append(f"{where}: must state the observed price direction (falling) reported by the direct evidence")
    elif observed == "rising":
        if any(cl.DOWN_CLAIM.search(s) and not cl.RISE_WORD.search(s) for s in sentences):
            out.append(f"{where}: claims falling prices although the direct evidence predominantly reports rising prices")
        elif not only_contradictions and not cl.RISE_WORD.search(text):
            out.append(f"{where}: must state the observed price direction (rising) reported by the direct evidence")
    return out


def validate_draft(draft: DraftReport, evidence: list[EvidenceItem], facts_text: str, financial_available: bool,
                   max_candidates: int, money_allowed: set[Decimal] | None = None,
                   ctx: ValidationContext | None = None) -> list[str]:
    ctx = ctx or ValidationContext()
    problems: list[str] = []
    by_id = {e.evidence_id: e for e in evidence}

    if len(draft.executive_summary.strip()) < 40:
        problems.append("executive_summary is missing or too short")

    observed = cl.observed_price_direction(evidence)
    reported = {c.identity_citation.evidence_id: c.name for c in draft.supplier_candidates}

    # ── risks ──
    ids = [r.risk_id for r in draft.risks]
    for dup in {i for i in ids if ids.count(i) > 1}:
        problems.append(f"risk_id {dup} is used more than once")
    for i, r in enumerate(draft.risks):
        w = f"risks[{i}]"
        if not RISK_ID_RE.match(r.risk_id):
            problems.append(f"{w}: risk_id must look like R1, R2, ...")
        internal_price = r.category == "market_price" and r.basis == "internal_model"       # pure sensitivity of the model, no market claim
        internal_cat = r.category in INTERNAL_CATEGORY_INDICATOR or internal_price
        if r.category in INTERNAL_CATEGORY_INDICATOR and r.basis != "internal_model":
            problems.append(f"{w}: a {r.category} risk is derived from internal data and must have basis internal_model")
        if not internal_cat and r.basis == "internal_model":
            problems.append(f"{w}: a {r.category} risk needs external evidence (basis external_evidence or combined)")
        strong = _check_basis(w, r.basis, f"{r.title} {r.description}", r.citations, r.internal_refs, by_id, ctx, problems,
                              need_risk_kind=True, category=r.category)
        full_text = f"{r.title}. {r.description} {r.severity_basis}"
        if r.basis == "external_evidence" and cl.INTERNAL_EXPOSURE.search(r.severity_basis):
            problems.append(f"{w}: the severity reasoning uses internal exposure figures; use basis 'combined' and cite the internal references")
        if internal_price:
            if PRICE_INDICATOR_REF not in r.internal_refs:
                problems.append(f"{w}: an internal price-sensitivity risk must cite {PRICE_INDICATOR_REF}")
            if cl.UP_CLAIM.search(full_text) or cl.DOWN_CLAIM.search(full_text):
                problems.append(f"{w}: an internal sensitivity finding must not assert a market direction; add external evidence and use basis 'combined'")
        elif r.category == "market_price":
            problems += _direction_problems(w, full_text, observed)
        if len(r.severity_basis.strip()) < 20:
            problems.append(f"{w}: severity_basis must explain the severity (indicator or qualitative reasoning)")
        if internal_cat:
            want = ctx.severity_for(r.category)
            if want is None:
                problems.append(f"{w}: no deterministic indicator supports a {r.category} risk")
            elif r.severity != want:
                problems.append(f"{w}: severity must be {want} per the documented {r.category} indicator, not {r.severity}")
        elif r.severity == "high" and strong < 2:
            problems.append(f"{w}: HIGH severity needs at least two distinct direct citations from official/trade-press sources")

    # ── mitigations and actions ──
    risk_ids = set(ids)

    def link(where: str, addresses: list[str]) -> None:
        if not addresses:
            problems.append(f"{where}: must address at least one identified risk (addresses)")
        for a in addresses:
            if a not in risk_ids:
                problems.append(f"{where}: addresses unknown risk {a}")

    for i, m in enumerate(draft.mitigation_options):
        w = f"mitigation_options[{i}]"
        link(w, m.addresses)
        _check_basis(w, m.basis, f"{m.option} {m.rationale}", m.citations, m.internal_refs, by_id, ctx, problems, need_risk_kind=False, allow_lead=True)
        _lead_problems(w, f"{m.option} {m.rationale}", m.citations, m.policy_citations, by_id, reported, problems)
        _check_policy(w, f"{m.option} {m.rationale}", m.policy_citations, ctx, problems, m.internal_refs)
        problems += _direction_problems(w, f"{m.option} {m.rationale}", observed, only_contradictions=True)
        if m.applies_to == "new_supplier_candidate" and m.basis != "combined":
            problems.append(f"{w}: a new-supplier option must be combined (external lead plus internal exposure), never external search alone")
        if m.basis == "external_evidence" and REPLACE_RE.search(f"{m.option} {m.rationale}"):
            problems.append(f"{w}: replacement/purchasing language is not allowed on external search results alone")
    for i, a in enumerate(draft.recommended_actions):
        w = f"recommended_actions[{i}]"
        link(w, a.addresses)
        if not a.requires_human_approval:
            problems.append(f"{w}: requires_human_approval must be true")
        _check_basis(w, a.basis, f"{a.action} {a.rationale}", a.citations, a.internal_refs, by_id, ctx, problems, need_risk_kind=False, allow_lead=True)
        _lead_problems(w, f"{a.action} {a.rationale}", a.citations, a.policy_citations, by_id, reported, problems)
        _check_policy(w, f"{a.action} {a.rationale}", a.policy_citations, ctx, problems, a.internal_refs)
        problems += _direction_problems(w, f"{a.action} {a.rationale}", observed, only_contradictions=True)
        if a.basis == "external_evidence" and REPLACE_RE.search(f"{a.action} {a.rationale}"):
            problems.append(f"{w}: replacement/purchasing language is not allowed on external search results alone")

    # ── supplier candidates (real alternative-supplier leads; always unverified) ──
    if len(draft.supplier_candidates) > max_candidates:
        problems.append(f"supplier_candidates: at most {max_candidates} allowed, got {len(draft.supplier_candidates)}")
    for i, c in enumerate(draft.supplier_candidates):
        w = f"supplier_candidates[{i}]"
        e = by_id.get(c.identity_citation.evidence_id)
        if c.status != "unverified":
            problems.append(f"{w}: status must be 'unverified'")
        if not c.verification_needed.strip():
            problems.append(f"{w}: state what independent verification is needed")
        if BREACH_RE.search(f"{c.name} {c.rationale} {c.verification_needed}"):
            problems.append(f"{w}: must not be described as approved/qualified/certified/confirmed or with price/capacity claims")
        if e is None:
            problems.append(f"{w}: unknown evidence_id {c.identity_citation.evidence_id}")
            continue
        text = _norm(f"{e.title} {e.snippet}")
        quote = _norm(c.identity_citation.quote)
        if e.kind != "supplier_discovery" or e.relevance != "direct":
            problems.append(f"{w}: identity must come from a direct supplier-discovery source")
        if e.credibility == "social":
            problems.append(f"{w}: a social-media source cannot establish a supplier's identity")
        if len(quote) < MIN_QUOTE or quote not in text:
            problems.append(f"{w}: identity quote is missing or not found verbatim in the source")
        if _norm(c.name) not in text:
            problems.append(f"{w}: company name '{c.name}' does not appear in the cited source")
        if c.country == "unknown":
            if c.country_quote.strip():
                problems.append(f"{w}: country_quote must be empty when the country is unknown")
        else:
            cq = _norm(c.country_quote)
            if not cq or cq not in text:
                problems.append(f"{w}: country '{c.country}' needs a verbatim quote from the source (otherwise use 'unknown')")
            elif not country_supported(c.country, c.country_quote):
                problems.append(f"{w}: country '{c.country}' is not named in the quote (phone prefixes, domains and vague regions are not evidence)")
        pq = _norm(c.product_quote)
        if c.product_type == "unknown":
            if pq:
                problems.append(f"{w}: product_quote must be empty when the product type is unknown")
        else:
            if not pq or pq not in text:
                problems.append(f"{w}: product type '{c.product_type}' needs a verbatim product quote from the source (otherwise 'unknown')")
            else:
                has_refined, has_crude = "refin" in pq or "food-grade" in pq or "food grade" in pq, "crude" in pq
                ok = {"refined_food_grade": has_refined, "crude": has_crude, "both": has_refined and has_crude}[c.product_type]
                if not ok:
                    problems.append(f"{w}: the product quote does not support product type '{c.product_type}' (crude and refined must be distinguished)")

    # ── hypothetical shocks are sensitivity scenarios, never forecasts ──
    for where, text in _texts(draft):
        problems += [f"{where}: {p}" for p in cl.shock_problems(text)]

    # ── currency amounts ──
    allowed =set(money_allowed) if money_allowed is not None else _numbers(facts_text)
    for e in evidence:
        if e.relevance in ("direct", "contextual"):
            allowed |= _numbers(f"{e.title} {e.snippet}")
    for where, text in _texts(draft):
        for m in AMOUNT_RE.finditer(text):
            token = m.group(1) or m.group(2)
            try:
                value = Decimal(token.replace(",", "")).normalize()
            except InvalidOperation:
                continue
            if value not in allowed:
                msg = f"{where}: currency amount {token} is not in the supplied facts or relevant evidence"
                if not financial_available:
                    msg += " (financial exposure is unavailable; no financial claim may be made)"
                problems.append(msg)
    # percentages: the model may not introduce a figure (e.g. an invented "30% hedge ratio") that is not in the facts,
    # the usable evidence or a retrieved policy passage
    pct_allowed = _numbers(facts_text) | set(allowed)
    for text in ctx.policy.values():          # policy figures may justify a percentage, never a currency amount
        pct_allowed |= _numbers(text)
    for where, text in _texts(draft):
        for m in PCT_RE.finditer(text):
            try:
                value = Decimal(m.group(1).replace(",", "")).normalize()
            except InvalidOperation:
                continue
            if value not in pct_allowed:
                problems.append(f"{where}: percentage {m.group(1)}% is not in the supplied facts, evidence or cited policy (no invented requirements)")
    return problems


# ───────────── rule-based internal risks and removal of unsupported claims ─────────────
_INJECT = {"contract_coverage": ("R_COV", "Contract coverage gap", "INT:indicator:coverage"),
           "supplier_status": ("R_SUP", "Existing supplier approval status", "INT:indicator:supplier_status")}


def inject_required_risks(draft: DraftReport, ctx: ValidationContext) -> tuple[DraftReport, list[str]]:
    """If a deterministic indicator exists but the draft has no risk for that category, add a rule-based risk
    (clearly marked) so an internal exposure is never silently omitted."""
    added, notes = list(draft.risks), []
    for cat, (rid, title, ref) in _INJECT.items():
        ind = next((i for i in ctx.indicators if i.category == cat), None)
        if ind and not any(r.category == cat for r in draft.risks) and rid not in {r.risk_id for r in added}:
            added.append(Risk(risk_id=rid, title=f"{title} (rule-based)", description=f"Rule-based finding from internal data: {ind.value}.",
                              category=cat, basis="internal_model", severity=ind.severity,
                              severity_basis=f"Documented indicator: {ind.rule}", time_relevance="across the assessment horizon",
                              citations=[], internal_refs=[ref]))
            notes.append(f"RULE_BASED_RISK_ADDED: the drafted report omitted a {cat} risk; one was added from the deterministic indicator")
    return (draft.model_copy(update={"risks": added}) if notes else draft), notes


def prune_unsupported(draft: DraftReport, evidence: list[EvidenceItem], facts_text: str, financial_available: bool,
                      max_candidates: int, money_allowed: set[Decimal] | None, ctx: ValidationContext,
                      max_passes: int = 4) -> tuple[DraftReport, list[str], list[str], list[str]]:
    """Remove ONLY the items whose support cannot be verified. Returns (draft, removed descriptions, notes, remaining
    problems). Remaining problems are non-empty when a problem cannot be fixed by removal (e.g. the summary)."""
    removed: list[str] = []
    notes: list[str] = []
    problems: list[str] = []
    for _ in range(max_passes):
        draft, n = inject_required_risks(draft, ctx)
        notes += [x for x in n if x not in notes]
        problems = validate_draft(draft, evidence, facts_text, financial_available, max_candidates, money_allowed, ctx)
        # The LLM's executive_summary is discarded: _finalize replaces it with a summary assembled by code from the retained findings.
        # NO problem in that discarded text can be fixed by removing an item, and none may fail the run or block the removal of the items that CAN be
        # removed: wording problems (live demo-001) and a missing or too-short summary (live run of 13 Nov). Claims cannot reach the report through it.
        problems = [p for p in problems if not p.startswith("executive_summary")]
        if not problems:
            return draft, removed, notes, []
        drop: dict[str, set[int]] = {}
        for p in problems:
            m = ITEM_RE.match(p)
            if not m:
                return draft, removed, notes, problems
            drop.setdefault(m.group(1), set()).add(int(m.group(2)))
        label = {"risks": lambda x: x.title, "mitigation_options": lambda x: x.option, "supplier_candidates": lambda x: x.name,
                 "recommended_actions": lambda x: x.action}
        updates = {}
        for kind, idx in drop.items():
            items = getattr(draft, kind)
            for i in sorted(idx):
                reason = next(p for p in problems if p.startswith(f"{kind}[{i}]")).split(": ", 1)[1]
                removed.append(f"{kind} '{label[kind](items[i])}' removed: {reason}")
            updates[kind] = [x for i, x in enumerate(items) if i not in idx]
        gone = {getattr(draft.risks[i], "risk_id") for i in drop.get("risks", set())}
        if gone:          # removal propagation: mitigations/actions that addressed ONLY removed risks go too (visibly); others just lose the reference
            trimmed = 0
            for kind in ("mitigation_options", "recommended_actions"):
                kept = []
                for x in updates.get(kind, getattr(draft, kind)):
                    left = [a for a in x.addresses if a not in gone]
                    if x.addresses and not left:
                        removed.append(f"{kind} '{label[kind](x)}' removed: it addressed only the removed risk(s) {', '.join(x.addresses)}")
                    else:
                        trimmed += left != x.addresses
                        kept.append(x if left == x.addresses else x.model_copy(update={"addresses": left}))
                updates[kind] = kept
            if trimmed:
                notes.append(f"REMOVED_RISK_REFERENCES_TRIMMED: {trimmed} item(s) no longer reference a removed risk ({', '.join(sorted(gone))})")
        draft = draft.model_copy(update=updates)
    return draft, removed, notes, problems
