# Demonstration scenarios — sunflower oil

Two complementary cases for the 13 Nov demonstration. Together they show the agent doing its job **and** refusing to do what the data cannot support.

| | **A — Normal assessment** | **B — Controlled limitation** |
|---|---|---|
| Purpose | A complete, evidence-backed assessment | Safe, transparent behaviour when the data is out of date |
| Request | `{"request_id":"demo-A","ingredient":"sunflower_oil","horizon_months":3}` | `{"request_id":"demo-B","ingredient":"sunflower_oil","horizon_months":3,"assessment_date":"2026-11-20"}` |
| Trigger | None | The benchmark (period ending 25 Oct) is 26 days old against the 21-day limit |
| Expected status | `completed` | `partial` |
| What to point at | Fresh benchmark with its period; baseline cost EUR 1,961,496.55 and the ±10/20 % table; coverage gap by month; SUP-003 **CONDITIONAL** with its own risk; findings tagged `internal_model` / `external_evidence` / `combined`; policy passages cited verbatim; alternative-supplier leads **UNVERIFIED** with unknown attributes left `unknown`; every action needing human approval | `STALE` benchmark with its age and **no price substituted**; **no euro figure anywhere** (not in tables, not in AI prose); explicit `FINANCIAL_UNAVAILABLE` warnings and missing data; override of the assessment date disclosed; SUP-003 status, external evidence and policy context **still reported** |
| Why it matters | Shows the value: exposure, coverage, evidence, policy and leads in one reviewable report | Shows trust: a plausible-looking but unsupported number is the dangerous failure, and the agent withholds it |

**Optional fault injection (mock only): `--inject-unsupported-claim`.** The model's draft gains a finding whose citation does not support it. The agent rejects it, allows one repair, then **removes it and lists it** under "Claims removed because their support could not be verified"; the status becomes `partial`. This demonstrates the unsupported-claim safeguard on demand, which a live model will not do deliberately.

Other failure states (covered by tests, available to show): research source down (`RESEARCH_SOURCE_*`, live gate `RESEARCH_UNAVAILABLE` before any LLM spend), policy retrieval unavailable (`POLICY_CONTEXT_UNAVAILABLE`), missing contract price (`FINANCIAL_UNAVAILABLE`), database down (`INTERNAL_DATA_UNAVAILABLE`), invalid draft after the repair (`REPORT_VALIDATION_FAILED`).

## Run now (mock research and drafting, real read-only database data)
```bash
python -m src.agent.demo --mock all
python -m src.agent.demo --mock all --inject-unsupported-claim
```
Outputs in `runs/`: `demo-A.md`, `demo-B.md` (reports), `demo-*-audit.md` (independent audit), `demo-scenarios-summary*.md` (expectations table). Each scenario is checked against 8–9 explicit expectations plus the 10-check audit; they are also executable tests (`tests/test_demo_scenarios.py`, offline; `tests/integration/test_demo_db.py`, read-only).

## What mock runs do and do not prove
They prove the workflow, guardrails, labelling and failure behaviour on real internal data. They do **not** show real market evidence, real model wording or real embedding retrieval. Every mock report carries a `MOCK RUN` banner.

## Live versions (Day 4) — prerequisites and approvals
| Needed | For | Approval |
|---|---|---|
| Load the 25 policy chunks (`--apply`) and one embedding batch (`--embed`, ≈ $0.00004) | Policy context in the live reports | **Required** (policy inventory first) |
| One live run per scenario (est. $0.02–0.04 each at the observed token use; budget cap $5, ≈ $0.019 spent so far) | Real evidence, real model | **Required** |
| Benchmark age ≤ 21 days on the day of the run | Scenario A | The newest loaded week ends 25 Oct: **A is valid until 15 Nov**; after that A behaves like B unless newer EC data is downloaded and loaded (`--apply`, approval) |
| Scenario B live | Uses the `assessment_date` override; needs no extra data | Same run approval |
Open quality caveats for the live versions: whether the real model's drafts satisfy the strict citation rules without ending `partial`, and GDELT currently contributing no articles (see implementation plan §14).
