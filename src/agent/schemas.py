"""Pydantic contracts: request, LLM-authored draft, deterministic report sections and the n8n response envelope.

Provenance rules
* Numbers and calendar facts come from SQL/Python (deterministic sections); the LLM writes interpretation only.
* Every finding/recommendation declares its `basis`:
    internal_model     – derived from contracts, forecasts, supplier records or calculation outputs (cites internal_refs only)
    external_evidence  – supported by retrieved external sources (cites verbatim-quoted evidence only)
    combined           – links external evidence with internal exposure (cites both)
* A citation is an evidence_id PLUS a verbatim quote from that source; an id alone is never accepted as support."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.agent.config import SUPPORTED_INGREDIENTS


class AssessmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    ingredient: str
    horizon_months: int = Field(ge=1, le=6)
    assessment_date: date | None = None     # default: today. Overriding is for reproducible demos/backtests and is flagged in the report

    @field_validator("assessment_date")
    @classmethod
    def _date_in_range(cls, v: date | None) -> date | None:
        if v is not None and not (date.today() - timedelta(days=365) <= v <= date.today() + timedelta(days=90)):
            raise ValueError("assessment_date must be within 365 days before and 90 days after today")
        return v

    @field_validator("ingredient")
    @classmethod
    def _supported(cls, v: str) -> str:
        if v not in SUPPORTED_INGREDIENTS:
            raise ValueError(f"unsupported ingredient {v!r}; supported: {list(SUPPORTED_INGREDIENTS)}")
        return v


Basis = Literal["internal_model", "external_evidence", "combined"]


# ───────────── LLM-authored draft (strict-schema friendly: no defaults) ─────────────
class Citation(BaseModel):
    evidence_id: str
    quote: str                     # verbatim text copied from that source's title or snippet


class PolicyCitation(BaseModel):
    policy_ref: str                # doc_id#chunk_index of a RETRIEVED passage
    quote: str                     # verbatim text copied from that passage


class Risk(BaseModel):
    risk_id: str                   # R1, R2, ... (unique)
    title: str
    description: str
    category: Literal["market_price", "supply_disruption", "contract_coverage", "supplier_status"]
    basis: Basis
    severity: Literal["low", "medium", "high"]
    severity_basis: str            # explicit reasoning or the supplied indicator
    time_relevance: str
    citations: list[Citation]
    internal_refs: list[str]


class Mitigation(BaseModel):
    option: str
    rationale: str
    applies_to: Literal["existing_supplier", "new_supplier_candidate", "contract_strategy", "monitoring"]
    basis: Basis
    addresses: list[str]           # risk_ids this option responds to
    citations: list[Citation]
    internal_refs: list[str]
    policy_citations: list[PolicyCitation]   # fictional internal policy passages, when the option rests on a policy


class SupplierCandidate(BaseModel):
    """A real company found by search: an UNVERIFIED sourcing lead, never an approved supplier."""
    name: str
    country: str                   # a country name supported by country_quote, otherwise "unknown"
    country_quote: str             # verbatim quote naming the country ("" when country is "unknown")
    product_type: Literal["refined_food_grade", "crude", "both", "unknown"]
    product_quote: str             # verbatim quote supporting the product type ("" when unknown)
    identity_citation: Citation    # quote showing the company's identity and sunflower-oil products
    rationale: str
    verification_needed: str
    status: Literal["unverified"]


class Action(BaseModel):
    action: str
    rationale: str
    next_step: str
    requires_human_approval: bool
    basis: Basis
    addresses: list[str]
    citations: list[Citation]
    internal_refs: list[str]
    policy_citations: list[PolicyCitation]


class DraftReport(BaseModel):
    executive_summary: str
    risks: list[Risk]
    mitigation_options: list[Mitigation]
    supplier_candidates: list[SupplierCandidate]
    recommended_actions: list[Action]
    uncertainties: list[str]


class EvidenceJudgement(BaseModel):
    evidence_id: str
    relevance: Literal["direct", "contextual", "irrelevant"]
    reason: str


class EvidenceJudgements(BaseModel):
    judgements: list[EvidenceJudgement]


class RefinedQueries(BaseModel):
    tavily_queries: list[str]
    gdelt_query: str


# ───────────── evidence and deterministic report sections ─────────────
class EvidenceItem(BaseModel):
    evidence_id: str
    source: Literal["tavily", "gdelt"]
    kind: Literal["risk", "supplier_discovery"]
    url: str
    domain: str
    credibility: Literal["official", "trade_press", "commercial", "social", "unrated"]
    title: str
    snippet: str                         # contact details already removed
    published_date: date | None          # publication date (may be missing)
    retrieved_on: date                   # retrieval date (different from the publication date)
    query: str
    relevance: Literal["direct", "contextual", "irrelevant", "unjudged"] = "unjudged"
    relevance_reason: str = ""
    is_mock: bool = False


class BenchmarkSection(BaseModel):
    status: Literal["available", "stale", "unavailable"]
    series_key: str
    price_eur_per_tonne: str | None
    period_start: date | None
    period_end: date | None
    age_days: int | None
    max_age_days: int
    excluded_observations: dict[str, int]
    reasons: list[str]
    caveat: str


class MonthRow(BaseModel):
    month: str
    forecast_t: str
    contract_commitments_t: str
    uncovered_t: str
    coverage_change_vs_prior_month_t: str | None
    over_commitment: bool
    baseline_modelled_cost_eur: str
    incremental_exposure_eur: dict[str, str]     # benchmark shock label -> EUR


class ScenarioRow(BaseModel):
    shock: str
    scenario_cost_eur: str
    incremental_exposure_eur: str
    pct_of_baseline: str


ROUNDING_NOTE = ("All EUR figures are computed at full precision and rounded to cents independently for display, so a displayed scenario cost can differ "
                 "from the displayed baseline plus the displayed incremental exposure by EUR 0.01; no calculation uses a rounded value.")


class AttributionRow(BaseModel):
    component: Literal["uncovered_demand", "index_linked", "fixed_price"]
    label: str
    detail: str
    tonnes: str
    incremental_exposure_eur: str
    share_pct: str


class FinancialSection(BaseModel):
    status: Literal["available", "unavailable"]
    months: list[MonthRow]
    scenarios: list[ScenarioRow]
    baseline_modelled_cost_eur: str | None
    total_forecast_t: str | None
    total_contract_commitments_t: str | None
    total_uncovered_t: str | None
    assumptions: str
    unavailable_reasons: list[str]
    disclaimer: str
    attribution_shock: str = "+10%"
    attribution: list[AttributionRow] = []      # what drives the hypothetical shock (deterministic)
    rounding_note: str = ROUNDING_NOTE


class SupplierExposure(BaseModel):
    contract_id: str
    supplier_id: str
    supplier_name: str
    country: str
    approval_status: str
    contract_type: str
    monthly_volume_t: str
    valid_from: date
    valid_to: date
    ends_within_horizon: bool


class PolicyPassageOut(BaseModel):
    policy_ref: str
    doc_id: str
    chunk_index: int
    doc_title: str
    doc_type: str
    content: str
    score: float | None


class PolicyContext(BaseModel):
    """Retrieved fictional policy passages: internal context, NOT proof of regulatory compliance."""
    status: Literal["ok", "fallback", "unavailable"]
    method: str
    note: str | None
    passages: list[PolicyPassageOut]


class Indicator(BaseModel):
    """A deterministic, documented severity indicator (the LLM must not override it)."""
    ref: str
    category: Literal["contract_coverage", "supplier_status", "price_exposure"]
    severity: Literal["low", "medium", "high"]
    value: str
    rule: str


class Report(BaseModel):
    request_id: str
    ingredient: str
    horizon_months: int
    assessment_date: date
    title: str
    draft: DraftReport                       # AI-authored interpretation (no numbers of its own)
    indicators: list[Indicator]
    benchmark: BenchmarkSection
    financial: FinancialSection
    policy: PolicyContext
    supplier_exposure: list[SupplierExposure]
    evidence: list[EvidenceItem]
    source_status: dict[str, str]
    removed_claims: list[str]                # findings dropped because their support could not be verified
    warnings: list[str]
    missing_data: list[str]
    limitations: list[str]
    disclaimers: list[str]


class ErrorInfo(BaseModel):
    code: str
    message: str


class Execution(BaseModel):
    elapsed_seconds: float
    research_rounds: int
    tool_calls: dict[str, int]
    llm_calls: int
    tokens: dict[str, int]
    mock: bool


class AssessmentResponse(BaseModel):
    """Envelope returned to n8n: check `status`, not just the HTTP code."""
    schema_version: str = "1.1"
    request_id: str
    assessment_date: date | None = None
    status: Literal["completed", "partial", "failed"]
    report_title: str | None
    report_markdown: str | None
    report: Report | None
    warnings: list[str]
    missing_data: list[str]
    limitations: list[str]
    error: ErrorInfo | None
    execution: Execution
