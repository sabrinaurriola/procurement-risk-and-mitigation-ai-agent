# Day 4 — Architecture, decisions and implementation plan

**Document status:** design record for Day 4. It supersedes the Day 4 parts of `docs/api-specification.md` and `docs/n8n-workflow-blueprint.md` where they differ (those files were aligned to it). `docs/day3-checkpoint.md` is the **unchanged historical Day 3 snapshot** (its section L still says "nothing committed, HEAD `f7f672d`"; that was true on 11 Nov and is no longer: see section 2).

**Status words used strictly.** **Planned** = designed, no code. **Implemented** = code exists. **Verified** = implemented and demonstrated by recorded test output or a recorded real-process run. Nothing here is "verified" without a listed result.

## 1. Scope of Day 4
Wrap the LangGraph agent (the only analysis component) for operation: **FastAPI** (assessment interface, idempotency, operational events), **n8n** (intake, routing, coordination), **Supabase** `ops.operational_events` (structured operational log, isolated from procurement data), **Notion** `Assessment Reports` (full archived report + human-review status), **Gmail** (notification with status and Notion link). Decision support only; every report needs human review. No analysis, LLM call or database access in n8n.

## 2. Baseline (verified 11 Nov 2026)
| Item | Result |
|---|---|
| Git | `d1f527f` = Day 3 work (committed after the Day 3 closure); `bc875b2` = demo-001 fix, committed separately (see below). S1 is committed as its own commit (see `git log`) |
| Tests before S1 | `pytest -q`: **491 passed, 47 skipped** (490 + the demo-001 regression test) |
| Mock demonstration | `python -m src.agent.demo all --mock --real-policy`: scenario A COMPLETED, scenario B PARTIAL, independent audit 10/10 each |
| demo-001 (live, FAILED) | `REPORT_VALIDATION_FAILED`: four prunable item problems plus a wording problem in the LLM's executive summary, which cannot be fixed by removing an item although `_finalize` replaces that summary with a code-built one. Fix (`bc875b2`, `src/agent/validation.py` `prune_unsupported`): summary wording problems no longer fail the run; a missing/too-short summary still blocks. Test: `test_bad_llm_summary_wording_never_fails_the_run_after_repair`. **Not re-verified in a live run** |
| demo-002 (live, PARTIAL) | Report produced. PARTIAL because GDELT returned HTTP 429 twice and four items were removed (falling-price risk without a direct citation; lead-verification action with an unrelated quote; supplier lead *Elitagro* whose quote did not show refined food-grade oil; a mitigation that addressed the removed risk R4). 72.2 s, 22,053 in / 7,224 out tokens, estimated ≈ USD 0.049 |

## 3. Decisions (review of 11 Nov, conditional approval)
| ID | Decision | Status |
|---|---|---|
| D1 | Three separate statuses (assessment, archive, delivery) plus `run_state`; Notion is the dedupe record for archive/delivery; no composite status | Approved |
| D2 | FastAPI is the single operational-event writer; n8n reports its outcomes to FastAPI, never to the database | Approved (design) |
| D3 | n8n runs locally (e.g. `npx`), not in Docker | Approved (not installed) |
| D4 | Migration `002` design | Approved as design only. **Not applied; gate G-OPS (section 8)** |
| D5 | Dedicated `procurement_reader` role | **Deferred until after the demo** |
| D6 | Report tables rendered as code blocks in Notion v1 | Approved |
| D7 | GDELT is optional when other validated evidence suffices; its failure stays visible; material claim removals still PARTIAL | Approved (design). **Not implemented**; see section 12 |
| D8 | Gmail: draft first, send only after approval; one fixed recipient | Approved |
| D9 | `CLAUDE.md` updated for the ops-event writer exception and the new integrations | Done in S0 |
| D10 | Commit the demo-001 fix separately | Done (`bc875b2`) |

## 4. Architecture
```
 n8n Form ► n8n Set(request_id) ► POST /assessments ─────────────► FastAPI ► worker thread ► LangGraph run_assessment
 (intake)                         (X-API-Key, wait ≤150 s)           │  │                       (read-only DB, Tavily, GDELT, OpenAI)
                                                                     │  ├─ file store runs/assessments/  ◄── AUTHORITATIVE record + run marker
                                                                     │  └─ EventSink ► Supabase ops.operational_events (INSERT-only role; outbox file) [planned]
 200 finished | 504/409/transport ► bounded GET polling ◄───────────┘
 completed|partial ► Notion archive (full report) ► Gmail notification ► events
 failed            ► NO Notion row ► operator Gmail notice (error code only) ► events
```
| Component | Owns | Does not |
|---|---|---|
| Python/LangGraph | analysis, validation, assessment status, report | send or store externally |
| FastAPI | request validation, auth, idempotency, single-flight, deadline, authoritative record, event derivation/writes, Notion block rendering | business content, calling Notion/Gmail |
| n8n | intake, request_id, routing on `assessment_status`, bounded polling, Notion/Gmail calls, delivery retries, operator alert | analysis, LLM, DB access, **starting a second assessment** |
| Supabase `ops` | append-only operational log | reports, evidence, prompts |
| Notion | human-readable archive, human-review status, archive/delivery dedupe record | feeding anything back into the agent |
| Gmail | notification (status, counts, link) | carrying report text, recipients from input |

## 5. Statuses (never merged)
| Layer | Values | Owner |
|---|---|---|
| **`run_state`** (execution of a request) | `running` · `finished` | FastAPI. A request that is running has **no** assessment status |
| **`assessment_status`** (result of a finished run) | `completed` · `partial` · `failed` (the existing `status`, kept as an alias for n8n) | FastAPI record (authoritative) |
| **`archive_status`** (Notion; completed/partial only) | `pending` · `incomplete` · `stored` · `failed` | n8n → Notion property + event |
| **`delivery_status`** (Gmail; also the operator notice for failed runs) | `pending` · `sent` · `failed` | n8n → Notion property (where a row exists) + event |
| `human_review` (Notion only) | Pending review · Accepted · Needs changes · Rejected · Not applicable | a person; nothing reads it automatically |
A `failed` assessment is **never** archived in the normal Notion report database.

## 6. FastAPI execution model — IMPLEMENTED (S1)
* The assessment runs in a **server-owned worker thread**; the HTTP handler only waits (≤ `ASSESSMENT_DEADLINE_SECONDS`, default 150). A deadline hit returns **504 `ASSESSMENT_TIMEOUT` meaning "still running"**; the worker is never cancelled, the record is never marked failed because of a timeout, and nothing is re-run.
* While running: `GET /assessments/{id}` → `202 {"run_state":"running","assessment_status":null,"started_at":…,"overdue":bool}` + `Retry-After`. A repeated `POST` (same id, same hash) → `409 ASSESSMENT_IN_PROGRESS`; same id, other parameters → `409 REQUEST_ID_CONFLICT`; another id while one runs → `429 BUSY`.
* The finished record is written **atomically** (temp file + fsync + `os.replace`, bounded retry on the transient Windows sharing violation) **before** the worker is released, so a poller sees either `202` or the complete result. If the file write fails the record is served from memory until restart (logged); a restart then reports the request as interrupted, which is true.
* **Restart recovery:** a `{id}.run.json` marker with no live worker in this process means the previous process died mid-run. The request becomes a stored `failed` record with `ASSESSMENT_INTERRUPTED`, never re-run (paid); a new `request_id` is required. This is decided only when the worker is known to be dead, never on a timeout. Detection happens at the next access of that id.
* **Idempotency:** `request_id` + SHA-256 of `ingredient, horizon_months, assessment_date, mode`. Same id + hash after completion = replay of the stored envelope (`idempotent_replay:true`, zero tool/LLM calls, also after a restart); a stored `failed` is replayed too.
* **Safe errors:** every error is `{"request_id","run_state","assessment_status":null,"error":{"code","message"}}` with fixed messages. No paths, hosts, credentials or exception text (the worker drops exception messages and logs only the class).
* **Auth:** `X-API-Key` (constant-time compare); the app refuses to start without `ASSESSMENT_API_KEY`; `/health` is open and returns only `status, version, live_enabled, db`. Docs/OpenAPI endpoints are disabled.
* **Live mode:** the gate exists (403 without `ALLOW_PAID_CALLS=1`) but live is **not wired** in S1 (501 `LIVE_NOT_AVAILABLE` even with the flag). Wired and approved in S7.
* **Database:** the mock runner reads real data through the existing read-only session; the API layer writes nothing to the database.
* **S1 limitations (honest):** one process only (the single-flight registry is in memory; do not run several workers); no request-body size limit or rate limiting (local, keyed); the in-memory finished cache and the store are not pruned; a sync endpoint holds one worker-pool thread for up to the deadline; interrupted runs are detected lazily; `notion-blocks` and the n8n event-relay endpoint are not built.

## 7. n8n behaviour (PLANNED — nothing here is built or verified)
* **Form Trigger acknowledgement — pending integration verification.** Assumption: the Form Trigger can respond immediately ("form submitted") while the execution continues and holds the synchronous `POST` (≤180 s). **Not proven.** Fallbacks: respond when the workflow finishes with a short confirmation page, or Webhook + Respond to Webhook. Verified only in S4.
* **Bounded polling after an uncertain POST (never re-POST).** POST outcomes → action: `200` → proceed; `504 ASSESSMENT_TIMEOUT`, `409 ASSESSMENT_IN_PROGRESS` or a transport timeout → enter the poll loop. Loop: Wait 10 s → `GET /assessments/{id}`; `202` → continue; `200` → proceed on `assessment_status`; GET `5xx`/transport error → counts as a failed poll (max 3 consecutive). **Limit: 24 polls (about 4 minutes) after the POST wait.** Exhausted while still `202` (or `overdue:true`) → operator e-mail "still running, request_id X"; the workflow ends; the operator redelivers later by submitting the same `request_id` (GET only). A `404` after a transport error means the POST may never have registered: **operator alert, no automatic re-POST**; resubmitting the same `request_id` is safe because the API deduplicates. Workflow execution timeout ≥ 15 minutes. `409 REQUEST_ID_CONFLICT`, `429 BUSY`, `401`, `422`, `403` are not polled: they alert/notify immediately.
* **Failures of archive/delivery never create a new assessment.** After the first successful POST every branch uses GET only. Redelivery = intake with an existing `request_id` → `IF request_id supplied` → `GET` (404 ⇒ stop with a message; never fall back to POST).

## 8. Operational events and the restricted writer
**Implemented (S1):** the taxonomy, the Pydantic validator (`src/api/events.py`: allow-listed types and `detail` keys, `detail` ≤ 4,096 bytes), the `EventSink` interface, `NullEventSink`, `InMemoryEventSink`, and deterministic derivation of events from the finished envelope. **No database writer exists; migration `002` is not applied; no role or table was created.**

**Event types:** `assessment.received|replayed|rejected|timeout|interrupted|finished`, `research.source` (one per source), `validation.removed`, `archive.stored|failed`, `delivery.sent|failed`. `assessment.rejected` is defined but not yet emitted (S1 returns 422 before a record exists).

**Every metric maps to a field that exists. Nothing is invented:**
| Event field | Source in the code | Notes |
|---|---|---|
| `assessment_status`, `error_code`, `severity` | envelope `status`, `error.code` | severity: failed=error, partial=warning, completed=info |
| `mode` | envelope `execution.mock` | |
| `duration_ms` | `execution.elapsed_seconds` × 1000 (graph clock); for `assessment.timeout` the API's own wall-clock wait | |
| `input_tokens`, `output_tokens` | `execution.tokens` | **LLM tokens only.** Embedding tokens go to `runs/usage.jsonl` and are not in the envelope, so they are not reported here |
| `cost_usd` | `estimate_cost(model, tokens)` from `src/agent/run.py` (`PRICE_PER_M`, only `gpt-5.4-mini` is priced) | An **estimate**, not an invoice; `NULL` for mock runs, unknown models or failed runs; `detail.cost_estimated` says which |
| `llm_calls`, `tool_calls`, `research_rounds` | `execution.*` | in `detail` |
| `research.source` rows (`source`, `status_class`, `status`) | `report.source_status` | one row per source; absent when the run failed without a report |
| `validation.removed` (`count`, `kinds`) | `report.removed_claims` | **counts and kinds only, never claim text** |
| `benchmark_status`, `financial_status`, `warnings`, `missing_data` | `report.benchmark.status`, `report.financial.status`, counts of envelope lists | |
| `archive_status`, `delivery_status`, `notion_page_id`, `report_sha256` | reported by n8n through the (planned) relay endpoint | |
Never stored: report text, evidence, prompts, e-mail addresses, exception text, credentials.

**Migration `002` — reconciled draft (design only; to be written as `sql/002_ops_events.sql` in S2 and reviewed before any dry-run).** Effect if later approved: creates schema `ops`, one table, one index, role `ops_writer`; records version `002` in `procurement.schema_migrations`; touches no `procurement` table.
```sql
BEGIN;
CREATE SCHEMA IF NOT EXISTS ops;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='ops_writer') THEN
  CREATE ROLE ops_writer LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;   -- no password in Git; set out-of-band
END IF; END $$;
REVOKE ALL ON SCHEMA ops FROM PUBLIC;
CREATE TABLE ops.operational_events (
  event_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_key         text NOT NULL UNIQUE CHECK (length(event_key) BETWEEN 1 AND 200),
  occurred_at       timestamptz NOT NULL DEFAULT now(),
  request_id        text NOT NULL CHECK (request_id ~ '^[A-Za-z0-9_-]{1,64}$'),
  component         text NOT NULL CHECK (component IN ('api','n8n')),
  event_type        text NOT NULL CHECK (event_type IN ('assessment.received','assessment.replayed','assessment.rejected','assessment.timeout',
                      'assessment.interrupted','assessment.finished','research.source','validation.removed',
                      'archive.stored','archive.failed','delivery.sent','delivery.failed')),
  severity          text NOT NULL DEFAULT 'info' CHECK (severity IN ('info','warning','error')),
  mode              text CHECK (mode IN ('live','mock')),
  assessment_status text CHECK (assessment_status IN ('completed','partial','failed')),
  archive_status    text CHECK (archive_status IN ('pending','incomplete','stored','failed')),
  delivery_status   text CHECK (delivery_status IN ('pending','sent','failed')),
  error_code        text CHECK (error_code ~ '^[A-Z_]{1,64}$'),
  duration_ms       integer CHECK (duration_ms >= 0),
  input_tokens      integer CHECK (input_tokens >= 0),
  output_tokens     integer CHECK (output_tokens >= 0),
  cost_usd          numeric(10,6) CHECK (cost_usd >= 0),              -- estimate; NULL when unknown
  detail            jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(detail)='object' AND octet_length(detail::text) <= 4096)
);
CREATE INDEX ON ops.operational_events (request_id, occurred_at);
ALTER TABLE ops.operational_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY ops_writer_insert ON ops.operational_events FOR INSERT TO ops_writer WITH CHECK (true);
GRANT USAGE ON SCHEMA ops TO ops_writer;
GRANT INSERT ON ops.operational_events TO ops_writer;               -- no SELECT/UPDATE/DELETE/TRUNCATE; nothing on schema procurement
INSERT INTO procurement.schema_migrations (version) VALUES ('002');
COMMIT;
```
**Reconciliation with the code (S2 acceptance):** the `event_type`, status and `component` lists above must equal the Python `Literal`s in `src/api/events.py` — a test must parse the SQL file and compare; every column maps to an `OperationalEvent` field (`detail` keys are validated in Python, not in SQL). `skipped` was removed from archive/delivery statuses (failed assessments are not archived).

**Gate G-OPS (all must pass before the database writer is used; any failure keeps `NullEventSink`/outbox):** (1) SQL, grants and RLS reviewed and approved by the user; (2) `python -m src.db.migrate` dry-run shows exactly this SQL; (3) separate `--apply` approval; (4) the user sets the `ops_writer` password out-of-band and names the connection `OPS_DATABASE_URL`; (5) post-apply checks, each with output kept (class/SQLSTATE only, never hostnames): connect as `ops_writer` through the pooler (the login name format for custom roles differs — to verify); INSERT succeeds; duplicate `event_key` is a no-op (`ON CONFLICT DO NOTHING`, or catch SQLSTATE `23505` if Postgres requires SELECT); `SELECT` on events denied (`42501`); any access to `procurement` denied; `UPDATE/DELETE/TRUNCATE` denied; each CHECK rejects a bad row; (6) failure of the sink never changes an assessment (already tested in S1 with a broken sink). Rollback, only with approval: `DROP SCHEMA ops CASCADE; DROP ROLE ops_writer; DELETE FROM procurement.schema_migrations WHERE version='002';`.

## 9. Notion `Assessment Reports` (PLANNED)
Created manually by the user; n8n creates/updates pages only. Properties: `Name`, `Request ID` (dedupe key), `Ingredient`, `Horizon (months)`, `Assessment date`, `Mode`, `Assessment status` (completed/partial only), `Archive status`, `Delivery status`, `Human review`, `Warnings`, `Claims removed`, `Report SHA-256`, `Total blocks`, `Schema version`, `Reviewer notes`.

**MVP acceptance criterion: the FULL report is archived in Notion, formatting may be simplified (tables as code blocks, flat blocks only). Never truncated.** If the whole report cannot be stored, `Archive status = incomplete`, the e-mail says so and attaches `report.md`; the demo is not "done" without one completely archived report.

**Rendering:** a planned FastAPI endpoint `GET /assessments/{id}/notion-blocks` returns `{report_sha256, total_blocks, blocks[]}` from the stored record, rendered by a deterministic, offline-tested function: flat top-level blocks only (no nested children), each rich-text ≤ 2,000 characters, appended in batches of ≤ 100. Limits and rate behaviour are **assumptions to verify against Notion's documentation in S3/S4.** demo-002 (23 KB, 153 lines, 21 headings, 22 table rows) is the sizing reference.

**Recovery from an interrupted upload without duplicate pages or duplicated content:**
1. Query by `Request ID` first. Found → reuse that page; several found (race) → use the oldest and record `duplicate_pages` in the event. Not found → create the page **with properties and no body**, `Archive status = incomplete`, `Report SHA-256` and `Total blocks` set from the endpoint.
2. Ground truth is the page itself: n8n counts the page's existing top-level children (`existing`) and appends only `blocks[existing:]` in batches. A crash between an append and any property update therefore cannot duplicate content, because the count is read from Notion, not from a counter.
3. Before resuming, the page's `Report SHA-256` must equal the endpoint's; a mismatch is a hard error (`archive.failed`), never a silent mix.
4. Completion: children count = `Total blocks` **and** SHA matches → `Archive status = stored` + `archive.stored`. Otherwise it stays `incomplete` and `archive.failed` is emitted after bounded retries (3, backoff, honouring `Retry-After`).
5. Re-running the workflow with the same `request_id` repeats steps 1–4 and is idempotent. No step ever calls `POST /assessments`.

## 10. Gmail (PLANNED)
One fixed recipient configured in n8n (never from a request or the report). **Draft first; send only after approval.** Subject `[MOCK|LIVE] Procurement risk assessment — {STATUS} — {request_id}`. Body built only from sanitised envelope fields: status and its meaning, ingredient/horizon/date, warning count and first three warning codes, claims-removed count, "human review required — decision support only", Notion link, `request_id`. `failed`: operator notice with `error.code` and the fixed message, no report link. Archive-failed fallback: attach `report.md`, banner "archive incomplete". Delivery dedupe: Notion `Delivery status = sent` is checked before sending and set immediately after (residual window: a crash between send and update may duplicate one e-mail). Google OAuth apps in "Testing" status expire refresh tokens after about seven days (to verify).

## 11. Security boundaries
Secrets only in `.env` / n8n credentials; names only in docs. FastAPI holds `DATABASE_URL` (read-only use), `OPENAI_API_KEY`, `TAVILY_API_KEY`, `ASSESSMENT_API_KEY`, `ALLOW_PAID_CALLS`, and (S2, not yet) `OPS_DATABASE_URL`. n8n holds the FastAPI key, a Notion integration token shared with only the one database, and Gmail OAuth; it never holds database, OpenAI or Tavily credentials. FastAPI binds to 127.0.0.1. `mode` comes from a workflow constant (default `mock`); the form cannot choose it. Report text derived from web content is plain text in Notion and never interpolated into e-mail HTML or SQL. Mock output is labelled everywhere (`Mode`, `[MOCK]`, `mode` column).

## 12. Report-quality gate before the live integrated test (G-QUALITY)
Regression coverage was inspected on 11 Nov; **gaps are listed, not silently closed**:
| Area | Existing coverage | Gap before S7 |
|---|---|---|
| Unsupported supplier product claims (crude vs refined, vague quotes) | `tests/test_evidence_validation.py` (product-type cases), `test_live002_regressions.py`, `test_quality_audit.py` Q3 — the validator removed *Elitagro* in demo-002 as designed | prompt guidance so the model does not propose `refined_food_grade` without a quote that says it; a regression from the demo-002 shape |
| Removed-risk dependencies | `test_evidence_validation.py` ("addresses unknown risk"); the live-004 regression covers removed supply risk in the summary | an explicit prune-cascade test: removing a risk removes the mitigations/actions that address only it, visibly and in one pass sequence |
| Optional-GDELT rule (D7) | **none; not implemented.** Today any `RESEARCH_SOURCE_*` problem makes the run PARTIAL | implement + test: GDELT failure stays visible in warnings, events, Notion and e-mail but is not by itself PARTIAL when Tavily is OK and no `NO_RELEVANT_EVIDENCE`/`NO_STRONG_EXTERNAL_EVIDENCE` condition exists; **any claim removal, Tavily failure, stale/unavailable benchmark or unavailable financials still ⇒ PARTIAL**; `docs/api-specification.md` §5 updated at that time |
Material report-quality limitations must still produce PARTIAL. This slice (S6) needs its own approval and must pass before S7.

## 13. Failure handling
| Failure | Handling |
|---|---|
| Invalid input | n8n rejects before the call or the API returns 422; no record |
| `failed` assessment | no Notion row; operator notice with the error code; event logged by the API |
| API unreachable/timeout | bounded GET polling (section 7); never a second POST; operator alert if exhausted |
| Event database down | never alters an assessment; planned outbox file; flush tool deferred |
| Notion failure | bounded retry → `archive.failed` → e-mail still sent with `report.md` and an "archive incomplete" banner |
| Gmail failure | bounded retry → `delivery.failed` → Notion `Delivery status = failed`; n8n execution list is the last-resort alert channel |
| Process restart mid-run | stored `failed` / `ASSESSMENT_INTERRUPTED`; new `request_id` required |

## 14. Implementation slices
| Slice | Content | Status | Needs approval |
|---|---|---|---|
| **S0** | Baseline, demo-001 fix commit, documentation | **Done and verified** (this document; `bc875b2`) | — |
| **S1** | FastAPI mock service | **Implemented and verified** (section 15); committed separately | — |
| **S2** | `sql/002_ops_events.sql` draft → review → gate G-OPS → `PostgresEventSink` + outbox + rollback-only DB tests | Planned | DDL, role, `OPS_DATABASE_URL` |
| **S3** | `notion-blocks` renderer and endpoint, `POST /assessments/{id}/events` relay, resume logic tests (offline) | Planned | — |
| **S4** | n8n install, workflow, Notion/Gmail credentials (by the user), draft-mode e-mail | Planned | install, connections, first send |
| **S5** | Mock end-to-end N01–N10 incl. polling, Notion interruption/resume, Gmail down, redelivery | Planned | e-mails |
| **S6** | Report-quality gate (section 12) | Planned | yes |
| **S7** | One controlled live run through the API; human review in Notion | Planned | paid calls |
| Post-demo | D5 reader role; further hardening | Deferred | DDL |
If time runs short drop, in order: post-demo items, Notion body formatting niceties (never the full-report requirement), n8n-relayed events (keep API events). Never drop S1, mock end-to-end or status routing.

**Acceptance criteria (per slice).** S1: see section 15. S2: dry-run shows exactly the reviewed SQL; all G-OPS checks pass with recorded output; SQL ⇔ Python enumerations test passes; DB-down writes an outbox line and changes no assessment. S3: renderer deterministic; every block ≤ 2,000 chars and ≤ 100 per batch; demo-002 and both mock reports render with no content loss (text round-trip); resume test appends only the missing blocks. S4/S5: completed/partial each give one Notion page, correct statuses and one e-mail; failed gives no Notion row and one operator notice; redelivery sends no duplicate; interrupted upload resumes without duplicate blocks; no automatic second POST in any branch (asserted on the API call log). S7: one live run with honest status (PARTIAL acceptable), cost logged in `runs/usage.jsonl`, human review recorded.

## 15. S1 implementation and verification (11 Nov 2026)
**Files (new unless noted):** `src/api/{__init__,models,store,events,runners,service,app}.py`; `tests/{api_fixtures,test_api,test_api_concurrency,test_api_events}.py`; `requirements.txt` (modified: `fastapi==0.162.4`, `uvicorn==0.54.0` pinned from the installed versions).
**Endpoints implemented:** `GET /health`, `POST /assessments`, `GET /assessments/{request_id}`, `GET /assessments/{request_id}/report.md`. **Not implemented:** `GET …/notion-blocks`, `POST …/events`.
**Run it (mock mode, local):** `ASSESSMENT_API_KEY` must be set in the environment or `.env`; `python -m uvicorn src.api.app:create_app --factory --host 127.0.0.1 --port 8000` (one worker).
**Test results:** full offline suite **561 passed, 47 skipped** (491 baseline + 70 new: 46 contract/validation/auth/safe-error/persistence in `test_api.py`, 9 concurrency/deadline/restart over real HTTP in `test_api_concurrency.py`, 15 event tests in `test_api_events.py`). No paid call, no database write.
**Real-process run (uvicorn subprocess, mock mode, real read-only database data, no paid call):** health `db: ok`; no key → 401; wrong key → 401; invalid ingredient → 422; `mode=live` → 403; mock POST → 200 `completed` in 1.8 s (graph 0.88 s, report 28,046 chars, baseline EUR 1,961,496.55); replay → 200 `idempotent_replay:true` in 0.01 s with an identical body; changed parameters → 409 `REQUEST_ID_CONFLICT`; while one run was in flight: duplicate → 409 `ASSESSMENT_IN_PROGRESS`, other id → 429 `BUSY`, GET → 202 running; after restart: replay and GET served from the store; with a 0.5 s deadline: 504 `ASSESSMENT_TIMEOUT` after 0.5 s, GET 202, re-POST 409, later GET 200 `completed`, replay 200; process killed mid-run: marker left on disk, after restart GET → `failed`/`ASSESSMENT_INTERRUPTED`, re-POST replays it, no re-run.
**Defect found and fixed during S1:** on Windows `os.replace` raised `PermissionError` while another thread had the destination open; a bounded retry (10 × 20 ms) was added to the store's write and read paths (test: a reader never sees a partial record; three consecutive passing runs).

## 16. Implemented / planned / verified summary
| Capability | Planned | Implemented | Verified |
|---|---|---|---|
| FastAPI core (health, POST, GET, report.md, auth, validation, safe errors) | ✔ | ✔ | ✔ tests + real process |
| Idempotency, single-flight, atomic persistence, restart replay, deadline/recovery, interruption | ✔ | ✔ | ✔ tests + real process |
| Event taxonomy, validation, derivation, in-memory sink | ✔ | ✔ | ✔ tests |
| `ops.operational_events` table, role, DB writer, outbox | ✔ | ✘ | ✘ |
| Notion renderer / archive / resume | ✔ | ✘ | ✘ |
| n8n workflow, polling, Form Trigger behaviour | ✔ | ✘ | ✘ (assumption pending S4) |
| Gmail notification | ✔ | ✘ | ✘ |
| Live mode through the API | ✔ | ✘ (501) | ✘ |
| Optional-GDELT rule (D7), prune-cascade test, product-type prompt guidance | ✔ | ✘ | ✘ |
| demo-001 fix in a live run | — | ✔ (`bc875b2`) | offline test only |

## 17. Risks and assumptions to verify
Notion block/length/rate limits and children-count behaviour; Form Trigger immediate response; Google OAuth testing-mode token expiry; pooler login format for custom roles; `ON CONFLICT DO NOTHING` under INSERT-only privileges; time (demo Fri 13 Nov); live COMPLETED has never been observed (PARTIAL likely); benchmark stale from 16 Nov (19 days old on 13 Nov, inside the 21-day limit); the S1 service is single-process; S2 work (draft SQL, model alignment, documentation) is uncommitted until approved.
