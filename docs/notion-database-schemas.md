# Notion database schemas (approved specification)

**Status:** these are the **agreed specifications** supplied by the project owner on 12 Nov 2026. They have **not been independently retrieved from the Notion workspace**. Any difference found during real integration must be reported and decided by the owner, never silently corrected in the workflows. The workflows (`n8n/procurement-report-archive.json`, `n8n/procurement-report-email.json`, built by `n8n/build_report_workflows.py`) use exactly the names, types and Select options below. No extra property is used.

## Database 1: Procurement Risk Assessments
One page per `request_id`. **Properties hold metadata and operational status only; the complete Markdown report lives as flat Notion blocks in the page body.**

| Exact property name | Notion type | Select options | Written by |
|---|---|---|---|
| Name | Title | - | archive (create): `[MODE] Procurement risk assessment <request_id>` |
| Request ID | Text | - | archive (create); the lookup key |
| Ingredient | Select | sunflower_oil | archive (create) |
| Assessment date | Date | - | archive (create) |
| Horizon months | Number | - | archive (create) |
| Mode | Select | mock, live | archive (create) |
| Assessment status | Select | completed, partial, failed | archive (create; a `failed` assessment is never archived, so the workflow only writes completed/partial) |
| Archive status | Select | incomplete, stored, failed | archive: `incomplete` at create, `stored` only after the whole body is re-read and verified |
| Report SHA-256 | Text | - | archive (create); echoed back to the caller and checked by the e-mail workflow |
| Total blocks | Number | - | archive (create and when marking stored) |
| Email status | Select | not_requested, sending, sent, failed, unknown | archive sets `not_requested`; e-mail workflow: `sending` (claim before the send), then `sent` / `failed` / `unknown` |
| Email message id | Text | - | e-mail: Gmail message id after a successful send |
| Delivery ID | Text | - | e-mail: stable id of the delivery, `del-<request_id>`, written with the claim |

`Archive status = failed` is not written by the current workflows (an unfinished archive stays `incomplete` so it can resume).

## Database 2: Procurement Operational Errors
One page per recorded failure or warning. Best-effort: a failure to write here never changes the workflow's answer.

| Exact property name | Notion type | Select options | Value written |
|---|---|---|---|
| Name | Title | - | `<Error code> <Request ID>` |
| Timestamp | Date | - | UTC time of the failure (ISO 8601) |
| Request ID | Text | - | the request id, or empty when it was invalid |
| Workflow | Select | archive, email | the workflow that failed |
| Component | Select | webhook, validation, notion, gmail, workflow | see the error table in `docs/day4-architecture.md` section 22 |
| Error code | Text | - | a fixed code such as `ARCHIVE_INCOMPLETE` (never an exception message) |
| Severity | Select | warning, error, critical | per code |
| Status | Select | open, resolved | always `open` when written; a person sets `resolved` |
| Execution ID | Text | - | n8n `$execution.id` |
| Retry count | Number | - | attempts or loop rounds used before giving up |
| Occurred during | Select | validation, lookup, create, append, verify, send, response | the stage |
| Error ID | Text | - | stable id `E-<workflow>-<request_id or noid>-<Error code>` |
| Resolution notes | Text | - | never written by the workflows (for people) |

## Naming rules (do not "fix")
`Email status` (not Delivery status) · `Email message id` (not Email sent) · `Occurred during` (not Failure stage) · `Error code` (not Sanitized error code) · `Delivery ID` is in the assessments database · `Error ID` is in the errors database · there is **no** `Human review` property.

## How the workflows address Notion
- **API version:** `2022-06-28`, sent as the `Notion-Version` header on every call (one value, kept in each workflow's `Config` node). Endpoints used consistently with that version: `POST /v1/databases/{database_id}/query`, `POST /v1/pages` (parent `database_id`), `PATCH /v1/pages/{page_id}`, `GET` and `PATCH /v1/blocks/{page_id}/children`. The newer data-source endpoints (`2025-09-03`) are **not** used. If the workspace databases have several data sources, this version may not address them; report it if integration shows that.
- **Identifiers are configuration, not code:** `assessments_db_id` and `errors_db_id` are placeholders in the `Config` node of each workflow and must be set after import (a placeholder makes the workflow refuse to run: `CONFIG_NOT_SET`). The Notion integration token is an n8n credential (`Notion API`), shared with both databases; it is never in the JSON.
- **Payload shapes:** Text = `rich_text`, Title = `title`, Select = `select.name`, Number = `number`, Date = `date.start`.
