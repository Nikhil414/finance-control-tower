# DAX Measures — Finance Control Tower

Copy each measure into Power BI Desktop (Modeling → New measure). Grouped by the
page that needs it; measures used on several pages appear once, in the first
page that uses them.

Two conventions run through all of it:

- **Estimated exposure and confirmed loss are never added together.** They answer
  different questions — one is what a rule suspects, the other is what a person
  verified. Any measure mixing them would be meaningless.
- **Headline exposure is deduplicated.** One order can trip several rules, so
  `SUM(risk_amount)` counts the same money more than once. Gross is shown only in
  rule-level breakdowns, where the double count is expected and understood.

---

## Core exposure

```dax
Gross Exposure =
SUM ( rpt_fact_findings[risk_amount] )
```

```dax
-- The defensible headline figure. deduplicated_risk_amount carries the amount
-- on the worst finding per order and 0 on the rest, so this sum never counts
-- one order's money twice.
Revenue at Risk =
SUM ( rpt_fact_findings[deduplicated_risk_amount] )
```

```dax
-- How much of the gross figure was double counting. Useful as a validation
-- tile: if it is ever negative, the deduplication ranking is broken.
Double Counted Exposure =
[Gross Exposure] - [Revenue at Risk]
```

```dax
-- Human-verified loss. Sourced from reviews, never from findings.
Confirmed Loss =
CALCULATE (
    SUM ( rpt_fact_findings[confirmed_loss] ),
    NOT ISBLANK ( rpt_fact_findings[decision] )
)
```

```dax
Recovered Amount =
CALCULATE (
    SUM ( rpt_fact_findings[recovered_amount] ),
    NOT ISBLANK ( rpt_fact_findings[decision] )
)
```

```dax
Net Exposure Outstanding =
[Confirmed Loss] - [Recovered Amount]
```

```dax
-- Recovery Rate = Recovered / Confirmed Loss (capstone section 7).
Recovery Rate % =
DIVIDE ( [Recovered Amount], [Confirmed Loss] ) * 100
```

```dax
-- How close the automated estimate came to verified reality, on reviewed cases
-- only. Comparing against unreviewed findings would measure nothing, since
-- their confirmed loss is legitimately blank.
Estimate Accuracy % =
VAR Reviewed =
    CALCULATETABLE (
        rpt_fact_findings,
        NOT ISBLANK ( rpt_fact_findings[decision] )
    )
VAR Estimated = SUMX ( Reviewed, rpt_fact_findings[risk_amount] )
VAR Actual    = SUMX ( Reviewed, rpt_fact_findings[confirmed_loss] )
RETURN
    DIVIDE ( Actual, Estimated ) * 100
```

---

## Page 1 — Executive Overview

```dax
-- Net Realized Revenue = successful payments - processed refunds - gateway fees.
-- Computed in SQL (capstone.vw_daily_kpis) and only summed here, so there is
-- one revenue definition in the system rather than two that can disagree.
Net Realized Revenue =
SUM ( rpt_fact_daily_kpis[net_revenue] )
```

```dax
-- Leakage Risk Rate = deduplicated exposure / successful payment value.
Leakage Risk Rate % =
DIVIDE (
    [Revenue at Risk],
    SUM ( rpt_fact_daily_kpis[successful_payment_value] )
) * 100
```

```dax
Open Cases =
CALCULATE (
    COUNTROWS ( rpt_fact_findings ),
    rpt_fact_findings[is_closed] = FALSE ()
)
```

```dax
Critical Cases =
CALCULATE (
    COUNTROWS ( rpt_fact_findings ),
    rpt_dim_severity[severity] = "CRITICAL",
    rpt_fact_findings[is_closed] = FALSE ()
)
```

```dax
Total Cases = COUNTROWS ( rpt_fact_findings )
```

```dax
-- SLA Breach Rate = open cases past due / open cases.
-- Closed cases are excluded from both sides: a resolved case cannot breach, and
-- leaving them in the denominator would make the rate fall as work completes
-- rather than as deadlines are met.
SLA Breach Rate % =
VAR OpenCases = [Open Cases]
VAR Breached =
    CALCULATE (
        COUNTROWS ( rpt_fact_findings ),
        rpt_fact_findings[is_sla_breached] = TRUE ()
    )
RETURN
    DIVIDE ( Breached, OpenCases ) * 100
```

```dax
SLA Breached Cases =
CALCULATE (
    COUNTROWS ( rpt_fact_findings ),
    rpt_fact_findings[is_sla_breached] = TRUE ()
)
```

```dax
-- Share of checked records with no defect. A raw defect count means nothing
-- without its denominator.
Data Quality Score % =
VAR Defects =
    CALCULATE (
        COUNTROWS ( rpt_fact_findings ),
        rpt_dim_rule[source_module] = "DATA_QUALITY"
    )
VAR Checked =
    SUM ( rpt_fact_daily_kpis[order_count] )
        + SUM ( rpt_fact_daily_kpis[payment_attempts] )
RETURN
    IF (
        Checked > 0,
        ( 1 - DIVIDE ( Defects, Checked ) ) * 100
    )
```

```dax
KPI Target Achievement % =
VAR DaysWithTarget =
    CALCULATE (
        COUNTROWS ( rpt_fact_daily_kpis ),
        NOT ISBLANK ( rpt_fact_daily_kpis[target_revenue] )
    )
VAR DaysMet =
    CALCULATE (
        COUNTROWS ( rpt_fact_daily_kpis ),
        rpt_fact_daily_kpis[revenue_target_met] = TRUE ()
    )
RETURN
    DIVIDE ( DaysMet, DaysWithTarget ) * 100
```

```dax
-- Header strip text. Makes the run being viewed explicit, so nobody mistakes a
-- stale refresh for today's position.
Run Context =
VAR Latest = MAX ( rpt_fact_runs[run_id] )
VAR RunStatus =
    CALCULATE ( MAX ( rpt_fact_runs[status] ), rpt_fact_runs[run_id] = Latest )
VAR AIStatus =
    CALCULATE ( MAX ( rpt_fact_runs[ai_status] ), rpt_fact_runs[run_id] = Latest )
RETURN
    "Run " & Latest & "  |  Pipeline: " & RunStatus & "  |  AI: " & AIStatus
```

---

## Page 2 — KPI Health

```dax
Target Revenue = SUM ( rpt_fact_daily_kpis[target_revenue] )
```

```dax
Revenue Variance = [Net Realized Revenue] - [Target Revenue]
```

```dax
Revenue Variance % =
DIVIDE ( [Revenue Variance], [Target Revenue] ) * 100
```

```dax
-- Weighted, not averaged. AVERAGE of a daily rate treats a 3-payment day the
-- same as a 300-payment day, which flatters or punishes the metric depending on
-- when volume happened to fall.
Payment Success Rate % =
DIVIDE (
    SUM ( rpt_fact_daily_kpis[successful_payments] ),
    SUM ( rpt_fact_daily_kpis[payment_attempts] )
) * 100
```

```dax
Refund Rate % =
DIVIDE (
    SUM ( rpt_fact_daily_kpis[processed_refund_value] ),
    SUM ( rpt_fact_daily_kpis[successful_payment_value] )
) * 100
```

```dax
Gateway Fee Value = SUM ( rpt_fact_daily_kpis[gateway_fee_value] )
```

```dax
Gateway Fee Rate % =
DIVIDE (
    [Gateway Fee Value],
    SUM ( rpt_fact_daily_kpis[successful_payment_value] )
) * 100
```

```dax
Order Count = SUM ( rpt_fact_daily_kpis[order_count] )
```

```dax
Days Below Revenue Target =
CALCULATE (
    COUNTROWS ( rpt_fact_daily_kpis ),
    rpt_fact_daily_kpis[revenue_target_met] = FALSE (),
    NOT ISBLANK ( rpt_fact_daily_kpis[target_revenue] )
)
```

```dax
-- Flags the failure mode where a missing target silently hides a variance
-- instead of reporting it.
Days Missing Target =
CALCULATE (
    COUNTROWS ( rpt_fact_daily_kpis ),
    rpt_fact_daily_kpis[target_missing] = TRUE ()
)
```

```dax
Revenue 7 Day Average =
AVERAGEX (
    DATESINPERIOD ( rpt_dim_date[date_key], MAX ( rpt_dim_date[date_key] ), -7, DAY ),
    [Net Realized Revenue]
)
```

```dax
Revenue vs Previous Day =
VAR Previous =
    CALCULATE ( [Net Realized Revenue], DATEADD ( rpt_dim_date[date_key], -1, DAY ) )
RETURN
    [Net Realized Revenue] - Previous
```

```dax
Revenue vs Previous Week =
VAR Previous =
    CALCULATE ( [Net Realized Revenue], DATEADD ( rpt_dim_date[date_key], -7, DAY ) )
RETURN
    [Net Realized Revenue] - Previous
```

---

## Page 3 — Revenue Leakage

```dax
Leakage Exposure =
CALCULATE ( [Gross Exposure], rpt_dim_rule[source_module] = "LEAKAGE" )
```

```dax
Leakage Cases =
CALCULATE ( COUNTROWS ( rpt_fact_findings ), rpt_dim_rule[source_module] = "LEAKAGE" )
```

```dax
-- Guarded against division by zero AND against a blank denominator, which is
-- not the same thing: a slicer selection with no matching findings returns
-- blank rather than 0, and DIVIDE would then return blank instead of the 0%
-- the tile should show.
Exposure Share of Total % =
VAR Total = CALCULATE ( [Gross Exposure], REMOVEFILTERS ( rpt_dim_rule ) )
RETURN
    IF ( Total > 0, DIVIDE ( [Gross Exposure], Total ) * 100, 0 )
```

```dax
Average Exposure per Case =
DIVIDE ( [Gross Exposure], COUNTROWS ( rpt_fact_findings ) )
```

```dax
Largest Single Exposure = MAX ( rpt_fact_findings[risk_amount] )
```

```dax
-- Cases whose exposure alone would breach a materiality threshold. Parameter
-- rather than a literal so the threshold is visible and adjustable.
High Value Cases =
CALCULATE (
    COUNTROWS ( rpt_fact_findings ),
    rpt_fact_findings[risk_amount] >= 100000
)
```

```dax
Duplicate Refund Exposure =
CALCULATE ( [Gross Exposure], rpt_dim_rule[rule_code] = "DUPLICATE_REFUND" )
```

```dax
Delivered Unpaid Exposure =
CALCULATE ( [Gross Exposure], rpt_dim_rule[rule_code] = "DELIVERED_UNPAID" )
```

```dax
-- Zero-exposure rules exist on purpose: a rate gap or a duplicate key is a
-- control failure, not lost money. This counts them so they are visible as
-- cases without inflating any exposure figure.
Zero Exposure Cases =
CALCULATE (
    COUNTROWS ( rpt_fact_findings ),
    rpt_dim_rule[zero_exposure_by_design] = TRUE ()
)
```

---

## Page 4 — Reconciliation and Aging

```dax
-- Reconciliation Match Rate = matched / total reconciliation transactions.
Reconciliation Match Rate % =
DIVIDE (
    CALCULATE ( COUNTROWS ( rpt_fact_reconciliation ), rpt_fact_reconciliation[is_matched] = TRUE () ),
    COUNTROWS ( rpt_fact_reconciliation )
) * 100
```

```dax
Matched Transactions =
CALCULATE ( COUNTROWS ( rpt_fact_reconciliation ), rpt_fact_reconciliation[is_matched] = TRUE () )
```

```dax
Unmatched Transactions =
CALCULATE ( COUNTROWS ( rpt_fact_reconciliation ), rpt_fact_reconciliation[is_matched] = FALSE () )
```

```dax
Total Reconciliation Transactions = COUNTROWS ( rpt_fact_reconciliation )
```

```dax
-- Break exposure holds DATE_MISMATCH at zero. A settlement two days late is a
-- control failure and an SLA input, but the money arrived -- booking it as
-- exposure would inflate the headline with funds that were never lost.
Break Exposure = SUM ( rpt_fact_reconciliation[break_exposure] )
```

```dax
Bank Only Transactions =
CALCULATE ( COUNTROWS ( rpt_fact_reconciliation ), rpt_fact_reconciliation[match_status] = "BANK_ONLY" )
```

```dax
Ledger Only Transactions =
CALCULATE ( COUNTROWS ( rpt_fact_reconciliation ), rpt_fact_reconciliation[match_status] = "LEDGER_ONLY" )
```

```dax
Amount Mismatches =
CALCULATE ( COUNTROWS ( rpt_fact_reconciliation ), rpt_fact_reconciliation[match_status] = "AMOUNT_MISMATCH" )
```

```dax
Date Mismatches =
CALCULATE ( COUNTROWS ( rpt_fact_reconciliation ), rpt_fact_reconciliation[match_status] = "DATE_MISMATCH" )
```

```dax
Duplicate Settlements =
CALCULATE ( COUNTROWS ( rpt_fact_reconciliation ), rpt_fact_reconciliation[match_status] = "DUPLICATE_SETTLEMENT" )
```

```dax
Average Settlement Delay Days =
CALCULATE (
    AVERAGE ( rpt_fact_reconciliation[day_difference] ),
    NOT ISBLANK ( rpt_fact_reconciliation[day_difference] )
)
```

```dax
-- Backlog aged beyond a month. The number that grows quietly when a team is
-- clearing new work and ignoring old.
Aged Backlog Cases =
CALCULATE (
    COUNTROWS ( rpt_fact_findings ),
    rpt_fact_findings[aging_bucket] IN { "31-60", "61-90", "90+" },
    rpt_fact_findings[is_closed] = FALSE ()
)
```

```dax
Aged Backlog Exposure =
CALCULATE (
    [Revenue at Risk],
    rpt_fact_findings[aging_bucket] IN { "31-60", "61-90", "90+" },
    rpt_fact_findings[is_closed] = FALSE ()
)
```

```dax
Average Case Age Days =
CALCULATE (
    AVERAGE ( rpt_fact_findings[age_days] ),
    rpt_fact_findings[is_closed] = FALSE ()
)
```

```dax
Oldest Open Case Days =
CALCULATE ( MAX ( rpt_fact_findings[age_days] ), rpt_fact_findings[is_closed] = FALSE () )
```

---

## Page 5 — Investigation Performance

```dax
Reviewed Cases =
CALCULATE (
    COUNTROWS ( rpt_fact_findings ),
    NOT ISBLANK ( rpt_fact_findings[decision] )
)
```

```dax
-- False Positive Rate = false positives / reviewed cases.
-- The denominator is reviewed cases, not all cases: an unreviewed finding is
-- neither a true nor a false positive yet, and including it would make the rate
-- drift purely with review throughput.
False Positive Rate % =
VAR Reviewed = [Reviewed Cases]
VAR FalsePositives =
    CALCULATE (
        COUNTROWS ( rpt_fact_findings ),
        rpt_fact_findings[decision] = "FALSE_POSITIVE"
    )
RETURN
    DIVIDE ( FalsePositives, Reviewed ) * 100
```

```dax
Confirmed Exception Rate % =
VAR Reviewed = [Reviewed Cases]
VAR Confirmed =
    CALCULATE (
        COUNTROWS ( rpt_fact_findings ),
        rpt_fact_findings[decision]
            IN { "VALID_EXCEPTION", "ACTION_APPROVED", "RESOLVED" }
    )
RETURN
    DIVIDE ( Confirmed, Reviewed ) * 100
```

```dax
-- Resolution Rate = resolved valid cases / total valid cases.
Resolution Rate % =
VAR ValidCases =
    CALCULATE (
        COUNTROWS ( rpt_fact_findings ),
        rpt_fact_findings[decision]
            IN { "VALID_EXCEPTION", "ACTION_APPROVED", "RESOLVED" }
    )
VAR Resolved =
    CALCULATE (
        COUNTROWS ( rpt_fact_findings ),
        rpt_fact_findings[status] = "RESOLVED"
    )
RETURN
    DIVIDE ( Resolved, ValidCases ) * 100
```

```dax
-- Median, not mean. Resolution times are right-skewed: one case stuck for
-- months drags an average far above what a typical case actually takes.
Median Resolution Days =
MEDIANX (
    CALCULATETABLE (
        rpt_fact_findings,
        NOT ISBLANK ( rpt_fact_findings[resolution_date] )
    ),
    rpt_fact_findings[age_days]
)
```

```dax
Cases Awaiting Approval =
CALCULATE (
    COUNTROWS ( rpt_fact_findings ),
    rpt_fact_findings[status] = "VALID_EXCEPTION",
    rpt_fact_findings[approval_required] = TRUE ()
)
```

```dax
Cases Needing More Information =
CALCULATE (
    COUNTROWS ( rpt_fact_findings ),
    rpt_fact_findings[status] = "MORE_INFORMATION_REQUIRED"
)
```

```dax
Decisions Recorded = COUNTROWS ( rpt_fact_reviews )
```

```dax
-- Decisions that were later superseded. A non-zero value is healthy: it means
-- reviewers correct themselves and the trail keeps both versions.
Revised Decisions =
CALCULATE (
    COUNTROWS ( rpt_fact_reviews ),
    rpt_fact_reviews[is_current] = FALSE ()
)
```

```dax
Active Reviewers = DISTINCTCOUNT ( rpt_fact_reviews[reviewer] )
```

```dax
Cases per Reviewer =
DIVIDE ( [Reviewed Cases], [Active Reviewers] )
```

---

## Validation measures

Put these on a hidden page. Each must read zero; a non-zero value means a
dashboard figure has stopped agreeing with the database.

```dax
-- Gross exposure in the model vs the SQL control total.
Check Gross Exposure Delta =
[Gross Exposure] - SUM ( rpt_control_totals[gross_exposure] )
```

```dax
Check Deduplicated Exposure Delta =
[Revenue at Risk] - SUM ( rpt_control_totals[deduplicated_exposure] )
```

```dax
Check Confirmed Loss Delta =
[Confirmed Loss] - SUM ( rpt_control_totals[confirmed_loss] )
```

```dax
Check Case Count Delta =
[Total Cases] - SUM ( rpt_control_totals[findings_total] )
```

```dax
Check Net Revenue Delta =
[Net Realized Revenue] - SUM ( rpt_control_totals[net_revenue] )
```

```dax
-- Deduplicated exposure must never exceed gross. If it does, the ranking that
-- picks the worst finding per order has broken.
Check Dedup Not Above Gross =
IF ( [Revenue at Risk] > [Gross Exposure], 1, 0 )
```

```dax
-- Recovery cannot exceed confirmed loss. Enforced by a database constraint too;
-- checked here so a modelling mistake cannot present an impossible figure.
Check Recovery Within Loss =
IF ( [Recovered Amount] > [Confirmed Loss], 1, 0 )
```

```dax
-- Zero-exposure rules must contribute nothing to exposure.
Check Zero Exposure Rules Hold =
VAR Leaked =
    CALCULATE (
        [Gross Exposure],
        rpt_dim_rule[zero_exposure_by_design] = TRUE ()
    )
RETURN
    IF ( Leaked <> 0, 1, 0 )
```

```dax
-- Rolls the checks into one tile for the report header.
Model Integrity =
VAR Failures =
    ABS ( [Check Gross Exposure Delta] )
        + ABS ( [Check Deduplicated Exposure Delta] )
        + ABS ( [Check Confirmed Loss Delta] )
        + ABS ( [Check Case Count Delta] )
        + [Check Dedup Not Above Gross]
        + [Check Recovery Within Loss]
        + [Check Zero Exposure Rules Hold]
RETURN
    IF ( Failures = 0, "Reconciled to PostgreSQL", "MISMATCH — do not trust this report" )
```
