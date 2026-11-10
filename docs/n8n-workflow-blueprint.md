# n8n Workflow Blueprint --- Procurement Risk & Mitigation Agent

**Company:** Bakery Foods Inc. (fictional)\
**Project:** Four-day MVP · **Status:** Proposed workflow, not an
imported or tested n8n export\
**Role of n8n:** Request intake, agent invocation, delivery, and failure
notification only.

## 1. Design decision

**LangGraph (Python) is the primary agent.** n8n is a thin integration
layer. It must not perform market research, RAG, supplier evaluation,
exposure calculations, or LLM report writing.

``` mermaid
flowchart TD
    A["Form Trigger or Webhook"] --> B["Edit Fields: normalize request"]
    B --> C{"IF: valid request?"}
    C -- No --> X["Respond to Webhook: 400 / error message"]
    C -- Yes --> D["HTTP Request: LangGraph service"]
    D --> E{"Switch: agent status"}
    E -- completed --> F["Send Email: report"]
    E -- partial --> G["Send Email: report + limitations"]
    E -- failed --> H["Send Email: failure notification"]
    D -- transport error --> H
    F --> I["Respond to Webhook: accepted/completed"]
    G --> I
    H --> J["Respond to Webhook: failed"]

    K["Error Trigger (separate workflow)"] --> L["Send Email or Slack: operator alert"]
```

**Note:** The diagram is conceptual. An n8n Form Trigger and a
Webhook/Respond to Webhook path are alternative entry designs, not nodes
to combine indiscriminately. For a browser form, use **Form Trigger**;
for an API client, use **Webhook**. `Respond to Webhook` applies only to
the Webhook path.

## 2. Main workflow: nodes and configuration

  --------------------------------------------------------------------------------------
  Step              n8n node          Purpose              MVP configuration
  ----------------- ----------------- -------------------- -----------------------------
  1                 **Form Trigger**  Accept ingredient    Example:
                    *or* **Webhook**  and horizon          `ingredient=sunflower_oil`,
                                                           `horizon_months=3`

  2                 **Edit Fields     Normalize request    Create `request_id`,
                    (Set)**           fields               `ingredient`,
                                                           `horizon_months`,
                                                           `requested_by` if available

  3                 **IF**            Reject invalid input Allow only supported
                                                           ingredients; require horizon
                                                           in supported range

  4                 **HTTP Request**  Call LangGraph HTTP  POST JSON to configured agent
                                      service              endpoint; set authentication
                                                           and timeout

  5                 **Switch**        Route by returned    `completed`, `partial`,
                                      `status`             `failed`; unexpected status
                                                           goes to error branch

  6a                **Send Email**    Deliver completed    Include report content or
                                      report               stable report link and
                                                           request ID

  6b                **Send Email**    Deliver partial      Prominent limitations and
                                      report               missing-data warnings

  6c                **Send Email**    Notify               No invented report; include
                                      requester/operator   request ID and safe error
                                      of failure           summary

  7                 **Respond to      Return execution     Match response to actual
                    Webhook**         outcome              execution state
                    *(Webhook path                         
                    only)*                                 
  --------------------------------------------------------------------------------------

**Node error settings:** Use the HTTP Request node's error output (where
supported) to route transport errors separately. For temporary delivery
errors, enable bounded **Retry On Fail**. Configure an **Error Trigger**
workflow for unexpected execution failures; use **Stop And Error** when
deliberately escalating an unrecoverable condition.

**Important:** Avoid sending multiple emails on retry. A stable
`request_id` and delivery status record are needed for reliable
deduplication in a future production implementation.

## 3. Interface contract with LangGraph

**Example request**

``` json
{
  "request_id": "demo-001",
  "ingredient": "sunflower_oil",
  "horizon_months": 3
}
```

**Example successful response shape** *(illustrative, not real market
findings)*

``` json
{
  "request_id": "demo-001",
  "status": "completed",
  "report_title": "Sunflower Oil Procurement Risk Assessment",
  "report_markdown": "...",
  "warnings": [],
  "missing_data": [],
  "error": null
}
```

**Partial response:** `status="partial"` and nonempty `warnings` /
`missing_data`, with only supported findings included.

**Failed response:** `status="failed"`, no usable report, and a
sanitized `error` object such as
`{"code":"INTERNAL_DATA_UNAVAILABLE","message":"Required procurement data could not be retrieved"}`.

Use a stable schema (Pydantic on the Python side). The workflow should
not assume that an HTTP 200 response means the assessment succeeded:
**check the application-level `status`**.

## 4. Execution model: a decision to make

A full research run may exceed the practical timeout of a synchronous
n8n HTTP Request. Choose one of these patterns:

  -----------------------------------------------------------------------
  Pattern                 When to use             Trade-off
  ----------------------- ----------------------- -----------------------
  **Synchronous MVP**     Demonstrated runs       Simplest, but long
                          consistently finish     research runs can time
                          within configured       out
                          timeouts                

  **Asynchronous job      Runs take longer or     `POST /assessments`
  (later or if needed)**  need durable retries    returns a job ID; n8n
                                                  uses **Wait + HTTP
                                                  Request** polling or
                                                  receives a callback
  -----------------------------------------------------------------------

**Recommendation:** Start synchronous for the four-day demo **only if
measured execution time supports it**. Otherwise use an asynchronous
job/status endpoint. Don't add a queue or polling before there is a
demonstrated need.

## 5. Error-handling ownership

  -----------------------------------------------------------------------
  Failure                 Owner                   Behavior
  ----------------------- ----------------------- -----------------------
  External research API   LangGraph + Python      Bounded retry; mark
  timeout / 429           integration             unavailable evidence

  Missing contract price  LangGraph + Python      Omit unsupported
  / forecast              validation              calculation; warn or
                                                  fail

  Invalid LLM report      LangGraph + Pydantic    Reject, retry once if
                                                  appropriate, or fail

  Research loop limit     LangGraph               Stop and disclose
  reached                                         evidence gaps

  LangGraph service       n8n HTTP Request error  Notify
  unreachable             branch                  requester/operator; do
                                                  not create a report

  `status=partial`        n8n Switch              Deliver clearly labeled
                                                  partial report

  Email service           n8n email node retry +  Retry delivery; notify
  unavailable             error workflow          operator if still
                                                  unsuccessful

  Unexpected n8n          n8n Error Trigger       Send operator alert
  execution failure       workflow                with execution ID
  -----------------------------------------------------------------------

**Retry rule:** Each operation should have one retry owner. n8n must not
blindly retry the entire agent request after an uncertain timeout, as
that may launch duplicate costly research runs.

## 6. Minimal tests for the n8n layer

  -----------------------------------------------------------------------
  Test                                Expected result
  ----------------------------------- -----------------------------------
  **N01 --- Valid request**           Correct JSON sent to LangGraph;
                                      completed report delivered

  **N02 --- Invalid ingredient**      Rejected before agent invocation

  **N03 --- Agent returns `partial`** Report delivered with prominent
                                      warning

  **N04 --- Agent returns `failed`**  Failure notification; no report
                                      presented as valid

  **N05 --- HTTP timeout /            Error branch and notification; no
  unreachable service**               silent success

  **N06 --- Delivery failure**        Bounded retry or operator alert;
                                      research not rerun
  -----------------------------------------------------------------------

Save an n8n execution screenshot or sanitized log for one successful and
one failed case. Record actual test outcomes in
`03-ai-workflow-evaluation.md`.

## 7. Four-day MVP implementation order

1.  Create the entry node, normalize inputs, and reject unsupported
    requests.
2.  Connect **HTTP Request** to a minimal Python endpoint returning the
    agreed schema.
3.  Add **Switch** branches for `completed`, `partial`, and `failed`.
4.  Configure email delivery and an error-notification workflow.
5.  Replace the stub response with the real LangGraph service and run
    N01, N04, and N05; add N03 and N06 if time permits.

**Not in MVP:** Automated supplier approvals, procurement-system writes,
multi-agent n8n logic, elaborate queues, dashboards, and continuous
monitoring.

## 8. Repository artifacts

Suggested files after implementation:

``` text
n8n/
  procurement-request-workflow.json
  error-notification-workflow.json
docs/
  n8n-workflow-blueprint.md
```

The JSON files should be **actual exports from n8n**, with credentials
removed. This blueprint is design documentation, not a runnable workflow
export.

------------------------------------------------------------------------

*All procurement records are fictional. Report recommendations are
advisory and require human review.*
