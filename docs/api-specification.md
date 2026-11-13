# Assessment API — Contract between n8n and the LangGraph service (Day 4 implementation specification)

**Status (11 Nov 2026):** the FastAPI service is **implemented in mock mode (S1) and verified** (tests and a real-process run recorded in `docs/day4-architecture.md` §15): `GET /health`, `POST /assessments`, `GET /assessments/{request_id}`, `GET /assessments/{request_id}/report.md`, authentication, validation, idempotency, single-flight, atomic persistence, restart handling, deadline/recovery and safe errors. **Planned, not built:** `GET …/notion-blocks`, `POST …/events` (n8n outcome relay), the database event writer, live mode (currently refused), the n8n workflow. Design record and decisions: `docs/day4-architecture.md`. Dependencies `fastapi`, `uvicorn` are installed and pinned in `requirements.txt`.

## 1. Responsibilities
| Python service (LangGraph + FastAPI) owns | n8n owns |
|---|---|
| Request validation (authoritative), ingredient and date rules, authentication | Intake (Form Trigger), generating a unique `request_id`, a first-line check for friendly errors |
| All data access, research, calculations, RAG, LLM calls, evidence rules, report validation | **No analysis, no LLM, no research, no calculations, no database access** |
| Status decision (`completed` / `partial` / `failed`), warnings, missing data, limitations | Routing on `assessment_status`; Notion archive; e-mail text; operator alert via a separate Error Trigger workflow |
| Retries of research tools and LLM calls (one owner per operation) | **No automatic re-POST of an assessment, in any branch.** Archive/delivery retries use GET only; a delivery failure never re-runs research |
| Idempotent, atomic, **authoritative** result storage; duplicate protection; deadline; operational events; logging with `request_id` | Waiting for the response; bounded GET polling after an uncertain POST |

## 2. Endpoints (local, bound to 127.0.0.1; header `X-API-Key`, value from `ASSESSMENT_API_KEY`; the app refuses to start without it)
| Method and path | Purpose | Status |
|---|---|---|
| `GET /health` | `{"status":"ok","version":…,"live_enabled":bool,"db":"ok"\|"error"}` — no key required; read-only `SELECT 1`; never returns connection details | Implemented, verified |
| `POST /assessments` | Run (or replay) one assessment. **Synchronous wait** up to the deadline; envelope in §4 | Implemented, verified |
| `GET /assessments/{request_id}` | `200` stored envelope · `202` still running · `404` unknown | Implemented, verified |
| `GET /assessments/{request_id}/report.md` | The report as `text/markdown`; `404 REPORT_NOT_AVAILABLE` for a failed run; `202`/`404` as above | Implemented, verified |
| `GET /assessments/{request_id}/notion-blocks?offset=0&limit=100` | Deterministic flat Notion-block rendering of the stored report, paginated (`limit` 1–100, `offset` ≥ 0, else `422`). Body: `report_sha256` (SHA-256 of the **exact** `report.md` bytes, never altered), `canonical_sha256` (what the blocks reproduce: layout-normalised, see `src/api/notion_blocks.py`), `blocks_sha256`, `total_blocks`, `offset`, `limit`, `count`, `next_offset`, `page{title, properties}` (no report text), `blocks[]`. Every text object ≤ 2,000 characters, ≤ 100 per block. `202` while running, `404 REPORT_NOT_AVAILABLE` for a failed/unknown record | **Implemented, tested offline (S3)** |
| `POST /assessments/{request_id}/notion-verify` | Read-only. Body `{"children":[…]}` = the blocks currently on the Notion page. Returns `prefix_ok`, `mismatch_index`, `existing`, `total_blocks`, `complete`, `next_offset`: the existing blocks must equal the expected rendering **by type and text, index by index** (counting alone is not accepted). Never starts or changes an assessment | **Implemented, tested offline (S3)** |
| `POST /assessments/{request_id}/events` | n8n reports `archive.*` / `delivery.*` outcomes; allow-listed; `404` if the id is unknown | **Planned (S3)** |
OpenAPI/docs endpoints are disabled. Run with **one** worker: the single-flight registry is in memory.

## 3. Request — `POST /assessments`
```json
{ "request_id": "demo-001", "ingredient": "sunflower_oil", "horizon_months": 3,
  "assessment_date": "2026-11-20", "mode": "mock" }
```
| Field | Rule |
|---|---|
| `request_id` | Required. `^[A-Za-z0-9_-]{1,64}$`. **Idempotency key** (also the file name, hence the strict pattern). |
| `ingredient` | Required. Currently only `sunflower_oil` (anything else → `422 INVALID_REQUEST`). |
| `horizon_months` | Required, integer 1–6. Months are the full calendar months after the assessment month. |
| `assessment_date` | Optional ISO date, default today. Allowed from 365 days before to 90 days after today. Adds the warning `ASSESSMENT_DATE_OVERRIDE`. Data freshness is judged against this date. |
| `mode` | Optional, **`live` is the default**, or `mock`. `mock` uses fixtures and is labelled `MOCK RUN` everywhere (real read-only database data, fixture research and drafting). `live` needs the server flag `ALLOW_PAID_CALLS=1`; otherwise `403 LIVE_NOT_AUTHORISED` and no external call, and an omitted `mode` is therefore refused on a default server. **In this build live is not wired even with the flag: `501 LIVE_NOT_AVAILABLE`.** |
| unknown fields | Rejected (`extra="forbid"`). |

## 4. Response envelope
A finished request always returns this shape (HTTP 200 unless stated; **n8n branches on `assessment_status`, not on the HTTP code**):
```json
{ "schema_version": "1.1", "request_id": "demo-001", "assessment_date": "2026-11-20",
  "run_state": "finished", "assessment_status": "partial", "status": "partial", "idempotent_replay": false,
  "report_title": "Sunflower oil Procurement Risk Assessment",
  "report_markdown": "…", "report": { "…structured report…" },
  "report_location": {"markdown": "/assessments/demo-001/report.md", "json": "/assessments/demo-001"},
  "warnings": ["…"], "missing_data": ["…"], "limitations": ["…"], "error": null,
  "execution": {"elapsed_seconds": 63.2, "research_rounds": 1, "tool_calls": {"tavily": 3, "gdelt": 1},
                "llm_calls": 2, "tokens": {"input": 5873, "output": 1970}, "mock": false} }
```
* `run_state` describes the **execution** (`running` / `finished`); `assessment_status` describes the **result** (`completed` / `partial` / `failed`). `status` is kept for the existing contract and always equals `assessment_status`. The additive fields (`run_state`, `assessment_status`, `idempotent_replay`, `report_location`) come from the API layer; `schema_version` stays `1.1`.
* **Still running** (`GET`, HTTP 202): `{"request_id","run_state":"running","assessment_status":null,"started_at","overdue":bool}` + `Retry-After`. There is no `status` and no report.
* **Errors that are not an assessment result** (401, 403, 404, 409, 422, 429, 501, 504, store/internal errors without a record): `{"request_id","run_state":null|"running","assessment_status":null,"error":{"code","message"}}` with fixed messages — no paths, hosts, credentials or exception text. `422` adds `error.problems[{field,message}]`.
* `error` is `null` unless the assessment `failed`.

## 5. Status definitions (exact)
| Status | Meaning | Rule |
|---|---|---|
| **`completed`** | A validated, usable report **with every required component present**. | Valid report **and** none of the PARTIAL conditions below. |
| **`partial`** | A validated report is delivered, but at least one required component is missing or degraded; its limits are shown prominently. | Any of: `FINANCIAL_UNAVAILABLE` · benchmark `stale` or `unavailable` · `RESEARCH_SOURCE_TAVILY` / `RESEARCH_SOURCE_GDELT` unavailable, partially failed or not configured · `NO_RELEVANT_EVIDENCE` · `NO_STRONG_EXTERNAL_EVIDENCE` · `UNSUPPORTED_CLAIMS_REMOVED` · `POLICY_CONTEXT_UNAVAILABLE`. |
| **`failed`** | **No report.** Nothing is presented as an assessment. | Error code in §6. |

**Implemented on 13 Nov 2026 (D7; tested offline, not yet validated by a live run):** GDELT is an optional source. Its failure stays visible (warning, event, Notion, e-mail) but is not by itself PARTIAL while Tavily is OK and neither `NO_RELEVANT_EVIDENCE` nor `NO_STRONG_EXTERNAL_EVIDENCE` applies. Claim removals, Tavily failure, a stale/unavailable benchmark and unavailable financials still ⇒ PARTIAL. The first matching row of the table above therefore applies to GDELT only when Tavily failed or the evidence is insufficient (`docs/day4-architecture.md` section 24).

Not partial by themselves (warning only): `OVER_COMMITMENT`, `POLICY_RETRIEVAL_FALLBACK`, `RULE_BASED_RISK_ADDED`, `ASSESSMENT_DATE_OVERRIDE`, `MOCK RUN`, a research source answering HTTP 200 with no articles while another source supplied strong evidence. Warnings are never dropped from the envelope.

## 6. Errors
| Code | HTTP | Cause | n8n action |
|---|---|---|---|
| `UNAUTHORIZED` | 401 | Missing/wrong `X-API-Key` | Operator alert |
| `INVALID_REQUEST` | 422 | Validation failed (no record is created) | Notify requester; fix input |
| `REQUEST_ID_CONFLICT` | 409 | Same `request_id`, different parameters (stored or running) | New `request_id` |
| `ASSESSMENT_IN_PROGRESS` | 409 + `Retry-After` | Same id and parameters already running | **Poll with GET; never POST again** |
| `BUSY` | 429 + `Retry-After` | Another assessment is running (limit: 1 concurrent); the refused request leaves no record | Wait; do not queue in n8n |
| `LIVE_NOT_AUTHORISED` | 403 | `mode=live` but the server flag is off; no record, no external call | Operator alert |
| `LIVE_NOT_AVAILABLE` | 501 | `mode=live` but live is not wired in this build | Operator alert |
| `ASSESSMENT_TIMEOUT` | 504 + `Retry-After` | The HTTP deadline was reached **while the run continues**. This is *not* a failure and writes no failed record | **Bounded GET polling** (`docs/day4-architecture.md` §7); never POST again |
| `NOT_FOUND` | 404 | Unknown `request_id` | Stop; after a transport error alert the operator (the POST may not have registered) |
| `REPORT_NOT_AVAILABLE` | 404 | The assessment produced no report | — |
| `INTERNAL_DATA_UNAVAILABLE` | 200 (`failed`) | Database unreachable | Operator alert |
| `RESEARCH_UNAVAILABLE` | 200 (`failed`) | Neither research API answered (stops before any LLM spend) | Operator alert |
| `LLM_UNAVAILABLE` | 200 (`failed`) | Model call failed after SDK retries | Operator alert |
| `REPORT_VALIDATION_FAILED` | 200 (`failed`) | Draft invalid after the one repair and not fixable by removal | Operator alert |
| `ASSESSMENT_INTERRUPTED` | 200 (`failed`, stored) | The service restarted while the run was in progress; its worker is known to be dead. Never re-run automatically | Operator alert; new `request_id` |
| `INTERNAL_ERROR` | 500 (first run, with the failed envelope) / 200 (replay) | Unexpected exception inside the run (sanitised) or a corrupt record | Operator alert |

## 7. Timeouts and latency budget
| Layer | Limit | Note |
|---|---|---|
| Research loop (graph) | 100 s wall clock (config `deadline_seconds`; currently 180 s, lower in a later slice) | Stops starting new calls; discloses `RESEARCH_DEADLINE` |
| Tavily / GDELT per attempt | 30 s; GDELT at most 2 attempts in live runs | Measured: GDELT HTTP 429 twice then 200 `{}`, and HTTP 429 twice in demo-001/002 |
| LLM per call | 60 s, SDK retries ≤ 2 | Token cap 40,000 per run |
| **HTTP wait (server)** | **150 s**, env `ASSESSMENT_DEADLINE_SECONDS` | Hit ⇒ `504 ASSESSMENT_TIMEOUT` ("still running"); the worker is **never** cancelled |
| Overdue marker | 600 s of running | `GET` shows `overdue:true`; the run is reported, not killed |
| **n8n HTTP Request node (POST)** | **180 s, Retry On Fail = OFF** | Longer than the server wait so the server answers first |
| n8n polling after an uncertain POST | Wait 10 s, up to 24 GETs (about 4 min); max 3 consecutive failed GETs | Then an operator e-mail; the workflow execution timeout is ≥ 15 min. **Design, not built** |
Measured live latency: 100.4 s (demo-001), 72.2 s (demo-002); mock runs about 1–2 s.

## 8. Idempotency, concurrency and persistence (implemented, verified)
- **Key:** `request_id`; `request_hash` = SHA-256 of `ingredient`, `horizon_months`, `assessment_date` (as sent) and `mode`.
- **Records (authoritative; archive/delivery outcomes never change them):** `runs/assessments/{request_id}.json` = `{request_hash, finished_at, envelope, trace}` (the trace is stored but never served), written **atomically** (temp file + fsync + `os.replace`) before the worker is released; `{request_id}.run.json` = running marker, removed when the record is written.
- **Same id, same hash, finished:** the stored envelope with `idempotent_replay: true`; **no new research, no new spend**, also after a restart. A stored `failed` is replayed too (a deliberate retry uses a new `request_id`).
- **Same id, different hash:** `409 REQUEST_ID_CONFLICT` (stored or running).
- **Same id while running:** `409 ASSESSMENT_IN_PROGRESS` (+`Retry-After`).
- **Single-flight:** one assessment at a time (`429 BUSY`), protecting the paid-call budget and GDELT's rate limit. Replays are not blocked by a running assessment.
- **Execution is decoupled from the HTTP connection:** a server-owned worker thread runs the assessment; a client that disconnects or times out neither cancels nor duplicates it.
- **Restart:** a running marker with no live worker in this process ⇒ the request becomes a stored `failed` / `ASSESSMENT_INTERRUPTED` (decided only when the worker is known to be dead). No automatic re-run.
- **If the record file cannot be written** the result is served from memory until restart (logged); a restart then reports the request as interrupted.

## 9. Logging, events and privacy
Every log line carries `request_id`; token usage goes to `runs/usage.jsonl`. No keys, `DATABASE_URL`, hosts, prompts, file paths or third-party contact details are logged or returned; exceptions are logged by class only.
**Operational events** (taxonomy, metric sources and the planned `ops.operational_events` table: `docs/day4-architecture.md` §8): the API derives events from the finished envelope and hands them to an `EventSink`. S1 has only `NullEventSink` and an in-memory test sink; **no database writer exists and migration `002` is not applied.** When it exists it is the **single, narrow exception to "no database writes" in this service**, uses its own INSERT-only credential (`OPS_DATABASE_URL`), and a failure never changes an assessment. Events never contain report text, evidence, prompts, addresses or exception text.

## 10. Day 4 deliverables and tests
Files: `src/api/{app,models,store,events,runners,service}.py`, `tests/{api_fixtures,test_api,test_api_concurrency,test_api_events}.py` (implemented); `n8n/*.json` (planned, sanitised exports).
| ID | Contract test | Status |
|---|---|---|
| A01 | Valid mock request → 200, envelope fields, `run_state`/`assessment_status` | Implemented, passing |
| A02 | Replay → identical envelope, `idempotent_replay: true`, zero tool/LLM calls (also after restart) | Implemented, passing |
| A03 | Same id, different parameters → 409; duplicate while running → 409; second concurrent id → 429; simultaneous identical posts run once | Implemented, passing |
| A04 | Invalid ingredient/horizon/date/id/mode/extra field/body → 422 `INVALID_REQUEST`, nothing recorded | Implemented, passing |
| A05 | `mode=live` without the flag → 403, no external call, no record; live with the flag → 501 | Implemented, passing |
| A06 | HTTP deadline → 504 `ASSESSMENT_TIMEOUT` **while the run continues**; GET 202; re-POST 409; later GET returns the stored result; exactly one execution | Implemented, passing (redefined from "failed") |
| A07 | Every `failed` response has no report and a sanitised error; unexpected exceptions leak nothing | Implemented, passing |
| A08 | `GET /health` never leaks connection details | Implemented, passing |
| A09 | Auth required on every protected endpoint; app refuses to start without a key | Implemented, passing |
| A10 | Atomic persistence: no partial/temporary file on a failed replace; a reader never sees a partial record; crash-left marker ⇒ `ASSESSMENT_INTERRUPTED`, no re-run | Implemented, passing |
| N01–N10 | n8n tests (`docs/n8n-workflow-blueprint.md`) | Planned |

## 11. Decisions
1. **Resolved:** `POST` stays synchronous; a deadline hit is "still running", recovered by bounded GET polling (measured latency 72–100 s; revisit only if p95 > 120 s).
2. **Done (13 Nov, offline):** the optional-source rule (§5). **Open:** GDELT retry policy validated against live behaviour.
3. **Resolved:** `fastapi` and `uvicorn` installed and pinned; n8n runs locally (not installed yet).
4. `mode=mock` stays available; it is labelled everywhere. The n8n workflow sets `mode` from a constant (default `mock`).
