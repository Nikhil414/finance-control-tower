-- ============================================================================
-- SQL control layer: official KPIs and data-quality controls.
--
-- This file owns financial truth. Every number a dashboard shows for revenue,
-- payment success or refund rate is computed here and nowhere else. Python does
-- not recalculate these -- it detects a different class of exception entirely.
-- One definition, one place, no drift.
--
-- Each control emits the shared finding contract's shape, so the loader can
-- read every control through one view without knowing which rule produced a
-- row. Severity, priority and SLA are deliberately NOT set here: the severity
-- ladder lives in src/contract.py so it exists once rather than twice in two
-- languages that could disagree.
--
-- Run after 01_schema.sql. Idempotent.
-- ============================================================================


-- ---------------------------------------------------------------- KPI base
--
-- Join multiplication is the trap in all of this. Orders, payments, refunds and
-- shipments are all one-to-many, so joining them directly multiplies rows and
-- inflates every SUM. Each grain is therefore aggregated to one row per day in
-- its own CTE before anything is combined, and the combination happens on the
-- date spine. This is the single most important structural decision in the file.

CREATE OR REPLACE VIEW capstone.vw_daily_kpis AS
WITH date_spine AS (
    SELECT DISTINCT order_date AS metric_date
    FROM staging.orders
    WHERE order_date IS NOT NULL
    UNION
    SELECT DISTINCT metric_date FROM staging.daily_kpi_targets
),
order_daily AS (
    SELECT
        order_date AS metric_date,
        COUNT(*) FILTER (WHERE COALESCE(order_status, '') <> 'Cancelled') AS order_count,
        COALESCE(SUM(gross_amount) FILTER (WHERE COALESCE(order_status, '') <> 'Cancelled'), 0) AS gross_order_value,
        COALESCE(SUM(discount_amount) FILTER (WHERE COALESCE(order_status, '') <> 'Cancelled'), 0) AS discount_value
    FROM staging.orders
    WHERE order_date IS NOT NULL
    GROUP BY order_date
),
payment_daily AS (
    SELECT
        payment_date::date AS metric_date,
        COUNT(*)                                                      AS payment_attempts,
        COUNT(*) FILTER (WHERE payment_status = 'Successful')          AS successful_payments,
        COALESCE(SUM(payment_amount) FILTER (WHERE payment_status = 'Successful'), 0) AS successful_payment_value,
        COALESCE(SUM(gateway_fee)    FILTER (WHERE payment_status = 'Successful'), 0) AS gateway_fee_value
    FROM staging.payments
    WHERE payment_date IS NOT NULL
    GROUP BY payment_date::date
),
refund_daily AS (
    SELECT
        refund_date::date AS metric_date,
        COUNT(*) FILTER (WHERE refund_status = 'Processed')            AS processed_refunds,
        COALESCE(SUM(refund_amount) FILTER (WHERE refund_status = 'Processed'), 0) AS processed_refund_value
    FROM staging.refunds
    WHERE refund_date IS NOT NULL
    GROUP BY refund_date::date
)
SELECT
    spine.metric_date,

    COALESCE(o.order_count, 0)               AS order_count,
    COALESCE(o.gross_order_value, 0)::NUMERIC(14, 2) AS gross_order_value,
    COALESCE(o.discount_value, 0)::NUMERIC(14, 2)    AS discount_value,

    COALESCE(p.payment_attempts, 0)          AS payment_attempts,
    COALESCE(p.successful_payments, 0)       AS successful_payments,
    COALESCE(p.successful_payment_value, 0)::NUMERIC(14, 2) AS successful_payment_value,
    COALESCE(p.gateway_fee_value, 0)::NUMERIC(14, 2)        AS gateway_fee_value,

    COALESCE(r.processed_refunds, 0)         AS processed_refunds,
    COALESCE(r.processed_refund_value, 0)::NUMERIC(14, 2) AS processed_refund_value,

    -- THE official revenue definition: successful payments, less processed
    -- refunds, less gateway fees. Every "revenue" figure downstream resolves to
    -- this expression.
    (COALESCE(p.successful_payment_value, 0)
     - COALESCE(r.processed_refund_value, 0)
     - COALESCE(p.gateway_fee_value, 0))::NUMERIC(14, 2) AS net_revenue,

    -- Rates are NULL rather than 0 when the denominator is empty. A day with no
    -- payment attempts has an undefined success rate, and reporting it as 0%
    -- would fabricate a failure that never happened.
    CASE WHEN COALESCE(p.payment_attempts, 0) > 0
         THEN ROUND(p.successful_payments::numeric / p.payment_attempts * 100, 2)
    END AS payment_success_rate,

    CASE WHEN COALESCE(p.successful_payment_value, 0) > 0
         THEN ROUND(COALESCE(r.processed_refund_value, 0) / p.successful_payment_value * 100, 3)
    END AS refund_rate_pct,

    t.target_revenue,
    t.target_payment_success_rate,
    t.target_order_count,
    t.max_refund_rate_pct,
    (t.metric_date IS NULL) AS target_missing
FROM date_spine spine
LEFT JOIN order_daily   o ON o.metric_date = spine.metric_date
LEFT JOIN payment_daily p ON p.metric_date = spine.metric_date
LEFT JOIN refund_daily  r ON r.metric_date = spine.metric_date
LEFT JOIN staging.daily_kpi_targets t ON t.metric_date = spine.metric_date;


COMMENT ON VIEW capstone.vw_daily_kpis IS
    'Official daily KPIs. net_revenue = successful payments - processed refunds '
    '- gateway fees. Each grain is pre-aggregated in its own CTE before joining '
    'to avoid the row multiplication that would otherwise inflate every SUM.';


-- ---------------------------------------------------------------- KPI controls

CREATE OR REPLACE VIEW capstone.vw_kpi_findings AS

-- Revenue shortfall. The only KPI rule carrying real exposure: the money is
-- genuinely not there.
SELECT
    'KPI'::text                  AS source_module,
    'REVENUE_BELOW_TARGET'::text AS rule_code,
    'KPI_DATE'::text             AS entity_type,
    metric_date::text            AS entity_id,
    NULL::text                   AS order_id,
    NULL::text                   AS customer_id,
    metric_date                  AS business_date,
    ROUND(target_revenue - net_revenue, 2)::numeric(14,2) AS risk_amount,
    jsonb_build_object(
        'metric_date',     metric_date::text,
        'net_revenue',     net_revenue,
        'target_revenue',  target_revenue,
        'variance_amount', ROUND(target_revenue - net_revenue, 2),
        'variance_pct',    ROUND((target_revenue - net_revenue) / NULLIF(target_revenue, 0) * 100, 2)
    ) AS evidence_json
FROM capstone.vw_daily_kpis
WHERE target_revenue IS NOT NULL
  AND net_revenue < target_revenue

UNION ALL

-- Payment success below target. Zero exposure: a success-rate gap is a
-- conversion problem, not a sum of money.
SELECT
    'KPI', 'PAYMENT_SUCCESS_BELOW_TARGET', 'KPI_DATE',
    metric_date::text, NULL, NULL, metric_date,
    0::numeric(14,2),
    jsonb_build_object(
        'metric_date',                 metric_date::text,
        'payment_success_rate',        payment_success_rate,
        'target_payment_success_rate', ROUND(target_payment_success_rate * 100, 2),
        'variance_pct_points',         ROUND(target_payment_success_rate * 100 - payment_success_rate, 2)
    )
FROM capstone.vw_daily_kpis
WHERE target_payment_success_rate IS NOT NULL
  AND payment_success_rate IS NOT NULL
  AND payment_success_rate < target_payment_success_rate * 100

UNION ALL

-- Order volume below target. Zero exposure.
SELECT
    'KPI', 'ORDER_COUNT_BELOW_TARGET', 'KPI_DATE',
    metric_date::text, NULL, NULL, metric_date,
    0::numeric(14,2),
    jsonb_build_object(
        'metric_date',        metric_date::text,
        'order_count',        order_count,
        'target_order_count', target_order_count,
        'variance_count',     target_order_count - order_count
    )
FROM capstone.vw_daily_kpis
WHERE target_order_count IS NOT NULL
  AND order_count < target_order_count

UNION ALL

-- Refund rate above tolerance. Exposure is the refund value above the
-- threshold, not the whole refund value: refunds up to the accepted rate are
-- normal business cost, and charging all of it as leakage would be wrong.
SELECT
    'KPI', 'REFUND_RATE_ABOVE_THRESHOLD', 'KPI_DATE',
    metric_date::text, NULL, NULL, metric_date,
    ROUND(
        processed_refund_value - (successful_payment_value * max_refund_rate_pct / 100), 2
    )::numeric(14,2),
    jsonb_build_object(
        'metric_date',         metric_date::text,
        'refund_value',        processed_refund_value,
        'refund_rate_pct',     refund_rate_pct,
        'threshold_rate_pct',  max_refund_rate_pct,
        'excess_refund_value', ROUND(
            processed_refund_value - (successful_payment_value * max_refund_rate_pct / 100), 2)
    )
FROM capstone.vw_daily_kpis
WHERE max_refund_rate_pct IS NOT NULL
  AND refund_rate_pct IS NOT NULL
  AND refund_rate_pct > max_refund_rate_pct;


-- ---------------------------------------------------------------- DQ controls

CREATE OR REPLACE VIEW capstone.vw_data_quality_findings AS

-- A trading day with no target row. Reported rather than tolerated, because a
-- null join hides the variance instead of showing it.
SELECT
    'DATA_QUALITY'::text       AS source_module,
    'MISSING_KPI_TARGET'::text AS rule_code,
    'KPI_DATE'::text           AS entity_type,
    metric_date::text          AS entity_id,
    NULL::text                 AS order_id,
    NULL::text                 AS customer_id,
    metric_date                AS business_date,
    0::numeric(14,2)           AS risk_amount,
    jsonb_build_object(
        'metric_date', metric_date::text,
        'order_count', order_count,
        'net_revenue', net_revenue
    ) AS evidence_json
FROM capstone.vw_daily_kpis
WHERE target_missing
  AND order_count > 0

UNION ALL

-- Orders with no payment record of any kind.
-- GREATEST(..., 0) because exposure can never be negative. ORD-INVALID-999
-- carries a negative final_amount and also has no payment, so the raw value
-- would emit negative exposure and quietly reduce the total revenue at risk.
-- The negative value itself is not ignored -- NEGATIVE_AMOUNT reports it as its
-- own finding, which is where that defect belongs.
SELECT
    'DATA_QUALITY', 'ORDER_WITHOUT_PAYMENT', 'ORDER',
    o.order_id, o.order_id, o.customer_id, o.order_date,
    GREATEST(COALESCE(o.final_amount, 0), 0)::numeric(14,2),
    jsonb_build_object(
        'order_id',     o.order_id,
        'order_date',   o.order_date::text,
        'order_status', COALESCE(o.order_status, 'UNKNOWN'),
        'final_amount', COALESCE(o.final_amount, 0)
    )
FROM staging.orders o
WHERE NOT EXISTS (
    SELECT 1 FROM staging.payments p WHERE p.order_id = o.order_id
)

UNION ALL

-- Successful payments that do not add up to the order's final amount.
-- Aggregated per order first: an order can legitimately have several part
-- payments, and comparing each one individually would flag every instalment.
SELECT
    'DATA_QUALITY', 'PAYMENT_AMOUNT_MISMATCH', 'PAYMENT',
    paid.first_payment_id, paid.order_id, o.customer_id, o.order_date,
    ROUND(ABS(paid.total_paid - o.final_amount), 2)::numeric(14,2),
    jsonb_build_object(
        'payment_id',          paid.first_payment_id,
        'order_id',            paid.order_id,
        'payment_amount',      paid.total_paid,
        'order_final_amount',  o.final_amount,
        'absolute_difference', ROUND(ABS(paid.total_paid - o.final_amount), 2)
    )
FROM (
    SELECT
        order_id,
        SUM(payment_amount)  AS total_paid,
        MIN(payment_id)      AS first_payment_id
    FROM staging.payments
    WHERE payment_status = 'Successful'
    GROUP BY order_id
) paid
JOIN staging.orders o ON o.order_id = paid.order_id
WHERE o.final_amount IS NOT NULL
  AND ABS(paid.total_paid - o.final_amount) > 0.01

UNION ALL

-- Payments referencing an order that does not exist.
SELECT
    'DATA_QUALITY', 'ORPHAN_PAYMENT', 'PAYMENT',
    p.payment_id, p.order_id, NULL, p.payment_date::date,
    GREATEST(COALESCE(p.payment_amount, 0), 0)::numeric(14,2),
    jsonb_build_object(
        'payment_id',     p.payment_id,
        'order_id',       COALESCE(p.order_id, 'NULL'),
        'payment_amount', COALESCE(p.payment_amount, 0),
        'payment_date',   p.payment_date::text
    )
FROM staging.payments p
WHERE NOT EXISTS (
    SELECT 1 FROM staging.orders o WHERE o.order_id = p.order_id
)

UNION ALL

-- Refunds referencing an order that does not exist. More serious than an
-- orphan payment: money left the business against nothing.
SELECT
    'DATA_QUALITY', 'ORPHAN_REFUND', 'REFUND',
    r.refund_id, r.order_id, NULL, r.refund_date::date,
    GREATEST(COALESCE(r.refund_amount, 0), 0)::numeric(14,2),
    jsonb_build_object(
        'refund_id',     r.refund_id,
        'order_id',      COALESCE(r.order_id, 'NULL'),
        'payment_id',    COALESCE(r.payment_id, 'NULL'),
        'refund_amount', COALESCE(r.refund_amount, 0)
    )
FROM staging.refunds r
WHERE NOT EXISTS (
    SELECT 1 FROM staging.orders o WHERE o.order_id = r.order_id
)

UNION ALL

-- Negative monetary values. Exposure is the absolute value: the sign is the
-- defect, the magnitude is the amount at stake.
SELECT
    'DATA_QUALITY', 'NEGATIVE_AMOUNT', 'ORDER',
    o.order_id, o.order_id, o.customer_id, o.order_date,
    ABS(LEAST(COALESCE(o.gross_amount, 0), COALESCE(o.final_amount, 0)))::numeric(14,2),
    jsonb_build_object(
        'table_name',     'staging.orders',
        'column_name',    CASE WHEN o.gross_amount < 0 THEN 'gross_amount' ELSE 'final_amount' END,
        'record_id',      o.order_id,
        'observed_value', LEAST(COALESCE(o.gross_amount, 0), COALESCE(o.final_amount, 0))
    )
FROM staging.orders o
WHERE o.gross_amount < 0 OR o.final_amount < 0

UNION ALL

-- Duplicate primary keys. Zero exposure -- an integrity failure, not a loss --
-- but CRITICAL, because everything downstream assumes these keys are unique.
SELECT
    'DATA_QUALITY', 'DUPLICATE_PRIMARY_KEY', dup.entity_type,
    dup.key_value, NULL, NULL, dup.business_date,
    0::numeric(14,2),
    jsonb_build_object(
        'table_name',       dup.table_name,
        'key_column',       dup.key_column,
        'duplicate_value',  dup.key_value,
        'occurrence_count', dup.occurrence_count
    )
FROM (
    SELECT 'ORDER'::text AS entity_type, 'staging.orders' AS table_name,
           'order_id' AS key_column, order_id AS key_value,
           COUNT(*) AS occurrence_count, MIN(order_date) AS business_date
    FROM staging.orders WHERE order_id IS NOT NULL
    GROUP BY order_id HAVING COUNT(*) > 1

    UNION ALL
    SELECT 'PAYMENT', 'staging.payments', 'payment_id', payment_id,
           COUNT(*), MIN(payment_date)::date
    FROM staging.payments WHERE payment_id IS NOT NULL
    GROUP BY payment_id HAVING COUNT(*) > 1

    UNION ALL
    SELECT 'REFUND', 'staging.refunds', 'refund_id', refund_id,
           COUNT(*), MIN(refund_date)::date
    FROM staging.refunds WHERE refund_id IS NOT NULL
    GROUP BY refund_id HAVING COUNT(*) > 1

    UNION ALL
    SELECT 'CUSTOMER', 'staging.customers', 'customer_id', customer_id,
           COUNT(*), NULL::date
    FROM staging.customers WHERE customer_id IS NOT NULL
    GROUP BY customer_id HAVING COUNT(*) > 1
) dup

UNION ALL

-- Orders pointing at a customer that is not in the master.
SELECT
    'DATA_QUALITY', 'UNKNOWN_CUSTOMER_REFERENCE', 'ORDER',
    o.order_id, o.order_id, o.customer_id, o.order_date,
    0::numeric(14,2),
    jsonb_build_object(
        'order_id',    o.order_id,
        'customer_id', COALESCE(o.customer_id, 'NULL'),
        'order_date',  o.order_date::text
    )
FROM staging.orders o
WHERE NOT EXISTS (
    SELECT 1 FROM staging.customers c WHERE c.customer_id = o.customer_id
)

UNION ALL

-- One gateway reference used by more than one payment. Zero exposure, but a
-- join-multiplication hazard: reconciliation matches on this reference, so an
-- ambiguous key fans out into false matches AND false breaks simultaneously.
SELECT
    'DATA_QUALITY', 'DUPLICATE_TRANSACTION_REFERENCE', 'PAYMENT',
    ref.transaction_reference, NULL, NULL, ref.business_date,
    0::numeric(14,2),
    jsonb_build_object(
        'transaction_reference', ref.transaction_reference,
        'occurrence_count',      ref.occurrence_count,
        'affected_payment_ids',  ref.payment_ids,
        'distinct_amounts',      ref.distinct_amounts
    )
FROM (
    SELECT
        transaction_reference,
        COUNT(*)                              AS occurrence_count,
        STRING_AGG(payment_id, ', ' ORDER BY payment_id) AS payment_ids,
        COUNT(DISTINCT payment_amount)        AS distinct_amounts,
        MIN(payment_date)::date               AS business_date
    FROM staging.payments
    WHERE transaction_reference IS NOT NULL
      AND payment_status = 'Successful'
    GROUP BY transaction_reference
    HAVING COUNT(*) > 1
) ref;


-- ---------------------------------------------------------------- roll-up

-- Everything the SQL control layer detected, in one contract-shaped stream.
-- The loader reads this and does not need to know which control produced a row.
-- Reconciliation joins in from 03_reconciliation via its own view.
CREATE OR REPLACE VIEW capstone.vw_sql_findings AS
SELECT * FROM capstone.vw_kpi_findings
UNION ALL
SELECT * FROM capstone.vw_data_quality_findings;


-- Data-quality scorecard for the executive page. Expressed as the share of
-- checked records that passed, so the tile reads as a percentage rather than a
-- raw defect count that means nothing without its denominator.
CREATE OR REPLACE VIEW capstone.vw_data_quality_score AS
WITH checked AS (
    SELECT
        (SELECT COUNT(*) FROM staging.orders)   AS orders,
        (SELECT COUNT(*) FROM staging.payments) AS payments,
        (SELECT COUNT(*) FROM staging.refunds)  AS refunds
),
defects AS (
    SELECT COUNT(*) AS defect_count
    FROM capstone.vw_data_quality_findings
)
SELECT
    checked.orders + checked.payments + checked.refunds AS records_checked,
    defects.defect_count,
    ROUND(
        100.0 * (1 - defects.defect_count::numeric
                     / NULLIF(checked.orders + checked.payments + checked.refunds, 0)),
        2
    ) AS data_quality_score_pct
FROM checked, defects;
