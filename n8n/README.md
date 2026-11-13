# n8n workflows

n8n is the **secondary** stack (see [stack_decision.md](../stack_decision.md)): it only archives a finished report in Notion and, on request, e-mails it with Gmail. All analysis stays in Python.

## Current workflows (import these)

| File | Webhook path | Nodes | Purpose |
|---|---|---|---|
| `procurement-report-archive.json` | `POST /webhook/procurement-archive` | 21 | Create the Notion page, append the report blocks, verify, answer the Python app; log errors to a Notion errors database. |
| `procurement-report-email.json` | `POST /webhook/procurement-email` | 23 | Re-check the archive, verify the PDF (SHA-256 of the decoded binary), send one Gmail message with the PDF, record the outcome. |

Both are **generated** by `build_report_workflows.py` (a test fails if the JSON and the script disagree). They contain **no credentials**: authentication uses an n8n *Header Auth* credential (`X-Webhook-Secret`) and the Notion and Gmail credentials you create in your own n8n. The `Config` node holds placeholders only (`REPLACE_WITH_ASSESSMENTS_DATABASE_ID`, `REPLACE_WITH_ERRORS_DATABASE_ID`, `REPLACE_WITH_APPROVED_TEST_RECIPIENT`).

Setup steps, the Notion database schemas and the exact request/response contracts are in [docs/notion-database-schemas.md](../docs/notion-database-schemas.md) and [docs/day4-architecture.md](../docs/day4-architecture.md) sections 19 and 22.
