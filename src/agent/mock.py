"""Offline demo components: sanitised fixture search results, an in-memory policy retriever and a template 'LLM'.
Everything produced here is labelled MOCK and must never be presented as live market intelligence. No network access,
no paid calls.

Fixture hosts use the non-routable `<tier>.example.invalid` convention so source-credibility tiers can be exercised
without naming real publishers. Fixture companies are invented."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

from src.agent.claims import other_market_markers, price_direction
from src.agent.credibility import COUNTRY_ALIASES, STRONG_TIERS, country_supported
from src.agent.schemas import (Action, Citation, DraftReport, EvidenceJudgement, EvidenceJudgements, Mitigation, PolicyCitation,
                               RefinedQueries, Risk, SupplierCandidate)
from src.rag.chunker import load_documents
from src.rag.embed import HashingEmbedder
from src.rag.retrieve import InMemoryPolicyRetriever
from src.tools.search import RawResult, ToolResult

DOCS_DIR = Path(__file__).resolve().parents[2] / "data" / "synthetic" / "documents"


def build_mock_policy(directory: Path = DOCS_DIR, **kw) -> InMemoryPolicyRetriever:
    """Policy retrieval over the real fictional documents, using the deterministic TEST-ONLY hashing embedder
    (no database, no paid embedding call)."""
    chunks, errors = load_documents(directory, {"sunflower_oil", "wheat"})
    if errors:
        raise ValueError("; ".join(errors))
    return InMemoryPolicyRetriever(chunks, HashingEmbedder(), **kw)


def _raw(source, kind, query, tier, n, title, snippet, published, language=None):
    return RawResult(source, f"https://{tier}.example.invalid/mock/{source}/{n}", f"[MOCK FIXTURE] {title}", snippet, published,
                     language, query, kind)


class MockSearch:
    """Fixture search tool. `fail_with` simulates an unavailable source; `empty` simulates HTTP 200 with no results."""

    def __init__(self, name: str, today: date, fail_with: str | None = None, empty: bool = False):
        self.name, self._today, self._fail, self._empty = name, today, fail_with, empty
        self.calls = 0

    def search(self, query: str, *, kind: str, lookback_days: int, max_results: int) -> ToolResult:
        self.calls += 1
        if self._fail:
            return ToolResult(False, [], self._fail, 3, attempt_log=[{"attempt": i, "outcome": "error", "code": self._fail,
                                                                       "retried": i < 3, "http_status": None} for i in (1, 2, 3)])
        if self._empty:
            return ToolResult(True, [], None, 1, http_status=200, note="HTTP 200 with an empty 'articles' list",
                              attempt_log=[{"attempt": 1, "outcome": "ok", "code": None, "retried": False, "http_status": 200, "results": 0}])
        d = lambda days: self._today - timedelta(days=days)  # noqa: E731
        # NOTE: the fixture returns up to 9 items per call (above the live per-call cap of 6) so that it can show falling-price, other-market and supply-mechanism items together
        if self.name == "tavily" and kind == "supplier_discovery":
            items = [
                _raw("tavily", kind, query, "company", 3, "Example Aceites Iberia S.A. - refined sunflower oil",
                     "Example Aceites Iberia S.A. is a refining company based in Spain producing refined, food-grade sunflower oil for industrial bakeries.", None),
                _raw("tavily", kind, query, "company", 4, "Example Danubian Oils - crude and refined sunflower oil",
                     "Example Danubian Oils supplies crude and refined sunflower oil to commercial buyers across Europe.", None),
                _raw("tavily", kind, query, "company", 5, "Example Girasol Crudo S.L. - crude sunflower oil",
                     "Example Girasol Crudo S.L. trades crude sunflower oil in bulk.", None),
                _raw("tavily", kind, query, "commercial", 6, "Sunflower oil - 88 manufacturers, traders and suppliers",
                     "Fixture directory page listing many sunflower oil manufacturers and traders.", None),
                _raw("tavily", kind, query, "social", 7, "Any sunflower oil suppliers?",
                     "Natural Foods is a trusted supplier of sunflower oils. WhatsApp: +34 600 000 000 Email: sales@example.invalid", None),
            ]
        elif self.name == "tavily":
            items = [
                _raw("tavily", kind, query, "tradepress", 1, "Black Sea shipping delays tighten sunflower oil supply",
                     "Fixture text: shipping delays on Black Sea routes are tightening sunflower oil supplies to Europe, and freight and insurance costs remain elevated.", d(20)),
                _raw("tavily", kind, query, "tradepress", 2, "Sunflower oil export outlook for the coming season",
                     "Fixture text: analysts expect sunflower oil exports from the Black Sea region to remain volatile into winter, with European buyers facing higher replacement costs.", d(12)),
                _raw("tavily", kind, query, "tradepress", 3, "Palm oil stocks weigh on vegetable oil prices",
                     "Fixture text: record palm oil stocks are pressuring vegetable oil prices; sunflower oil is mentioned only as a minor comparison.", d(9)),
                _raw("tavily", kind, query, "commercial", 4, "Sunflower oil price report and forecast",
                     "Fixture text: a market-research vendor forecasts sunflower oil price movements for the coming quarter.", d(30)),
                _raw("tavily", kind, query, "social", 5, "Forum thread: sunflower oil shortages",
                     "Fixture forum post about sunflower oil shortages. Contact the poster: tel +34 611 000 000, buyer@example.invalid", d(10)),
                _raw("tavily", kind, query, "tradepress", 6, "Fuel markets update",
                     "Fixture text about crude oil and fuel prices, unrelated to edible oils.", d(5)),
                _raw("tavily", kind, query, "tradepress", 7, "Sunflower oil prices fall on seasonal supply increases in the EU market",
                     "Fixture text: sunflower oil prices in Europe fell during the week as seasonal supply increased, while Black Sea export restrictions keep supply volatile.", d(7)),
                _raw("tavily", kind, query, "tradepress", 8, "Black Sea sunflower oil offers lose ground as new-crop supply grows",
                     "Fixture text: sunflower oil prices for Black Sea origin fell by $20/t during the week as new-crop supply grew.", d(6)),
                _raw("tavily", kind, query, "tradepress", 9, "Sunflower oil demand prices in India stay firm on delivery delays",
                     "Fixture text: sunflower oil demand prices in India remain at $1,380/t CIF Mumbai due to delays in deliveries from the Black Sea region.", d(10)),
            ]
        else:
            items = [_raw("gdelt", kind, query, "tradepress", 11, "Sunflower oil prices firm in the EU market", "", d(8), "english"),
                     _raw("gdelt", kind, query, "commercial", 12, "Regional sunflower festival opens", "", d(3), "english"),
                     _raw("gdelt", kind, query, "tradepress", 13, "Aceite de girasol: cosecha y precios", "", d(15), "french")]
        return ToolResult(True, items[:max(max_results, 9)], None, 1, http_status=200,
                          attempt_log=[{"attempt": 1, "outcome": "ok", "code": None, "retried": False, "http_status": 200,
                                        "results": len(items[:max(max_results, 9)])}])


def _quote(snippet: str, title: str) -> str:
    text = snippet if len(snippet.strip()) >= 25 else title
    return text.strip()[:110]


def _policy_cite(passages: list[dict], *keywords: str) -> list[PolicyCitation]:
    """First retrieved sentence (>= 25 chars) mentioning a keyword, quoted verbatim from its passage."""
    for p in passages:
        for sent in re.split(r"(?<=[.])\s+", " ".join(p["text"].split())):
            if len(sent) >= 25 and any(k in sent.lower() for k in keywords):
                return [PolicyCitation(policy_ref=p["policy_ref"], quote=sent.strip())]
    return []


class DemoLLM:
    """Template-based stand-in for the LLM (offline demo only). Quotes numbers, evidence and policy text exactly as supplied."""

    def __init__(self):
        self._calls = 0

    def usage(self) -> dict[str, int]:
        return {"calls": self._calls, "input_tokens": 0, "output_tokens": 0}

    def judge_evidence(self, ingredient: str, items: list[dict]) -> EvidenceJudgements:
        self._calls += 1
        out = []
        for i in items:
            t = i["title"].lower()
            tier = i.get("source_tier", "unrated")
            if tier == "social" or "festival" in t:
                rel, why = "irrelevant", "social-media chatter or unrelated to procurement conditions"
            elif "palm" in t or tier == "commercial":
                rel, why = "contextual", "related market information or directory, not specific enough to support a claim"
            else:
                rel, why = "direct", "bears directly on sunflower-oil supply, price or a specific supplier"
            out.append(EvidenceJudgement(evidence_id=i["evidence_id"], relevance=rel, reason="mock judgement: " + why))
        return EvidenceJudgements(judgements=out)

    def refine_queries(self, ingredient: str, context: dict) -> RefinedQueries:
        self._calls += 1
        return RefinedQueries(tavily_queries=["sunflower oil harvest outlook"], gdelt_query='"sunflower oil" Ukraine')

    def draft_report(self, ingredient: str, facts_text: str, evidence: list[dict], problems: list[str],
                     max_candidates: int) -> DraftReport:
        self._calls += 1
        facts = json.loads(facts_text)
        fin, bench = facts["financial"], facts["benchmark"]
        passages = facts.get("policy_context", {}).get("passages", [])
        inds = {i["category"]: i for i in facts["severity_indicators"]}
        refs = {r["ref"] for r in facts["internal_refs"]}
        strong = [e for e in evidence if e["kind"] == "risk" and e["relevance"] == "direct" and e["source_tier"] in STRONG_TIERS
                  and e["published_date"]]
        risks: list[Risk] = []
        mitig: list[Mitigation] = []
        actions: list[Action] = []
        cov_id = sup_id = ext_id = comb_id = None

        if fin["status"] == "available":
            s10 = next(s for s in fin["scenarios"] if s["shock"] == "+10%")
            summary = (f"[MOCK DRAFT] Modelled baseline procurement cost over the horizon is EUR {fin['baseline_modelled_cost_eur']}; "
                       f"a hypothetical +10% benchmark shock scenario would add EUR {s10['incremental_exposure_eur']} ({s10['pct_of_baseline']} of baseline); "
                       f"this is a sensitivity of the model, not a price forecast. Uncovered demand rises over the horizon, which raises market exposure.")
        else:
            summary = ("[MOCK DRAFT] The financial exposure could not be calculated from the available data, so no financial "
                       "estimate is given; see the data gaps listed in this report.")
        if not passages:
            summary += " Internal policy guidance is unavailable for this run."

        if "contract_coverage" in inds:
            i = inds["contract_coverage"]
            cov_id = "R1"
            risks.append(Risk(risk_id=cov_id, title="Contract coverage gap", category="contract_coverage", basis="internal_model",
                              description=f"Committed contract volume does not cover forecast demand: {i['value']}.",
                              severity=i["severity"], severity_basis=f"Documented indicator ({i['rule']}): {i['value']}.",
                              time_relevance="across the assessment horizon", citations=[],
                              internal_refs=[r for r in ("INT:indicator:coverage", "INT:calc:baseline") if r in refs]))
        if "supplier_status" in inds:
            i = inds["supplier_status"]
            sup_id = "R2"
            risks.append(Risk(risk_id=sup_id, title="Existing supplier approval status", category="supplier_status",
                              basis="internal_model", description=f"Part of the committed volume sits with suppliers that are not fully approved: {i['value']}.",
                              severity=i["severity"], severity_basis=f"Documented indicator ({i['rule']}): {i['value']}.",
                              time_relevance="across the assessment horizon", citations=[],
                              internal_refs=["INT:indicator:supplier_status"]))
        if strong:
            e = strong[0]
            q = _quote(e["snippet"], e["title"])
            ext_id = "R3"
            risks.append(Risk(risk_id=ext_id, title="Supply-side disruption reported in trade press", category="supply_disruption",
                              basis="external_evidence", description=f"Trade press reports: {q}", severity="medium",
                              severity_basis="One direct trade-press source reports tightening supply; a single source does not justify high severity.",
                              time_relevance="recent reports", citations=[Citation(evidence_id=e["evidence_id"], quote=q)], internal_refs=[]))
            if "INT:indicator:coverage" in refs:
                comb_id = "R4"
                falling = [x for x in strong if price_direction(x["title"], x["snippet"]) == "down" and not other_market_markers(_quote(x["snippet"], x["title"]))]
                if falling:           # report the OBSERVED direction, quoting a source that actually says so (never an other-market price)
                    f0 = falling[0]
                    fq = f0["title"].replace("[MOCK FIXTURE] ", "")
                    risks.append(Risk(risk_id=comb_id, title="Observed sunflower-oil prices are falling; uncovered volume stays exposed", category="market_price",
                                      basis="combined",
                                      description=f"Trade press reports falling sunflower-oil prices ({fq}); a lower price level does not remove the benchmark exposure of the uncovered volume.",
                                      severity="medium", severity_basis="Direct trade-press reports show falling prices while the internal coverage indicator shows uncovered volume; the evidence base is small.",
                                      time_relevance="coming months", citations=[Citation(evidence_id=f0["evidence_id"], quote=fq)],
                                      internal_refs=["INT:indicator:coverage"]))
                else:
                    risks.append(Risk(risk_id=comb_id, title="Market price risk on uncovered volume", category="market_price", basis="combined",
                                      description=f"Reported conditions ({q}) matter because part of the demand is uncovered by contracts.",
                                      severity="medium", severity_basis="External reports point to tight supply and the internal coverage indicator shows uncovered volume; evidence is limited to one source.",
                                      time_relevance="coming months", citations=[Citation(evidence_id=e["evidence_id"], quote=q)],
                                      internal_refs=["INT:indicator:coverage"]))
        if "price_exposure" in inds and fin["status"] == "available":
            i = inds["price_exposure"]
            risks.append(Risk(risk_id="R5", title="Modelled sensitivity to the benchmark price", category="market_price", basis="internal_model",
                              description=(f"In a hypothetical +10% benchmark shock scenario the modelled cost rises by {s10['pct_of_baseline']} of baseline "
                                           f"(EUR {s10['incremental_exposure_eur']}); this is a sensitivity of the model, not a price forecast and not an observation of the market."),
                              severity=i["severity"],
                              severity_basis=f"Documented indicator for the hypothetical benchmark shock scenario ({i['rule']}): {i['value']}.",
                              time_relevance="across the assessment horizon", citations=[], internal_refs=["INT:indicator:price_exposure"]))

        cov_pol = _policy_cite(passages, "uncovered volume increases", "coverage review")
        sup_pol = _policy_cite(passages, "requires sign-off by the procurement director", "sign-off by the procurement director")
        alt_pol = _policy_cite(passages, "must not receive orders", "sourcing leads")

        def with_policy(text: str, cites: list[PolicyCitation]) -> str:
            return text            # the policy citation itself carries the passage; it is not pasted into the prose again

        if cov_id:
            mitig.append(Mitigation(option="Review contract coverage for the months with the largest uncovered volume",
                                    rationale=with_policy("Uncovered volume carries full benchmark exposure under the model.", cov_pol),
                                    applies_to="contract_strategy", basis="internal_model", addresses=[cov_id], citations=[],
                                    internal_refs=["INT:indicator:coverage"], policy_citations=cov_pol))
            actions.append(Action(action="Decide whether to extend or add contract coverage",
                                  rationale=with_policy("The coverage table shows uncovered tonnes by month.", cov_pol),
                                  next_step="Procurement manager reviews renewal and coverage options against risk appetite.",
                                  requires_human_approval=True, basis="internal_model", addresses=[cov_id], citations=[],
                                  internal_refs=["INT:indicator:coverage"], policy_citations=cov_pol))
        if sup_id:
            mitig.append(Mitigation(option="Confirm the outcome of the pending audit for the conditionally approved supplier before relying on its contract",
                                    rationale=with_policy("Its approval status is conditional.", sup_pol), applies_to="existing_supplier",
                                    basis="internal_model", addresses=[sup_id], citations=[],
                                    internal_refs=["INT:indicator:supplier_status"], policy_citations=sup_pol))
        cond = next((s for s in facts.get("supplier_exposure", []) if s.get("approval_status") == "conditional"), None)
        if sup_id and cond and sup_pol:
            asm_pol = _policy_cite(passages, "until both conditions are closed")
            sid = cond["supplier_id"]
            actions.append(Action(action=f"Ask {sid} to complete and verify its open conditions, and escalate any renewal or volume increase to the Procurement Director",
                                  rationale=with_policy(f"{sid} is conditionally approved, not blocked; it may keep delivering under its existing contract while the outstanding conditions are completed and verified.", sup_pol),
                                  next_step="Raw Materials Procurement Manager initiates a review, coordinates the corrective actions and prepares an escalation note; no renewal or volume increase is committed before the conditions are closed and the Procurement Director has approved in writing.",
                                  requires_human_approval=True, basis="internal_model", addresses=[sup_id], citations=[],
                                  internal_refs=["INT:indicator:supplier_status", f"INT:supplier:{sid}"], policy_citations=sup_pol + asm_pol))
        if ext_id:
            e = strong[0]
            q = _quote(e["snippet"], e["title"])
            actions.append(Action(action="Monitor supply-route and price developments weekly", rationale=f"Trade press reports: {q}",
                                  next_step="Track the cited trade sources and escalate if disruption intensifies.", requires_human_approval=True,
                                  basis="external_evidence", addresses=[ext_id], citations=[Citation(evidence_id=e["evidence_id"], quote=q)],
                                  internal_refs=[], policy_citations=[]))
        prem_pol = _policy_cite(passages, "market indicators")
        if "price_exposure" in inds and fin["status"] == "available":
            actions.append(Action(action="Review whether the 150 EUR/tonne spot premium over the crude-oil benchmark remains appropriate for refined, food-grade sunflower oil",
                                  rationale=with_policy("The benchmark is a crude-oil price; the premium over it is a modelled assumption, not a supplier quote.", prem_pol),
                                  next_step="Raw Materials Procurement Manager confirms or replaces the premium assumption before the figures are used in a price decision.",
                                  requires_human_approval=True, basis="internal_model", addresses=["R5"], citations=[],
                                  internal_refs=["INT:indicator:price_exposure", "INT:benchmark"], policy_citations=prem_pol))

        cands: list[SupplierCandidate] = []
        for e in evidence:
            if len(cands) >= max_candidates:
                break
            if e["kind"] != "supplier_discovery" or e["relevance"] != "direct" or e["source_tier"] == "social":
                continue
            name = e["title"].replace("[MOCK FIXTURE] ", "").split(" - ")[0].strip()
            snippet = e["snippet"]
            country = next((c for c in COUNTRY_ALIASES if country_supported(c, snippet)), "unknown")
            refined, crude = "refined" in snippet.lower(), "crude" in snippet.lower()
            ptype = "both" if refined and crude else "refined_food_grade" if refined else "crude" if crude else "unknown"
            cands.append(SupplierCandidate(
                name=name, country=country, country_quote=snippet if country != "unknown" else "", product_type=ptype,
                product_quote=snippet if ptype != "unknown" else "", identity_citation=Citation(evidence_id=e["evidence_id"], quote=snippet),
                rationale="Appears in fixture search results; self-reported information only.",
                verification_needed="Independently verify identity, product specification (crude vs refined food-grade), capacity, certifications and pricing; qualify before any commercial action.",
                status="unverified"))
        if cands and alt_pol:          # verification of a REPORTED lead rests on the lead itself plus the qualification policy, not on trade-press evidence
            lead = cands[0]
            addr = [cov_id or comb_id or sup_id or ext_id or risks[0].risk_id]
            lead_ref = "INT:indicator:coverage" if "INT:indicator:coverage" in refs else "INT:indicator:supplier_status"
            lead_cite = [Citation(evidence_id=lead.identity_citation.evidence_id, quote=lead.identity_citation.quote)]
            mitig.append(Mitigation(option=f"Have procurement independently verify the reported sourcing lead {lead.name} under the qualification policy before any sourcing change",
                                    rationale=with_policy("The lead is an unverified search result; the uncovered volume justifies identifying options, not choosing one.", alt_pol),
                                    applies_to="new_supplier_candidate", basis="combined", addresses=addr, citations=lead_cite,
                                    internal_refs=[lead_ref], policy_citations=alt_pol))
            actions.append(Action(action=f"Verify the reported lead {lead.name} independently (identity, specification, certification, capacity, price) before any commercial contact",
                                  rationale=with_policy("It is an UNVERIFIED lead; no order or commitment is made before the qualification steps are completed and the required approval is given.", alt_pol),
                                  next_step="Raw Materials Procurement Manager starts the qualification steps described in the policy; no sourcing change is decided by this report.",
                                  requires_human_approval=True, basis="combined", addresses=addr, citations=lead_cite,
                                  internal_refs=[lead_ref], policy_citations=alt_pol))
        return DraftReport(executive_summary=summary, risks=risks, mitigation_options=mitig, supplier_candidates=cands,
                           recommended_actions=actions,
                           uncertainties=[f"The benchmark is a crude-oil price, not the refined-oil contract price. {bench['caveat']}",
                                          "Mock evidence is not live market intelligence."])
