# Finance Control Tower — Power BI Build Guide

The `.pbix` file is the one artifact in this project that cannot be generated from
code. Everything it needs, however, is already built: the star schema exists as
views in PostgreSQL, and every measure is written out in `dax_measures.md`.

This guide is the assembly step. Budget 60–90 minutes.

---

## 1. Connect

**Home → Get Data → PostgreSQL database**

| Field | Value |
|---|---|
| Server | `localhost:5432` |
| Database | `finance_capstone` |
| Data Connectivity mode | **Import** |

Import rather than DirectQuery. The dataset is small, import gives faster
visuals, and the numbers are meant to be a snapshot of a specific pipeline run
rather than a live feed — the `Run Context` measure puts that run's ID on the
page so nobody mistakes a snapshot for real time.

Select exactly these ten views from the `capstone` schema:

```
rpt_dim_date          rpt_fact_findings
rpt_dim_customer      rpt_fact_reviews
rpt_dim_rule          rpt_fact_daily_kpis
rpt_dim_owner         rpt_fact_reconciliation
rpt_dim_severity      rpt_fact_runs
rpt_dim_status        rpt_fact_evidence
rpt_control_totals
```

Do not import anything from `staging`. Those tables deliberately contain
unvalidated source data including known defects; putting them in the model
invites someone to build a visual over uncontrolled numbers.

---

## 2. Model relationships

**Model view.** Create each relationship below. All are single-direction,
one-to-many, from the dimension to the fact.

| From (1) | To (many) | Cardinality | Cross-filter |
|---|---|---|---|
| `rpt_dim_date[date_key]` | `rpt_fact_findings[business_date]` | One-to-many | Single |
| `rpt_dim_date[date_key]` | `rpt_fact_daily_kpis[metric_date]` | One-to-many | Single |
| `rpt_dim_date[date_key]` | `rpt_fact_reconciliation[business_date]` | One-to-many | Single |
| `rpt_dim_rule[rule_code]` | `rpt_fact_findings[rule_code]` | One-to-many | Single |
| `rpt_dim_severity[severity]` | `rpt_fact_findings[severity]` | One-to-many | Single |
| `rpt_dim_status[status]` | `rpt_fact_findings[status]` | One-to-many | Single |
| `rpt_dim_customer[customer_id]` | `rpt_fact_findings[customer_id]` | One-to-many | Single |
| `rpt_dim_owner[owner_id]` | `rpt_fact_findings[owner]` | One-to-many | Single |
| `rpt_fact_findings[finding_key]` | `rpt_fact_reviews[finding_key]` | One-to-many | Single |
| `rpt_fact_findings[finding_key]` | `rpt_fact_evidence[finding_key]` | One-to-many | Single |

`rpt_fact_runs` and `rpt_control_totals` stay **unrelated** to everything. Runs
feed a header text measure; control totals are the validation baseline and must
not be filtered by a slicer, or the comparison would move with the selection and
always appear to match.

### Two settings that will otherwise cost you an afternoon

**Mark the date table.** Select `rpt_dim_date` → Table tools → **Mark as date
table** → `date_key`. Without this, time-intelligence functions
(`DATESINPERIOD`, `DATEADD` in the trend measures) return wrong results silently
rather than erroring.

**Set sort columns**, or every categorical axis orders alphabetically:

| Table | Column | Sort by |
|---|---|---|
| `rpt_dim_severity` | `severity` | `severity_sort` |
| `rpt_dim_status` | `status` | `status_sort` |
| `rpt_dim_date` | `month_year` | `month_year_sort` |

Alphabetical severity puts LOW above MEDIUM, which makes every severity chart
actively misleading rather than merely untidy.

### Hide noise from the report view

Right-click → Hide in report view: every `*_key`, `*_sort`, and
`rpt_control_totals` (its values are reached only through validation measures).

---

## 3. Measures

Create all measures from `dax_measures.md`. Put them on `rpt_fact_findings`
except the KPI ones, which belong on `rpt_fact_daily_kpis`.

Formatting, applied per measure in Measure tools:

| Measure pattern | Format | Decimals |
|---|---|---|
| Exposure, loss, revenue, recovery | Currency, `₹` | 0 |
| Anything ending `%` | Decimal number | 1 |
| Counts, days | Whole number | 0 |

---

## 4. Pages

### Page 1 — Executive Overview

Card row across the top:

- `Net Realized Revenue`
- `Revenue at Risk`
- `Confirmed Loss`
- `Recovered Amount`
- `Open Cases`
- `Critical Cases`
- `SLA Breach Rate %`
- `Data Quality Score %`
- `KPI Target Achievement %`

Put `Revenue at Risk` and `Confirmed Loss` **side by side** and label them
"Estimated" and "Confirmed". Their whole purpose is to be read as a pair — one
is what rules suspect, the other what humans verified.

Body:

- Line chart — `Net Realized Revenue` and `Target Revenue` by `rpt_dim_date[date_key]`
- Donut — `Revenue at Risk` by `rpt_dim_rule[control_family]`
- Stacked column — `Open Cases` by `rpt_dim_date[month_year]`, legend `rpt_dim_severity[severity]`
- Text box, top-right — `Run Context`
- Card, small, top-right — `Model Integrity`

`Model Integrity` on the executive page is deliberate. A report that silently
disagrees with its source is worse than no report, so the reconciliation status
is visible to whoever is making decisions from it.

Conditional formatting: `SLA Breach Rate %` red above 20, amber 10–20, green
below 10.

### Page 2 — KPI Health

- Cards: `Net Realized Revenue`, `Target Revenue`, `Revenue Variance`, `Revenue Variance %`, `Payment Success Rate %`, `Refund Rate %`, `Days Below Revenue Target`, `Days Missing Target`
- Line and clustered column — columns `Net Realized Revenue`, line `Target Revenue`, axis `date_key`
- Line chart — `Payment Success Rate %` with a constant line at the target
- Line chart — `Revenue 7 Day Average`
- Bar — `Net Realized Revenue` by `rpt_fact_findings[region]`
- Matrix — rows `date_key`, values `Order Count`, `Net Realized Revenue`, `Revenue Variance`, `Payment Success Rate %`, `Refund Rate %`
- Slicers: date range, `region`, `sales_channel`

`Days Missing Target` earns a card because a day with no target produces no
variance and therefore no breach — the failure is invisible unless counted.

### Page 3 — Revenue Leakage

- Cards: `Leakage Exposure`, `Leakage Cases`, `Average Exposure per Case`, `Largest Single Exposure`, `High Value Cases`, `Duplicate Refund Exposure`, `Delivered Unpaid Exposure`
- Bar — `Gross Exposure` by `rpt_dim_rule[rule_code]`, descending
- Treemap — `Gross Exposure` by `region`
- Bar — `Gross Exposure` by `sales_channel`
- Bar — `Gross Exposure` by `customer_segment`
- Table — top 20 cases: `finding_id`, `rule_code`, `order_id`, `severity`, `risk_amount`, `priority_score`, `status`
- Slicers: `control_family`, `severity`, `status_group`

On the rule-level bar chart use `Gross Exposure`, not `Revenue at Risk`. Per-rule
double counting is correct and expected — the same order genuinely tripped both
rules. Add a subtitle: *"Gross — an order flagged by two rules appears in both.
Deduplicated total: [Revenue at Risk]."*

### Page 4 — Reconciliation and Aging

- Cards: `Reconciliation Match Rate %`, `Matched Transactions`, `Unmatched Transactions`, `Break Exposure`, `Bank Only Transactions`, `Ledger Only Transactions`, `Amount Mismatches`, `Date Mismatches`, `Duplicate Settlements`
- Donut — count by `match_status`
- Column — `Break Exposure` by `match_status`
- Column — `Aged Backlog Cases` by `aging_bucket`, sorted `0-7 → 90+`
- Cards: `Average Case Age Days`, `Oldest Open Case Days`, `Aged Backlog Exposure`
- Line — `Unmatched Transactions` by date
- Table — all breaks: `bank_txn_id`, `ledger_txn_id`, `match_status`, `bank_amount`, `ledger_amount`, `amount_difference`, `day_difference`, `aging_bucket`

Annotate the `Date Mismatches` card: *"Zero exposure — late, not lost."* It is
the first thing an interviewer will ask about, and the answer is a design
decision worth stating on the page.

### Page 5 — Investigation Performance

- Cards: `Reviewed Cases`, `Confirmed Exception Rate %`, `False Positive Rate %`, `Resolution Rate %`, `Median Resolution Days`, `Recovery Rate %`, `Cases Awaiting Approval`, `Revised Decisions`
- Funnel — `Total Cases` → `Reviewed Cases` → `Confirmed Exception Rate %` → `Resolution Rate %`
- Clustered bar — `Gross Exposure` and `Confirmed Loss` by `rule_code`, side by side
- Bar — `Reviewed Cases` by `rpt_fact_reviews[reviewer]`
- Column — decisions by `rpt_fact_reviews[decision]`
- Table — reviewer log: `finding_id`, `decision`, `reviewer`, `reviewed_at`, `estimated_exposure`, `confirmed_loss`, `recovered_amount`

The estimate-versus-confirmed bar chart is the most important visual in the
report. It is the only place that shows whether the automated detection is
actually any good, and it is the chart that proves the human review step changes
outcomes rather than rubber-stamping them.

### Page 6 — Model Validation (hide from navigation)

Every `Check *` measure as a card, plus `Model Integrity`. All must read zero.

---

## 5. Drill-through: case evidence

Create a page named **Case Evidence**, set Page information → **Drill-through**
→ add `rpt_fact_findings[finding_id]`.

On it:

- Card: `finding_id`, `rule_code`, `severity`, `risk_amount`, `status`, `due_date`
- Table: `rpt_fact_evidence[evidence_key]` and `[evidence_value]`
- Table: `rpt_fact_reviews` filtered to the case — the full decision history, not just the current one
- Text box: the rule's `exposure_basis` from `rpt_dim_rule`

This page is what makes the dashboard defensible. Any figure can be drilled to
the evidence behind it, and the evidence is sufficient to reproduce the amount by
hand — which is exactly the standard the finding contract sets.

---

## 6. Validate before declaring it done

Run this and compare against the report:

```bash
psql -d finance_capstone -c "SELECT * FROM capstone.rpt_control_totals;"
```

| Check | Where |
|---|---|
| `Total Cases` = `findings_total` | Page 6 |
| `Gross Exposure` = `gross_exposure` | Page 6 |
| `Revenue at Risk` = `deduplicated_exposure` | Page 6 |
| `Confirmed Loss` = `confirmed_loss` | Page 6 |
| `Net Realized Revenue` = `net_revenue` | Page 6 |
| `Reconciliation Match Rate %` = `match_rate_pct` | Page 4 |
| Every `Check *` measure reads 0 | Page 6 |
| `Model Integrity` reads "Reconciled to PostgreSQL" | Page 1 |

Then clear every slicer and confirm the totals still match. A filter left
applied on a hidden page is the most common reason a dashboard "loses" money
between refreshes.

Save as `powerbi/Finance_Control_Tower.pbix`.

---

## 7. Screenshots for the README

Capture at 1920×1080: each of the five pages, one drill-through showing case
evidence, and the validation page showing all zeros.

The validation screenshot is worth more than the pretty ones. Anyone can build a
dashboard; showing that its totals provably tie back to the database is the part
that distinguishes a reporting layer from a decorative one.
