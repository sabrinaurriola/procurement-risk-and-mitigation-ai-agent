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

from src.agent.credibility import STRONG_TIERS, country_supported
from src.agent.schemas import Citation, DraftReport, EvidenceItem, Indicator, Risk

NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
AMOUNT_RE = re.compile(r"(?:€|EUR|USD|\$)\s?(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s?(?:€|EUR|USD|euros?|dollars?)", re.I)
BREACH_RE = re.compile(r"\b(approved|pre-?qualified|qualified|capacity[- ]confirmed|certified|verified supplier|preferred supplier|"
                       r"guarantee[sd]?|in stock|confirmed (?:capacity|price|availability)|competitive price)\b", re.I)
REPLACE_RE = re.compile(r"\b(replace|replacing|switch|switching|terminate|cancel|place (?:an? )?orders?|purchase|buy)\b", re.I)
ITEM_RE = re.compile(r"^(risks|mitigation_options|supplier_candidates|recommended_actions)\[(\d+)\]")
RISK_ID_RE = re.compile(r"^R\w{1,9}$")
STOP = set("""about above after again against could their there these those which while would should where being between
because before during other since through under until within without across around among whether market markets prices price
supply sunflower oils edible vegetable global world trade demand report reports according current future""".split())
MIN_QUOTE = 25
INTERNAL_CATEGORY_INDICATOR = {"contract_coverage": "contract_coverage", "supplier_status": "supplier_status"}


@dataclass
class ValidationContext:
    internal_refs: set[str] = field(default_factory=set)
    indicators: list[Indicator] = field(default_factory=list)

    def severity_for(self, category: str) -> str | None:
        ind = next((i for i in self.indicators if i.category == INTERNAL_CATEGORY_INDICATOR.get(category)), None)
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
                     problems: list[str]) -> int:
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
        if e.relevance == "direct" and e.credibility in STRONG_TIERS:
            strong.add(c.evidence_id)
    return len(strong)


def _check_basis(where: str, basis: str, claim: str, cites: list[Citation], refs: list[str], by_id, ctx: ValidationContext,
                 problems: list[str], *, need_risk_kind: bool) -> int:
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
        if refs:
            problems.append(f"{where}: basis is external_evidence but internal references were given (use basis 'combined')")
        if not cites:
            problems.append(f"{where}: an external_evidence finding must cite evidence with verbatim quotes")
    else:  # combined
        if not cites or not refs:
            problems.append(f"{where}: a combined finding must cite BOTH external evidence and internal references")
    if basis != "internal_model":
        strong = _check_citations(where, claim, cites, by_id, need_risk_kind=need_risk_kind, problems=problems)
        if cites and strong < 1:
            problems.append(f"{where}: no direct citation from an official or trade-press source supports this finding")
    return strong


def validate_draft(draft: DraftReport, evidence: list[EvidenceItem], facts_text: str, financial_available: bool,
                   max_candidates: int, money_allowed: set[Decimal] | None = None,
                   ctx: ValidationContext | None = None) -> list[str]:
    ctx = ctx or ValidationContext()
    problems: list[str] = []
    by_id = {e.evidence_id: e for e in evidence}

    if len(draft.executive_summary.strip()) < 40:
        problems.append("executive_summary is missing or too short")

    # ── risks ──
    ids = [r.risk_id for r in draft.risks]
    for dup in {i for i in ids if ids.count(i) > 1}:
        problems.append(f"risk_id {dup} is used more than once")
    for i, r in enumerate(draft.risks):
        w = f"risks[{i}]"
        if not RISK_ID_RE.match(r.risk_id):
            problems.append(f"{w}: risk_id must look like R1, R2, ...")
        internal_cat = r.category in INTERNAL_CATEGORY_INDICATOR
        if internal_cat and r.basis != "internal_model":
            problems.append(f"{w}: a {r.category} risk is derived from internal data and must have basis internal_model")
        if not internal_cat and r.basis == "internal_model":
            problems.append(f"{w}: a {r.category} risk needs external evidence (basis external_evidence or combined)")
        strong = _check_basis(w, r.basis, f"{r.title} {r.description}", r.citations, r.internal_refs, by_id, ctx, problems,
                              need_risk_kind=True)
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
        _check_basis(w, m.basis, f"{m.option} {m.rationale}", m.citations, m.internal_refs, by_id, ctx, problems, need_risk_kind=False)
        if m.applies_to == "new_supplier_candidate" and m.basis != "combined":
            problems.append(f"{w}: a new-supplier option must be combined (external lead plus internal exposure), never external search alone")
        if m.basis == "external_evidence" and REPLACE_RE.search(f"{m.option} {m.rationale}"):
            problems.append(f"{w}: replacement/purchasing language is not allowed on external search results alone")
    for i, a in enumerate(draft.recommended_actions):
        w = f"recommended_actions[{i}]"
        link(w, a.addresses)
        if not a.requires_human_approval:
            problems.append(f"{w}: requires_human_approval must be true")
        _check_basis(w, a.basis, f"{a.action} {a.rationale}", a.citations, a.internal_refs, by_id, ctx, problems, need_risk_kind=False)
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

    # ── currency amounts ──
    allowed = set(money_allowed) if money_allowed is not None else _numbers(facts_text)
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
                removed.append(f"{kind} '{label[kind](items[i])[:80]}' removed: {reason}")
            updates[kind] = [x for i, x in enumerate(items) if i not in idx]
        draft = draft.model_copy(update=updates)
    return draft, removed, notes, problems
