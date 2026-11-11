# PROCUREMENT RISK AGENT

### AI-Powered Procurement Risk Intelligence | Food Manufacturing · Spain

[![Python](https://img.shields.io/badge/Python-3.x-blue)](https://www.python.org/)
![Status](https://img.shields.io/badge/status-in%20development-orange)
![Focus](https://img.shields.io/badge/focus-AI%20%7C%20Data%20Engineering%20%7C%20Automation-blue)

**Procurement Risk Agent** is a Python-based portfolio project designed to combine external commodity data, procurement records, document retrieval, and AI-assisted research into a single **Procurement Risk Assessment & Mitigation Report**.

It is intended for raw-material procurement managers who need to understand market disruptions, estimate exposure, and evaluate mitigation options before making sourcing decisions.

The business scenario uses **Bakery Foods Inc.**, a fictional industrial bakery manufacturer headquartered in **Spain** (operating market, configurable), with Spanish and European suppliers. Fictional profile: ~800 employees, ~€175M annual revenue, 3 factories in Spain, a centralized procurement team, and key ingredients wheat/flour, sunflower oil and sugar. These are invented assumptions; the MVP covers sunflower oil and wheat, and exposure is calculated from synthetic contracts, forecasts and benchmarks, not from revenue.

> **Project status (11 November 2026, Day 3):** data layer, deterministic exposure engine, a bounded LangGraph workflow, evidence-provenance controls and a policy-RAG implementation are built. Verification: **490 offline tests** pass (47 opt-in skipped) and the opt-in read-only database tier passes (**537**, incl. the populated policy table); two live assessments were run on 10 Nov (before the provenance fixes and RAG) and one GDELT diagnostic on 11 Nov. The 25 fictional policy chunks are loaded and embedded in Supabase (`text-embedding-3-small`, 1,536 dims) and retrieval was checked against real pgvector (11 Nov, see implementation plan §16). One live end-to-end assessment of the current code (live-003, 11 Nov) finished PARTIAL (GDELT 429; 6 unverifiable items removed), structural audit 10/10, with content-quality defects listed in implementation plan §17. Targeted offline corrections for those defects (price direction, hypothetical shocks, other-market prices, SUP-003 escalation, unattributed cadence wording) are implemented and tested offline only (490 passed; 537 with the read-only database tier), **not yet re-tested with a live model** (plan section 18). POL-001 and ASM-003 were aligned on the conditional-supplier approval sequence and re-embedded (2 chunks, plan section 19). A final live run (live-004, 11 Nov) finished PARTIAL with correct figures, price direction and SUP-003 governance, but lost one risk and one action to the strict rules and left an unsupported sentence in the summary (plan section 20; CONDITIONAL GO). Those gaps were then corrected offline (plan section 21; not re-tested live). **Day 3 closure: CONDITIONAL GO for Day 4** — see [docs/day3-checkpoint.md](docs/day3-checkpoint.md) for the full snapshot and handoff. **Not yet done:** FastAPI, n8n. See [docs/implementation-plan.md](docs/implementation-plan.md) §6, §14–15 and [docs/day3-checkpoint.md](docs/day3-checkpoint.md).

## The Problem

Bakery manufacturers rely on ingredients such as **sunflower oil** and **wheat/flour**, which are exposed to commodity-price movements, weather events, geopolitical disruptions, and supplier constraints.

A procurement manager must connect external signals with internal supplier contracts and demand forecasts. Without that connection, a market headline may appear important but have little operational impact—or a genuine exposure may be overlooked.

The project addresses a practical question:

> **What changed in the market, how could it affect our procurement exposure, and which mitigation actions deserve human review?**

## What the Project Is Designed to Do

The intended MVP will:

- Research market developments and supply disruptions for **sunflower oil and wheat**, prioritizing one complete ingredient workflow first.
- Ingest and normalize selected commodity-price datasets using **Python and PostgreSQL**.
- Retrieve fictional supplier, contract, and demand-forecast data through SQL.
- Retrieve relevant procurement policies and supplier assessments using **RAG with pgvector**.
- Calculate financial exposure scenarios through **deterministic Python functions**, not LLM arithmetic.
- Discover **real alternative-supplier leads** with Tavily (up to three, possibly none), each labelled **UNVERIFIED** and each attribute (identity, product type, country) shown only when a source states it; otherwise `unknown`.
- Generate an evidence-backed report with assumptions, source references, uncertainties, and recommended next steps.
- Use **n8n** for request intake and report delivery.

The agent is **advisory**. It cannot approve suppliers, commit spending, place purchase orders, or change contracts.

## Data Sources and Integrations

| Source / integration | Purpose | Current status |
| --- | --- | --- |
| **EU cereals prices (XLSX)** | Primary wheat benchmark: Spanish (`ES`) breadmaking common wheat | Downloaded; contains the Spanish series; ingestion not yet built |
| **French cereals quotations (XLSX)** | Supplementary French wheat indicator | Downloaded; messy headers and `#REF!` cells; optional |
| **European Commission oilseeds prices (XLSX)** | Primary sunflower oil benchmark (Spain, crude, weekly, EUR/tonne) | Loaded into PostgreSQL (308 observations, 2 market-stage series, quality-flagged) |
| **European Commission Cereals REST API** | Direct cereals price retrieval | Tested; HTTP 500 errors, **not usable as an MVP dependency** |
| **Tavily Search API** | Research and preliminary supplier discovery | One connectivity request succeeded (HTTP 200); relevance was mixed |
| **GDELT DOC 2.0** | News and disruption signals | One connectivity request succeeded (HTTP 200); results were noisy |
| **OpenAI API** | Language-model synthesis and document embeddings | Core infrastructure; planned; key and model not yet verified |
| **Supabase PostgreSQL + pgvector** | Structured procurement data and document retrieval | PostgreSQL connectivity verified (`SELECT 1`); migration 001 applied (10 tables, pgvector 0.8.2); no data loaded yet |

Tavily and GDELT are the two research APIs; a third research API is not required. OpenAI and Supabase are core infrastructure, and the European Commission files are official price sources ingested into PostgreSQL. FRED, MCP and LLM-as-a-Judge are optional and deferred.

## Proposed Workflow

```text
Procurement manager
        |
        v
n8n: request form / webhook
        |
        v
LangGraph (primary Python agent)
        |
        +--> External research tools (Tavily / GDELT)
        |
        +--> PostgreSQL: suppliers, contracts, demand forecasts
        |
        +--> pgvector: procurement policies and assessments
        |
        +--> PostgreSQL: normalized commodity price history
        |
        v
Evidence review + deterministic Python calculations
        |
        v
Report validation + explicit limitations
        |
        v
n8n: deliver report or failure notification
        |
        v
Procurement manager reviews decisions
```

**Architecture boundary:** LangGraph owns research orchestration and report generation. n8n is a thin automation layer; it does not perform the underlying analysis. **MCP** is an optional integration for one research tool, not a prerequisite for the MVP.

## Project Structure

**Proposed repository layout** (folders are added as components are built):

```text
procurement-risk-agent/
├── README.md
├── docs/
│   ├── 01-business-case.md
│   ├── 02-solution-architecture.md
│   └── 03-ai-workflow-evaluation.md
├── src/
│   ├── ingestion/             # External files / API normalization
│   ├── agent/                 # LangGraph workflow and tools
│   ├── retrieval/             # SQL and document retrieval
│   └── calculations/          # Deterministic exposure scenarios
├── tests/                     # Unit and failure tests
├── examples/                  # Reviewed sample reports
├── n8n/                       # Actual workflow exports when ready
├── .env.example
└── requirements.txt
```

### Design documentation

- [Business Case](docs/01-business-case.md) — Business problem, user, value hypothesis, MVP boundaries.
- [Solution Architecture](docs/02-solution-architecture.md) — Components, data flow, responsibilities, and integrations.
- [AI & Workflow Design Evaluation](docs/03-ai-workflow-evaluation.md) — AI boundaries, safeguards, evaluation criteria, and test cases.

## Getting Started

**A runnable setup is not available yet.** The repository will include verified installation commands, environment variables, sample data initialization, and a demo request **after** the first end-to-end workflow is implemented.

Planned requirements include Python 3, a PostgreSQL database with pgvector, and API credentials for the external integrations ultimately selected. Secrets will be managed through environment variables and excluded from version control.

Until then, the design documents describe the intended system; they are **not** instructions for running a finished application.

## Evaluation and Limitations

The prototype will be evaluated on:

- **Evidence quality:** Material external claims are traceable to dated sources.
- **Business relevance:** Findings relate to the ingredient, geography, and assessment horizon.
- **Calculation accuracy:** Exposure scenarios pass deterministic unit tests.
- **Missing-data behavior:** The system does not invent unavailable prices, forecasts, or supplier qualifications.
- **Workflow reliability:** API failures, malformed outputs, and delivery failures produce visible statuses and safe fallbacks.
- **Report usefulness:** A reviewer can identify risks, assumptions, options, and decisions requiring approval.

**Priority planned tests:** successful sunflower oil assessment; research API timeout; missing contract price; known financial calculation; invalid report schema. An offline **LLM-as-a-Judge** review may supplement—but not replace—manual and deterministic checks.

No accuracy score, time saving, ROI, or production reliability is claimed before testing.

## Scope and Status

Status words are used strictly: **implemented** = code exists and offline tests pass; **tested with mocks** = exercised only against recorded/mocked responses; **tested live** = run against the real service at least once (see `docs/implementation-plan.md` §11–§13); **planned** = not built.

### Supplier intelligence: three separate capabilities

| Capability | Scope | What it means |
| --- | --- | --- |
| **A. Internal supplier and contract exposure** | In MVP | Five **fictional** suppliers, their synthetic contracts, approval statuses and demand forecasts represent Bakery Foods Inc.'s simulated internal environment. They are never replaced by or attributed to real companies. |
| **B. Real alternative-supplier discovery** | In MVP | Tavily finds **real companies** that might supply sunflower oil. They are sourcing leads, not suppliers: always `UNVERIFIED`, attributes only if a source states them, crude vs refined distinguished, independent verification and qualification required. |
| **C. Public-risk monitoring of existing suppliers** | **Future enhancement, not implemented** | With a client's real supplier master data, the agent could monitor publicly reported supplier-specific events (plant closure, insolvency, strike, sanctions) and map a verified event to contracts and *potentially affected* volume. Not meaningful with fictional suppliers, so it is out of scope. |

### Implemented MVP capabilities

| Capability | Status |
| --- | --- |
| Fictional internal suppliers, contracts and forecasts (CSV → PostgreSQL, idempotent) | Implemented; loaded in Supabase; read-only use in live runs |
| Official commodity benchmark (EC Spanish crude sunflower oil, 308 observations, quality-flagged) | Implemented; loaded in Supabase |
| Deterministic financial exposure (fixed / index-linked / uncovered) | Implemented; offline tests; figures verified in two live runs |
| External market-risk research (Tavily, GDELT) | Implemented; tested live twice (live-002: Tavily returned results; GDELT returned HTTP 200 with zero articles, cause not yet diagnosed) |
| Evidence-provenance controls (`basis`, verbatim-quote citations, source tiers, contact redaction, severity indicators, removal of unsupported claims) | Implemented; **tested with mocks only** (added after live-002; not yet run live) |
| Real alternative-supplier discovery with unverified leads | Implemented; **tested with mocks only** after the remediation (live-002 showed unsupported country attributions, now blocked by validation) |
| Human-reviewed mitigation recommendations | Implemented; every action requires human approval |

### Planned, not yet implemented

| Capability | Status |
| --- | --- |
| Procurement-policy RAG: loading 25 chunks and embeddings into pgvector | **Loaded and embedded** (6 documents / 25 chunks, one embedding batch of 1,586 tokens); real pgvector retrieval verified; offline workflow with real retrieval verified; **not yet used in a live LangGraph run** |
| n8n workflow integration and delivery | Planned; interface specified in [docs/api-specification.md](docs/api-specification.md) |
| Further report-quality evaluation and a second live evaluation after the remediation | Planned |
| Two reviewed example reports | Scenarios A (normal) and B (stale-data limitation) defined and passing on mock data ([docs/demo-scenarios.md](docs/demo-scenarios.md)); live reports pending approvals |

### Out of scope / potential future enhancements

- Monitoring actual existing suppliers with real supplier master data; supplier-specific disruption-to-contract exposure mapping (potentially affected volume is never presented as confirmed delivery loss or financial loss).
- Automated supplier qualification or onboarding; live supplier quotations, capacity verification and commercial negotiation.
- Automatic supplier switching, purchasing or contract execution.
- Optional FRED integration, MCP tooling, a more sophisticated evaluation system (e.g. LLM-as-a-Judge).
- Production ERP integration, alerting and monitoring.

## Tech Stack

**Planned implementation:**

- **Python** — Data ingestion, API integration, validation, and calculations
- **LangGraph** — Stateful AI agent orchestration
- **OpenAI API** — LLM analysis and embeddings
- **Supabase PostgreSQL** — Structured procurement and commodity data
- **pgvector** — Retrieval over embedded procurement documents
- **Tavily / GDELT** — External web research and disruption signals, pending validation
- **n8n** — Workflow triggers, report delivery, and delivery failure routing
- **Pydantic / pytest** — Output validation and automated tests
- **Git / GitHub** — Version control and portfolio documentation

## Intended Audience

Procurement and supply-chain professionals interested in early risk assessment, and technical reviewers evaluating **AI engineering, data engineering, business-process integration, and safe agent design**.

## Disclaimer

**Bakery Foods Inc. and its internal procurement records are fictional.** The project is an experimental decision-support prototype, not a live procurement system. External sources may be incomplete or outdated; supplier candidates require independent qualification. Financial impact figures are hypothetical scenarios unless backed by explicitly identified inputs. All procurement decisions remain subject to human review and approval.

## Verification status (11 Nov 2026)
| Level | Result |
|---|---|
| Offline (mocks) | 490 passed, 47 opt-in skipped |
| Real read-only database | 48 passed (constraint tests run inside rolled-back transactions); row counts unchanged |
| Live services | 2 assessments on 10 Nov (before the latest changes), 1 GDELT diagnostic on 11 Nov: GDELT returned HTTP 429 twice, then HTTP 200 with no articles |
| Independent report audit | 10/10 on the mock scenarios; verifies structure and provenance, **not** the business quality of real LLM text |
