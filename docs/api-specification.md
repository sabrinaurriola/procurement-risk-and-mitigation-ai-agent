# Assessment API — Contract between n8n and the LangGraph service (Day 4 implementation specification)

**Status:** specification only. The request/response models already exist in code (`src/agent/schemas.py`, including `assessment_date`); the FastAPI service, the request store and the n8n workflow are **not built** (Day 4). New dependencies (`fastapi`, `uvicorn`) need approval before installation.

## 1. Responsibilities
| Python service (LangGraph) owns | n8n owns |
|---|---|
| Request validation (authoritative), ingredient and date rules | Intake (form/webhook), generating a unique `request_id`, a first-line check for friendly errors |
| All data access, research, calculations, RAG, LLM calls, evidence rules, report validation | **No analysis, no LLM, no research, no calculations** |
| Status decision (`completed` / `partial` / `failed`), warnings, missing data, limitations | Routing on `status`; email/notification text; operator alert via a separate Error Trigger workflow |
| Retries of research tools and LLM calls (one owner per operation) | **No automatic re-POST of an assessment.** Delivery (email) retries only; a delivery failure never re-runs research |
| Idempotent result storage, duplicate protection, timeouts, logging with `request_id` | Waiting for the response; one recovery `GET` after a timeout |

## 2. Endpoints (local, bound to 127.0.0.1; header `X-API-Key` from an environment variable)
| Method and path | Purpose |
|---|---|
| `GET /health` | `{"status":"ok","version":…,"live_enabled":bool,"db":"ok"\|"error"}`. Database check is a read-only `SELECT 1`; never returns connection details. |
| `POST /assessments` | Run (or replay) one assessment. **Synchronous**; returns the envelope in §4. |
| `GET /assessments/{request_id}` | Stored envelope (idempotent recovery after an n8n timeout). `404` unknown, `202` still running. |
| `GET /assessments/{request_id}/report.md` | The report as `text/markdown` (for e-mail attachment). |

Synchronous is justified by measurement: live runs took 54–68 s, ~45 s of it GDELT retries (§7). If a measured p95 exceeds 120 s on Day 4, switch `POST` to `202 {"status":"running"}` plus polling `GET`; the envelope does not change.

## 3. Request — `POST /assessments`
```json
{ "request_id": "demo-001", "ingredient": "sunflower_oil", "horizon_months": 3,
  "assessment_date": "2026-11-20", "mode": "live" }
```
| Field | Rule |
|---|---|
| `request_id` | Required. `^[A-Za-z0-9_-]{1,64}$`. **Idempotency key.** |
| `ingredient` | Required. Currently only `sunflower_oil` (wheat is not implemented; anything else → `INVALID_REQUEST`). |
| `horizon_months` | Required, integer 1–6. Months are the full calendar months after the assessment month. |
| `assessment_date` | Optional ISO date, default today. Allowed from 365 days before to 90 days after today. Overriding is for reproducible demos; it is echoed in the response and adds the warning `ASSESSMENT_DATE_OVERRIDE`. Data freshness is judged against this date. |
| `mode` | Optional, `live` (default) or `mock`. `mock` uses sanitised fixtures and is labelled `MOCK RUN` everywhere. `live` makes paid calls and requires the server flag `ALLOW_PAID_CALLS=1`; otherwise `FAILED` with `LIVE_NOT_AUTHORISED` and **no** external call. |
| unknown fields | Rejected (`extra="forbid"`). |

## 4. Response envelope (always the same shape)
```json
{ "schema_version": "1.1", "request_id": "demo-001", "assessment_date": "2026-11-20",
  "status": "partial", "idempotent_replay": false,
  "report_title": "Sunflower oil Procurement Risk Assessment",
  "report_markdown": "…", "report": { "…structured report…" },
  "report_location": {"markdown": "/assessments/demo-001/report.md", "json": "/assessments/demo-001"},
  "warnings": ["…"], "missing_data": ["…"], "limitations": ["…"],
  "error": null,
  "execution": {"elapsed_seconds": 63.2, "research_rounds": 1, "tool_calls": {"tavily": 3, "gdelt": 1},
                "llm_calls": 2, "tokens": {"input": 5873, "output": 1970}, "mock": false} }
```
`error` is `null` unless `status` is `failed`: `{"code": "...", "message": "..."}` (sanitised: no credentials, hosts or raw exception text). **n8n must branch on `status`, not on the HTTP code.** `idempotent_replay`, `report_location` and `schema_version` are added by the API layer; everything else is the existing `AssessmentResponse`.

## 5. Status definitions (exact)
| Status | Meaning | Rule |
|---|---|---|
| **`completed`** | A validated, usable report **with every required component present**. | Valid report **and** none of the PARTIAL conditions below. |
| **`partial`** | A validated report is delivered, but at least one required component is missing or degraded; its limits are shown prominently. Deliver it with the warnings. | Any of: `FINANCIAL_UNAVAILABLE` (missing contract price/forecast month or benchmark) · benchmark `stale` or `unavailable` · `RESEARCH_SOURCE_TAVILY` / `RESEARCH_SOURCE_GDELT` unavailable, partially failed or not configured · `NO_RELEVANT_EVIDENCE` · `NO_STRONG_EXTERNAL_EVIDENCE` · `UNSUPPORTED_CLAIMS_REMOVED` · `POLICY_CONTEXT_UNAVAILABLE`. |
| **`failed`** | **No report.** Nothing is presented as an assessment. | Error code in §6. |

Not partial by themselves (warning only): `OVER_COMMITMENT`, `POLICY_RETRIEVAL_FALLBACK`, `RULE_BASED_RISK_ADDED`, `ASSESSMENT_DATE_OVERRIDE`, `MOCK RUN`, a research source answering HTTP 200 with no articles while another source supplied strong evidence. Warnings are never dropped from the envelope.

## 6. Errors
| Code | HTTP | Cause | n8n action |
|---|---|---|---|
| `INVALID_REQUEST` | 422 | Validation failed | Notify requester; fix input; new `request_id` |
| `REQUEST_ID_CONFLICT` | 409 | Same `request_id`, different parameters | Generate a new `request_id` |
| `ASSESSMENT_IN_PROGRESS` | 409 + `Retry-After` | Same `request_id` already running | Wait, then `GET` |
| `BUSY` | 429 + `Retry-After` | Another assessment is running (limit: 1 concurrent) | Wait; do not queue in n8n |
| `LIVE_NOT_AUTHORISED` | 403 | `mode=live` but the server flag is off | Operator alert |
| `INTERNAL_DATA_UNAVAILABLE` | 200 (`failed`) | Database unreachable | Operator alert |
| `RESEARCH_UNAVAILABLE` | 200 (`failed`) | Neither research API answered (live gate, stops before any LLM spend) | Operator alert |
| `LLM_UNAVAILABLE` | 200 (`failed`) | Model call failed after SDK retries | Operator alert |
| `REPORT_VALIDATION_FAILED` | 200 (`failed`) | Draft invalid after the one repair and not fixable by removal | Operator alert |
| `ASSESSMENT_TIMEOUT` | 504 (`failed`) | Hard request deadline reached | One `GET` recovery, then alert |
| `INTERNAL_ERROR` | 500 (`failed`) | Unexpected exception (sanitised) | Operator alert |

## 7. Timeouts and latency budget
| Layer | Limit | Note |
|---|---|---|
| Research loop (graph) | 100 s wall clock (config `deadline_seconds`; currently 180 s, lower on Day 4) | Stops starting new calls; discloses `RESEARCH_DEADLINE` |
| Tavily / GDELT per attempt | 30 s, ≤ 3 attempts | Measured: GDELT answered **HTTP 429 twice (~12 s each) then HTTP 200 `{}`**, ≈ 45 s. **Decision (Day 4):** cap GDELT at 1 retry / ~20 s total, or run it last |
| LLM per call | 60 s, SDK retries ≤ 2 | Token cap 40,000 per run |
| **Hard request deadline** (server) | **150 s** | → `ASSESSMENT_TIMEOUT`; result stored if it later finishes |
| **n8n HTTP Request node** | **180 s, Retry On Fail = OFF** | Longer than the server deadline so the server answers first |

## 8. Idempotency and duplicate protection
- **Key:** `request_id`. Stored result: `runs/assessments/{request_id}.json` (file store; **no database writes**, the agent's session is read-only). `request_hash` = SHA-256 of `ingredient`, `horizon_months`, `assessment_date`, `mode`.
- **Same id, same hash, finished:** return the stored envelope with `idempotent_replay: true`; **no new research, no new spend.** (A stored `failed` is replayed too: a deliberate retry uses a new `request_id`.)
- **Same id, different hash:** `409 REQUEST_ID_CONFLICT`.
- **Same id while running:** `409 ASSESSMENT_IN_PROGRESS` (lock file `{request_id}.lock`, removed on completion; stale locks older than the hard deadline are ignored and logged).
- **Concurrency:** one assessment at a time (`BUSY`); protects the paid-call budget and GDELT's one-request-per-5-seconds limit.
- **Timeout recovery:** n8n never re-POSTs; it calls `GET /assessments/{request_id}` once, so a slow run cannot be duplicated.

## 9. Logging and privacy
Every log line and the per-request trace (`runs/{request_id}.json`) carry `request_id`; token usage goes to `runs/usage.jsonl`. No keys, `DATABASE_URL`, hosts, prompts or third-party contact details are logged. n8n stores the envelope in its execution log, so reports contain synthetic data and public sources only.

## 10. Day 4 deliverables and tests
Files: `src/api/app.py` (FastAPI), `src/api/store.py` (idempotent store + lock), `tests/test_api.py` (FastAPI `TestClient`, mock mode), `n8n/*.json` (sanitised exports).
| ID | Contract test |
|---|---|
| A01 | Valid mock request → 200, envelope fields present, `status` in `{completed, partial, failed}` |
| A02 | Replay same request → identical envelope, `idempotent_replay: true`, zero tool/LLM calls |
| A03 | Same id, different parameters → 409; duplicate while running → 409; second concurrent request → 429 |
| A04 | Invalid ingredient/horizon/date/extra field → 422 `INVALID_REQUEST` |
| A05 | `mode=live` without the server flag → 403 and no external call |
| A06 | Hard deadline → 504 `ASSESSMENT_TIMEOUT`; later `GET` returns the stored result |
| A07 | Every `failed` response has no report and a sanitised error |
| A08 | `GET /health` never leaks connection details |
| N01–N06 | n8n tests from `docs/n8n-workflow-blueprint.md`, mapped to the statuses above |

## 11. Open decisions for Day 4
1. Confirm synchronous `POST` (switch to `202` + polling only if measured p95 > 120 s).
2. GDELT retry/timeout policy (§7) and whether to change its 3-month window after the follow-up diagnostic.
3. Approve installing `fastapi` and `uvicorn`; choose local n8n (Docker or `npx`).
4. Whether `mode=mock` stays exposed outside demos.
