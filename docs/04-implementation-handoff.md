# Implementation Handoff — Procurement Risk & Mitigation Agent

**For:** Claude Code  
**Project:** Personal AI engineering / data engineering portfolio  
**Fictional client:** Bakery Foods Inc., a bakery manufacturer  
**Status as of:** 10 November 2026 · Day 2 of a four-day build  
**Purpose:** Continue implementation from the existing research and documentation. Do not mistake proposed features for implemented functionality.

## 1. Goal and design principles

Build an **on-demand Procurement Risk Assessment & Mitigation Agent**. A procurement manager asks, for example:

> Assess sunflower oil procurement risks for the next three months.

The system should combine **external commodity prices and disruption news**, **synthetic internal supplier/contract/forecast data**, and **procurement-policy document retrieval** to produce **one evidence-backed report**. The report should explain risk, relevant suppliers, scenario-based financial exposure, mitigation options, evidence gaps, and decisions requiring human review.

**Business boundary:** This is decision support. The agent must not approve suppliers, place orders, change contracts, or make procurement commitments.

**Implementation boundaries:**
- **LangGraph + Python** is the only primary agent orchestrator: research, tool selection, SQL/RAG access, calculations, report synthesis, and execution state.
- **n8n is a thin helper:** receive request, invoke the Python agent service, deliver report or error notification. No research/LLM reasoning in n8n.
- **Supabase PostgreSQL** stores structured procurement records and commodity price history; **pgvector** supports document RAG where feasible.
- **OpenAI API** is planned for the LLM and embeddings; confirm key and model availability before use.
- **Tavily and GDELT** provide external research. One MCP-enabled Tavily integration is **optional after the core flow works**; it is not required.
- **Python, not the LLM**, performs financial arithmetic and required-data checks.
- **Source-supported facts, synthetic business records, and scenario assumptions must be explicitly separated.**

Country is **not finalized**: France was the initial provisional operating market, while the verified sunflower oil series is **Spain**. Keep country/market configurable; do not label Spanish prices as French prices.

## 2. What has actually been done

| Area | Status | Evidence / detail |
|---|---|---|
| Business case, architecture, AI evaluation, n8n blueprint | **Drafted, not implemented** | Markdown documents prepared; working code is not established |
| Portfolio README | **Drafted** | Independent project; should be updated as components are verified |
| **Tavily Search API** | **Working connectivity test** | One authenticated request returned HTTP 200, valid JSON, **4** results with URLs/dates |
| **GDELT DOC 2.0 API** | **Working connectivity test** | One unauthenticated request returned HTTP 200, valid JSON, **5** articles |
| EU Cereals REST API | **Failed** | `/agrifood/api/cereal/prices` returned HTTP 500 with placeholder `marketCodes=string`; **`/agrifood/api/cereal/products` also returned HTTP 500 without parameters** |
| French and EU wheat downloads | **Downloaded** | XLSX sources available for ingestion; exact chosen benchmark series still needs selection in code |
| European Commission sunflower oil dataset | **Downloaded and inspected** | Spanish standard crude sunflower oil, weekly, EUR/tonne, **308 data rows**; latest uploaded week 19–25 Oct 2026 |
| Supabase project / PostgreSQL URI | **Provisioned/configuration prepared** | Session pooler / URI selected; `DATABASE_URL` intended for local `.env` |
| Supabase database connection | **NOT tested** | First action is `SELECT 1`; no verified tables/ingestion yet |
| Internal procurement tables and synthetic data | **Not built** | Proposed ~5 suppliers, ~5 contracts, 3-month forecast |
| LangGraph, RAG, n8n, MCP, report tests | **Not verified as implemented** | Design documents only |

**Integration count:** Two live external research APIs have been successfully tested (**Tavily, GDELT**). Downloadable XLSX market data is a valid external data source, **not an API test**. If the project must demonstrate three distinct external tool/API integrations, explicitly validate and document the third rather than silently counting the workbook.

**API interpretation:** A successful HTTP 200 proves connectivity and parsable data—not relevance or verified procurement conclusions. Tavily results included irrelevant fossil-fuel content; GDELT returned multilingual/unrelated articles. Implement relevance filtering, source dates, and human-readable uncertainty.

**API/key status:**
- **Tavily:** User has a working API key (already used successfully). Read `TAVILY_API_KEY` from `.env`; never print it.
- **GDELT:** Public DOC API test succeeded; no key used.
- **OpenAI:** Planned; **key/model have not been confirmed in this handoff**.
- **Supabase:** A PostgreSQL connection URI was obtained through the dashboard's **Session pooler → URI**. **Connectivity has not been confirmed**.
- **Custom FastAPI service:** Not yet built or tested.
- **EU Cereals API:** Do **not** depend on it for the MVP.

## 3. Source workbooks to place in the repository

Store **original workbooks unchanged** under `data/raw/`; build transformed outputs separately in PostgreSQL. Do not manually edit a source file to remove anomalies.

| Filename (exact) | Role | Include? |
|---|---|---|
| `European_Commission_Oilseeds_Prices.xlsx` | **Primary sunflower oil dataset**. Spain; standard **crude** sunflower oil; weekly EUR/tonne | **Yes — priority 1** |
| `SCR-COT-CER_FR-A26.xlsx` | Correct French cereals quotation workbook; inspect and select an appropriate **soft/milling wheat** series | **Yes — priority 2** |
| `cereals-eu-prices.xlsx` | European Commission cereals prices; optional cross-check / alternate wheat benchmark | **Yes, optional** |


**Sunflower oil workbook facts already checked:** Worksheet `MyWorkSheet-1`; columns include `Product`, `Product Type`, `Member State`, `Marketing Year`, `Week`, `Week Begin Date`, `Week End Date`, `Market`, `Market Stage`, `Unit of Measure`, `Price (EUR)`. The top record is Spain, Standard crude sunflower oil, EUR/tonne, **€1,457.40/t**, week **19–25 October 2026**. There are 308 data rows. Earlier review noted duplicate boundary weeks and suspicious 2022 outliers: independently validate and **flag**, never silently fix or interpolate.

**Wheat caveat:** Wheat quotations (including FOB or delivered market prices) are **not bakery flour procurement prices**. Sunflower **crude oil** is **not necessarily refined food-grade oil**. Use these as market signals, not direct supplier quotes.

For a public GitHub repository, first confirm licensing/redistribution terms before committing full third-party spreadsheets. If necessary, keep `data/raw/` untracked, document official download locations, and commit only small synthetic test fixtures.

## 4. Suggested repository layout

```text
procurement-risk-agent/
├── README.md
├── .gitignore
├── .env                  # LOCAL ONLY — never commit
├── .env.example          # Placeholders only
├── requirements.txt      # Or pyproject.toml
├── data/
│   ├── raw/
│   │   ├── European_Commission_Oilseeds_Prices.xlsx
│   │   ├── SCR-COT-CER_FR-A26.xlsx
│   │   └── cereals-eu-prices.xlsx      # Optional
│   └── synthetic/                     # Clearly labeled fictional inputs
├── src/
│   ├── data_ingestion/
│   ├── db/
│   ├── tools/
│   ├── rag/
│   ├── calculations/
│   ├── agent/
│   └── api/                           # Thin Python HTTP interface
├── tests/
├── examples/                           # Reviewed reports and test evidence
├── n8n/                                # Actual exports only, when built
└── docs/
    ├── 01-business-case.md
    ├── 02-solution-architecture.md
    ├── 03-ai-workflow-evaluation.md
    ├── n8n-workflow-blueprint.md
    └── 04-implementation-handoff.md    # This file
```

The existing documentation is **proposed design**. Audit it before publishing for bootcamp references, outdated location claims, or features still presented as complete.

## 5. Next actions — perform in this order

### A. First task: Safely validate Supabase connectivity (do now)

1. Inspect the current repository structure and dependencies without modifying data.
2. Confirm `.gitignore` excludes `.env`, credentials, and temporary outputs. Do not read back or print any secret values.
3. Load `DATABASE_URL` from local `.env` with `python-dotenv` or the process environment.
4. Use `psycopg` to open a short-lived TLS connection to the **exact Session pooler URI provided by Supabase**. If SSL is not already specified, use `sslmode=require`.
5. Execute only **`SELECT 1`** (optionally inspect `current_database()` and `current_user` without printing credentials).
6. Report **PASS/FAIL**, elapsed time if measurable, and a sanitized error type. Do not log the URI, credentials, or raw connection exception if it includes secrets.
7. **STOP and report the result** before creating or changing any database objects.

Do not invent a Supabase project reference, hostname, password, or API key. `DATABASE_URL` is a PostgreSQL connection URI; `SUPABASE_URL` / `SUPABASE_KEY` are not required for `psycopg`.

### B. Once connection succeeds: Create the smallest usable data layer

- Propose and then create tables for `commodity_prices`, `suppliers`, `contracts`, and `demand_forecasts`. Keep schema minimal and use relevant constraints (positive quantities/prices, units, unique source records).
- Use **synthetic**, clearly labeled company suppliers/contracts/forecasts; approximately five suppliers, five contracts, three months of forecast data.
- Build an idempotent Python loader for `European_Commission_Oilseeds_Prices.xlsx`: parse Excel dates, normalize types, validate required fields, retain original market stage, preserve source metadata, flag anomalies, and deduplicate repeated week records **only according to documented rules**.
- Prefer a controlled ingestion role with write access; LangGraph should later use a read-only role.
- Verify row counts, newest observation, duplicate handling, and rerun behavior; record a concise ingest summary without leaking credentials.
- Then inspect `SCR-COT-CER_FR-A26.xlsx` and select one clearly identified wheat series. Avoid mixing currencies, products, and quotation bases.

**Minimum Day 2 deliverable:** `SELECT 1` passes, sunflower oil workbook is loaded/retrievable from PostgreSQL with provenance and quality flags, and synthetic procurement data can be queried.

### C. Day 3: Working LangGraph vertical slice

1. Connect read-only SQL lookup (prices, suppliers, contracts, forecasts).
2. Wrap the **already-tested** Tavily and GDELT endpoints as Python tools; enforce timeouts and relevance checks. Do not repeatedly call APIs while developing tests—use mocked responses.
3. Add a small procurement-policy document set and `pgvector` retrieval if feasible; keep RAG separate from exact SQL retrieval.
4. Compute financial exposure with deterministic Python functions. Explicitly define any assumed conversion or pass-through from a **crude oil benchmark** to a **synthetic contracted refined-oil price**.
5. Implement a bounded LangGraph loop (e.g., <=3 research rounds) and Pydantic report schema.
6. Produce the first source-backed sunflower oil report. Label unsupported findings or calculations as unavailable.

**Day 3 deliverable:** One reproducible end-to-end sunflower oil report via Python, without n8n.

### D. Day 4: Thin workflow, testing, and portfolio

1. Expose a simple Python API endpoint for the agent; use a short synchronous run only if observed execution time fits n8n timeouts.
2. Build n8n **Form/Webhook → validation → HTTP Request → Switch (`completed`/`partial`/`failed`) → email/failure notification**.
3. Implement bounded retry and error routing, without duplicating retry policies between n8n and Python.
4. Generate **two reviewed sample reports**: sunflower oil plus wheat if feasible, otherwise two differentiated sunflower scenarios with a clearly documented scope limitation.
5. Record real test outcomes and evidence; update the README from “planned” to “implemented” only where verified.

**Core tests:**
- **DB01:** `SELECT 1` succeeds, with no secret exposure.
- **DATA01:** XLSX loader parses valid records, dates, units, and provenance correctly.
- **DATA02:** Loader handles duplicate weeks and suspicious outliers without silent modification; rerunning is idempotent.
- **CALC01:** Financial calculation equals a hand-checked example; missing contract price produces **unavailable**, not a fabricated estimate.
- **API01:** Simulated Tavily/GDELT timeout triggers bounded retry or visible partial/failure.
- **AGENT01:** End-to-end sunflower oil report has traceable evidence, warnings, and correct calculations.
- **REPORT01:** Invalid report schema is rejected; one bounded repair attempt or failure.
- **N8N01:** `completed`/`partial`/`failed` routing works; delivery failure does not trigger another full research run.

An offline **LLM-as-a-Judge** for two reports and **Tavily MCP** are optional *after* the vertical slice works.

## 6. Failure handling and evaluation rules

- One retry owner per operation; retry transient network/429/5xx errors only, with bounded backoff.
- Research has a fixed iteration/tool/time budget. On exhaustion, disclose unknowns.
- No fabricated suppliers, quotations, forecast records, or market-event claims.
- Source dates and retrieval dates are different; record both where possible.
- Validate report JSON/required fields with Pydantic, then manually inspect material claims.
- Use manual/source-grounded review for relevance and faithfulness; unit tests for arithmetic.
- Never present synthetic data or mock API responses as live market findings.
- No auto-purchasing, supplier approval, or contract changes.

## 7. First message to Claude Code

> Read `docs/04-implementation-handoff.md` and inspect the existing repository. Treat the tables and milestones as **plans**, not completed features. **Start only with Task A: safe Supabase connectivity validation (`SELECT 1`).** Use `.env` without exposing credentials. Do not run schema migrations, modify records, make paid API requests, or install additional skills/MCP servers yet. Report the connection result and your proposed next minimal implementation steps before proceeding.

**Success condition for the immediate next step:** A confirmed, credential-safe PostgreSQL `SELECT 1` response. Everything else comes after that checkpoint.
