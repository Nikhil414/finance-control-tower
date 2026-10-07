# Finance Analytics Agent Operating System

## Combined Analyst Portfolio Capstone

**Project type:** SQL + Python + AI + Excel + Power BI  
**Recommended domain:** Order-to-Cash Finance Operations and Revenue Assurance  
**Difficulty:** Intermediate to advanced  
**Estimated build time:** 8–12 focused days when the KPI and leakage projects are reusable; 14–20 focused days when reconciliation and Power BI must be built from scratch.  
**Career value:** Flagship portfolio project for Data Analyst, Business Analyst, Finance Analyst, BI Analyst, Risk Analyst, Operations Analyst and Consulting Analyst roles.

This should become the flagship portfolio project. It combines the completed SQL KPI project and Python Revenue Leakage project, then adds Excel review, Power BI monitoring, AI explanations and an auditable approval workflow.

It is not merely a dashboard or chatbot. It is a small but realistic finance-control operating system.

---

## 1. Recommended business domain

Use one clear domain:

> **Order-to-Cash Finance Operations and Revenue Assurance for an e-commerce/fintech company**

This domain naturally connects:

- Customer orders
- Payments
- Refunds
- Discounts
- Product pricing
- Payment-gateway fees
- Deliveries
- Bank settlements
- Ledger transactions
- KPI targets
- Reconciliation breaks
- Revenue leakage
- Human approvals

Do not present it as a generic system for every industry. Build it for order-to-cash finance operations, then explain that the architecture can be adapted to banking, insurance, SaaS, telecom and retail.

---

## 2. Business scenario

A growing digital commerce company processes thousands of daily transactions across regions, products, channels and payment methods.

The finance team currently has several disconnected processes:

- SQL reports monitor revenue and payment KPIs.
- Analysts manually investigate data-quality problems.
- Finance teams reconcile bank and ledger transactions in Excel.
- Operations teams investigate duplicate refunds and unpaid deliveries.
- Managers receive manually prepared reports.
- Investigation decisions are stored in emails and spreadsheets.
- High-value exceptions can remain unresolved beyond SLA.

The proposed operating system creates one controlled workflow that:

1. Validates incoming data.
2. Calculates official KPIs.
3. Detects revenue leakage.
4. Identifies reconciliation breaks.
5. Converts exceptions into standardized cases.
6. Prioritizes cases by severity and financial exposure.
7. Generates AI-assisted explanations.
8. Sends cases for human review.
9. Records decisions and approvals.
10. Displays operational performance in Power BI.

---

## 3. Questions the system answers

Management should be able to answer:

- Are revenue and payment KPIs meeting targets?
- Can today's numbers be trusted?
- Where might revenue be leaking?
- What is the estimated financial exposure?
- Which cases require immediate investigation?
- Which exceptions are getting older?
- Which teams, regions or channels produce the most exceptions?
- How many findings were confirmed or rejected?
- How much confirmed loss was recovered?
- Are teams meeting investigation SLAs?
- What actions have reviewers approved?
- What changed compared with yesterday, last week or last month?

---

## 4. Complete architecture

```text
Orders / Payments / Refunds / Shipments / Bank / Ledger / Targets
                              ↓
                     PostgreSQL staging
                              ↓
              SQL validation and control layer
                  ├── Data-quality checks
                  ├── Official KPI calculations
                  ├── Target comparisons
                  └── Reconciliation controls
                              ↓
                 Python investigation engine
                  ├── Revenue-leakage rules
                  ├── Exposure calculation
                  ├── Case prioritization
                  └── Evidence collection
                              ↓
                    Unified findings table
                              ↓
                  Structured investigation JSON
                              ↓
                 Controlled AI investigator
                  ├── Explains findings
                  ├── Groups related cases
                  ├── Drafts investigation steps
                  └── Produces management brief
                              ↓
                    Excel human-review queue
                  ├── Accept or reject finding
                  ├── Add commentary
                  ├── Approve action
                  └── Record recovered amount
                              ↓
               Validated decisions imported to SQL
                              ↓
                  Power BI Control Tower
                  ├── Executive overview
                  ├── KPI health
                  ├── Revenue leakage
                  ├── Reconciliation and aging
                  └── Review performance
```

PostgreSQL remains the source of truth. AI never receives direct database-write access.

---

## 5. How the completed projects connect

### Existing SQL KPI project

The Agentic KPI and Data-Quality Investigator supplies:

- Daily KPIs
- KPI targets
- Target variances
- Data-quality findings
- Regional investigations
- Product investigations
- Payment-method investigations
- Pipeline run status

### Existing Python leakage project

The Revenue Leakage and Exception Triage Agent supplies:

- Duplicate refunds
- Refunds exceeding payments
- Excessive discounts
- Delivered-but-unpaid orders
- Pricing errors
- Unusual gateway fees
- Estimated financial exposure
- Severity and priority
- Evidence-linked findings

### New components required

The capstone must add:

- Unified case model
- Reconciliation controls
- Controlled AI investigator
- Excel review workflow
- Decision importer
- Power BI Control Tower
- Historical case events
- End-to-end orchestration
- Audit trail and documentation

---

## 6. Main system modules

### Module 1: Data ingestion

The system loads business data into PostgreSQL.

Recommended input files:

- `customers.csv`
- `products.csv`
- `orders.csv`
- `order_items.csv`
- `payments.csv`
- `refunds.csv`
- `shipments.csv`
- `bank_transactions.csv`
- `ledger_transactions.csv`
- `daily_kpi_targets.csv`
- `discount_policies.csv`
- `fee_rules.csv`
- `product_prices.csv`

Every run must receive a unique `run_id`.

Example:

```text
RUN-2026-09-12-001
```

This allows every metric, exception and decision to be traced back to its pipeline run.

### Module 2: SQL validation and control layer

SQL should perform deterministic controls such as:

- Missing mandatory values
- Duplicate primary keys
- Invalid dates
- Negative monetary values
- Orphan orders, payments or refunds
- Unknown product or customer IDs
- Invalid transaction statuses
- Order-total reconciliation
- Payment-total reconciliation
- Late-arriving source files
- KPI calculation
- KPI target comparison
- Bank-to-ledger reconciliation

Important outputs:

```text
finance.pipeline_runs
finance.data_quality_results
finance.daily_kpis
finance.reconciliation_breaks
finance.control_findings
```

### Module 3: Python investigation engine

Python handles more complex exception analysis:

- Revenue-leakage rules
- Cross-file investigation
- Evidence collection
- Risk scoring
- Aging calculations
- Related-case grouping
- Case generation
- JSON export

Python should not independently recalculate official SQL metrics unless required for validation.

### Module 4: Unified case-management layer

Every exception must use one standard structure, regardless of whether it came from SQL, Python or reconciliation.

Recommended fields:

| Field | Purpose |
|---|---|
| finding_id | Unique exception identifier |
| run_id | Pipeline execution that created it |
| source_module | KPI, data quality, leakage or reconciliation |
| rule_code | Control that failed |
| entity_type | Order, payment, refund, transaction or KPI |
| entity_id | Affected record |
| detected_at | Detection timestamp |
| severity | Critical, high, medium or low |
| risk_amount | Estimated financial exposure |
| evidence_json | Supporting facts |
| status | Current workflow status |
| owner | Assigned analyst |
| due_date | Investigation deadline |
| approval_required | Whether human approval is mandatory |

Recommended workflow:

```text
OPEN
  ↓
ASSIGNED
  ↓
UNDER_REVIEW
  ├── FALSE_POSITIVE
  ├── VALID_EXCEPTION
  └── MORE_INFORMATION_REQUIRED
          ↓
    ACTION_APPROVED
          ↓
       RESOLVED
```

AI cannot change these statuses.

### Module 5: Controlled AI investigator

The AI reads structured JSON, not uncontrolled raw data or unrestricted database tables.

It can:

- Explain why a case was detected.
- Summarize available evidence.
- Identify missing evidence.
- Group related exceptions.
- Draft investigation commentary.
- Recommend a next check.
- Produce a daily management brief.
- Highlight high-priority cases.
- Summarize reviewer decisions.

It cannot:

- Change official KPI values.
- Recalculate official financial exposure.
- Write directly to PostgreSQL.
- Issue refunds.
- Contact customers.
- Mark cases resolved.
- Approve journal entries.
- Claim that estimated exposure is confirmed loss.
- Invent evidence.

#### Defensible agentic loop

```text
Observe findings
      ↓
Check evidence completeness
      ↓
Prioritize cases
      ↓
Select an allowed recommendation
      ↓
Generate investigation package
      ↓
Request human review
      ↓
Pause until a decision is recorded
```

Allowed AI actions should be limited to:

- `draft_commentary`
- `recommend_next_check`
- `request_review`
- `flag_missing_evidence`
- `group_related_cases`
- `prepare_management_summary`

This is controlled agentic analytics, not unrestricted automation.

### Module 6: Excel human-review workbook

Excel becomes the operational review interface.

Recommended sheets:

1. **Instructions**
2. **Review Queue**
3. **Finding Evidence**
4. **Reviewer Decisions**
5. **Reconciliation**
6. **Control Totals**
7. **Management Summary**
8. **Lists and Rules**

Reviewers should record:

- Decision
- Reviewer name
- Review date
- Commentary
- Approved action
- Confirmed loss
- Recovered amount
- False-positive reason
- Resolution date

#### Safe write-back design

Do not use complicated VBA or direct Excel database writes initially.

Use this flow:

```text
PostgreSQL → Excel review queue
Excel reviewer → review_decisions.csv
Python validation → PostgreSQL decision tables
```

Python validates the decision file before recording anything.

### Module 7: Power BI Finance Control Tower

Build five report pages.

#### Page 1: Executive Overview

Show:

- Net realized revenue
- Estimated revenue at risk
- Confirmed loss
- Recovered amount
- Open cases
- Critical cases
- SLA breach rate
- Data-quality score
- KPI target achievement

#### Page 2: KPI Health

Show:

- Actual versus target
- Daily and weekly trends
- Target variance
- Revenue by region
- Payment-success rate
- Refund rate
- Data-quality warnings
- Drill-through to affected records

#### Page 3: Revenue Leakage

Show:

- Exposure by leakage rule
- Exposure by region
- Exposure by product
- Exposure by sales channel
- Exposure by payment method
- High-risk orders
- Duplicate refund cases
- Delivered-but-unpaid cases

#### Page 4: Reconciliation and Aging

Show:

- Matched transactions
- Unmatched transactions
- Match rate
- Ledger-only transactions
- Bank-only transactions
- Amount and date mismatches
- Aging buckets
- Historical open backlog
- SLA breaches

#### Page 5: Investigation Performance

Show:

- Open versus resolved findings
- Confirmed exception rate
- False-positive rate
- Median resolution time
- Cases by owner
- SLA performance
- Recovered amount
- AI recommendation acceptance rate
- Case drill-through and evidence

---

## 7. Important Power BI measures

Examples include:

```text
Net Realized Revenue =
Successful Payments - Processed Refunds - Gateway Fees
```

```text
Leakage Risk Rate =
Deduplicated Estimated Exposure / Successful Payment Value
```

```text
Resolution Rate =
Resolved Valid Cases / Total Valid Cases
```

```text
False Positive Rate =
False Positive Cases / Reviewed Cases
```

```text
SLA Breach Rate =
Open Cases Past Due / Open Cases
```

```text
Recovery Rate =
Recovered Amount / Confirmed Loss
```

```text
Reconciliation Match Rate =
Matched Transactions / Total Reconciliation Transactions
```

Important Power BI totals must reconcile with PostgreSQL source totals.

---

## 8. Recommended database model

### Business facts

- `fact_orders`
- `fact_order_items`
- `fact_payments`
- `fact_refunds`
- `fact_shipments`
- `fact_bank_transactions`
- `fact_ledger_transactions`

### Analytics and control facts

- `fact_daily_kpis`
- `fact_data_quality_results`
- `fact_findings`
- `fact_finding_events`
- `fact_reviews`
- `fact_reconciliation_results`
- `fact_pipeline_runs`

### Dimensions

- `dim_date`
- `dim_customer`
- `dim_product`
- `dim_region`
- `dim_payment_method`
- `dim_rule`
- `dim_owner`

The important distinction is:

- `fact_findings` stores the original detected case.
- `fact_finding_events` stores status changes.
- `fact_reviews` stores human decisions.
- Original findings must not be overwritten by AI or reviewers.

---

## 9. Required technology

### Mandatory

- PostgreSQL
- SQL
- Python 3.11 or newer
- Pandas
- JSON
- Microsoft Excel
- Power Query
- Power BI Desktop
- DAX
- Git and GitHub

### AI

Choose one:

- OpenAI API
- Claude API
- A local model if API cost is a concern

The system must work without an AI key. AI failure must not stop SQL controls, Python detection or reporting.

### Optional later

- Windows Task Scheduler or GitHub Actions
- Streamlit review interface
- Docker
- Cloud PostgreSQL
- Power Automate
- Microsoft Fabric
- dbt

None of these are required for the first portfolio version.

---

## 10. Functional requirements

The completed system must:

1. Load source data.
2. Validate required files and schemas.
3. Record a pipeline run.
4. Calculate official KPIs.
5. Compare KPIs with targets.
6. Detect data-quality issues.
7. Detect revenue-leakage exceptions.
8. Perform bank-to-ledger reconciliation.
9. Calculate risk and severity.
10. Create unified findings.
11. Export structured JSON.
12. Generate AI-assisted commentary.
13. Create an Excel review queue.
14. Validate reviewer decisions.
15. Record review decisions.
16. Refresh Power BI from approved database tables.
17. Preserve a complete audit history.
18. Produce a run summary.
19. Continue safely if AI is unavailable.
20. Prevent duplicate findings when the same run is repeated.

---

## 11. Non-functional requirements

### Reliability

The same input and rules should produce the same financial results.

### Idempotency

Running the same `run_id` twice should not create duplicate findings.

### Traceability

Every dashboard number should be traceable to a database table, metric definition and pipeline run.

### Explainability

Every exception must include its rule, affected record, calculation and evidence.

### Security

- Use synthetic or masked customer data.
- Keep API keys in environment variables.
- Do not commit secrets to GitHub.
- Send only necessary structured evidence to AI.
- Do not give AI database-write access.

### Failure handling

- Critical validation failures stop financial processing.
- Warning-level issues continue with disclosure.
- AI failure does not stop deterministic outputs.
- Invalid review files are rejected before database import.
- Failed runs are recorded with their error state.

---

## 12. Recommended project structure

```text
finance-analytics-agent-os/
│
├── data/
│   └── sample/
│
├── config/
│   └── policies.json
│
├── sql/
│   ├── 01_schema.sql
│   ├── 02_quality_and_kpis.sql
│   ├── 03_reconciliation.sql
│   └── 04_case_model.sql
│
├── src/
│   ├── run_pipeline.py
│   ├── leakage_engine.py
│   ├── ai_investigator.py
│   └── import_reviews.py
│
├── excel/
│   └── Finance_Review_Workbook.xlsx
│
├── powerbi/
│   └── Finance_Control_Tower.pbix
│
├── outputs/
│   ├── findings.json
│   ├── executive_brief.md
│   ├── review_queue.csv
│   └── run_summary.json
│
├── tests/
│   └── test_end_to_end.py
│
├── README.md
├── requirements.txt
└── architecture.png
```

Do not create many microservices or separate agents. One Python orchestrator is enough.

---

## 13. Minimum viable capstone

The first version should include:

- One coherent order-to-cash dataset
- SQL KPI and data-quality controls
- Six Python leakage rules
- Five reconciliation exception types
- Unified case model
- Structured JSON handoff
- One controlled AI investigator
- Excel human-review queue
- Four or five Power BI pages
- Review-decision importer
- One end-to-end test
- One deliberate failure demonstration
- Complete README and architecture

Skip cloud deployment and complex autonomous actions.

---

## 14. Build sequence

Because the KPI and leakage projects are already completed, use this order.

### Phase 1: Audit and reuse

- Confirm both completed projects run.
- Identify their output tables and files.
- Preserve their existing business logic.
- Do not rewrite calculations unnecessarily.

### Phase 2: Create the shared contract

Define:

- Common `run_id`
- Common finding schema
- Severity levels
- Case statuses
- Rule codes
- Evidence format
- Approval fields

This is the most important integration step.

### Phase 3: Build PostgreSQL case tables

Create:

- Pipeline runs
- Findings
- Finding events
- Reviews
- Reconciliation results

### Phase 4: Integrate SQL and Python outputs

Convert KPI, quality, leakage and reconciliation results into the common finding structure.

### Phase 5: Build the AI investigator

- Read approved JSON.
- Check evidence completeness.
- Draft explanations.
- Produce an executive brief.
- Fail safely when unavailable.

### Phase 6: Build Excel review

- Import open findings.
- Provide controlled decision fields.
- Export reviewer decisions.
- Validate and import decisions into PostgreSQL.

### Phase 7: Build Power BI

Start with:

1. Executive Overview
2. Revenue Leakage
3. Reconciliation and Aging
4. Investigation Performance

Add KPI Health after those pages reconcile correctly.

### Phase 8: Test and package

- Run end to end.
- Demonstrate one source-file failure.
- Demonstrate AI outage handling.
- Reconcile database and dashboard totals.
- Capture screenshots.
- Document assumptions and limitations.

---

## 15. Definition of done

The capstone is complete when:

- One command starts the deterministic workflow.
- Every run has a unique run ID.
- Source validation works.
- KPI totals reconcile.
- Leakage calculations reconcile.
- Reconciliation exceptions are correct.
- Findings follow one data contract.
- Re-running a run does not duplicate findings.
- AI receives structured evidence only.
- The system succeeds without AI.
- Human decisions are append-only and validated.
- Power BI totals match PostgreSQL.
- Historical backlog works for previous dates.
- At least one end-to-end test passes.
- One failure scenario is documented.
- README contains setup, architecture, rules and screenshots.
- The complete project can be demonstrated in approximately three minutes.

---

## 16. Domains where the project is useful

| Domain | Relevant use |
|---|---|
| E-commerce | Orders, refunds, discounts, deliveries and pricing |
| Fintech | Payments, settlements, fees, refunds and transaction exceptions |
| Banking | Reconciliation, payment operations, breaks, controls and approvals |
| Fund accounting | Cash or securities reconciliation, aged breaks and NAV controls |
| Consulting | Process transformation, control design and management reporting |
| Big 4 audit or advisory | Data-quality testing, exception analysis and audit trails |
| Retail | Pricing, discount leakage, returns and channel performance |
| SaaS | Billing, failed collections, credits and subscription leakage |
| Telecom | Revenue assurance, incorrect charges and payment exceptions |
| Insurance | Premium reconciliation, claims exceptions and approval workflows |
| Logistics | Delivered-but-unpaid orders, claims and operational SLA monitoring |

---

## 17. Role relevance

### Data Analyst

Demonstrates:

- SQL
- Python
- Data validation
- KPI calculation
- Data modelling
- Root-cause investigation
- Power BI
- Business communication

### Business Analyst

Demonstrates:

- Business-process mapping
- Stakeholder requirements
- Control rules
- Exception workflows
- Decision ownership
- UAT scenarios
- Acceptance criteria
- Process-improvement thinking

### Finance Analyst

Demonstrates:

- Revenue calculations
- Payment and refund controls
- Reconciliation
- Financial exposure
- Confirmed loss versus estimated risk
- Recovery tracking
- Management reporting

### BI Analyst

Demonstrates:

- Star-schema design
- Power Query
- DAX
- Historical backlog
- Drill-through
- Metric definitions
- Source reconciliation

### Risk or Fraud Analyst

Demonstrates:

- Rule-based detection
- Risk scoring
- Case prioritization
- Evidence collection
- False-positive measurement
- Investigation workflow

### Operations Analyst

Demonstrates:

- Backlog management
- SLA monitoring
- Aging analysis
- Owner performance
- Escalation logic
- Resolution tracking

### Consulting Analyst

Demonstrates:

- Current-state problem identification
- Future-state process design
- Control improvement
- Automation opportunity
- Governance
- Executive communication
- Measurable business impact

### Analytics Engineer

Demonstrates:

- Source-to-report pipelines
- Data contracts
- Reusable models
- Testing
- Run metadata
- Data lineage
- Reliable metric definitions

---

## 18. Why recruiters may notice it

Most entry-level candidates show:

- A static sales dashboard
- A notebook with basic charts
- A chatbot connected to CSV
- SQL queries without a business workflow

This project can demonstrate:

- A recurring financial-control process
- Multiple connected data sources
- Deterministic business logic
- Financial-impact measurement
- AI with clear restrictions
- Human approval
- Historical case tracking
- Executive and operational reporting
- Testing and failure handling

The differentiation is not simply “I used AI.” It is:

> “I designed a controlled analytics operating system that detects, investigates, explains and tracks financial exceptions without allowing AI to control financial truth.”

---

## 19. Resume-ready bullets

Use actual results after completing the project:

- Built an end-to-end finance analytics operating system integrating PostgreSQL, Python, Excel, Power BI and controlled AI to monitor KPIs, detect revenue leakage and manage reconciliation exceptions.

- Standardized SQL and Python control outputs into an auditable case-management model containing severity, financial exposure, supporting evidence, ownership, SLA and human-approval status.

- Developed Power BI control-tower reporting for KPI performance, estimated revenue exposure, reconciliation backlog, SLA breaches, false-positive rates and recovery outcomes.

- Implemented an evidence-restricted AI investigator that generated case explanations and management briefs while preventing autonomous database updates or financial decisions.

---

## 20. Best interview explanation

> “I had two separate analytics projects: a SQL KPI and data-quality investigator and a Python revenue-leakage agent. I integrated them into a finance analytics operating system. SQL calculates official KPIs and controls, while Python detects complex exceptions and standardizes cases. The system sends evidence-linked JSON to an AI investigator, which drafts explanations but cannot alter financial records. Analysts review cases in Excel, approved decisions are validated before being recorded in PostgreSQL, and Power BI monitors exposure, backlog, SLAs and resolution outcomes.”

The strongest technical discussion points will be:

- How duplicate findings are avoided
- How join multiplication is prevented
- How defensible exposure is calculated
- How estimated risk is separated from confirmed loss
- How historical backlog works
- How AI failure is handled
- How review decisions remain auditable
- How Power BI totals reconcile with PostgreSQL

---

## 21. Final recommendation

The capstone can be started without first building separate Excel and Power BI projects. Build the missing Excel reconciliation and Power BI components directly inside the capstone while reusing the completed SQL and Python logic.

The first implementation task is not AI or Power BI. It is defining the shared finding contract, run IDs, statuses and PostgreSQL case tables. Every later component depends on that foundation.

### Immediate implementation order

1. Audit the KPI and leakage outputs.
2. Define the shared finding contract.
3. Define run IDs, statuses, severity and approval rules.
4. Create PostgreSQL case-management tables.
5. Convert existing outputs into unified findings.
6. Add reconciliation controls.
7. Generate structured investigation JSON.
8. Add the controlled AI investigator.
9. Build the Excel review workflow.
10. Build and reconcile the Power BI Control Tower.
11. Add one end-to-end test and one failure demonstration.
12. Package the project for GitHub, resumes and interviews.

