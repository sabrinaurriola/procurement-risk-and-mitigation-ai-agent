"""Deterministic Markdown rendering of a validated Report (the LLM never formats numbers)."""
from __future__ import annotations

from src.agent.schemas import Report

TIER_LABEL = {"official": "official", "trade_press": "trade press", "commercial": "commercial/market research",
              "social": "SOCIAL MEDIA (low confidence)", "unrated": "unrated (self-published or unknown)"}


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def render_markdown(r: Report, status: str) -> str:
    L: list[str] = [f"# {r.title}", ""]
    L.append(f"**Request:** {r.request_id} · **Assessment date:** {r.assessment_date} · **Status:** {status.upper()}")
    if any(e.is_mock for e in r.evidence) or any(w.startswith("MOCK") for w in r.warnings):
        L += ["", "> **MOCK RUN — fixture data, not live market intelligence.**"]
    if r.warnings:
        L += ["", "## Warnings"] + [f"- {w}" for w in r.warnings]
    if r.missing_data:
        L += ["", "## Missing data"] + [f"- {w}" for w in r.missing_data]
    L += ["", "## Executive summary", r.draft.executive_summary, "", "_The summary is assembled by code from the validated findings and the "
          "deterministic figures. Interpretation text in the findings below is AI-generated and must be reviewed._"]
    if r.removed_claims:
        L += ["", "## Unverified concerns - drafted claims removed because their support could not be verified",
              "_These are not findings. A person may still want to investigate them._"] + [f"- {x}" for x in r.removed_claims]

    L += ["", "## How to read the findings",
          "- **Basis `internal_model`:** derived from the fictional internal contracts, forecasts, supplier records or the calculation outputs.",
          "- **Basis `external_evidence`:** supported by retrieved external sources (each citation carries a verbatim quote).",
          "- **Basis `combined`:** links external evidence to the internal exposure. Market, contract-coverage and supplier-status risks are kept separate."]

    b = r.benchmark
    L += ["", "## Market benchmark (external data)"]
    if b.status == "available":
        L.append(f"- **{b.price_eur_per_tonne} EUR/tonne**, observation period **{b.period_start} to {b.period_end}** "
                 f"({b.age_days} days old; limit {b.max_age_days} days from period end)")
    else:
        L.append(f"- **Benchmark {b.status.upper()}:** " + "; ".join(b.reasons))
    L += [f"- Series: `{b.series_key}`", f"- {b.caveat}"]
    if b.excluded_observations:
        L.append(f"- Observations excluded from calculations (kept in the database): {b.excluded_observations}")

    f = r.financial
    L += ["", "## Financial exposure (modelled; not supplier quotes)"]
    if f.status == "unavailable":
        L.append("**UNAVAILABLE — no financial estimate is given.** " + "; ".join(f.unavailable_reasons))
    else:
        L += [f"**Baseline modelled procurement cost ({r.horizon_months} months): EUR {f.baseline_modelled_cost_eur}** "
              f"(forecast {f.total_forecast_t} t; contract commitments {f.total_contract_commitments_t} t; "
              f"uncovered {f.total_uncovered_t} t)", "", "### Incremental price-shock exposure vs baseline",
              _table(["Benchmark shock", "Scenario cost (EUR)", "Incremental exposure (EUR)", "% of baseline"],
                     [[s.shock, s.scenario_cost_eur, s.incremental_exposure_eur, s.pct_of_baseline] for s in f.scenarios])]
    if f.status == "available" and f.attribution:
        L += ["", f"### What drives the hypothetical {f.attribution_shock} sensitivity (deterministic split; a scenario, not a forecast)",
              _table(["Cost driver", "Volume in the model (t)", "Incremental exposure (EUR)", "Share"],
                     [[a.detail, a.tonnes, a.incremental_exposure_eur, a.share_pct] for a in f.attribution])]
    if f.status == "available":
        L += ["", f"_{f.rounding_note}_"]
    if f.months:
        shocks = list(f.months[0].incremental_exposure_eur)
        L += ["", "### Contract coverage and monthly detail (coverage changes are separate from price shocks)",
              _table(["Month", "Forecast t", "Committed t", "Uncovered t", "Δ uncovered vs prior month t", "Baseline cost EUR"]
                     + [f"Exposure {s}" for s in shocks],
                     [[m.month, m.forecast_t, m.contract_commitments_t, m.uncovered_t, m.coverage_change_vs_prior_month_t or "—",
                       m.baseline_modelled_cost_eur] + [m.incremental_exposure_eur[s] for s in shocks] for m in f.months])]
    L += ["", f"**Assumptions:** {f.assumptions}", f"_{f.disclaimer}_"]

    if r.supplier_exposure:
        L += ["", "## Existing supplier and contract exposure (fictional internal data)",
              _table(["Contract", "Supplier", "Country", "Approval status", "Type", "Monthly t", "Valid", "Ends in horizon"],
                     [[s.contract_id, f"{s.supplier_name} ({s.supplier_id})", s.country, s.approval_status.upper() if s.approval_status != "approved" else s.approval_status,
                       s.contract_type, s.monthly_volume_t, f"{s.valid_from} → {s.valid_to}", "yes" if s.ends_within_horizon else "no"]
                      for s in r.supplier_exposure])]
    if r.indicators:
        L += ["", "## Severity indicators (deterministic; internal findings must match them)"]
        L += [f"- **{i.category.replace('_', ' ')}: {i.severity.upper()}** — {i.value}. _Rule: {i.rule}._" for i in r.indicators]

    d = r.draft
    ev = {e.evidence_id: e for e in r.evidence}

    def cites(cs) -> str:
        return " ".join(f"[{c.evidence_id}: \"{c.quote}\"]" for c in cs)

    def pol(cs) -> str:
        return " ".join(f"[{c.policy_ref}: \"{c.quote}\"]" for c in cs)

    def refs(rs) -> str:
        return (" Internal basis: " + ", ".join(f"`{x}`" for x in rs) + ".") if rs else ""

    pc = r.policy
    L += ["", "## Internal policy context (fictional policy documents)"]
    if pc.status == "unavailable":
        L.append(f"**UNAVAILABLE — no policy guidance is given.** {pc.note or ''}")
    else:
        L.append(f"Retrieval: **{pc.method}**" + (" (fallback)" if pc.status == "fallback" else "") + (f" — {pc.note}" if pc.note else "")
                 + ". These passages come from fictional internal documents; they explain internal process and are not evidence of real regulatory "
                   "compliance or of a supplier's current compliance.")
        for p in pc.passages:
            text = " ".join(p.content.split())
            L.append(f"- **[{p.policy_ref}]** {p.doc_title} ({p.doc_type.replace('_', ' ')}): \"{text[:230]}{'…' if len(text) > 230 else ''}\"")
    L += ["", "## Risks"]
    L += [f"- **{x.risk_id} · {x.title}** — {x.category.replace('_', ' ')} · basis `{x.basis}` · severity **{x.severity.upper()}** "
          f"({x.severity_basis}) · {x.time_relevance}\n  {x.description}{refs(x.internal_refs)} {cites(x.citations)}" for x in d.risks] or [
        "- No external risk is asserted: no direct official or trade-press evidence supported one."]
    if not any(x.basis != "internal_model" for x in d.risks):
        L.append("- **No external market or supply risk is asserted:** no direct official or trade-press evidence supported one.")
    L += ["", "## Mitigation options"]
    L += [f"- {m.option} ({m.applies_to.replace('_', ' ')}; basis `{m.basis}`; addresses {', '.join(m.addresses)}): {m.rationale}"
          f"{refs(m.internal_refs)} {cites(m.citations)} {pol(m.policy_citations)}" for m in d.mitigation_options] or ["- None proposed."]
    L += ["", "## Alternative-supplier leads — UNVERIFIED (real companies found by search; not approved, qualified or capacity-confirmed)"]
    if d.supplier_candidates:
        for c in d.supplier_candidates:
            ident = ev.get(c.identity_citation.evidence_id)
            tier = TIER_LABEL.get(ident.credibility, "") if ident else ""
            L.append(f"- **{c.name}** · **UNVERIFIED** · country: **{c.country}**"
                     + (f" (\"{c.country_quote}\")" if c.country != "unknown" else " (not stated in the source)")
                     + f" · product: **{c.product_type.replace('_', ' ')}**"
                     + (f" (\"{c.product_quote}\")" if c.product_type != "unknown" else " (not stated in the source)")
                     + f"\n  Source [{c.identity_citation.evidence_id}] ({tier}): \"{c.identity_citation.quote}\"\n  {c.rationale}"
                       f" **To verify independently:** {c.verification_needed}")
    else:
        L.append("- No candidate is reported: the retrieved evidence was insufficient to identify a real supplier lead with supported attributes.")
    L += ["", "## Recommended next steps (all require human review and approval)"]
    L += [f"- {a.action} (basis `{a.basis}`; addresses {', '.join(a.addresses)}) — {a.rationale} _Next step: {a.next_step}_"
          f"{refs(a.internal_refs)} {cites(a.citations)} {pol(a.policy_citations)}" for a in d.recommended_actions] or ["- None."]
    L += ["", "## Uncertainties"]
    L += [f"- {u}" for u in d.uncertainties] or ["- None stated."]
    L += ["", "## Research status"] + [f"- {k}: {v}" for k, v in r.source_status.items()]
    L += ["", "## Evidence register (source tier and relevance are assigned by rules and a screening step; they are not fact-checks)"]
    L += [f"- **[{e.evidence_id}]** {e.title or e.url} — {e.url} · source: {e.source} · tier: {TIER_LABEL[e.credibility]} · "
          f"relevance: {e.relevance} · published: {e.published_date or 'date unknown'} · retrieved: {e.retrieved_on}"
          + (" · MOCK" if e.is_mock else "") for e in r.evidence] or ["- No evidence retained."]
    L += ["", "## Scope and limitations"] + [f"- {x}" for x in r.limitations]
    L += ["", "---"] + [f"_{x}_" for x in r.disclaimers]
    return "\n".join(L)
