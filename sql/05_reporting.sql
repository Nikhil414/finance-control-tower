-- ============================================================================
-- Reporting layer: the star schema Power BI imports.
--
-- Views, not tables. A materialised reporting layer would need a refresh step,
-- and the moment that step can lag, a dashboard can disagree with the database
-- it claims to report. Views cannot drift.
--
-- Naming: rpt_dim_* and rpt_fact_* so the Power BI model reads as a star
-- without renaming anything on import. Every measure in
-- powerbi/dax_measures.md resolves to one of these.
--
-- Run after 04_case_model.sql. Idempotent.
-- ============================================================================


-- ---------------------------------------------------------------- dimensions

CREATE OR REPLACE VIEW capstone.rpt_dim_date AS
SELECT
    date_key,
    year,
    quarter,
    'Q' || quarter                       AS quarter_name,
    month,
    month_name,
    TO_CHAR(date_key, 'Mon YYYY')        AS month_year,
    -- Sort key: Power BI orders text alphabetically unless told otherwise, so
    -- "Apr 2026" would precede "Aug 2026" precede "Feb 2026" on every axis.
    (year * 100 + month)                 AS month_year_sort,
    day_of_month,
    day_of_week,
    day_name,
    is_weekend,
    iso_week
FROM capstone.dim_date;


CREATE OR REPLACE VIEW capstone.rpt_dim_customer AS
SELECT
    customer_id,
    customer_segment,
    region,
    risk_level,
    signup_date
FROM staging.customers
WHERE customer_id IS NOT NULL;


CREATE OR REPLACE VIEW capstone.rpt_dim_rule AS
SELECT
    r.rule_code,
    r.source_module,
    r.entity_type,
    r.default_severity,
    r.approval_required,
    r.zero_exposure_by_design,
    r.exposure_basis,
    -- Grouping for the leakage page, which reports by control family rather
    -- than by individual rule.
    CASE r.source_module
        WHEN 'LEAKAGE'        THEN 'Revenue Leakage'
        WHEN 'RECONCILIATION' THEN 'Settlement Reconciliation'
        WHEN 'DATA_QUALITY'   THEN 'Data Quality'
        WHEN 'KPI'            THEN 'KPI Variance'
    END AS control_family
FROM capstone.dim_rule r;


CREATE OR REPLACE VIEW capstone.rpt_dim_owner AS
SELECT owner_id, owner_name, team, is_approver, active
FROM capstone.dim_owner;


-- A severity dimension exists purely to control sort order. Without it every
-- chart lists CRITICAL/HIGH/LOW/MEDIUM alphabetically, which puts LOW above
-- MEDIUM and makes the whole visual misleading at a glance.
CREATE OR REPLACE VIEW capstone.rpt_dim_severity AS
SELECT * FROM (VALUES
    ('CRITICAL', 1, 'Immediate'),
    ('HIGH',     2, 'Same week'),
    ('MEDIUM',   3, 'Routine'),
    ('LOW',      4, 'Monitor')
) AS s(severity, severity_sort, response_expectation);


CREATE OR REPLACE VIEW capstone.rpt_dim_status AS
SELECT * FROM (VALUES
    ('OPEN',                      1, 'Awaiting pickup',   FALSE),
    ('ASSIGNED',                  2, 'Awaiting pickup',   FALSE),
    ('UNDER_REVIEW',              3, 'In progress',       FALSE),
    ('MORE_INFORMATION_REQUIRED', 4, 'In progress',       FALSE),
    ('VALID_EXCEPTION',           5, 'Confirmed',         FALSE),
    ('ACTION_APPROVED',           6, 'Confirmed',         FALSE),
    ('FALSE_POSITIVE',            7, 'Closed',            TRUE),
    ('RESOLVED',                  8, 'Closed',            TRUE)
) AS s(status, status_sort, status_group, is_closed);


-- ---------------------------------------------------------------- facts

-- Case-level grain: one row per finding, with the current human position joined
-- on. This is the table most pages slice.
CREATE OR REPLACE VIEW capstone.rpt_fact_findings AS
SELECT
    f.finding_key,
    f.finding_id,
    f.run_id AS first_seen_run_id,
    f.last_seen_run_id,
    f.times_seen,
    (f.times_seen > 1) AS is_recurring,
    f.source_module,
    f.rule_code,
    f.entity_type,
    f.entity_id,
    f.order_id,
    f.customer_id,
    f.business_date,
    f.detected_at,
    f.severity,
    f.status,
    f.owner,
    f.due_date,
    f.approval_required,
    f.priority_score,
    f.days_open,

    -- Estimated exposure. Never summed with confirmed loss.
    f.risk_amount,

    -- Ranking the worst finding per order, so a deduplicated exposure measure
    -- can be written in DAX without a per-visual subquery. Computed here
    -- because the same logic in DAX would have to be repeated in every measure
    -- that needs it.
    CASE
        WHEN ROW_NUMBER() OVER (
            PARTITION BY f.last_seen_run_id, COALESCE(f.order_id, f.entity_id)
            ORDER BY f.risk_amount DESC, f.finding_key
        ) = 1 THEN f.risk_amount
        ELSE 0
    END AS deduplicated_risk_amount,

    -- Human outcome, null until reviewed.
    cr.decision,
    cr.reviewer,
    cr.reviewed_at,
    cr.confirmed_loss,
    cr.recovered_amount,
    cr.resolution_date,

    st.is_closed,
    CASE
        WHEN st.is_closed THEN FALSE
        WHEN CURRENT_DATE > f.due_date THEN TRUE
        ELSE FALSE
    END AS is_sla_breached,

    CASE
        WHEN cr.resolution_date IS NOT NULL
            THEN cr.resolution_date - f.detected_at::date
        ELSE CURRENT_DATE - f.detected_at::date
    END AS age_days,

    CASE
        WHEN (CURRENT_DATE - f.detected_at::date) <= 7  THEN '0-7'
        WHEN (CURRENT_DATE - f.detected_at::date) <= 30 THEN '8-30'
        WHEN (CURRENT_DATE - f.detected_at::date) <= 60 THEN '31-60'
        WHEN (CURRENT_DATE - f.detected_at::date) <= 90 THEN '61-90'
        ELSE '90+'
    END AS aging_bucket,

    -- Order context, so the leakage page can slice exposure by channel and
    -- region without a second fact table.
    o.sales_channel,
    COALESCE(o.region, c.region) AS region,
    c.customer_segment
FROM capstone.fact_findings f
LEFT JOIN capstone.vw_current_review cr ON cr.finding_key = f.finding_key
LEFT JOIN capstone.rpt_dim_status st    ON st.status = f.status
LEFT JOIN staging.orders o              ON o.order_id = f.order_id
LEFT JOIN staging.customers c           ON c.customer_id = f.customer_id;


COMMENT ON COLUMN capstone.rpt_fact_findings.deduplicated_risk_amount IS
    'risk_amount on the worst finding per order, 0 on the rest. Summing this '
    'column gives defensible exposure; summing risk_amount gives gross, which '
    'double-counts an order flagged by several rules.';


-- Review grain: every decision ever recorded, not just the current one. Needed
-- for reviewer throughput and for showing that a decision was revised.
CREATE OR REPLACE VIEW capstone.rpt_fact_reviews AS
SELECT
    r.review_id,
    r.finding_key,
    r.finding_id,
    r.decision,
    r.reviewer,
    r.reviewed_at,
    r.reviewed_at::date AS reviewed_date,
    r.confirmed_loss,
    r.recovered_amount,
    r.resolution_date,
    f.rule_code,
    f.source_module,
    f.severity,
    f.risk_amount AS estimated_exposure,
    -- Marks the decision that currently stands, so a measure can count either
    -- all decisions made or only the ones in force.
    (cr.finding_key IS NOT NULL AND cr.reviewed_at = r.reviewed_at) AS is_current
FROM capstone.fact_reviews r
JOIN capstone.fact_findings f USING (finding_key)
LEFT JOIN capstone.vw_current_review cr
       ON cr.finding_key = r.finding_key AND cr.reviewed_at = r.reviewed_at;


CREATE OR REPLACE VIEW capstone.rpt_fact_daily_kpis AS
SELECT
    metric_date,
    order_count,
    gross_order_value,
    discount_value,
    payment_attempts,
    successful_payments,
    successful_payment_value,
    gateway_fee_value,
    processed_refunds,
    processed_refund_value,
    net_revenue,
    payment_success_rate,
    refund_rate_pct,
    target_revenue,
    target_payment_success_rate * 100 AS target_payment_success_rate_pct,
    target_order_count,
    max_refund_rate_pct,
    target_missing,
    net_revenue - target_revenue                      AS revenue_variance,
    order_count - target_order_count                  AS order_count_variance,
    (target_revenue IS NOT NULL AND net_revenue >= target_revenue) AS revenue_target_met
FROM capstone.vw_daily_kpis;


CREATE OR REPLACE VIEW capstone.rpt_fact_reconciliation AS
SELECT
    rr.recon_id,
    rr.run_id,
    rr.bank_txn_id,
    rr.ledger_txn_id,
    rr.match_status,
    rr.bank_amount,
    rr.ledger_amount,
    rr.amount_difference,
    rr.bank_value_date,
    rr.ledger_posting_date,
    rr.day_difference,
    rr.business_date,
    rr.aging_bucket,
    rr.finding_key,
    (rr.match_status = 'MATCHED') AS is_matched,
    -- Break exposure, with date mismatches held at zero. Late money is not lost
    -- money, and including it would inflate the headline with funds that
    -- arrived.
    CASE rr.match_status
        WHEN 'MATCHED'         THEN 0
        WHEN 'DATE_MISMATCH'   THEN 0
        WHEN 'AMOUNT_MISMATCH' THEN ABS(COALESCE(rr.amount_difference, 0))
        ELSE ABS(COALESCE(rr.bank_amount, rr.ledger_amount, 0))
    END AS break_exposure
FROM capstone.fact_reconciliation_results rr;


-- Run metadata, for the header strip that tells a viewer which run they are
-- looking at and whether it succeeded.
CREATE OR REPLACE VIEW capstone.rpt_fact_runs AS
SELECT
    run_id,
    run_date,
    started_at,
    finished_at,
    status,
    contract_version,
    orders_loaded,
    payments_loaded,
    refunds_loaded,
    bank_txns_loaded,
    ledger_txns_loaded,
    findings_created,
    findings_skipped,
    ai_status,
    ai_failure_reason,
    error_message,
    EXTRACT(EPOCH FROM (finished_at - started_at))::int AS duration_seconds
FROM capstone.fact_pipeline_runs;


-- Drill-through target: evidence as rows, so a user can click a case and read
-- the calculation behind the amount rather than being shown a JSON blob.
CREATE OR REPLACE VIEW capstone.rpt_fact_evidence AS
SELECT
    f.finding_key,
    f.finding_id,
    f.rule_code,
    e.key   AS evidence_key,
    e.value #>> '{}' AS evidence_value
FROM capstone.fact_findings f
CROSS JOIN LATERAL jsonb_each(f.evidence_json) AS e(key, value);


-- ---------------------------------------------------------------- validation

-- The reconciliation check between database and dashboard. Power BI totals must
-- equal these numbers; a page that disagrees is wrong, because the database is
-- the source of truth and the report is a view of it.
CREATE OR REPLACE VIEW capstone.rpt_control_totals AS
SELECT
    (SELECT COUNT(*) FROM capstone.fact_findings)                        AS findings_total,
    (SELECT COALESCE(SUM(risk_amount), 0) FROM capstone.fact_findings)   AS gross_exposure,
    (SELECT COALESCE(SUM(deduplicated_risk_amount), 0)
       FROM capstone.rpt_fact_findings)                                  AS deduplicated_exposure,
    (SELECT COALESCE(SUM(confirmed_loss), 0) FROM capstone.vw_current_review)
                                                                         AS confirmed_loss,
    (SELECT COALESCE(SUM(recovered_amount), 0) FROM capstone.vw_current_review)
                                                                         AS recovered_amount,
    (SELECT COUNT(*) FROM capstone.fact_findings WHERE status NOT IN ('RESOLVED', 'FALSE_POSITIVE'))
                                                                         AS open_cases,
    (SELECT COUNT(*) FROM capstone.fact_findings WHERE severity = 'CRITICAL')
                                                                         AS critical_cases,
    (SELECT COALESCE(SUM(net_revenue), 0) FROM capstone.vw_daily_kpis)   AS net_revenue,
    (SELECT match_rate_pct FROM capstone.vw_reconciliation_summary)      AS match_rate_pct,
    (SELECT data_quality_score_pct FROM capstone.vw_data_quality_score)  AS data_quality_score_pct,
    (SELECT COUNT(*) FROM capstone.fact_reviews)                         AS reviews_recorded;
