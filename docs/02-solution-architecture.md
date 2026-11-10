# Solution Architecture — Procurement Risk & Mitigation Agent

**Project:** Ironhack AI Consulting Bootcamp · Project 3  
**Company:** Bakery Foods Inc. (fictional industrial bakery manufacturer in Spain; ~800 employees, ~€175M revenue, 3 factories, centralized procurement — fictional assumptions; operating market Spain, configurable)  
**Update (10 Nov 2026):** Gate B approved the data design — dedicated `procurement` Postgres schema, CSV-first synthetic data, EC Excel price files ingested by Python, Tavily and GDELT as the two research APIs. See `implementation-plan.md` for the current plan; this document remains the proposed architecture.  
**Scope:** Four-day MVP prototype  
**Status:** Proposed architecture; implementation and test results to be documented separately

## 1. Architecture at a glance

The system turns an on-demand procurement question into a research-backed risk report. **LangGraph (Python) is the primary agent orchestrator.** n8n is a thin entry and delivery layer, not a second reasoning engine.

```mermaid
flowchart TD
    U["Procurement Manager"] --> N["n8n: Form / Webhook"]
    N --> A["LangGraph Agent (Python)"]
    A --> R["Research tools: market data, GDELT, web search"]
    A --> S["Supabase PostgreSQL: suppliers, contracts, forecasts"]
    A --> V["pgvector: procurement document retrieval"]
    A --> C["Python: deterministic exposure calculations"]
    R --> A
    S --> A
    V --> A
    C --> A
    A --> Q["Validate evidence, calculations and report"]
    Q --> N
    N --> O["Email / report delivery"]
    O --> U
```

The model interprets evidence and drafts recommendations. **SQL and Python supply factual records and calculations. Procurement decisions remain human-owned.**

## 2. Component responsibilities

| Component | Responsibility | Design rationale |
|---|---|---|
| **LangGraph + Python** | Manage research state, choose tools, assess evidence gaps, stop research, and assemble the report | One primary orchestration layer with explicit workflow control |
| **OpenAI API** | Interpret requests and evidence; draft report sections; create embeddings for document retrieval | Language understanding and synthesis, not financial authority |
| **External research integrations** | Retrieve commodity benchmarks, relevant news, and supplier candidate information | Bring current outside evidence into the assessment |
| **Supabase PostgreSQL** | Store fictional suppliers, contracts, forecasts, and structured assessment records | Exact queries for structured procurement data |
| **pgvector + document chunks** | Retrieve relevant fictional policies and supplier assessments | Semantic search for unstructured internal knowledge |
| **Python calculation functions** | Compute exposure scenarios from validated inputs and stated assumptions | Reproducible, testable financial logic |
| **n8n** | Accept the request, call the agent service, route status, and deliver the report | Simple business workflow automation without duplicating agent logic |

**MCP decision:** If feasible after the core workflow is working, connect **one web-research tool via MCP** to demonstrate standardized tool access. Other integrations can use direct Python APIs. MCP is optional for this MVP; it does not improve the model's reasoning by itself.

## 3. End-to-end workflow

1. **Request:** The manager selects an ingredient and time horizon (for example, sunflower oil over three months). n8n validates required fields and calls the Python agent service.
2. **Plan and research:** LangGraph identifies information needs and calls appropriate external tools. The research loop is bounded by iteration, tool-call, and time limits.
3. **Internal context:** SQL retrieves relevant suppliers, contracts, and forecasts. Vector search retrieves relevant procurement policy or supplier-document passages.
4. **Evidence review:** The agent checks relevance, publication dates, source references, and gaps. It can request more research within the defined budget.
5. **Exposure calculation:** Python calculates scenario-based cost impact using available contract/forecast inputs and explicitly labeled price assumptions. Missing essential inputs produce an unavailable result, not an invented figure.
6. **Mitigation options:** The agent compares approved suppliers and may identify up to three **unverified** external supplier candidates for follow-up.
7. **Report validation and delivery:** Required sections, calculation outputs, and source references are checked. n8n sends the completed or clearly labeled partial report; failures trigger notification.

**Example output:** A single *Procurement Risk Assessment & Mitigation Strategy Report* with executive summary, evidence, supplier exposure, financial scenarios, mitigation options, uncertainties, and decisions requiring approval.

## 4. Data and retrieval design

| Data | Storage / access | Important boundary |
|---|---|---|
| Fictional suppliers, contracts, and demand forecasts | PostgreSQL tables, queried with read-only SQL | Structured records are not generated by the LLM |
| Fictional procurement policies and supplier assessments | Document chunks and embeddings in PostgreSQL + pgvector | Retrieved passages are supporting context, not automatic proof of current supplier compliance |
| Commodity benchmarks | External source or periodically loaded dataset | Benchmark movements are not actual supplier contract prices |
| News and disruption signals | External API/search results | Preserve source, date, and uncertainty; filter irrelevant signals |
| New supplier candidates | External search evidence | Discovery is not qualification, capacity confirmation, or approval |

**External sources under consideration:** World Bank Pink Sheet commodity data, GDELT news data, and Tavily web search. Their coverage, API access, pricing, and suitability for sunflower oil must be checked during implementation. A source that lacks the required commodity or timely evidence should be replaced or excluded, not treated as validated.

## 4a. Supplier intelligence: three separate capabilities

| | Capability | Scope | Data | Output |
|---|---|---|---|---|
| **A** | Internal supplier and contract exposure | **In MVP** | Five fictional suppliers, five synthetic contracts, approval statuses and three-month forecasts (Postgres) | Contract coverage, fixed / index-linked / uncovered exposure, supplier-status findings |
| **B** | Real alternative-supplier discovery | **In MVP** | Tavily results about real companies | Up to three **unverified** sourcing leads (possibly none) |
| **C** | Public-risk monitoring of existing suppliers | **Future enhancement, not implemented** | A client's real supplier master data + public news | Verified supplier-specific events mapped to *potentially affected* contracts and volumes |

**A** keeps the fictional internal environment intact. Fictional suppliers are never replaced by, or attributed to, real companies, and no invented contract, relationship or purchasing volume is attributed to a real company.

**B** rules: a candidate is a *lead*, not a supplier. Its identity, product type and any country must each be supported by a verbatim quote from a non-social, direct source; unsupported attributes are `unknown`; a country is never inferred from a phone prefix, web domain or vague region ("across Europe"). Crude and refined food-grade sunflower oil are distinguished. Capacity, price, availability, certification and suitability are never asserted. Fewer than three candidates (including zero) is the correct output when evidence is thin. Independent procurement verification and qualification are required before any action, and a supplier replacement is never recommended on external search results alone.

**C** (not built) business value: earlier warning of a supplier-specific disruption (plant closure, insolvency, strike, sanctions, operational incident) and a first estimate of which contracts and volumes could be affected. Prerequisites: real supplier master data (legal entities, sites, products), entity resolution so news about the right company is matched, source-verification rules, contract and volume data, and an owner for the alerts. Caveats: the existing suppliers here are fictional, so external research about them would be meaningless and public news about a real company must never be presented as concerning a fictional one; and *potentially affected contractual volume* must never be confused with confirmed delivery loss or financial loss.

### Evidence provenance rules (apply to every finding and recommendation)
Each risk, mitigation and action declares its `basis`: `internal_model` (cites internal records or calculation outputs only), `external_evidence` (cites retrieved sources only) or `combined` (cites both). A citation is an evidence ID plus a verbatim quote that must appear in that source and relate to the claim; the source must be judged direct or contextual, must not be social media, and each finding needs at least one direct official or trade-press source (commercial, unrated and contextual sources may accompany but never carry a finding). Severity for internal categories equals a deterministic documented indicator; `high` for an external finding needs two distinct strong sources. Claims that cannot be verified after one repair attempt are **removed and listed in the report**, never kept with a decorative citation. Third-party emails and phone numbers are removed before snippets are stored or sent to the model.

## 5. AI, rules, and human responsibilities

| AI-assisted | Deterministic / system-controlled | Human-owned |
|---|---|---|
| Research planning and tool selection | Request validation and access permissions | Approve mitigation actions |
| News relevance and evidence synthesis | SQL retrieval and financial formulas | Qualify and approve new suppliers |
| Drafting risk assessments and options | Output schemas, research budgets, retries | Confirm quotes, capacity, and contract changes |
| Identifying unresolved questions | Status routing and delivery controls | Decide whether and when to purchase |

The agent has **no purchasing or supplier-approval tool**. It must not represent assumptions as verified facts.

## 6. MVP operating boundaries

- **Ingredient coverage:** Sunflower oil end-to-end; a second ingredient if time permits.
- **Internal dataset:** Approximately five fictional suppliers, five contracts, and three months of forecasts.
- **Research limits:** A small bounded loop (for example, at most three research rounds), plus API and execution limits.
- **Failure handling:** Retry temporary external failures; report evidence gaps; stop calculations that lack required data; notify on unrecoverable errors.
- **Delivery:** n8n sends reports or failure notifications without performing the underlying analysis.
- **Security:** Synthetic data only; credentials stored outside source control; read-only database access for the agent.

The prototype is **not** a production procurement platform. Real ERP integration, continuous alerts, supplier qualification, comprehensive monitoring, and deployment governance are future work.

## 7. Implementation priorities and proof

| Priority | Deliverable |
|---|---|
| **1 — Working vertical slice** | One sunflower oil request → research + internal data → calculated scenarios → report |
| **2 — Reliable output** | Source references, explicit assumptions, report schema, missing-data handling |
| **3 — Integration** | n8n trigger and delivery; RAG and external tool connections |
| **4 — Portfolio evidence** | Two sample reports, workflow diagram, reproducible setup, and selected failure-test results |
| **Optional** | One MCP-enabled research integration, only if it does not threaten the core workflow |

For evaluation criteria, detailed error-handling rules, LLM-as-a-Judge, and test evidence, see `03-ai-workflow-evaluation.md`.

---

*Portfolio disclosure: Bakery Foods Inc. and internal procurement records are fictional. External sources and any live results should be identified and dated. This document describes the proposed design, not verified implementation.*
