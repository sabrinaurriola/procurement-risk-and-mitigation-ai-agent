"""Two complementary sunflower-oil demonstration cases (mock research + template drafting, REAL read-only database data).

  A  Normal assessment            fresh benchmark, financial exposure, policy context, human-reviewed recommendations
  B  Controlled limitation        STALE benchmark (assessed after the data has gone out of date): the agent must refuse to
                                  state euro figures, say why, and still report what it can support (supplier status,
                                  external evidence, policy context)
  --inject-unsupported-claim      adds a decorative, unsupported finding to the model's drafts to show that it is REMOVED
                                  visibly (works with A or B)

Each scenario is checked against explicit expectations and the independent audit (src.agent.quality). Mock output is
labelled MOCK everywhere; live versions (real evidence, loaded policy) need the approvals listed in docs/demo-scenarios.md.

Usage: python -m src.agent.demo --mock [A|B|all] [--inject-unsupported-claim]
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

from src.agent.config import PROFILES
from src.agent.data import PostgresData
from src.agent.facts import financial_section, horizon_months
from src.agent.graph import Deps, run_assessment
from src.agent.mock import DemoLLM, MockSearch, build_mock_policy
from src.agent.quality import audit_report, render_audit
from src.calculations.exposure import calculate_exposure

RUNS = Path(__file__).resolve().parents[2] / "runs"
SCENARIOS = {
    "A": {"title": "Normal assessment", "request": {"request_id": "demo-A", "ingredient": "sunflower_oil", "horizon_months": 3}},
    "B": {"title": "Controlled limitation: stale benchmark",
          "request": {"request_id": "demo-B", "ingredient": "sunflower_oil", "horizon_months": 3, "assessment_date": "2026-11-20"}},
}


class UnsupportedClaimLLM(DemoLLM):
    """Demo-only fault injection: appends a finding whose citation does not support it, on EVERY draft (so the repair fails too)."""

    def draft_report(self, *a, **kw):
        d = super().draft_report(*a, **kw)
        ext = next((r for r in d.risks if r.basis == "external_evidence"), None)
        if ext is None:
            return d
        fake = ext.model_copy(update={"risk_id": "R9", "title": "Fertiliser rail closures raise input costs",
                                      "description": "Closures of inland rail terminals raise fertiliser costs for growers."})
        return d.model_copy(update={"risks": d.risks + [fake]})


def verify_policy_citations_in_db(report) -> tuple[bool, str]:
    """Every policy citation must resolve to a stored chunk, the passage shown in the report must equal the stored text, and the
    quotation must appear verbatim in that stored text (read-only)."""
    from src.db.preflight import connect
    cited = [(c.policy_ref, c.quote) for x in list(report.draft.mitigation_options) + list(report.draft.recommended_actions) for c in x.policy_citations]
    shown = {p.policy_ref: p.content for p in report.policy.passages}
    norm = lambda t: " ".join(t.split()).casefold()  # noqa: E731
    with connect() as conn:
        stored = {f"{d}#{i}": c for d, i, c in conn.execute("SELECT doc_id, chunk_index, content FROM procurement.policy_chunks").fetchall()}
    problems = [f"{ref}: not a stored chunk" for ref, _ in cited if ref not in stored]
    problems += [f"{ref}: quote not in stored text" for ref, q in cited if ref in stored and norm(q) not in norm(stored[ref])]
    problems += [f"{ref}: shown passage differs from the stored text" for ref in shown if ref in stored and shown[ref] != stored[ref]]
    problems += [f"{ref}: shown passage is not stored" for ref in shown if ref not in stored]
    return (not problems, f"{len(cited)} citations / {len(shown)} passages checked" if not problems else "; ".join(problems[:4]))


def expectations(key: str, resp, inject: bool, real_policy: bool = False) -> list[tuple[str, bool]]:
    r = resp.report
    ok = r is not None
    md = resp.report_markdown or ""
    ext_strong = ok and any(x.basis != "internal_model" and x.citations for x in r.draft.risks)
    base = [("a report was produced", ok)]
    if not ok:
        return base
    amounts_in_prose = bool(re.search(r"(?:€|EUR)\s?\d", " ".join([r.draft.executive_summary] + [x.description for x in r.draft.risks])))
    if key == "A":
        base += [("status COMPLETED", resp.status == "completed" or inject),
                 ("benchmark fresh (within the 21-day limit) and financial exposure available", r.benchmark.status == "available" and r.financial.status == "available"),
                 ("baseline modelled cost EUR 1,961,496.55", r.financial.baseline_modelled_cost_eur == "1,961,496.55"),
                 ("policy context retrieved and cited", r.policy.status == "ok" and any(m.policy_citations for m in r.draft.mitigation_options)),
                 ("external risk backed by a quoted trade-press source", ext_strong),
                 ("alternative suppliers present and all UNVERIFIED", bool(r.draft.supplier_candidates) and all(c.status == "unverified" for c in r.draft.supplier_candidates)),
                 ("SUP-003 shown as CONDITIONAL with its own risk", "CONDITIONAL" in md and any(x.category == "supplier_status" for x in r.draft.risks)),
                 ("every action requires human approval", all(a.requires_human_approval for a in r.draft.recommended_actions))]
    else:
        base += [("status PARTIAL (not completed, not failed)", resp.status == "partial"),
                 ("benchmark reported STALE with its age, no price substituted", r.benchmark.status == "stale" and r.benchmark.price_eur_per_tonne is None and (r.benchmark.age_days or 0) > 21),
                 ("financial exposure UNAVAILABLE: no euro scenario table", r.financial.status == "unavailable" and not r.financial.scenarios and "Incremental price-shock exposure" not in md),
                 ("no euro amount appears in the AI-written prose", not amounts_in_prose),
                 ("limitation is visible in warnings and missing data", any("STALE_BENCHMARK" in w for w in resp.warnings) and bool(resp.missing_data)),
                 ("assessment-date override is disclosed", any(w.startswith("ASSESSMENT_DATE_OVERRIDE") for w in resp.warnings)),
                 ("SUP-003 conditional status still reported without the financial model", any(x.category == "supplier_status" for x in r.draft.risks)),
                 ("policy context and external evidence still reported", r.policy.status == "ok" and ext_strong)]
    if real_policy:
        ok_db, detail = verify_policy_citations_in_db(r)
        prose = " ".join([r.draft.executive_summary] + [m.option + " " + m.rationale for m in r.draft.mitigation_options]
                         + [a.action + " " + a.rationale for a in r.draft.recommended_actions])
        refs = {c.policy_ref for x in list(r.draft.mitigation_options) + list(r.draft.recommended_actions) for c in x.policy_citations}
        base += [("policy retrieved by pgvector from the populated database table", r.policy.status == "ok" and r.policy.method == "pgvector"),
                 (f"every policy citation resolves to a stored chunk with a verbatim quote ({detail})", ok_db),
                 ("recommendations cite relevant policy: coverage review (POL-002) and, if reported, conditional-supplier sign-off (POL-001/ASM-003)",
                  (key == "B" or any(x.startswith("POL-002") for x in refs)) and any(x.startswith(("POL-001", "ASM-003")) for x in refs)),
                 ("no regulatory-compliance claim and no agent approval/qualification claim in the AI prose",
                  not re.search(r"regulat|legal compliance|complies with|compliant with", prose, re.I) and not re.search(r"\b(agent|system) (approves|has approved|qualifies)\b", prose, re.I)),
                 ("human control: every action requires approval and the policy says who approves",
                  all(a.requires_human_approval for a in r.draft.recommended_actions) and any("Procurement Director" in p.content for p in r.policy.passages))]
    if inject:
        base += [("injected unsupported claim was REMOVED and listed", any("Fertiliser" in c for c in r.removed_claims) and "R9" not in [x.risk_id for x in r.draft.risks]),
                 ("removal downgraded the status to PARTIAL", resp.status == "partial")]
    return base


def run_scenario(key: str, inject: bool = False, out: Path = RUNS, data=None, policy=None, tag: str = "") -> dict:
    spec = SCENARIOS[key]
    req = dict(spec["request"])
    if inject:
        req["request_id"] += "-fault"
    req["request_id"] += tag
    as_of = date.fromisoformat(req["assessment_date"]) if req.get("assessment_date") else date.today()
    data = data or PostgresData()
    deps = Deps(data, UnsupportedClaimLLM() if inject else DemoLLM(), MockSearch("tavily", as_of), MockSearch("gdelt", as_of),
                policy=policy or build_mock_policy(), mock=True)
    resp, trace = run_assessment(req, deps)
    exp = expectations(key, resp, inject, real_policy=policy is not None)
    checks = []
    if resp.report:
        inp = data.load("sunflower_oil")
        calc = calculate_exposure("sunflower_oil", horizon_months(as_of, 3), inp.contracts, inp.forecasts, inp.observations,
                                  inp.benchmark_key or "", as_of, PROFILES["sunflower_oil"].assumptions)
        checks = audit_report(resp.report, expected_financial=financial_section(calc))
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{req['request_id']}.md").write_text(resp.report_markdown, encoding="utf-8")
        (out / f"{req['request_id']}-audit.md").write_text(render_audit(checks), encoding="utf-8")
    return {"key": key, "title": spec["title"] + (" + injected unsupported claim" if inject else ""), "request_id": req["request_id"],
            "response": resp, "trace": trace, "expectations": exp, "audit": checks}


def render_summary(results: list[dict]) -> str:
    L = ["# Demonstration scenarios — mock research and drafting on real read-only database data", ""]
    for r in results:
        resp = r["response"]
        rep = resp.report
        L += [f"## Scenario {r['key']}: {r['title']}", f"- request `{r['request_id']}` · assessment date **{resp.assessment_date}** · status **{resp.status.upper()}** · warnings {len(resp.warnings)}"]
        if rep:
            L += [f"- benchmark **{rep.benchmark.status}**" + (f" ({rep.benchmark.price_eur_per_tonne} EUR/t, {rep.benchmark.age_days} days old)" if rep.benchmark.price_eur_per_tonne else f" ({rep.benchmark.age_days} days old, limit {rep.benchmark.max_age_days})")
                  + f" · financial **{rep.financial.status}**" + (f" (baseline EUR {rep.financial.baseline_modelled_cost_eur})" if rep.financial.status == 'available' else "")
                  + f" · policy **{rep.policy.status}** ({len(rep.policy.passages)} passages)",
                  f"- risks: {[(x.risk_id, x.category, x.basis, x.severity) for x in rep.draft.risks]}",
                  f"- leads: {[(c.name, c.country, c.product_type) for c in rep.draft.supplier_candidates]}",
                  f"- removed claims: {len(rep.removed_claims)}", "- key warnings:"]
            L += [f"  - {w}" for w in resp.warnings[:8]]
        L += ["", "| Expectation | Result |", "|---|---|"] + [f"| {n} | {'PASS' if ok else '**FAIL**'} |" for n, ok in r["expectations"]]
        if r["audit"]:
            L.append(f"\nIndependent audit: **{sum(c.passed for c in r['audit'])}/{len(r['audit'])}** checks passed.")
        L.append("")
    L.append("_Mock evidence and template drafting are not live market findings; the audit checks structure and provenance, not the business quality of real LLM text._")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", required=True, help="no external calls; real read-only database data")
    ap.add_argument("which", nargs="?", default="all", choices=["A", "B", "all"])
    ap.add_argument("--inject-unsupported-claim", action="store_true")
    ap.add_argument("--real-policy", action="store_true", help="retrieve policy from the populated pgvector table (read-only; cached query embeddings, no paid call)")
    args = ap.parse_args(argv)
    keys = ["A", "B"] if args.which == "all" else [args.which]
    policy, tag = None, ""
    if args.real_policy:
        from src.rag.embed import CachedEmbedder
        from src.rag.retrieve import PostgresPolicyRetriever
        policy, tag = PostgresPolicyRetriever(CachedEmbedder(None)), "-rag"          # cache-only: a missing query falls back to full-text, never to a paid call
    results = [run_scenario(k, args.inject_unsupported_claim, policy=policy, tag=tag) for k in keys]
    text = render_summary(results)
    suffix = ("-fault" if args.inject_unsupported_claim else "") + ("-rag" if args.real_policy else "")
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"demo-scenarios-summary{suffix}.md").write_text(text, encoding="utf-8")
    print(text)
    failed = [n for r in results for n, ok in r["expectations"] if not ok] + [c.id for r in results for c in r["audit"] if not c.passed]
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
