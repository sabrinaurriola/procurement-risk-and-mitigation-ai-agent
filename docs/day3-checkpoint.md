# Day 3 Closure and Coding-Agent Handoff — Procurement Risk & Mitigation Agent

**Snapshot:** Wed 11 Nov 2026 (end of Day 3) · **Demo:** Fri 13 Nov 2026 · **Decision: Day 3 closure — CONDITIONAL GO for Day 4 integration.**
This file is the authoritative snapshot of the project at Day 3 closure. It is **not** a claim that any Day 4 functionality (FastAPI, n8n) exists. It is written so a new developer or AI coding agent can continue without the earlier conversation. Read `CLAUDE.md` (project rules) first.

**Status words used:** *Implemented* = code exists · *Tested offline* = automated tests with mocks · *Tested read-only on DB* = run against the real database without writing · *Tested live* = run against real services · *Planned* / *Deferred* / *Out of scope*.

---

## A. Project overview and business objective
| Item | Detail |
|---|---|
| Company | **Bakery Foods Inc.** — a **fictional** industrial bakery manufacturer in Spain (~800 employees, ~€175M revenue, 3 factories, centralized procurement; all company figures are fictional assumptions) |
| Problem | Commodity prices and supply (e.g. sunflower oil, Black Sea logistics) move with market events. A Raw Materials Procurement Manager must connect external signals to internal contracts, demand forecasts and supplier status to decide whether to act |
| Output | On request, one **evidence-backed risk and mitigation report** for an ingredient over a **3-month horizon** (Dec 2026–Feb 2027 at the 11 Nov assessment date): market evidence, contract coverage, modelled financial exposure, existing-supplier status, mitigation options, and *unverified* alternative-supplier leads |
| Implemented ingredient | **Sunflower oil** (the working MVP) |
| Deferred ingredient | **Wheat** — synthetic records exist (2 suppliers, 2 contracts, 3 forecasts) but there is no wheat price ingestion and no wheat analysis |
| Autonomy boundary | **Decision support only.** The agent cannot approve suppliers, place orders, qualify leads or change contracts. Every recommended action carries `requires_human_approval = true`; policy approvals belong to the (fictional) Procurement Director |

Exposure is computed from synthetic contracts and forecasts, market benchmarks and explicit scenario assumptions — **never from company revenue**. Scenario exposure is not company-wide financial impact.

## B. Scope and functionality
| Area | Status |
|---|---|
| **Implemented** | |
| Synthetic suppliers, contracts, demand forecasts (committed CSVs, loaded by Python, flagged `is_synthetic`) | Implemented; loaded; read-only tested |
| Official European Commission sunflower-oil price ingestion (Spain, crude) with quality flags | Implemented; loaded (308 observations) |
| PostgreSQL data layer (Supabase) + pgvector | Implemented; applied |
| Deterministic financial exposure engine (`Decimal`, no LLM arithmetic) | Implemented; hand-checked tests; confirmed in live runs |
| LangGraph orchestration (bounded research, one repair, claim removal) | Implemented; tested offline, read-only DB, and live (4 runs) |
| Tavily and GDELT research tools | Implemented; Tavily works live; GDELT returned HTTP 429 or empty results every time (see H) |
| Evidence screening and provenance controls | Implemented; tested offline; exercised live in live-003/004 |
| Fictional procurement-policy RAG (25 chunks, pgvector + full-text fallback) | Implemented; loaded and embedded; retrieval verified on the real database |
| Real alternative-supplier discovery, always **UNVERIFIED** | Implemented; tested live (live-004: one lead) |
| Report generation, deterministic summary, structural/provenance audit, failure handling | Implemented; tested offline and read-only DB |
| **Planned for Day 4** | FastAPI service (`docs/api-specification.md`), thin n8n workflow (`docs/n8n-workflow-blueprint.md`: intake → HTTP call → status routing → e-mail), integrated delivery and operational tests |
| **Deferred / out of scope** | Wheat ingestion and analysis · public-risk monitoring of **existing** suppliers (never implement: they are fictional) · automated supplier qualification or purchasing · FRED, MCP, LLM-as-a-Judge and other integrations (do not build unasked) |

**Why fictional suppliers and real leads are kept separate:** existing suppliers/contracts are fictional internal data, so no real news may be attributed to them and no real company may appear in synthetic contracts. Real companies may appear only as **UNVERIFIED sourcing leads** found by search, with identity/country/product quoted from a source (else `unknown`). A supplier website supports what the company says about itself; it never verifies registration, capacity, certification or qualification.

## C. System architecture
```
Request → LangGraph → PostgreSQL internal data → deterministic calculations → policy RAG → external research
        → evidence validation → structured report (+ deterministic summary) → status/result
```
Graph nodes (`src/agent/graph.py`): `load_internal → calculate → retrieve_policy → plan_research → search → filter_evidence → (loop ≤3 rounds) → draft → validate → (1 repair) → finalize`.

| Component | Responsibility |
|---|---|
| Python / LangGraph | All business logic and orchestration (the only agent orchestrator) |
| Supabase PostgreSQL | Stored market, supplier, contract, forecast and policy data; agent sessions are **read-only** |
| `src/calculations` | All financial computation; the LLM never does arithmetic and may only quote supplied figures |
| Tavily / GDELT | External retrieval (the only research APIs) |
| OpenAI (`gpt-5.4-mini`, low reasoning; `text-embedding-3-small`) | Evidence screening, structured drafting, embeddings (query embeddings at run time) |
| pgvector | Retrieval of the fictional policy chunks (cosine, with PostgreSQL full-text fallback) |
| FastAPI / n8n | **Planned** interface, routing and delivery only — n8n holds no analysis or LLM logic |

**Bounded research and limits** (`src/agent/config.py::Limits`): ≤3 research rounds; ≤6 Tavily and ≤3 GDELT calls; 6 results per call; 90-day look-back; 180 s deadline; ≤3 supplier candidates; **one** report-repair attempt, after which unsupported items are **removed visibly**. Retries: one owner — `tenacity` inside `src/tools/search.py` (3 attempts max for transient errors, `Retry-After` capped at 10 s); the live runner sets GDELT to **2 attempts** (`GdeltTool(max_attempts=2)`). LLM token cap 40,000 per run (`--max-tokens`). Human-approval boundaries: see G.

## D. Repository structure and entry points
| Purpose | Path |
|---|---|
| Project rules (authoritative) | `CLAUDE.md` |
| Agent configuration, ingredient profile, limits | `src/agent/config.py` |
| SQL migration (single, v001) / DB utilities | `sql/001_schema.sql` · `src/db/migrate.py`, `src/db/preflight.py` |
| Synthetic data + validation + loader | `data/synthetic/*.csv` · `src/synthetic/validate.py` · `src/ingestion/load_synthetic.py` |
| EC ingestion | `src/ingestion/ec_oilseeds.py`, `src/ingestion/series_key.py` · raw files in `data/raw/` (untracked, see `data/raw/MANIFEST.md`) |
| Financial calculations | `src/calculations/{exposure,benchmark,models,repository,run_demo}.py` |
| LangGraph workflow, data access, LLM wrapper | `src/agent/{graph,data,llm}.py` |
| Search tools | `src/tools/search.py` · GDELT diagnostic `src/tools/gdelt_diagnostic.py` |
| Evidence handling | `src/agent/{evidence,credibility,claims}.py` |
| Policy documents / RAG | `data/synthetic/documents/*.md` · `src/rag/{chunker,embed,load_policies,retrieve,verify_retrieval}.py` |
| Schemas, validation, summary, rendering, audit | `src/agent/{schemas,validation,summary,render,quality,facts}.py` · prompts `src/agent/prompts.py` |
| Mock / demo / live commands | `src/agent/{mock,demo,run}.py` |
| API specification / n8n blueprint | `docs/api-specification.md` · `docs/n8n-workflow-blueprint.md` (**specifications only; no code**) |
| Tests | `tests/` (unit) · `tests/integration/` (opt-in, read-only/rolled back) |
| Documentation | `README.md` · `docs/implementation-plan.md` (§6, §14–20 history) · `docs/03-ai-workflow-evaluation.md` · `docs/demo-scenarios.md` · `docs/04-implementation-handoff.md` (Day 2 handoff, historical) |

**Verified commands** (Windows; paths contain spaces — quote them). Python is the 3.13 venv outside OneDrive; do not use the system Python 3.14.
```bash
PY="C:/Users/sabri/venvs/procurement-agent/Scripts/python.exe"
$PY -m pytest -q                                   # offline suite (database tests skipped)
RUN_DB_TESTS=1 $PY -m pytest -q --tb=no            # + read-only/rolled-back database tier (use --tb=no: a traceback can echo a host name)
$PY -m src.agent.demo all --mock                   # scenarios A (normal) and B (stale benchmark), in-memory policy
$PY -m src.agent.demo all --mock --real-policy     # same, real read-only DB policy retrieval using cached query embeddings (no paid call)
$PY -m src.agent.run --mock                        # offline demo report on real read-only data
$PY -m src.agent.quality --mock                    # independent audit on the mock report
$PY -m src.rag.verify_retrieval                    # retrieval relevance report (cache only unless ALLOW_PAID_CALLS=1)
$PY -m src.calculations.run_demo                   # deterministic exposure demo
```
Data pipelines are dry-run by default and need `--apply`: `src.db.migrate`, `src.ingestion.load_synthetic`, `src.ingestion.ec_oilseeds`, `src.rag.load_policies` (`--apply [--allow-updates]`, `--embed`). A live run (needs approval): `ALLOW_PAID_CALLS=1 OPENAI_MODEL=gpt-5.4-mini OPENAI_REASONING_EFFORT=low $PY -m src.agent.run --live --request-id <id> --max-tokens 40000`. Saved report/trace: `runs/<id>.md|.json`. The structural audit on a saved live report is `audit_report()` in `src/agent/quality.py` (the CLI is mock-only).

## E. Database and datasets
| Item | Fact (verified read-only on 11 Nov) |
|---|---|
| Schema | Schema `procurement`, migration **001** applied (10 Nov); pgvector 0.8.2 in schema `extensions` |
| Tables (10) | `data_sources` (1), `ingest_runs` (2), `commodity_prices` (308), `ingredients` (2), `suppliers` (5), `supplier_ingredients` (5), `contracts` (5), `demand_forecasts` (6), `policy_chunks` (25), `schema_migrations` (1) |
| Fictional suppliers / contracts / forecasts | 5 suppliers (SUP-003 `conditional`, others `approved`), 5 contracts, 6 forecasts: **sunflower oil** SUP-001/002/003 · CT-001 fixed 100 t/mo, CT-002 fixed 80 t/mo (ends 2027-01-15), CT-003 index-linked 120 t/mo; forecasts Dec 400 t, Jan 420 t, Feb 410 t · **wheat** SUP-004/005, CT-004/005, 3 forecasts (loaded, unused). All rows `is_synthetic = true` |
| EC sunflower oil | **308** observations, two distinct series (`series_key` includes market stage, currency, unit): `…|DEPPROC||EUR|tonne` (293 obs, 2021-04-19 → 2026-10-25, the benchmark used) and `…|ex-factory||EUR|tonne` (15 obs, 2021-01-04 → 2021-04-18). Latest eligible: €1,457.40/t, period 19–25 Oct 2026 |
| Quality handling | Flags, never silent fixes: `DUPLICATE_PERIOD` ×10 (kept, separate occurrence numbers), `OUTLIER_VS_MEDIAN` ×2 (kept in the database, excluded from calculations). Raw files unedited |
| Policy corpus | 6 fictional documents → **25 chunks**: ASM-001 (3), ASM-003 (4), POL-001 (4), POL-002 (5), POL-003 (5), POL-004 (4). Embeddings `text-embedding-3-small`, **1,536 dims**, all 25 present; content hash per chunk; `is_synthetic = true` |
| Safety | Pipelines idempotent (repeat = 0 changes); changed policy content nulls its old embedding and is refused without `--allow-updates`; assessments use a **read-only session** and parameterised queries only |

Actually loaded: everything above. Scenario assumptions (not data): spot premium, pass-through and shocks. Planned: nothing further in the database.

## F. Financial model and verified results
| Topic | Rule |
|---|---|
| Volumes | Forecast = allocated + uncovered (no double counting). Contracts are prorated by active days: **CT-002 ends 15 Jan → 15/31 of 80 t in January** (38.71 t), none in February |
| Fixed-price | Price = contract price; incremental exposure **0** for any benchmark shock |
| Index-linked (CT-003) | Price = base 1,600 + 0.80 × (benchmark − 1,400); incremental = volume × 0.80 × Δbenchmark |
| Uncovered demand | Priced at benchmark + **fictional spot premium of €150/t** (an assumption); incremental = volume × Δbenchmark |
| Over-commitment | Reported and flagged (`OVER_COMMITMENT`), never hidden |
| Benchmark | Latest eligible EC observation; **max age 21 days** from period end; outliers excluded; a missing/old/mismatched benchmark makes the month `unavailable` — nothing is estimated |
| Crude vs refined | The benchmark is a **crude** sunflower-oil price; the company uses **refined, food-grade** oil. The premium over the benchmark is an assumption (POL-004). Benchmark ≠ supplier quote |

**Verified results (assessment date 11 Nov; benchmark 17 days old):**
| Metric | Value |
|---|---|
| Forecast / contracted / uncovered (3 months) | 1,230.000 t / 778.710 t / **451.290 t** (Dec 100.000, Jan 161.290, Feb 190.000 t uncovered; peak share 46.3% in Feb) |
| Baseline modelled cost | **€1,961,496.55** |
| Hypothetical +10% benchmark shock | **+€107,744.17** (+5.49%); ±20% = ±€215,488.34 |
| December +10% | **€28,565.04** |
| Attribution of the +10% exposure (deterministic, `FinancialSection.attribution`) | uncovered demand **€65,771.05 (61.0%)** · index-linked CT-003 **€41,973.12 (39.0%)** · fixed-price **€0.00 (0.0%)** |

**Rounding convention (investigated, not changed):** the engine keeps 6-decimal precision and rounds to cents (half-up) **only for display**, each figure independently. At full precision scenario cost − baseline = incremental exactly (e.g. 2,069,240.726452 − 1,961,496.554839 = 107,744.171613). The displayed values 2,069,240.73 − 1,961,496.55 = 107,744.18 therefore differ from the displayed incremental €107,744.17 by €0.01 (also at +20%). The report states this (`rounding_note`), and a test pins the ≤ €0.01 bound. **Do not "fix" the calculation to hide it.**

Scenario sensitivities are **hypothetical** — not forecasts, quotations, or realized savings. Keep them separate from the *observed* price direction in the evidence.

## G. Evidence, RAG and governance rules (current safeguards)
| Safeguard | What it enforces |
|---|---|
| Provenance `basis` | `internal_model` (internal records only, no external citations) · `external_evidence` (evidence only; may not use internal exposure figures) · `combined` (must cite both). A pure model sensitivity is allowed as `market_price` + `internal_model` citing `INT:indicator:price_exposure` |
| Citations | Evidence id + verbatim quote (≥25 chars) that shares distinctive wording with the claim; item must be direct/contextual, dated for risks, not social |
| Source credibility | Tiers: official / trade press / commercial / social / unrated. A finding needs ≥1 **direct** official/trade-press citation. Exception (narrow): a listed **logistics/shipping publication** may carry a **supply-disruption** finding when the quote itself describes the disruption |
| Geographic/market relevance | A price quotation for another market or basis (e.g. India CIF Mumbai, Russian FOB, Ukrainian **seed** prices) is context only: never a strong source, usable for a price claim only if the claim names that market. Supply-mechanism quotes without prices are unaffected |
| Price direction | Observed direction is computed from direct trade-press items; market text must state it; falling evidence cannot support "upward pressure" |
| Hypothetical shocks | Any "+10% shock" text must say scenario/sensitivity/hypothetical and may not use forecast verbs |
| Policy citations | Must name a **retrieved** passage and quote it verbatim; cadence/process words ("monthly", "short-list") must be in the cited quote; approval wording needs a policy citation; no "regulatory compliance" claims; no exceptions/waivers |
| Candidate suppliers | Max 3, always `unverified`; identity from a direct discovery source; country/product only if quoted, else `unknown`; never approved/qualified/certified/capacity-confirmed. A **lead-verification recommendation** may rest on the reported lead + the qualification policy POL-003 (no trade-press needed), must name the lead and cite only reported leads |
| SUP-003 rules (fictional policy, aligned in POL-001 and ASM-003) | SUP-003 is **CONDITIONAL**, not blocked. It may keep delivering under its existing valid contract, subject to internal monitoring; outstanding conditions (food-safety certificate, follow-up audit) must be completed and verified; a **renewal or volume increase needs written Procurement Director sign-off and cannot be committed until all conditions are closed**; no exception exists; the Raw Materials Procurement Manager may initiate review, coordinate corrective actions and recommend escalation; the agent cannot approve or authorise |
| Unsupported claims | Removed visibly after one repair; listed in the report as **Unverified concerns** (not findings). The executive summary is **assembled by code** (`src/agent/summary.py`) from validated findings only, so a removed claim cannot survive in it |
| Status | **COMPLETED** = no research source failed, no claim removed, benchmark/financials available. **PARTIAL** = a report exists but a source failed (e.g. GDELT), claims were removed, or the benchmark/financials/policy were unavailable. **FAILED** = no report (invalid request, data load failure, LLM unavailable, or validation still failing). A technically successful run can be PARTIAL |

**What validators can establish:** structure, provenance labels, verbatim quotes, source tiers, numeric/figure consistency, policy-citation resolution, forbidden wording. **What still needs human judgment:** whether the evidence truly supports the interpretation, relevance and proportionality, severity thresholds (my design, not expert-validated), usefulness of recommendations, the meaning of `DEPPROC`, and the fictional policy design itself. The wording rules are lexical heuristics.

## H. Testing and evaluation history
**Current verified totals (11 Nov, after all Day 3 changes):** offline `pytest -q` → **490 passed, 47 skipped** (the skipped are opt-in database tests); `RUN_DB_TESTS=1 pytest -q --tb=no` → **537 passed** (read-only / rolled-back). Revised mock scenarios A and B (with and without real policy retrieval): **independent audit 10/10 each**; A COMPLETED, B PARTIAL as designed.

| Run | Date | Result | Main lessons |
|---|---|---|---|
| live-001 | 10 Nov | **PARTIAL** — Tavily TLS connectivity failed (fixed) | Connectivity/TLS handling; the report was drafted from limited evidence |
| live-002 | 10 Nov | **COMPLETED** technically, with evidence-provenance and supplier-quality defects (decorative citations, country inferred from phone prefix, social-media identity, weak sources judged relevant, SUP-003 ignored) | Led to the provenance remediation and source tiers |
| live-003 | 11 Nov | **PARTIAL** — RAG worked (pgvector, 9 passages); R3 claimed "continuing price pressure" against falling sources, used India CIF Mumbai prices, was mislabelled external-only; SUP-003 escalation and the lead were removed; 6 items removed; $0.046 | Led to price-direction, scenario, geographic and policy-wording rules |
| live-004 | 11 Nov | **PARTIAL** — assessed **CONDITIONAL GO**: figures exact, price direction correct, SUP-003 governance correct, one valid UNVERIFIED lead, 8/8 policy citations verified, audit 10/10; but one risk and the lead-verification action were removed, the summary still asserted the removed supply risk, the exposure drivers were unexplained, a Ukrainian seed-price headline was cited; GDELT 429 ×2; 76 s; 20,393 in / 7,862 out tokens; $0.051 | Led to the final offline corrections below |

Live spend to date ≈ **$0.12** of the **$5** budget (usage in `runs/usage.jsonl`, git-ignored).

**Final offline corrections (after live-004; NOT validated in a subsequent live model run):** deterministic executive summary from retained findings with an "Unverified concerns" list · logistics-publication support for supply-disruption findings · seed/other-market price quotes cannot support Spanish benchmark claims · lead-verification recommendations allowed on lead + POL-003 · four recommendation types restored in the mock draft (coverage review, SUP-003 escalation, lead verification, premium-assumption review) · deterministic +10% attribution table · rounding note · prompt version `2026-11-11.1` with new rules 9–15 · audit mirrors these rules. Tests: `tests/test_live003_regressions.py`, `tests/test_live004_regressions.py`, `tests/test_policy_documents.py`.

**Three different kinds of success — keep them apart:** *technical* (the pipeline ran, no error), *structural* (the 10-check audit passes — structure and provenance only), *business quality* (a person judges usefulness and correctness; only live-003/004 got such a review, and the post-live-004 corrections have had none).

## I. Day 3 final status
- **Original Day 3 goals:** baseline check; policy RAG; provenance/quality audit; GDELT diagnostic; API/n8n specification; final testing and documentation — **all done** (Tasks 1–6).
- **Beyond the goals:** policy documents authored and corrected; policy ingestion (25 chunks, one embedding batch of 1,586 tokens) and a later 2-chunk correction (313 tokens) with idempotency and unchanged-table checks; real pgvector retrieval verification (8/8 relevant queries rank first; relevant top-1 ≥ 0.482 vs off-topic controls ≤ 0.376; no similarity threshold implemented); `assessment_date` override; scenarios A/B; four live assessments; four rounds of evidence-quality fixes.
- **Outstanding limitations:** the last corrections are untested live; real-model drafts may still lose items to the strict rules (PARTIAL is common); GDELT has never returned articles; severity thresholds and policies are unvalidated designs; `DEPPROC` unconfirmed; EC data licence unconfirmed; mock prose is templated; **the benchmark goes stale from 16 Nov** (period end 25 Oct + 21 days) unless newer EC data is loaded, after which a live assessment degrades to the "stale benchmark" behaviour (no euro figures); only one search round occurs in practice.
- **Decision — Day 3 closure: CONDITIONAL GO for Day 4 integration.** The agent is ready to be wrapped and tested operationally (API, routing, delivery, failure paths). It is **not approved for unattended procurement advice or autonomous decisions**: every delivered report needs human review.

## J. Day 4 implementation handoff (prioritised)
| # | Step | Needs approval? |
|---|---|---|
| 1 | Confirm tests (490 / 537) and repository state (L); decide whether to commit Day 3 | Commit/push: **yes** |
| 2 | Implement FastAPI per `docs/api-specification.md` (`GET /health`, `POST /assessments`, `GET /assessments/{request_id}`, `…/report.md`; synchronous; file-store results in `runs/assessments/`; no database writes) | New dependency (FastAPI/uvicorn): **yes** |
| 3 | Thin n8n workflow per `docs/n8n-workflow-blueprint.md` (intake → HTTP → status routing → e-mail) | Sending e-mail: **yes** |
| 4 | Test valid, partial, failed, timeout and duplicate-request paths **in mock mode** | No |
| 5 | Idempotency by `request_id` + request hash; never re-POST an assessment automatically; block duplicate paid runs | No |
| 6 | One controlled integrated assessment | **Separate approval; paid APIs** |
| 7 | Human business-quality review of the delivered report (use the live-003/004 review checklist) | No |
| 8 | Finalise documentation; rehearse the 13 Nov demo (one success, one controlled failure/partial) | No |

**Blockers/risks:** benchmark staleness from 16 Nov (the 13 Nov demo is still within 21 days: 19 days); real-model drafts of the corrected prompt unproven; GDELT unreliable (treat as optional evidence); everything since `f7f672d` uncommitted. **Recommended pre-demo content check:** one human-reviewed live run after the API exists.

## K. Technical and operational constraints
| Topic | Rule |
|---|---|
| Environment | Windows, Git Bash/PowerShell; Python **3.13** venv `C:\Users\sabri\venvs\procurement-agent` (outside OneDrive); dependencies pinned in `requirements.txt` (psycopg, python-dotenv, pydantic, pytest, openpyxl, langgraph, openai, httpx, tenacity) |
| Environment variable **names** (values only in git-ignored `.env`; never print them) | In `.env.example`: `DATABASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `TAVILY_API_KEY`, `OPERATING_MARKET` (not read by code at present). Read by code but set per process: `ALLOW_PAID_CALLS` (must be `1` for any paid call), `OPENAI_REASONING_EFFORT` (low), `RUN_DB_TESTS` (`1` enables DB tests). `OPENAI_MODEL` is empty in `.env.example`; live runs set `gpt-5.4-mini` |
| Database | Read-only during assessments; writes only through approved dry-run-first pipelines (`--apply`); never hand-insert records; show SQL/effect before any migration |
| Paid APIs | OpenAI budget max **$5** total, logged in `runs/usage.jsonl`; live runs need `--live`, `ALLOW_PAID_CALLS=1` and prior approval; Tavily and embeddings also need approval |
| Retries / timeouts / cost | One retry owner (tenacity in `src/tools/search.py`); GDELT 2 attempts in live runs; research ≤3 rounds, ≤6 Tavily / ≤3 GDELT calls, 180 s deadline; LLM 40,000-token cap; one repair then visible removal |
| Failure states | `failed` (no report) / `partial` (report with limitations) / `completed`; errors carry codes, never connection details |
| Data restrictions | Raw third-party files untracked and never edited; synthetic rows flagged; no real company names in synthetic data; no ROI/accuracy claims; mock data never presented as live findings |
| Logs / git-ignored | `.env`, `runs/` (reports, traces, usage, embedding cache `runs/policy-query-embeddings.json`), `data/raw/*` (except the manifest). Usage logs hold counts only, never text. Evidence snippets are contact-redacted. Do not paste hostnames or `DATABASE_URL`; run DB tests with `--tb=no` |
| Benchmark freshness | 21-day limit from the observation's period end; latest data period ends 2026-10-25 |

## L. Version control and continuation
- **Last commit:** `f7f672d` — "feat: build procurement data pipeline, exposure engine, and LangGraph workflow" (Day 2). **Nothing from Day 3 is committed or pushed.** Branch `main`.
- **Working tree (verified with `git status --short`, 40 entries):** 19 modified tracked files — `README.md`, `docs/03-ai-workflow-evaluation.md`, `docs/implementation-plan.md`, `src/agent/{config,facts,graph,mock,prompts,render,run,schemas,validation}.py`, `src/tools/search.py`, `tests/{agent_fixtures,test_agent_graph,test_search_tools,validation_fixtures}.py`, `tests/integration/{test_agent_db,test_db_constraints}.py` — and 21 untracked items — `data/synthetic/documents/`, `docs/{api-specification,day3-checkpoint,demo-scenarios}.md`, `src/agent/{claims,demo,quality,summary}.py`, `src/rag/`, `src/tools/gdelt_diagnostic.py`, and tests `test_{assessment_date,demo_scenarios,live003_regressions,live004_regressions,policy_documents,policy_graph,quality_audit,rag}.py` plus `tests/integration/{test_demo_db,test_policy_db,test_policy_populated}.py`. (Git also warns that LF will become CRLF on some files.)
- **Verify the state:**
```bash
git status --short && git log --oneline | head -3
$PY -m pytest -q                                   # expect 490 passed, 47 skipped
RUN_DB_TESTS=1 $PY -m pytest -q --tb=no            # expect 537 passed
$PY -m src.agent.demo all --mock --real-policy     # expect A COMPLETED, B PARTIAL, audit 10/10 each
```
- **First actions for a new coding agent:** read `CLAUDE.md` and this file → run the verification above → read `docs/api-specification.md` and `docs/n8n-workflow-blueprint.md` → ask for approval before adding FastAPI/uvicorn → implement the API in mock mode first → keep any live call behind explicit approval.
- **Decisions already made (do not reopen without evidence):** LangGraph is the only orchestrator; n8n is thin; Tavily + GDELT only; sunflower oil only for the MVP; fictional existing suppliers vs UNVERIFIED real leads; deterministic `Decimal` calculations with display-only rounding; no similarity threshold in RAG; policy approvals belong to people, never the agent; unsupported claims are removed, not decorated; the executive summary is built by code; no automatic re-POST of paid assessments.
- **Uncertainties needing investigation:** whether the corrected prompt and rules let a real model finish COMPLETED (the supply-disruption risk, lead action and four recommendations were only shown in the mock); whether GDELT can ever contribute (HTTP 429/empty); the source-tier list is small (unlisted shipping/trade sites are `unrated`); `DEPPROC` meaning; ranking beyond the top result (rank 2–3 passages are sometimes loosely relevant); refreshing EC data before the benchmark expires on 16 Nov (needs an approved `--apply` of the EC pipeline).
