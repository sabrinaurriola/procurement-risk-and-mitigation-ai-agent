# Implementation Plan — Procurement Risk & Mitigation Agent

**Status (10 Nov 2026):** Gate A passed (Supabase `SELECT 1`). Gate B design approved; schema SQL, synthetic CSVs and offline CSV validation tests are prepared but **not applied to the database**. Everything not listed under "Evidence" is planned, not implemented. This plan replaces the earlier `implementation_plan_draft` (still available in Git history, commit `78dc14b`).

**Window:** build Nov 10–12, demo Fri Nov 13.

## 1. Company scenario (fictional)
Bakery Foods Inc.: industrial bakery manufacturer headquartered in Spain; ~800 employees; ~€175M annual revenue; 3 factories in Spain; centralized raw-material procurement; Spanish and European suppliers; key ingredients wheat/flour, sunflower oil, sugar. These are fictional assumptions. The MVP covers **sunflower oil (primary)** and **wheat (secondary)**; sugar is out of scope. The five synthetic suppliers are a sample of the supplier portfolio. No factory-level forecasting or inventory model. Exposure is computed from synthetic contracts, forecasts, benchmarks and explicit assumptions — not from revenue — and is scenario-level procurement exposure, not company-wide financial impact.

## 2. Approved decisions
Spain operating market (configurable) · Tavily + GDELT as research APIs (no third API required) · OpenAI + Supabase core infrastructure · EC Excel files ingested into Postgres (EC ES wheat primary, French quotations supplementary) · dedicated `procurement` schema · CSV-first synthetic data in Git · forecast window Dec 2026–Feb 2027 · assumptions: spot premium €150/t, pass-through 0.8 (sunflower) / 0.6 (wheat), shocks ±10 % / ±20 % (fictional, configurable) · OpenAI budget ≤ $5 with usage tracking and prior approval for live calls · raw Excel untracked with `data/raw/MANIFEST.md` · local n8n (Docker or npx chosen at integration) · Python 3.13 venv `C:\Users\sabri\venvs\procurement-agent` · FRED, MCP, LLM-as-a-Judge deferred.

## 3. Architecture (simplified)
Fixed-topology LangGraph: `load_internal → research (≤3 rounds) → evidence_filter → calculate → draft → validate`. Direct `openai` SDK + Pydantic structured output (no LangChain agents or checkpointer). Parameterised read-only SQL; exact-scan pgvector over ~30 synthetic chunks (full-text fallback). Synchronous FastAPI `POST /assessments`. One retry owner per operation. Run logs in local JSONL (`runs/`), not in the database. n8n only does intake, routing and delivery.

## 4. Data model summary
`procurement` schema; tables `data_sources, ingest_runs, commodity_prices, ingredients, suppliers, supplier_ingredients, contracts, demand_forecasts, policy_chunks, schema_migrations` (`sql/001_schema.sql`, draft). Highlights:
- `series_key = source|product|product_type|member_state|market|market_stage|quotation_basis|currency|unit` — distinct quotations never merge.
- Contracts reference `(supplier_id, ingredient_code)` through `supplier_ingredients` (supplier-ingredient consistency enforced by FK).
- `is_synthetic` is mandatory (`CHECK`) on suppliers, contracts, forecasts and chunks; synthetic names carry "(fictional)".
- Fixed vs index-linked completeness enforced by `CHECK` constraints.

## 5. Exposure model rules
Per ingredient, month and scenario (Python `Decimal`):
1. **Active-day proration:** committed volume in a month = `monthly_volume × active_days / days_in_month` (e.g., CT-002 ending 15 Jan 2027 contributes 15/31 of its volume in January and none in February).
2. **Allocation:** if total committed ≤ forecast, uncovered = forecast − committed. If committed > forecast, allocate pro-rata **as a costing convention only**; raise `OVER_COMMITMENT` and report the committed volume and surplus; contractual obligations are not removed. Invariant: allocated + uncovered = forecast.
3. **Fixed:** impact vs baseline = 0 within validity; mark-to-market vs spot proxy reported separately and informational.
4. **Index-linked:** `price = base_price + pass_through × (B_s − index_base_value) + fixed_premium`; impact = `V × pass_through × (B_s − B0)`. Before use, validate that the benchmark series exists, currency and unit match the contract, and the observation date is within a configurable maximum age; otherwise `unavailable`.
5. **Uncovered:** spot proxy `B_s + premium`; impact = `U × (B_s − B0)`.
6. Missing price/forecast/benchmark ⇒ `unavailable` + `missing_data`, no total.

Worked example (sunflower oil, Dec 2026, +10 %, 400 t, benchmark €1,457.40/t): fixed lines 0.00; index line +13,991.04; uncovered +14,574.00; total **+28,565.04 (+4.49 % of 636,650.40 baseline cost)**. It becomes unit test CALC01.

## 6. Roadmap

| # | Step | Deliverable | Gate / test | Status |
|---|---|---|---|---|
| 0 | Repo organisation, `CLAUDE.md`, `.gitignore`, manifest | tidy repo | — | Done (local, uncommitted) |
| 1 | Schema + synthetic CSV design review | `sql/001_schema.sql`, `data/synthetic/*.csv` | **user approval** | Awaiting approval |
| 2 | Offline validation, preflight, migration runner (dry-run default) | `src/synthetic/validate.py`, `src/db/{preflight,migrate}.py`, `src/ingestion/series_key.py` | 46 pytest cases pass; read-only preflight done | Done |
| 3 | Apply migration, enable pgvector | 10 tables, pgvector 0.8.2 in `extensions`, version 001 | 19 rollback-only DB tests pass; rerun is a no-op | Done (10 Nov 2026) |
| 4 | Synthetic loader (idempotent, dry-run default) | `src/ingestion/load_synthetic.py` | Applied 10 Nov 2026: 23 rows inserted; repeat load = 0 inserts, 0 updates | Done |
| 5 | EC sunflower loader, then EC ES wheat loader | `src/ingestion/ec_oilseeds.py` | Sunflower applied 10 Nov 2026: 308 observations loaded; repeat apply = 0 changes (1 extra audit row); wheat loader not started | Sunflower done |
| 6 | Calculations + tests | `src/calculations/{models,benchmark,exposure,repository,run_demo}.py` | 73 calculation tests + opt-in read-only DB check pass (T06, T07 evidence) | Done, awaiting review |
| 7 | Tools: Tavily, GDELT, SQL reads (RAG deferred) | `src/tools/search.py`, `src/calculations/repository.py` | T04, T05 pass with mocked transport; no live call made | Built (mocked tests only) |
| 8 | LangGraph workflow + Pydantic report | `src/agent/*` | 234 offline tests pass; mock demo on real DB data; no paid run yet | Built; 2 live runs done; provenance remediation (§13) tested with mocks only |
| 9 | FastAPI + local n8n | `n8n/*.json`, screenshots | N01, N02, N04, N05 | Not started |
| 10 | Second report (wheat, else second sunflower scenario), rubric review, README update | `examples/` | T02/T03/T09 as time allows | Not started |
| 11 | Demo rehearsal: one success, one controlled failure | | | Not started |

Slip rule (drop in order): French BT sheet → wheat as second ingredient → RAG breadth (tsvector fallback) → N03/N06 → optional features.

## 7. Acceptance criteria and evidence
Must work: DB01 evidence · market prices and synthetic data queryable with provenance and flags · deterministic fixed/index/uncovered tests · bounded Tavily/GDELT tools · bounded LangGraph + Pydantic report validation · sunflower report with sources, assumptions, missing data · n8n `completed/partial/failed` routing · two reports · test log (minimum T01, T05, T06, T07, T08) including one safe failure · README statuses matching evidence.
Evidence: pytest output, ingest summaries, saved reports with JSONL traces (tokens, tool calls, elapsed time), n8n export and screenshots, secret scan, raw-data manifest.

## 8. Cost and approval controls
OpenAI model set via `OPENAI_MODEL` (small, cost-efficient model; name and price to be confirmed by one approved check). Cap $5 total; estimated a few cents per full run; Tavily ≤ 6 searches per run. Live calls require `--live` and prior approval. Migrations, `--apply` loads, destructive actions, new integrations and commits follow `CLAUDE.md`.

## 9. Evidence recorded so far
- Gate A: `SELECT 1` passed (10 Nov 2026), read-only session, TLS.
- `pytest`: 79 passed, 32 skipped (opt-in DB tests), offline. 13 read-only PREPARE checks pass against the live schema. Read-only preflight (10 Nov 2026): PostgreSQL 17.11; pgvector 0.8.2 available, not installed; schema `extensions` usable; schema `procurement` absent. The contract CHECK expression was evaluated in a read-only SELECT on 13 cases and never returned NULL.
- Tavily and GDELT: one successful connectivity request each (per handoff); relevance was weak.
- Not yet evidenced: database schema, any ingestion, calculations, agent, API, n8n, reports.

## 10. Benchmark data rules (decided 10 Nov 2026)
- **Primary sunflower oil benchmark:** Spanish crude standard sunflower oil, market stage `DEPPROC` (293 observations, 2021-04-19 to 2026-10-19). The 15 earlier `ex-factory` observations (2021-01-04 to 2021-04-12) form a separate series and are not linked to the ingredient.
- **Limitation:** the meaning of the `DEPPROC` market stage has not been independently confirmed against EC documentation.
- **Crude vs refined:** the benchmark is a crude sunflower oil price; it is not the fictional company's refined-oil contract price. The spot-premium assumption bridges the two and is always labelled as an assumption.
- **Calculation eligibility:** observations flagged as outliers, non-numeric or otherwise invalid are excluded from financial calculations and trend analysis. They stay in PostgreSQL with their flags; nothing is corrected or deleted.
- **Freshness (provisional):** a benchmark is stale when `current_date - period_end` exceeds 21 days. Reports show the observation period (`period_start`–`period_end`) of the benchmark used. Latest valid observation: 2026-10-19 to 2026-10-25, 1,457.40 EUR/tonne (16 days old on 10 Nov 2026).
- **Audit log:** each actual apply adds an `ingest_runs` row, including zero-insert runs. Audit rows are not market-data changes.
- **Loaded evidence (10 Nov 2026):** 308 inserted, 0 skipped, 0 rejected, 12 flagged rows (10 `DUPLICATE_PERIOD`, 2 `OUTLIER_VS_MEDIAN`); repeat apply 0 inserts, 308 skipped, price rows and row versions unchanged.

## 11. First live assessment (10 Nov 2026, request `live-001`) — result: PARTIAL, research not evaluated
- **Setup:** `gpt-5.4-mini` (availability confirmed by a metadata call), reasoning effort `low`, 40,000-token cap, Tavily ≤ 6 and GDELT ≤ 3 calls, read-only database access. Pricing used for estimates: $0.75 / $4.50 per million input / output tokens (OpenAI model page, checked 10 Nov 2026; verify).
- **Outcome:** database load, deterministic calculation and one LLM draft succeeded; the draft passed schema validation first time. Tavily (3 calls, 9 attempts) and GDELT (1 call, 3 attempts) both failed with `NETWORK_ERROR`, so no external evidence was retrieved. The report degraded visibly to PARTIAL and asserted no risks.
- **Usage:** 1 LLM call, 2,474 input / 814 output tokens, estimated $0.0055; 54 s elapsed (most of it retry back-off). The evidence-screening call was not needed because there was nothing to screen.
- **Root cause (fixed locally, not yet re-run live):** `httpx`'s bundled certificate list cannot verify TLS on this Windows machine, while the system trust store can (the OpenAI SDK handles this itself, which is why the model call worked). The search tools now use a client verifying against the system store; verification is not disabled.
- **Defect found in the draft (fixed locally, not yet re-run live):** the model wrote "EUR 451.290 t uncovered" (a tonnage labelled as euros). The validator accepted it because the number existed in the facts. Currency-labelled numbers must now be genuine monetary facts, with tests.
- **Not yet evidenced:** relevance of live Tavily/GDELT results, live evidence filtering and source URL/date preservation, live supplier-candidate behaviour, GDELT `timespan` handling. **RAG is not implemented.**

## 12. Second live assessment (10 Nov 2026, request `live-002`) — result: COMPLETED, with review findings
- **Research connectivity:** Tavily 3 calls, HTTP 200, 1 attempt each, 6 results each (18). GDELT 1 call, HTTP 200 after 3 attempts, 0 articles. TLS verification stayed on (system trust store). The research gate would have stopped the run before any LLM call had both sources failed.
- **Evidence:** 18 retrieved; 6 rejected (`OUTSIDE_DATE_WINDOW` 4, `NO_INGREDIENT_MATCH` 1, `DUPLICATE_URL` 1); 12 retained (6 risk, 6 supplier discovery), all judged relevant. All risk sources dated 2026-08-28 to 2026-10-11.
- **Operations:** status COMPLETED, 1 research round, 2 LLM calls (judge 1,495 in / 433 out; draft 4,378 in / 1,537 out), 5,873 in / 1,970 out tokens, estimated $0.0133 at $0.75 / $4.50 per million tokens (verify). Cumulative live model spend so far about $0.019 of the $5 budget. 67.7 s elapsed.
- **Financial integrity:** every figure in the executive summary matches the deterministic tables; euros, tonnes, percentages and periods are labelled correctly (the "EUR 451.290 t" defect did not recur).
- **Open findings (not yet fixed):**
  1. Risks derived from internal data (uncovered volume, coverage gap) cite external evidence (E3, E5) that does not support them; the schema forces every risk to cite evidence. Needs a `basis` field (`internal_model` vs `external_evidence`).
  2. Supplier-candidate countries are not checked against evidence: "Horus Oil (Spain)" is unsupported (source says "across Europe"); "Natural Foods (Spain)" rests on a phone prefix in a Facebook post.
  3. The relevance judge accepted all 12 items, including a palm-oil-centred article (E1), a US-market article (E5) and a social-media post (E10); source credibility is not assessed.
  4. GDELT reported "ok" with zero articles after 3 attempts; the per-attempt errors and the `timespan` format are not recorded or verified.
  5. Saved evidence snippets can contain third-party contact details (an email and phone number appeared in E10's snippet in `runs/live-002.json`); they should be redacted.
  6. The conditional approval status of SUP-003 was not used in the analysis; the "high" severity label for coverage is an unanchored judgement; the trace has no per-node timings.
- **Still not implemented:** RAG (pgvector). Not marked complete.

## 13. Evidence-provenance and report-quality remediation (10 Nov 2026) — offline, tested with mocks only
**Status:** implemented and tested offline (324 tests pass; read-only database tests pass). **No live run has been made since**, so none of this is yet tested live. RAG is still not implemented.

| live-002 finding | Remediation | Evidence (offline) |
|---|---|---|
| Internal findings cited unrelated external evidence (E3, E5) | `basis` field; internal findings cite `internal_refs` only; external/combined need quoted, direct, official/trade-press support | `tests/test_live002_regressions.py` |
| Unsupported supplier countries ("Horus Oil (Spain)", "Natural Foods (Spain)" from a phone prefix) | Structured candidates; country/product/identity need verbatim quotes from a non-social direct source, else `unknown`; crude vs refined enforced | regression + `test_evidence_validation.py` |
| Judge accepted weak sources | Direct / contextual / irrelevant labels; source tiers (official, trade press, commercial, social, unrated); social never supports a finding | graph + regression tests |
| Severity without basis | Deterministic indicators (coverage, supplier status, price exposure) with documented thresholds; internal severity must match; `high` external needs two strong sources | regression tests |
| SUP-003 conditional status unused | Supplier-status indicator (also when the financial model is unavailable) and a required rule-based risk | regression tests |
| Contact details persisted | Emails/phones removed before storage and before the LLM | regression tests |
| GDELT "ok" with zero articles | Per-attempt log (outcome, HTTP status, results), response notes, request parameters; status separates HTTP success from usable evidence; per-node timings | `test_search_tools.py`, graph tests |
| Decorative citations | After one repair, unverifiable items are **removed and listed** (`removed_claims`), status becomes partial | graph + regression tests |

Not changed: the deterministic calculation module and every financial figure (verified identical).
Open items: the empty GDELT result is not yet diagnosed (needs one live request with the new per-attempt log); the judge/validator rules are unproven on live model output; RAG, n8n and a second live evaluation remain planned.
