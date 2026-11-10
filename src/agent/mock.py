"""Offline demo components: sanitised fixture search results and a template 'LLM'. Everything produced here is labelled
MOCK and must never be presented as live market intelligence. No network access, no paid calls.

Fixture hosts use the non-routable `<tier>.example.invalid` convention so source-credibility tiers can be exercised
without naming real publishers. Fixture companies are invented."""
from __future__ import annotations

import json
from datetime import date, timedelta

from src.agent.credibility import COUNTRY_ALIASES, STRONG_TIERS, country_supported
from src.agent.schemas import (Action, Citation, DraftReport, EvidenceJudgement, EvidenceJudgements, Mitigation, RefinedQueries,
                               Risk, SupplierCandidate)
from src.tools.search import RawResult, ToolResult


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
            ]
        else:
            items = [_raw("gdelt", kind, query, "tradepress", 11, "Sunflower oil prices firm in the EU market", "", d(8), "english"),
                     _raw("gdelt", kind, query, "commercial", 12, "Regional sunflower festival opens", "", d(3), "english"),
                     _raw("gdelt", kind, query, "tradepress", 13, "Aceite de girasol: cosecha y precios", "", d(15), "french")]
        return ToolResult(True, items[:max_results], None, 1, http_status=200,
                          attempt_log=[{"attempt": 1, "outcome": "ok", "code": None, "retried": False, "http_status": 200,
                                        "results": len(items[:max_results])}])


def _quote(snippet: str, title: str) -> str:
    text = snippet if len(snippet.strip()) >= 25 else title
    return text.strip()[:110]


class DemoLLM:
    """Template-based stand-in for the LLM (offline demo only). Quotes numbers and evidence exactly as supplied."""

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
                       f"a +10% benchmark shock adds EUR {s10['incremental_exposure_eur']} ({s10['pct_of_baseline']} of baseline). "
                       f"Uncovered demand rises over the horizon, which raises market exposure.")
        else:
            summary = ("[MOCK DRAFT] The financial exposure could not be calculated from the available data, so no financial "
                       "estimate is given; see the data gaps listed in this report.")

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
                risks.append(Risk(risk_id=comb_id, title="Market price risk on uncovered volume", category="market_price", basis="combined",
                                  description=f"Reported conditions ({q}) matter because part of the demand is uncovered by contracts.",
                                  severity="medium", severity_basis="External reports point to tight supply and the internal coverage indicator shows uncovered volume; evidence is limited to one source.",
                                  time_relevance="coming months", citations=[Citation(evidence_id=e["evidence_id"], quote=q)],
                                  internal_refs=["INT:indicator:coverage"]))

        if cov_id:
            mitig.append(Mitigation(option="Review contract coverage for the months with the largest uncovered volume",
                                    rationale="Uncovered volume carries full benchmark exposure under the model.", applies_to="contract_strategy",
                                    basis="internal_model", addresses=[cov_id], citations=[], internal_refs=["INT:indicator:coverage"]))
            actions.append(Action(action="Decide whether to extend or add contract coverage", rationale="The coverage table shows uncovered tonnes by month.",
                                  next_step="Procurement manager reviews renewal and coverage options against risk appetite.",
                                  requires_human_approval=True, basis="internal_model", addresses=[cov_id], citations=[],
                                  internal_refs=["INT:indicator:coverage"]))
        if sup_id:
            mitig.append(Mitigation(option="Confirm the outcome of the pending audit for the conditionally approved supplier before relying on its contract",
                                    rationale="Its approval status is conditional.", applies_to="existing_supplier", basis="internal_model",
                                    addresses=[sup_id], citations=[], internal_refs=["INT:indicator:supplier_status"]))
        if ext_id:
            e = strong[0]
            q = _quote(e["snippet"], e["title"])
            actions.append(Action(action="Monitor supply-route and price developments weekly", rationale=f"Trade press reports: {q}",
                                  next_step="Track the cited trade sources and escalate if disruption intensifies.", requires_human_approval=True,
                                  basis="external_evidence", addresses=[ext_id], citations=[Citation(evidence_id=e["evidence_id"], quote=q)],
                                  internal_refs=[]))
        if comb_id:
            e = strong[0]
            q = _quote(e["snippet"], e["title"])
            mitig.append(Mitigation(option="Have procurement independently verify and qualify alternative-supplier leads before any sourcing change",
                                    rationale=f"Trade press reports: {q}. Together with the uncovered volume this justifies identifying options, not choosing one.",
                                    applies_to="new_supplier_candidate", basis="combined", addresses=[comb_id],
                                    citations=[Citation(evidence_id=e["evidence_id"], quote=q)], internal_refs=["INT:indicator:coverage"]))

        cands: list[SupplierCandidate] = []
        for e in evidence:
            if len(cands) >= max_candidates:
                break
            if e["kind"] != "supplier_discovery" or e["relevance"] != "direct" or e["source_tier"] == "social":
                continue
            text = f"{e['title']} {e['snippet']}"
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
        return DraftReport(executive_summary=summary, risks=risks, mitigation_options=mitig, supplier_candidates=cands,
                           recommended_actions=actions,
                           uncertainties=[f"The benchmark is a crude-oil price, not the refined-oil contract price. {bench['caveat'][:90]}",
                                          "Mock evidence is not live market intelligence."])
