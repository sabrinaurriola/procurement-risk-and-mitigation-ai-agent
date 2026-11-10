# Business Case — Procurement Risk & Mitigation Agent
 
**Company:** Bakery Foods Inc. (fictional French bakery manufacturer)  
**Business function:** Raw Materials Procurement  
**Scope:** Four-day MVP build and final demonstration  
**Status:** Proposed prototype; business impact not yet validated

## 1. Business problem

Bakery manufacturers depend on agricultural ingredients exposed to price volatility and supply disruptions. Procurement managers must connect external market developments with internal supplier contracts and demand forecasts to decide whether action is needed.

**Key question:** Which market developments matter to our ingredients, what is our exposure, and which mitigation options should we consider?

The time spent on this work and the potential benefits of automation are hypotheses to validate, not measured facts for this fictional company.

## 2. Proposed solution

An **on-demand Procurement Risk Assessment & Mitigation Agent** that produces an evidence-backed report for a selected ingredient and planning horizon.

**Example request:** “Assess sunflower oil procurement risks for the next three months.”

The agent will:
1. Research relevant commodity signals, supply disruptions, and news.
2. Retrieve synthetic supplier, contract, and demand-forecast data.
3. Consult relevant procurement policies and supplier documents.
4. Calculate exposure scenarios using deterministic formulas and explicit assumptions.
5. Identify mitigation options, including existing approved suppliers and up to three preliminary new supplier candidates.
6. Generate a report for procurement-manager review.

**Decision boundary:** The agent does not approve suppliers, place orders, change contracts, or make procurement commitments. Those decisions remain with people.

## 3. User, output, and value

| Item | MVP definition |
|---|---|
| Primary user | Raw Materials Procurement Manager |
| Decision supported | Whether a price or supply risk requires investigation or mitigation |
| Input | Ingredient and assessment horizon |
| Output | Procurement Risk Assessment & Mitigation Strategy Report |
| Report sections | Executive summary; risks and evidence; supplier exposure; scenario-based financial impact; alternatives; recommended actions; uncertainties and approval decisions |

**Value hypothesis:** Reduce first-pass research effort and make procurement risk assessments easier to review and act upon. The prototype will demonstrate feasibility, **not** proven time savings or ROI.

## 4. MVP scope

**In scope**
- Sunflower oil end-to-end; a second ingredient if time permits.
- Approximately five fictional suppliers, five contracts, and three months of synthetic demand forecasts.
- External research with identifiable sources and dates.
- Retrieval of fictional procurement documents.
- Deterministic financial-impact scenarios with labeled assumptions.
- One integrated report per request, including uncertainties.
- Two example reports and selected functional and failure tests.

**Out of scope**
- Real ERP/S&OP integration or confidential company data.
- Continuous monitoring and automated alerts.
- Verified supplier qualification, negotiated quotes, or guaranteed capacity.
- Autonomous purchasing or supplier approval.
- Production-scale operations and service-level commitments.

**Data limitation:** Public commodity benchmarks are not the same as contracted supplier prices. Scenario assumptions must never be presented as actual quotes.

## 5. Prototype success criteria

The MVP should demonstrate that it can:
- Complete the research-to-report workflow for sunflower oil.
- Trace material claims to sources and distinguish facts from assumptions.
- Retrieve relevant synthetic internal records.
- Produce reproducible financial calculations.
- Avoid inventing prices, supplier qualifications, or missing inputs.
- Handle selected failures visibly, including API unavailability and missing contract prices.
- Produce two reviewable example reports.

**Business validation comes later:** Procurement users must assess usefulness, trust, time saved, and whether reports improve their decisions.

## 6. Proposed go-to-market / adoption sprints

| Sprint | Goal | Deliverable | Proposed measure |
|---|---|---|---|
| **1 — Validate** | Test report usefulness with procurement users | Interviews, reviewed reports, revised requirements | Target: 80% of reviewed reports rated useful |
| **2 — Pilot** | Measure value with approved data and selected users | Controlled pilot and baseline comparison | Target: 30% less first-pass research preparation time |
| **3 — Operationalize** | Plan wider adoption and monitoring | Integration, alerting, governance, and ownership plan | Target: 90% of qualifying alerts reviewed within an agreed service window |

*These are proposed targets, not measured outcomes. Definitions and baselines must be agreed during validation.*

## 7. Key risks and next decision

- **Evidence quality:** News may be outdated, irrelevant, or contradictory.
- **Data quality:** Missing contracts or forecasts can invalidate exposure estimates.
- **Supplier discovery:** A search result is a candidate, not an approved supplier.
- **Adoption:** Procurement users must be able to understand and use the recommendations.
- **Operations:** Any deployment needs clear ownership of data, incidents, and approvals.

**Next decision:** Validate the report's questions, calculations, and mitigation options against the procurement manager's real workflow before expanding the solution.

---

**Related documents:** `02-solution-architecture.md` and `03-ai-workflow-evaluation.md` (planned).

*Portfolio disclosure: Bakery Foods Inc. and its internal procurement records are fictional. External market evidence must be identified separately from synthetic data.*
