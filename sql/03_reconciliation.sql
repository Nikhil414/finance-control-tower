-- ============================================================================
-- Bank-to-ledger reconciliation.
--
-- Matches the bank statement against the ledger on the gateway reference -- the
-- only identifier both sides genuinely share -- and classifies every row into
-- exactly one outcome.
--
-- Two structural rules drive the design:
--
-- 1. Every transaction is classified exactly once. Classification is mutually
--    exclusive by construction, not by hoping the WHERE clauses do not overlap.
--    If a row could be counted as both matched and broken, the match rate and
--    the break count would each be wrong, and they would disagree with each
--    other -- which is how a reconciliation dashboard loses all credibility.
--
-- 2. Duplicates are resolved before matching, never during it. A reference
--    appearing twice on the bank side would otherwise join to its single ledger
--    row twice, producing two "matches" for one real settlement: the classic
--    join multiplication that inflates the numerator and the denominator at the
--    same time.
--
-- Run after 01_schema.sql. Idempotent.
-- ============================================================================

-- Tolerances. The gateway settles one day after capture, so a one-day gap
-- between ledger posting and bank value date is the expected state, not a break.
CREATE OR REPLACE VIEW capstone.vw_recon_config AS
SELECT
    1        AS date_tolerance_days,
    0.01     AS amount_tolerance;


-- ---------------------------------------------------------------- matching

CREATE OR REPLACE VIEW capstone.vw_reconciliation_matches AS
WITH config AS (
    SELECT * FROM capstone.vw_recon_config
),

-- Rank bank rows within each reference. Rank 1 is the genuine settlement; any
-- higher rank is a re-presentment of money already credited. Resolving this
-- first is what keeps the matching step strictly one-to-one.
bank_ranked AS (
    SELECT
        b.*,
        ROW_NUMBER() OVER (
            PARTITION BY b.bank_reference
            ORDER BY b.value_date, b.bank_txn_id
        ) AS reference_rank,
        COUNT(*) OVER (PARTITION BY b.bank_reference) AS reference_count,
        FIRST_VALUE(b.bank_txn_id) OVER (
            PARTITION BY b.bank_reference
            ORDER BY b.value_date, b.bank_txn_id
        ) AS original_bank_txn_id
    FROM staging.bank_transactions b
),

bank_primary AS (
    SELECT * FROM bank_ranked WHERE reference_rank = 1
),

bank_duplicate AS (
    SELECT * FROM bank_ranked WHERE reference_rank > 1
),

-- The ledger side gets the same treatment for symmetry: if the ledger ever
-- double-posts a reference, the extra row must not create a phantom match.
ledger_ranked AS (
    SELECT
        l.*,
        ROW_NUMBER() OVER (
            PARTITION BY l.ledger_reference
            ORDER BY l.posting_date, l.ledger_txn_id
        ) AS reference_rank
    FROM staging.ledger_transactions l
),

ledger_primary AS (
    SELECT * FROM ledger_ranked WHERE reference_rank = 1
),

-- One row per reference pair. FULL OUTER JOIN so both one-sided cases surface
-- in the same pass rather than needing separate anti-join queries that could
-- drift apart.
paired AS (
    SELECT
        b.bank_txn_id,
        b.value_date,
        b.amount            AS bank_amount,
        b.bank_reference,
        b.narration,
        l.ledger_txn_id,
        l.posting_date,
        l.amount            AS ledger_amount,
        l.ledger_reference,
        l.account_code,
        l.source_entity_id
    FROM bank_primary b
    FULL OUTER JOIN ledger_primary l
        ON l.ledger_reference = b.bank_reference
),

classified AS (
    -- Two-sided rows: matched, or broken on amount, or broken on date.
    -- Precedence is deliberate. Amount beats date because missing money matters
    -- more than late money, and a single CASE guarantees one outcome per row.
    SELECT
        p.bank_txn_id,
        p.ledger_txn_id,
        p.bank_amount,
        p.ledger_amount,
        ROUND(COALESCE(p.bank_amount, 0) - COALESCE(p.ledger_amount, 0), 2) AS amount_difference,
        p.value_date   AS bank_value_date,
        p.posting_date AS ledger_posting_date,
        (p.value_date - p.posting_date) AS day_difference,
        COALESCE(p.bank_reference, p.ledger_reference) AS reference,
        p.narration,
        'bank_reference = ledger_reference'::text AS matched_on,
        NULL::text AS original_bank_txn_id,
        NULL::integer AS occurrence_count,
        CASE
            WHEN ABS(COALESCE(p.bank_amount, 0) - COALESCE(p.ledger_amount, 0)) > c.amount_tolerance
                THEN 'AMOUNT_MISMATCH'
            WHEN ABS(p.value_date - p.posting_date) > c.date_tolerance_days
                THEN 'DATE_MISMATCH'
            ELSE 'MATCHED'
        END AS match_status,
        COALESCE(p.posting_date, p.value_date) AS business_date
    FROM paired p
    CROSS JOIN config c
    WHERE p.bank_txn_id IS NOT NULL
      AND p.ledger_txn_id IS NOT NULL

    UNION ALL

    -- Cash arrived that the ledger cannot explain.
    SELECT
        p.bank_txn_id, NULL, p.bank_amount, NULL, NULL,
        p.value_date, NULL, NULL,
        p.bank_reference, p.narration,
        NULL, NULL, NULL,
        'BANK_ONLY', p.value_date
    FROM paired p
    WHERE p.bank_txn_id IS NOT NULL
      AND p.ledger_txn_id IS NULL

    UNION ALL

    -- Revenue booked that never turned into cash.
    SELECT
        NULL, p.ledger_txn_id, NULL, p.ledger_amount, NULL,
        NULL, p.posting_date, NULL,
        p.ledger_reference, NULL,
        NULL, NULL, NULL,
        'LEDGER_ONLY', p.posting_date
    FROM paired p
    WHERE p.ledger_txn_id IS NOT NULL
      AND p.bank_txn_id IS NULL

    UNION ALL

    -- Re-presented settlements, excluded from matching above so they can never
    -- be double-counted as matches.
    SELECT
        d.bank_txn_id, NULL, d.amount, NULL, NULL,
        d.value_date, NULL, NULL,
        d.bank_reference, d.narration,
        NULL, d.original_bank_txn_id, d.reference_count::integer,
        'DUPLICATE_SETTLEMENT', d.value_date
    FROM bank_duplicate d
)
SELECT
    match_status,
    bank_txn_id,
    ledger_txn_id,
    reference,
    matched_on,
    bank_amount,
    ledger_amount,
    amount_difference,
    bank_value_date,
    ledger_posting_date,
    day_difference,
    narration,
    original_bank_txn_id,
    occurrence_count,
    business_date,
    CASE
        WHEN (CURRENT_DATE - business_date) <= 7  THEN '0-7'
        WHEN (CURRENT_DATE - business_date) <= 30 THEN '8-30'
        WHEN (CURRENT_DATE - business_date) <= 60 THEN '31-60'
        WHEN (CURRENT_DATE - business_date) <= 90 THEN '61-90'
        ELSE '90+'
    END AS aging_bucket
FROM classified;


COMMENT ON VIEW capstone.vw_reconciliation_matches IS
    'Every bank and ledger transaction classified into exactly one outcome. '
    'Includes MATCHED rows because the match rate needs a denominator -- a view '
    'of breaks alone cannot tell you what share of settlement reconciled.';


-- ---------------------------------------------------------------- findings

CREATE OR REPLACE VIEW capstone.vw_reconciliation_findings AS

-- Unexplained cash. Full amount at stake: until it is identified, it may belong
-- to someone else.
SELECT
    'RECONCILIATION'::text         AS source_module,
    'BANK_ONLY_TRANSACTION'::text  AS rule_code,
    'BANK_TXN'::text               AS entity_type,
    m.bank_txn_id                  AS entity_id,
    NULL::text                     AS order_id,
    NULL::text                     AS customer_id,
    m.business_date                AS business_date,
    ABS(m.bank_amount)::numeric(14,2) AS risk_amount,
    jsonb_build_object(
        'bank_txn_id',     m.bank_txn_id,
        'bank_amount',     m.bank_amount,
        'bank_value_date', m.bank_value_date::text,
        'bank_reference',  m.reference,
        'match_attempts',  'bank_reference = ledger_reference'
    ) AS evidence_json
FROM capstone.vw_reconciliation_matches m
WHERE m.match_status = 'BANK_ONLY'

UNION ALL

-- Revenue booked but never banked. The most expensive break type in practice.
SELECT
    'RECONCILIATION', 'LEDGER_ONLY_TRANSACTION', 'LEDGER_TXN',
    m.ledger_txn_id, NULL, NULL, m.business_date,
    ABS(m.ledger_amount)::numeric(14,2),
    jsonb_build_object(
        'ledger_txn_id',       m.ledger_txn_id,
        'ledger_amount',       m.ledger_amount,
        'ledger_posting_date', m.ledger_posting_date::text,
        'ledger_reference',    m.reference,
        'match_attempts',      'bank_reference = ledger_reference'
    )
FROM capstone.vw_reconciliation_matches m
WHERE m.match_status = 'LEDGER_ONLY'

UNION ALL

-- Bank and ledger disagree on the amount. Exposure is the gap, not the whole
-- transaction: most of the money did arrive.
SELECT
    'RECONCILIATION', 'AMOUNT_MISMATCH', 'BANK_TXN',
    m.bank_txn_id, NULL, NULL, m.business_date,
    ABS(m.amount_difference)::numeric(14,2),
    jsonb_build_object(
        'bank_txn_id',         m.bank_txn_id,
        'ledger_txn_id',       m.ledger_txn_id,
        'bank_amount',         m.bank_amount,
        'ledger_amount',       m.ledger_amount,
        'absolute_difference', ABS(m.amount_difference),
        'matched_on',          m.matched_on
    )
FROM capstone.vw_reconciliation_matches m
WHERE m.match_status = 'AMOUNT_MISMATCH'

UNION ALL

-- Settlement landed outside the tolerance window. Zero exposure by contract:
-- the money is all present, just late. Still worth a case -- it is an SLA and
-- forecasting problem -- but booking it as revenue at risk would inflate the
-- headline figure with money that was never lost.
SELECT
    'RECONCILIATION', 'DATE_MISMATCH', 'BANK_TXN',
    m.bank_txn_id, NULL, NULL, m.business_date,
    0::numeric(14,2),
    jsonb_build_object(
        'bank_txn_id',         m.bank_txn_id,
        'ledger_txn_id',       m.ledger_txn_id,
        'bank_value_date',     m.bank_value_date::text,
        'ledger_posting_date', m.ledger_posting_date::text,
        'day_difference',      m.day_difference,
        'tolerance_days',      (SELECT date_tolerance_days FROM capstone.vw_recon_config)
    )
FROM capstone.vw_reconciliation_matches m
WHERE m.match_status = 'DATE_MISMATCH'

UNION ALL

-- The same reference credited more than once.
SELECT
    'RECONCILIATION', 'DUPLICATE_SETTLEMENT', 'BANK_TXN',
    m.bank_txn_id, NULL, NULL, m.business_date,
    ABS(m.bank_amount)::numeric(14,2),
    jsonb_build_object(
        'original_bank_txn_id',  m.original_bank_txn_id,
        'duplicate_bank_txn_id', m.bank_txn_id,
        'settlement_amount',     m.bank_amount,
        'bank_reference',        m.reference,
        'days_between',          (
            m.bank_value_date - (
                SELECT b.value_date FROM staging.bank_transactions b
                WHERE b.bank_txn_id = m.original_bank_txn_id
            )
        )
    )
FROM capstone.vw_reconciliation_matches m
WHERE m.match_status = 'DUPLICATE_SETTLEMENT';


-- ---------------------------------------------------------------- summary

CREATE OR REPLACE VIEW capstone.vw_reconciliation_summary AS
SELECT
    COUNT(*)                                                      AS total_transactions,
    COUNT(*) FILTER (WHERE match_status = 'MATCHED')              AS matched,
    COUNT(*) FILTER (WHERE match_status <> 'MATCHED')              AS breaks,
    COUNT(*) FILTER (WHERE match_status = 'BANK_ONLY')            AS bank_only,
    COUNT(*) FILTER (WHERE match_status = 'LEDGER_ONLY')          AS ledger_only,
    COUNT(*) FILTER (WHERE match_status = 'AMOUNT_MISMATCH')      AS amount_mismatch,
    COUNT(*) FILTER (WHERE match_status = 'DATE_MISMATCH')        AS date_mismatch,
    COUNT(*) FILTER (WHERE match_status = 'DUPLICATE_SETTLEMENT') AS duplicate_settlement,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE match_status = 'MATCHED') / NULLIF(COUNT(*), 0),
        2
    ) AS match_rate_pct,
    -- Date mismatches are excluded from break exposure on purpose; see the
    -- DATE_MISMATCH comment above.
    ROUND(SUM(
        CASE match_status
            WHEN 'MATCHED' THEN 0
            WHEN 'DATE_MISMATCH' THEN 0
            WHEN 'AMOUNT_MISMATCH' THEN ABS(COALESCE(amount_difference, 0))
            ELSE ABS(COALESCE(bank_amount, ledger_amount, 0))
        END
    ), 2) AS break_exposure
FROM capstone.vw_reconciliation_matches;
