# AI & Workflow Design Evaluation

**Project:** Procurement Risk & Mitigation Agent\
**Company:** Bakery Foods Inc. (fictional)\
**Scope:** Four-day MVP · **Status:** Design and test plan, not verified
results

## 1. Purpose and scope

This document explains how the prototype will be assessed for
**usefulness, correctness, reliability, and safe failure**. It
complements the [business case](01-business-case.md) and [solution
architecture](02-solution-architecture.md).

The MVP aims to demonstrate **Stages 1--3** of the AI & Operational
Design Checklist at prototype depth, plus five essential safeguards
normally associated with **Stage 4: Production-Readiness**. Full
production readiness is **not** claimed.

  -----------------------------------------------------------------------
  Stage                               What this MVP addresses
  ----------------------------------- -----------------------------------
  **1 --- Conception & MVP**          Justify AI use and define its role

  **2 --- Solution Design**           Define boundaries, human review,
                                      model responsibilities

  **3 --- Prototype Testing**         Evaluate outputs, test prompts and
                                      guardrails, track basic costs

  **4 --- Production-Readiness        Bounded retries, safe fallbacks,
  (selected controls)**               limits, error notification, basic
                                      logs
  -----------------------------------------------------------------------

**Status key:** *Planned* = specified but not built; *Implemented* =
exists in code; *Tested* = evidence recorded; *Deferred* = future work.
Update statuses as the project progresses.

## 2. AI Design Considerations

  -----------------------------------------------------------------------
  Consideration           MVP decision            Initial status
  ----------------------- ----------------------- -----------------------
  **AI Role**             LLM plans research,     Planned
                          selects permitted       
                          tools, interprets       
                          evidence, and drafts    
                          risk findings and       
                          mitigation options      

  **AI Boundaries**       No invented prices or   Planned
                          sources; no supplier    
                          approval, contract      
                          changes, or purchasing; 
                          no LLM-generated        
                          financial arithmetic    
                          used as authoritative   
                          output                  

  **Evaluation**          Review two reports      Planned
                          against a rubric;       
                          verify calculations and 
                          data rules with         
                          deterministic tests     

  **Failure Modes**       Detect                  Planned
                          irrelevant/outdated     
                          news, unsupported       
                          claims, missing inputs, 
                          API failures, and       
                          invalid report          
                          structure               

  **Guardrails**          Read-only data access,  Planned
                          bounded research,       
                          structured report       
                          schema, source          
                          references,             
                          deterministic           
                          calculations, human     
                          approval                

  **Fallback Strategy**   Retry transient         Planned
                          failures; label         
                          evidence gaps; omit     
                          unsupported estimates;  
                          stop safely if          
                          essential data or model 
                          access is unavailable   
  -----------------------------------------------------------------------

**Responsibility split:** The LLM interprets and synthesizes. Python and
SQL retrieve records, enforce constraints, and calculate scenarios. The
procurement manager reviews recommendations and makes business
decisions.

**Human review:** Every report is advisory. New suppliers are
**unverified candidates** until independently qualified. A report is not
authorization to buy.

## 3. Evaluation criteria

Use two example assessments: **sunflower oil** (primary end-to-end case)
and **one second ingredient if feasible**. Keep a small reference set of
source documents, expected calculations, and known limitations for each.

  -----------------------------------------------------------------------
  Criterion               How to check            MVP acceptance rule
  ----------------------- ----------------------- -----------------------
  **Evidence              Manual review of        Every material external
  traceability**          important claims        claim has a usable
                          against cited           reference and date;
                          URLs/document passages  unsupported claims
                                                  flagged

  **Procurement           Manual review of risks  No clearly irrelevant
  relevance**             against ingredient,     event presented as a
                          geography, and time     key risk
                          horizon                 

  **Financial             `pytest` unit tests     All tested calculations
  correctness**           against hand-calculated match expected values
                          scenarios               

  **Missing-data          Remove a required       No fabricated number;
  honesty**               contract price/forecast unavailable impact or
                                                  explicit failure

  **Supplier screening**  Inspect candidate       Candidates not
                          evidence and labels     described as approved,
                                                  qualified, or
                                                  capacity-confirmed

  **Report completeness** Pydantic output schema  Required sections and
                                                  status fields present

  **Actionability**       Manual                  Recommendations
                          procurement-style       identify rationale,
                          review                  uncertainty, and human
                                                  next step
  -----------------------------------------------------------------------

**Optional LLM-as-a-Judge (Stage 3):** Run an offline judge on the two
reports to assess evidence faithfulness, relevance, and recommendation
quality. Give it the report **and supporting evidence**, require
structured findings, and compare its judgments with manual review. Do
not use judge scores as proof of factual accuracy or as a mandatory
delivery gate in this MVP.

**Cost observation:** Record approximate model usage, API calls, and
elapsed time per report if the selected tools expose them. These are
prototype measurements, not production cost estimates.

## 4. Five MVP safeguards: tools and expected behavior

  ------------------------------------------------------------------------
  Priority          Failure / risk    Implementation     Expected behavior
  ----------------- ----------------- ------------------ -----------------
  **1. External     API timeout, HTTP Python `httpx`     Retry transient
  research          429, temporary    timeouts; bounded  errors (initial
  reliability**     service failure   retries with       call + up to two
                                      `tenacity` or      retries); respect
                                      LangGraph          rate-limit
                                      `RetryPolicy`      guidance; then
                                                         record
                                                         unavailable
                                                         source

  **2. Bounded      Agent loops       LangGraph state    Stop research at
  research**        indefinitely or   counters,          budget; disclose
                    consumes          conditional        unresolved
                    excessive API     routing, maximum   evidence gaps
                    calls             research           
                                      rounds/tool calls  
                                      and execution      
                                      deadline           

  **3.              Missing contract  Read-only SQL,     Never invent
  Internal-data     price, supplier   Python input       missing values or
  validation**      record, or        checks, Pydantic   compute
                    forecast          models; `pytest`   unsupported
                                      for formulas       impact

  **4. Report and   Invalid JSON,     Pydantic report    Deliver only a
  evidence          missing sections, schema;            valid,
  validation**      unsupported       source-reference   appropriately
                    claims            checks; one        qualified report;
                                      bounded            otherwise return
                                      regeneration       a visible failure
                                      attempt            

  **5. Workflow and Agent call fails  n8n **HTTP         Route `completed`
  delivery          or email delivery Request**,         / `partial` /
  reliability**     fails             **IF/Switch**,     `failed`; notify
                                      **Send Email** (or on failure; retry
                                      Slack), **Stop And delivery without
                                      Error**, **Error   rerunning
                                      Trigger**          research
  ------------------------------------------------------------------------

**Implementation notes**

-   Choose **one retry owner per operation**. Avoid stacking LangGraph
    and `tenacity` retries around the same request, which can multiply
    attempts.
-   Retry only transient failures. Invalid credentials, missing business
    data, and unsupported financial inputs are **not** retryable.
-   Use a clear status such as `completed`, `partial`, or `failed`, with
    `warnings` and `missing_data` fields.
-   A **partial** report is acceptable only when the remaining findings
    are supported and the limitations are obvious. If core exposure data
    is unavailable, do not issue a confident financial recommendation.
-   Log failure category and execution ID; never expose API keys or
    confidential payloads in logs.
-   Mock research responses may support repeatable tests, but mock
    results must never be presented as live market intelligence.

## 5. Recommended MVP test cases

  ---------------------------------------------------------------------------
  ID                Test              Expected result       Evidence to save
  ----------------- ----------------- --------------------- -----------------
  **T01**           Valid sunflower   End-to-end report     Example report +
                    oil request       with source           execution trace
                                      references, internal  
                                      exposure, and         
                                      explicit assumptions  

  **T02**           Second ingredient Reuses workflow       Second report +
                    request, if       without hard-coded    trace
                    feasible          sunflower-only        
                                      conclusions           

  **T03**           Relevant          Event included only   Source-to-claim
                    disruption news   with evidence and     review
                                      procurement relevance 

  **T04**           Irrelevant news   Event excluded or     Filter
                    result            labeled irrelevant    decision/log

  **T05**           Research API      Bounded retries;      Retry log
                    timeout           visible warning or    
                                      safe failure          

  **T06**           Missing contract  No invented cost      Test fixture +
                    price             estimate;             report excerpt
                                      `missing_data`        
                                      populated             

  **T07**           Known financial   Python result equals  Passing `pytest`
                    scenario          hand-calculated       output
                                      expected value        

  **T08**           Malformed LLM     Schema rejects it;    Validation error
                    report            bounded retry or      trace
                                      failure               

  **T09**           Unverifiable      Candidate excluded or Candidate
                    supplier          labeled unverified    evidence review
                    candidate                               

  **T10**           Email delivery    Delivery              n8n execution
                    error             retry/notification;   screenshot/log
                                      research not rerun    
  ---------------------------------------------------------------------------

**Minimum demonstration set if time is tight:** **T01, T05, T06, T07,
and T08**. Aim to produce two sample reports; if the second ingredient
is incomplete, describe that limitation explicitly. Test **T10** if n8n
delivery is implemented.

### Test results log

**Superseded by §9 below (updated 11 Nov 2026) — the table that follows is the Day 1 placeholder.**

Complete this table **after** running tests; do not mark planned tests
as passed.

  -----------------------------------------------------------------------
  Test ID           Result (Pass /    Evidence link     Notes / next
                    Fail / Not run)                     action
  ----------------- ----------------- ----------------- -----------------
  T01--T10          Not run           ---               Update
                                                        individually
                                                        during
                                                        implementation

  -----------------------------------------------------------------------

## 6. Four-day execution plan

  -----------------------------------------------------------------------
  Day                     Focus                   Evidence
  ----------------------- ----------------------- -----------------------
  **1 --- Define**        Confirm report          Dataset, source
                          questions, synthetic    inventory, test
                          data, API suitability,  fixtures
                          schemas, and acceptance 
                          tests                   

  **2 --- Build**         LangGraph vertical      First end-to-end
                          slice: external         execution
                          research, SQL/RAG       
                          retrieval,              
                          deterministic           
                          calculations            

  **3 --- Integrate**     Report validation,      Sample report and
                          supplier screening, n8n integration trace
                          trigger/delivery;       
                          optional MCP only if    
                          core flow works         

  **4 --- Evaluate and    Run priority failure    Test log, screenshots,
  harden**                tests, create second    two example reports if
                          report if feasible,     completed
                          document results and    
                          limitations             
  -----------------------------------------------------------------------

Build basic validation and limits **during** implementation; Day 4 is
for verification, not the first time failures are considered.

## 7. Deferred production-readiness work

The following checklist items matter for a real deployment but are **not
claims of MVP implementation**:

-   **Security & access control:** Enterprise identity, granular
    permissions, secrets rotation, and data-governance review.
-   **Observability & logging:** Centralized tracing, monitoring
    dashboards, alert thresholds, incident ownership, and model-quality
    trends.
-   **Asynchronous behavior:** Durable job queues, idempotent retries,
    concurrent execution, and delivery guarantees.
-   **Data drift:** Monitoring changes in commodity sources, procurement
    records, and user questions.
-   **Stress testing:** Load, concurrency, performance, and cost testing
    under realistic usage.
-   **Backup & recovery:** Database backups, workflow recovery, and
    tested restoration procedures.
-   **Collaboration handoffs:** Runbooks, ownership model, and
    engineering/operations handover.
-   **Bias assessment:** No human-profile scoring in this MVP; assess
    supplier-selection fairness and source coverage if supplier ranking
    becomes operational.

**Prompt versioning:** Store prompts in Git from the start. **Model
selection:** Record the actual model and embedding model used once
configured; do not claim a model was evaluated if no comparison was
performed.

## 8. What the portfolio should show

A reviewer should be able to inspect **two sample reports**, a small set
of **test results**, a **workflow diagram**, and a **README** that
explains how to reproduce the prototype. Screenshots and logs should
show at least one safe failure, not only a successful run.

**Bottom line:** The MVP demonstrates a bounded, evidence-aware
procurement research workflow. It does **not** establish real-world
procurement ROI, supplier qualification, or production reliability.

------------------------------------------------------------------------

*Portfolio disclosure: Bakery Foods Inc. and its internal procurement
data are fictional. Proposed safeguards and tests become implementation
claims only after evidence is recorded.*


## 9. Test results log (updated 11 Nov 2026)
Evidence levels: **offline** = automated tests with mocks · **read-only DB** = against the real database without writing · **live** = real services. A pass at one level is not a pass at another.

| ID | Test | Offline | Read-only DB | Live | Evidence / note |
|---|---|---|---|---|---|
| T01 | Valid sunflower-oil request | Pass | Pass (scenario A) | live-002 completed **before** the provenance fixes and RAG; post-fix live run **not run** | `test_agent_graph.py`, `test_demo_scenarios.py`; `runs/live-002.*` |
| T02 | Second ingredient | Not run | Not run | Not run | Wheat is outside the implemented MVP (no benchmark or assumptions) |
| T03 | Relevant disruption news | Pass (fixtures) | n/a | live-002: 6 dated risk items retained, relevance judged leniently by the model (fixed offline, not re-run live) | `test_live002_regressions.py` |
| T04 | Irrelevant news excluded | Pass | n/a | live-002: 6 of 18 results rejected by rule (4 date window, 1 no ingredient match, 1 duplicate) | `test_evidence_validation.py` |
| T05 | Research API timeout/failure | Pass | n/a | live-001: Tavily and GDELT failed (TLS), report degraded to PARTIAL; GDELT HTTP 429 ×2 observed on 11 Nov | `test_search_tools.py`, `runs/live-001.*`, §14 of the implementation plan |
| T06 | Missing contract price | Pass | n/a | Not run | `test_agent_graph.py`, `test_exposure.py` |
| T07 | Known financial scenario (EUR 28,565.04) | Pass | Pass | Figures matched in live-001 and live-002 | `test_exposure.py`, `test_exposure_inputs.py` |
| T08 | Malformed / non-compliant LLM report | Pass (repair, removal, failure) | n/a | Not observed live | `test_agent_graph.py`, `test_evidence_validation.py` |
| T09 | Unverifiable supplier candidate | Pass | n/a | live-002 showed unsupported countries; fixed offline, post-fix live **not run** | `test_live002_regressions.py` |
| T10 | Email delivery error | Not run | n/a | Not run | n8n not built (Day 4) |
| — | Policy retrieval unavailable / fallback | Pass | Pass (empty table reported as limitation) | Not run | `test_policy_graph.py`, `test_policy_db.py` |
| — | Stale benchmark / controlled limitation (scenario B) | Pass | Pass | Not run | `test_demo_scenarios.py`, `test_demo_db.py` |

Counts on 11 Nov 2026 (after policy ingestion): offline **445 passed, 47 skipped** (skipped = opt-in database tests); full suite with `RUN_DB_TESTS=1` (read-only/rolled-back) **492 passed**. Policy RAG results: docs/implementation-plan.md §16.

## 10. Business-requirements evaluation of the mock reports (scenarios A and B)
Criteria are the prototype success criteria of the business case (§5). "Evidence" is what exists today; "Not shown" is stated explicitly.

| Business criterion | Evidence | Not shown / limits |
|---|---|---|
| Complete research-to-report workflow | Scenarios A and B run end to end on real internal data; live-002 completed an earlier version | The current code (provenance fixes + RAG) has **not** run live |
| Trace material claims to sources; separate facts from assumptions | Findings declare `basis`; external claims carry verbatim quotes from direct trade-press sources; assumptions and the crude-vs-refined caveat are explicit; independent audit Q1/Q2/Q9 pass | Real model drafts may fail the strict rules and end `partial`; quote relevance is checked by wording overlap, not by understanding |
| Retrieve relevant internal records | SQL exposure table, supplier approval statuses (SUP-003 conditional), contract end dates; policy passages retrieved per theme (mock embeddings) | Policy chunks are not in the database; real embedding retrieval untested |
| Reproducible financial calculations | Deterministic, hand-checked, identical across runs; audit Q7 recomputes independently | Scenario-level procurement exposure only; benchmark is crude oil, not the refined contract price |
| Avoid inventing prices, qualifications or missing inputs | Scenario B withholds every euro figure; unsupported country/product stay `unknown`; leads are UNVERIFIED; unsupported claims are removed visibly | Only structure and provenance are verified |
| Handle failures visibly | `partial` / `failed` states, warnings, missing data, GDELT diagnostics, research gate | n8n routing not built |
| Two reviewable example reports | Scenarios A and B (mock labelled) | Live reports need the approvals in `docs/demo-scenarios.md` |

**Reviewer observations (honest quality notes).** (1) Recommendations are traceable but generic ("review coverage", "verify leads"): that is partly the template drafter, and real-model usefulness is unmeasured. (2) Reports are long (about 100 lines); the policy section lists every retrieved passage, which could be limited to cited ones. (3) In scenario B the engine withholds coverage **tonnes** as well as euros although coverage needs no price; reporting tonnes without euros would be more useful (not changed: the calculation module was approved as is). (4) POL-002's coverage objective is qualitative ("a majority"), so the agent cannot and does not claim it is breached. (5) The benchmark goes stale on 16 Nov unless newer EC data is loaded.

**What the 10/10 audit does and does not prove.** It re-derives provenance and consistency properties from the finished report with code independent of the validator, and it is shown to fail for each planted defect (`test_quality_audit.py`). It does **not** prove that real LLM-generated recommendations are insightful, correctly prioritised or useful to a procurement manager; that requires human review of live output, for which a short rubric is: *Is the top risk the right one? Is each recommendation actionable and proportionate to the exposure? Is anything missing a manager would expect? Would you trust it enough to act on after checking the cited sources?* (score 1–5, with comments).

## 11. Live assessment live-003 - summary
See docs/implementation-plan.md section 17 for the full record: PARTIAL, audit 10/10, financials unchanged, policy citations verified, qualitative defects listed (price-direction framing, lost escalation action, no supplier lead, invented cadence).

## 12. Corrections after live-003 (offline)
See docs/implementation-plan.md section 18. Offline 468 passed / 47 skipped; database tier 515 passed; not re-run live.

## 13. Policy consistency correction (POL-001 / ASM-003)
See docs/implementation-plan.md section 19. Offline 473 passed / 47 skipped; database tier 520 passed; 2 chunks re-embedded (313 tokens); no live run.

## 14. Final corrections after live-004
See docs/implementation-plan.md section 21 and docs/day3-checkpoint.md (section H). Offline 490 passed / 47 skipped; database tier 537 passed; not re-run live.
