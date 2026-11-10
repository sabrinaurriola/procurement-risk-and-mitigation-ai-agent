# PROCUREMENT RISK AGENT

### AI-Powered Procurement Risk Intelligence | Food Manufacturing · France (Provisional)

[![Python](https://img.shields.io/badge/Python-3.x-blue)](https://www.python.org/)
![Status](https://img.shields.io/badge/status-in%20development-orange)
![Focus](https://img.shields.io/badge/focus-AI%20%7C%20Data%20Engineering%20%7C%20Automation-blue)

**Procurement Risk Agent** is a Python-based portfolio project designed to combine external commodity data, procurement records, document retrieval, and AI-assisted research into a single **Procurement Risk Assessment & Mitigation Report**.

It is intended for raw-material procurement managers who need to understand market disruptions, estimate exposure, and evaluate mitigation options before making sourcing decisions.

The business scenario uses **Bakery Foods Inc.**, a fictional bakery manufacturer. **France is the provisional operating market**, with potential sourcing across the European Union.

> **Project status (10 November 2026):** Business case, solution architecture, and evaluation plan are drafted. Official EU and French cereals datasets have been downloaded for feasibility review; the European Commission Cereals REST endpoints returned HTTP 500 during testing. The data ingestion pipeline, LangGraph agent, API integrations, n8n workflow, and end-to-end reports are **not yet implemented or validated**.

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
- Investigate approved supplier alternatives and identify a limited number of **unverified** external candidates.
- Generate an evidence-backed report with assumptions, source references, uncertainties, and recommended next steps.
- Use **n8n** for request intake and report delivery.

The agent is **advisory**. It cannot approve suppliers, commit spending, place purchase orders, or change contracts.

## Data Sources and Integrations

| Source / integration | Purpose | Current status |
| --- | --- | --- |
| **French cereals quotations (XLSX)** | Wheat market benchmark and ingestion prototype | Downloaded; relevant series identified for further normalization |
| **EU cereals prices (XLSX)** | Alternative or complementary wheat benchmark | Downloaded; suitability and schema still being assessed |
| **European Commission Cereals REST API** | Direct cereals price retrieval | Tested; HTTP 500 errors, **not usable as an MVP dependency** |
| **Sunflower oil price source** | Sunflower oil market benchmark | **Not yet selected or verified** |
| **Tavily Search API** | Research and preliminary supplier discovery | Planned; connectivity and limits not yet tested |
| **GDELT** | News and disruption signals | Planned; connectivity and relevance not yet tested |
| **OpenAI API** | Language-model synthesis and document embeddings | Planned; model selection pending |
| **Supabase PostgreSQL + pgvector** | Structured procurement data and document retrieval | Planned; database and pipeline not yet implemented |


The final three external tool/API integrations will be confirmed only after working connectivity tests. Downloadable files contribute to the data pipeline but are not automatically counted as live API integrations.

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

## Development Roadmap

| Capability | Status |
| --- | --- |
| Business case and architecture documentation | ✅ Drafted |
| AI design, evaluation, and failure-test plan | ✅ Drafted |
| French / EU wheat source feasibility | 🟡 Initial review |
| Sunflower oil market-data source | 🔜 To verify |
| Python commodity-data ingestion and PostgreSQL schema | 🔜 Planned |
| Synthetic supplier, contract, and forecast dataset | 🔜 Planned |
| LangGraph research and retrieval workflow | 🔜 Planned |
| Deterministic exposure calculation tests | 🔜 Planned |
| RAG over procurement documents | 🔜 Planned |
| n8n request intake and report delivery | 🔜 Planned |
| Two example reports with supporting evidence | 🔜 Planned |
| Error-handling tests and execution logs | 🔜 Planned |
| MCP-enabled research tool | 💡 Optional |
| Production ERP integration, alerting, and monitoring | 💡 Future work |

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
