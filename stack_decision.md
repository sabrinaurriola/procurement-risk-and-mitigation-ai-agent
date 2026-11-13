# Stack decision

**Primary stack: LangGraph (Python), with Gradio as the local interface.**
**Secondary stack: n8n, used only to archive the finished report in Notion and, on request, to e-mail it through Gmail.**

## The problem the stack has to serve

A procurement manager at an industrial bakery (the fictional Bakery Foods Inc., Spain) has to connect three kinds of information before deciding anything about sunflower oil:

1. **External market signals**: news and price developments found on the web.
2. **Internal records**: contracts, demand forecasts and supplier status, held in SQL.
3. **Internal policy**: which suppliers may be used and how coverage must be handled, held as documents.

The output is a report whose figures and claims can be checked. A wrong exposure figure or an invented source is worse than no report, so the stack must make correctness enforceable, not just likely.

## Why LangGraph is the primary stack

| Need in this problem | How LangGraph + Python meets it |
|---|---|
| **Bounded, repeatable research** (at most 3 rounds, fixed tool-call and time budgets) | The workflow is an explicit graph of nodes (`load_internal`, `calculate`, `search`, `filter_evidence`, draft, validate, ...). Budgets and retries live in code, not in a canvas. |
| **Internal RAG over policies** | A retrieval step queries pgvector (`src/rag/`) with parameterised, read-only SQL. The model never writes SQL. |
| **Deterministic calculations** | Exposure (fixed, index-linked, uncovered volume) is computed in Python with `Decimal` in `src/calculations/`, covered by hand-checked tests. The LLM never does arithmetic. |
| **Evidence validation** | Every finding declares its `basis`, citations need a verbatim quote, and unsupported claims are removed visibly (`src/agent/validation.py`). That needs real code and typed state; a no-code node cannot express it. |
| **Honest failure states** | A run ends `completed`, `partial` or `failed`; a partial report lists exactly what was removed. This is part of the graph's state, not a side effect. |
| **Testability** | The whole agent runs offline against recorded or mocked Tavily, GDELT and OpenAI responses (`pytest tests`). A visual workflow cannot be unit-tested this way. |
| **Cost control** | A budget guard, a token cap and a one-live-run-per-session limit are enforced in Python before any paid call. |

**Gradio** is the thin local front end (`src/ui/`): commodity and horizon in, report out, no analysis logic. It binds to `127.0.0.1` only.

## Why n8n is secondary, not primary

n8n is good at connecting services and at routing work; this MVP uses it for exactly that and nothing else:

- **What it does here:** receive an authenticated webhook call from the Python app, create and verify the Notion page that archives the report, and, only when the user asks, send an e-mail with the PDF through Gmail. Two workflows: `n8n/procurement-report-archive.json` and `n8n/procurement-report-email.json`.
- **Why it is not the agent framework:** the parts that make this product trustworthy (SQL and RAG access, `Decimal` arithmetic, citation validation, removal of unsupported claims, spend limits) would have to be rebuilt as chains of Code and HTTP nodes. They would be harder to test, harder to review and easy to break with a canvas edit.
- **Why it is still worth having:** delivery is the one place where integration breadth matters. Notion and Gmail connectors, credentials stored outside the repository, and a visible execution log come for free, and a non-developer can change where reports go without touching the agent.

The boundary is deliberate and enforced: **no analysis, LLM call or database access happens in n8n**, and n8n never starts a second assessment.

## What this decision costs

- Running the agent needs Python, a Supabase/PostgreSQL database and API keys, which is more setup than an n8n-only demo.
- n8n adds a second system to run for the archive and e-mail steps. They are optional: without n8n the app still produces the report.
- The n8n workflows have been exercised against real Notion and Gmail by the project owner during integration testing. The repository does not contain screenshots of those executions (see the README status table).
