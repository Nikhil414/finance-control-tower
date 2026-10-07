-- ============================================================================
-- Case-management model. Implements docs/finding_contract.md v1.0.0.
--
-- Design rule that drives every table below: detected facts are immutable,
-- human judgement is append-only, and current state is derived rather than
-- overwritten. Without that separation there is no audit trail -- only a
-- mutable row that happens to hold the latest opinion.
--
-- Run after 01_schema.sql. Idempotent.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS capstone;


-- ---------------------------------------------------------------- dimensions

-- Rule registry, mirrored from config/contract.json so Power BI can join to
-- rule metadata without parsing JSON. config/contract.json stays authoritative;
-- src/sync_dim_rule.py refreshes this from it.
CREATE TABLE IF NOT EXISTS capstone.dim_rule (
    rule_code               VARCHAR(48) PRIMARY KEY,
    source_module           VARCHAR(20) NOT NULL
        CHECK (source_module IN ('KPI', 'DATA_QUALITY', 'LEAKAGE', 'RECONCILIATION')),
    entity_type             VARCHAR(20) NOT NULL,
    default_severity        VARCHAR(10) NOT NULL
        CHECK (default_severity IN ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW')),
    approval_required       BOOLEAN     NOT NULL,
    zero_exposure_by_design BOOLEAN     NOT NULL DEFAULT FALSE,
    exposure_basis          TEXT        NOT NULL,
    contract_version        VARCHAR(16) NOT NULL
);

COMMENT ON COLUMN capstone.dim_rule.zero_exposure_by_design IS
    'TRUE for timing/integrity rules. Their risk_amount is 0 on purpose: a late '
    'settlement or a duplicate key is a control failure, not money lost. Summing '
    'them into revenue-at-risk would inflate the headline with phantom exposure.';

CREATE TABLE IF NOT EXISTS capstone.dim_owner (
    owner_id    VARCHAR(20)  PRIMARY KEY,
    owner_name  VARCHAR(100) NOT NULL,
    team        VARCHAR(50)  NOT NULL,
    is_approver BOOLEAN      NOT NULL DEFAULT FALSE,
    active      BOOLEAN      NOT NULL DEFAULT TRUE
);

-- Date dimension. Power BI needs a contiguous calendar for time intelligence;
-- gaps in fact dates otherwise produce gaps in trend lines.
CREATE TABLE IF NOT EXISTS capstone.dim_date (
    date_key       DATE    PRIMARY KEY,
    year           INTEGER NOT NULL,
    quarter        INTEGER NOT NULL,
    month          INTEGER NOT NULL,
    month_name     VARCHAR(12) NOT NULL,
    day_of_month   INTEGER NOT NULL,
    day_of_week    INTEGER NOT NULL,
    day_name       VARCHAR(12) NOT NULL,
    is_weekend     BOOLEAN NOT NULL,
    iso_week       INTEGER NOT NULL
);


-- ---------------------------------------------------------------- pipeline runs

CREATE TABLE IF NOT EXISTS capstone.fact_pipeline_runs (
    run_id            VARCHAR(20) PRIMARY KEY
        CHECK (run_id ~ '^RUN-[0-9]{8}-[0-9]{3}$'),
    run_date          DATE        NOT NULL,
    started_at        TIMESTAMPTZ NOT NULL,
    finished_at       TIMESTAMPTZ,
    status            VARCHAR(20) NOT NULL
        CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED', 'VALIDATION_FAILED')),
    contract_version  VARCHAR(16) NOT NULL,

    -- Load counts, for reconciling dashboard totals back to source volumes.
    orders_loaded     INTEGER NOT NULL DEFAULT 0,
    payments_loaded   INTEGER NOT NULL DEFAULT 0,
    refunds_loaded    INTEGER NOT NULL DEFAULT 0,
    bank_txns_loaded  INTEGER NOT NULL DEFAULT 0,
    ledger_txns_loaded INTEGER NOT NULL DEFAULT 0,

    findings_created  INTEGER NOT NULL DEFAULT 0,
    findings_skipped  INTEGER NOT NULL DEFAULT 0,

    -- AI status is recorded, never allowed to fail the run. A control system
    -- that stops controlling because a language model is rate-limited is not a
    -- control system.
    ai_status         VARCHAR(20) NOT NULL DEFAULT 'NOT_ATTEMPTED'
        CHECK (ai_status IN ('NOT_ATTEMPTED', 'SUCCESS', 'DEGRADED', 'UNAVAILABLE')),
    ai_failure_reason TEXT,

    error_message     TEXT,

    CONSTRAINT run_finished_after_started
        CHECK (finished_at IS NULL OR finished_at >= started_at),
    CONSTRAINT failed_run_has_reason
        CHECK (status NOT IN ('FAILED', 'VALIDATION_FAILED') OR error_message IS NOT NULL)
);

COMMENT ON COLUMN capstone.fact_pipeline_runs.findings_skipped IS
    'Findings the loader suppressed as already present for this run_id. A rerun '
    'reports a high skipped count and zero created -- that is proof idempotency '
    'held, not a warning.';


-- ---------------------------------------------------------------- findings

-- Immutable. Written once by a detector, never updated by AI or reviewers.
-- The only mutable column is `status`, and only the decision importer may move
-- it, always alongside an appended row in fact_finding_events.
CREATE TABLE IF NOT EXISTS capstone.fact_findings (
    finding_key        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    finding_id         VARCHAR(32) NOT NULL
        CHECK (finding_id ~ '^(KPI|DQ|LK|RC)-[0-9]{8}-[0-9]{5}$'),

    -- The run that FIRST detected this case. Never changes, so case age is
    -- measured from genuine first detection rather than from the most recent
    -- time a still-broken thing was noticed again.
    run_id             VARCHAR(20) NOT NULL
        REFERENCES capstone.fact_pipeline_runs(run_id),

    -- The most recent run in which the exception was still present, and how
    -- many runs have seen it. A case whose times_seen keeps climbing is an
    -- exception nobody has fixed -- which is exactly the backlog signal a
    -- control tower exists to surface.
    last_seen_run_id   VARCHAR(20) NOT NULL
        REFERENCES capstone.fact_pipeline_runs(run_id),
    times_seen         INTEGER     NOT NULL DEFAULT 1 CHECK (times_seen >= 1),
    source_module      VARCHAR(20) NOT NULL
        CHECK (source_module IN ('KPI', 'DATA_QUALITY', 'LEAKAGE', 'RECONCILIATION')),
    rule_code          VARCHAR(48) NOT NULL
        REFERENCES capstone.dim_rule(rule_code),

    entity_type        VARCHAR(20) NOT NULL,
    entity_id          VARCHAR(64) NOT NULL,
    order_id           VARCHAR(32),
    customer_id        VARCHAR(32),

    detected_at        TIMESTAMPTZ NOT NULL,
    business_date      DATE        NOT NULL,

    severity           VARCHAR(10) NOT NULL
        CHECK (severity IN ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW')),
    risk_amount        NUMERIC(14, 2) NOT NULL CHECK (risk_amount >= 0),
    currency           VARCHAR(3)  NOT NULL DEFAULT 'INR',
    priority_score     NUMERIC(5, 1) NOT NULL
        CHECK (priority_score BETWEEN 0 AND 100),
    days_open          INTEGER     NOT NULL DEFAULT 0 CHECK (days_open >= 0),

    evidence_json      JSONB       NOT NULL,
    evidence_hash      CHAR(64)    NOT NULL,

    recommended_action TEXT,
    approval_required  BOOLEAN     NOT NULL,

    status             VARCHAR(30) NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'ASSIGNED', 'UNDER_REVIEW', 'FALSE_POSITIVE',
                          'MORE_INFORMATION_REQUIRED', 'VALID_EXCEPTION',
                          'ACTION_APPROVED', 'RESOLVED')),
    owner              VARCHAR(20) REFERENCES capstone.dim_owner(owner_id),
    due_date           DATE        NOT NULL,

    contract_version   VARCHAR(16) NOT NULL,
    loaded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The idempotency guarantee. finding_id is only a label: its counter depends
    -- on row iteration order, so a rerun can legitimately relabel the same
    -- exception. Uniqueness therefore rests on what the exception *is*.
    --
    -- run_id is deliberately absent from this key. A case is the exception, not
    -- the run that noticed it -- so tomorrow's run finding the same unresolved
    -- problem updates this row rather than opening a rival case beside it,
    -- leaving the original's review history stranded.
    CONSTRAINT findings_natural_key
        UNIQUE (rule_code, entity_type, entity_id, evidence_hash),

    CONSTRAINT evidence_is_object
        CHECK (jsonb_typeof(evidence_json) = 'object'),
    CONSTRAINT due_after_detection
        CHECK (due_date >= detected_at::date)
);

COMMENT ON TABLE capstone.fact_findings IS
    'Immutable detected exceptions. risk_amount is ESTIMATED exposure, never '
    'confirmed loss -- confirmed_loss and recovered_amount live in fact_reviews '
    'and are never summed with this column.';

COMMENT ON COLUMN capstone.fact_findings.risk_amount IS
    'Estimated exposure from a deterministic rule. Unconfirmed by definition. '
    'Pair with fact_reviews.confirmed_loss to show estimate vs reality.';

CREATE INDEX IF NOT EXISTS idx_findings_run        ON capstone.fact_findings(run_id);
CREATE INDEX IF NOT EXISTS idx_findings_last_seen  ON capstone.fact_findings(last_seen_run_id);
CREATE INDEX IF NOT EXISTS idx_findings_status     ON capstone.fact_findings(status);
CREATE INDEX IF NOT EXISTS idx_findings_rule       ON capstone.fact_findings(rule_code);
CREATE INDEX IF NOT EXISTS idx_findings_order      ON capstone.fact_findings(order_id);
CREATE INDEX IF NOT EXISTS idx_findings_business   ON capstone.fact_findings(business_date);
CREATE INDEX IF NOT EXISTS idx_findings_open_due   ON capstone.fact_findings(due_date)
    WHERE status NOT IN ('RESOLVED', 'FALSE_POSITIVE');
CREATE INDEX IF NOT EXISTS idx_findings_evidence   ON capstone.fact_findings USING GIN (evidence_json);


-- ---------------------------------------------------------------- events

-- Append-only status history. Every transition lands here, including the
-- initial detection, so a case's full life is reconstructable without relying
-- on fact_findings.status having been correct at every moment.
CREATE TABLE IF NOT EXISTS capstone.fact_finding_events (
    event_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    finding_key   BIGINT      NOT NULL REFERENCES capstone.fact_findings(finding_key),
    finding_id    VARCHAR(32) NOT NULL,
    run_id        VARCHAR(20) NOT NULL REFERENCES capstone.fact_pipeline_runs(run_id),

    event_seq     INTEGER     NOT NULL CHECK (event_seq >= 1),
    from_status   VARCHAR(30),
    to_status     VARCHAR(30) NOT NULL,
    event_type    VARCHAR(20) NOT NULL
        CHECK (event_type IN ('DETECTED', 'ASSIGNED', 'REVIEWED', 'APPROVED',
                              'RESOLVED', 'REOPENED_AS_NEW')),
    actor         VARCHAR(100) NOT NULL,
    actor_type    VARCHAR(10)  NOT NULL CHECK (actor_type IN ('SYSTEM', 'HUMAN')),
    event_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    note          TEXT,

    CONSTRAINT event_seq_unique UNIQUE (finding_key, event_seq),
    -- Detection is the only event with no prior status, and the only one an
    -- automated actor may create.
    CONSTRAINT detection_is_first
        CHECK ((event_type = 'DETECTED') = (from_status IS NULL)),
    CONSTRAINT ai_is_never_an_actor
        CHECK (actor NOT ILIKE 'ai%' AND actor NOT ILIKE '%investigator%')
);

COMMENT ON CONSTRAINT ai_is_never_an_actor ON capstone.fact_finding_events IS
    'Belt-and-braces: the AI investigator has no database credentials at all, so '
    'it cannot reach this table. This constraint documents the intent at the '
    'schema level and catches a future wiring mistake.';

CREATE INDEX IF NOT EXISTS idx_events_finding ON capstone.fact_finding_events(finding_key, event_seq);


-- ---------------------------------------------------------------- reviews

-- Append-only human judgement. Deliberately NOT deduplicated: a second review
-- of the same finding is a legitimate event (escalation, correction), not a
-- duplicate. Current state is the latest reviewed_at per finding, derived in
-- vw_current_review below.
CREATE TABLE IF NOT EXISTS capstone.fact_reviews (
    review_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    finding_key         BIGINT      NOT NULL REFERENCES capstone.fact_findings(finding_key),
    finding_id          VARCHAR(32) NOT NULL,

    decision            VARCHAR(30) NOT NULL
        CHECK (decision IN ('FALSE_POSITIVE', 'VALID_EXCEPTION',
                            'MORE_INFORMATION_REQUIRED', 'ACTION_APPROVED', 'RESOLVED')),
    reviewer            VARCHAR(100) NOT NULL,
    reviewed_at         TIMESTAMPTZ  NOT NULL,
    reviewer_notes      TEXT,

    approved_action     TEXT,
    -- Human-confirmed outcome. Separate from fact_findings.risk_amount on
    -- purpose: one is an estimate a rule produced, the other is what a person
    -- verified actually happened.
    confirmed_loss      NUMERIC(14, 2) CHECK (confirmed_loss IS NULL OR confirmed_loss >= 0),
    recovered_amount    NUMERIC(14, 2) CHECK (recovered_amount IS NULL OR recovered_amount >= 0),
    false_positive_reason TEXT,
    resolution_date     DATE,

    source_file         VARCHAR(200) NOT NULL,
    imported_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    run_id              VARCHAR(20)  REFERENCES capstone.fact_pipeline_runs(run_id),

    -- A dismissal without a reason is how control systems quietly rot.
    CONSTRAINT false_positive_needs_reason
        CHECK (decision <> 'FALSE_POSITIVE' OR false_positive_reason IS NOT NULL),
    CONSTRAINT approval_needs_action
        CHECK (decision <> 'ACTION_APPROVED' OR approved_action IS NOT NULL),
    CONSTRAINT resolution_needs_date
        CHECK (decision <> 'RESOLVED' OR resolution_date IS NOT NULL),
    -- You cannot recover more than you confirmed you lost.
    CONSTRAINT recovery_within_confirmed_loss
        CHECK (recovered_amount IS NULL OR confirmed_loss IS NULL
               OR recovered_amount <= confirmed_loss),
    -- A false positive has no loss to confirm.
    CONSTRAINT false_positive_has_no_loss
        CHECK (decision <> 'FALSE_POSITIVE'
               OR COALESCE(confirmed_loss, 0) = 0)
);

CREATE INDEX IF NOT EXISTS idx_reviews_finding  ON capstone.fact_reviews(finding_key, reviewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_reviews_reviewer ON capstone.fact_reviews(reviewer);
CREATE INDEX IF NOT EXISTS idx_reviews_decision ON capstone.fact_reviews(decision);


-- ---------------------------------------------------------------- reconciliation

-- Match-level detail for every reconciliation attempt, matched or not. Findings
-- are raised only for breaks, but the match rate denominator needs the matched
-- rows too -- otherwise "match rate" has no total to divide by.
CREATE TABLE IF NOT EXISTS capstone.fact_reconciliation_results (
    recon_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id           VARCHAR(20) NOT NULL REFERENCES capstone.fact_pipeline_runs(run_id),

    bank_txn_id      VARCHAR(32),
    ledger_txn_id    VARCHAR(32),

    match_status     VARCHAR(20) NOT NULL
        CHECK (match_status IN ('MATCHED', 'BANK_ONLY', 'LEDGER_ONLY',
                                'AMOUNT_MISMATCH', 'DATE_MISMATCH', 'DUPLICATE_SETTLEMENT')),
    matched_on       VARCHAR(40),

    bank_amount      NUMERIC(14, 2),
    ledger_amount    NUMERIC(14, 2),
    amount_difference NUMERIC(14, 2),

    bank_value_date     DATE,
    ledger_posting_date DATE,
    day_difference      INTEGER,

    business_date    DATE        NOT NULL,
    -- Aging bucket is stored rather than computed in DAX so the SQL total and
    -- the dashboard total cannot drift apart.
    aging_bucket     VARCHAR(12) NOT NULL
        CHECK (aging_bucket IN ('0-7', '8-30', '31-60', '61-90', '90+')),
    finding_key      BIGINT REFERENCES capstone.fact_findings(finding_key),

    CONSTRAINT recon_has_at_least_one_side
        CHECK (bank_txn_id IS NOT NULL OR ledger_txn_id IS NOT NULL),
    CONSTRAINT matched_rows_have_both_sides
        CHECK (match_status <> 'MATCHED'
               OR (bank_txn_id IS NOT NULL AND ledger_txn_id IS NOT NULL)),
    CONSTRAINT breaks_have_a_finding
        CHECK (match_status = 'MATCHED' OR finding_key IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_recon_run    ON capstone.fact_reconciliation_results(run_id);
CREATE INDEX IF NOT EXISTS idx_recon_status ON capstone.fact_reconciliation_results(match_status);


-- ---------------------------------------------------------------- derived views

-- Latest review per finding. This is the "current human position" that Power BI
-- reads. Deriving it from the append-only table means re-reviews never destroy
-- the earlier decision.
CREATE OR REPLACE VIEW capstone.vw_current_review AS
SELECT DISTINCT ON (finding_key)
    finding_key,
    finding_id,
    decision,
    reviewer,
    reviewed_at,
    reviewer_notes,
    approved_action,
    confirmed_loss,
    recovered_amount,
    false_positive_reason,
    resolution_date
FROM capstone.fact_reviews
ORDER BY finding_key, reviewed_at DESC, review_id DESC;


-- Case-level reporting surface: finding + current review + live aging.
CREATE OR REPLACE VIEW capstone.vw_case_summary AS
SELECT
    f.finding_key,
    f.finding_id,
    f.run_id,
    f.source_module,
    f.rule_code,
    r.default_severity AS rule_default_severity,
    r.zero_exposure_by_design,
    f.entity_type,
    f.entity_id,
    f.order_id,
    f.customer_id,
    f.business_date,
    f.detected_at,
    f.severity,
    f.risk_amount,
    f.priority_score,
    f.status,
    f.owner,
    f.due_date,
    f.approval_required,

    cr.decision,
    cr.reviewer,
    cr.reviewed_at,
    cr.confirmed_loss,
    cr.recovered_amount,

    -- Aging counts from detection to resolution, or to today while still open.
    CASE
        WHEN cr.resolution_date IS NOT NULL
            THEN cr.resolution_date - f.detected_at::date
        ELSE CURRENT_DATE - f.detected_at::date
    END AS age_days,

    CASE
        WHEN f.status IN ('RESOLVED', 'FALSE_POSITIVE') THEN FALSE
        WHEN CURRENT_DATE > f.due_date THEN TRUE
        ELSE FALSE
    END AS is_sla_breached,

    CASE
        WHEN (CURRENT_DATE - f.detected_at::date) <= 7  THEN '0-7'
        WHEN (CURRENT_DATE - f.detected_at::date) <= 30 THEN '8-30'
        WHEN (CURRENT_DATE - f.detected_at::date) <= 60 THEN '31-60'
        WHEN (CURRENT_DATE - f.detected_at::date) <= 90 THEN '61-90'
        ELSE '90+'
    END AS aging_bucket
FROM capstone.fact_findings f
JOIN capstone.dim_rule r ON r.rule_code = f.rule_code
LEFT JOIN capstone.vw_current_review cr ON cr.finding_key = f.finding_key;


-- Defensible exposure per run. Gross double-counts by construction: one order
-- can trip EXCESSIVE_DISCOUNT and DELIVERED_UNPAID at once. Deduplicated takes
-- the worst finding per order, and is the figure the executive tile uses.
-- Scoped on last_seen_run_id, not run_id: "what this run found" means every
-- exception present in it, including cases first detected earlier and still
-- unresolved. Scoping on first detection would show a run only its brand-new
-- cases and report a falling exposure simply because problems had aged.
CREATE OR REPLACE VIEW capstone.vw_run_exposure AS
WITH per_order AS (
    SELECT
        last_seen_run_id,
        COALESCE(order_id, entity_id) AS exposure_group,
        MAX(risk_amount) AS worst_risk
    FROM capstone.fact_findings
    GROUP BY last_seen_run_id, COALESCE(order_id, entity_id)
)
SELECT
    f.last_seen_run_id AS run_id,
    COUNT(*)                                  AS findings_count,
    SUM(f.risk_amount)                        AS gross_exposure,
    (SELECT SUM(worst_risk) FROM per_order p
      WHERE p.last_seen_run_id = f.last_seen_run_id)
                                              AS deduplicated_exposure,
    COUNT(*) FILTER (WHERE f.severity = 'CRITICAL') AS critical_count,
    COUNT(*) FILTER (WHERE f.severity = 'HIGH')     AS high_count,
    COUNT(*) FILTER (WHERE f.severity = 'MEDIUM')   AS medium_count,
    COUNT(*) FILTER (WHERE f.severity = 'LOW')      AS low_count,
    COUNT(*) FILTER (WHERE f.times_seen > 1)        AS recurring_count
FROM capstone.fact_findings f
GROUP BY f.last_seen_run_id;


-- Estimate vs reality. The measure that makes the whole control loop worth
-- running: how close the automated exposure estimate came to confirmed loss,
-- and how much of that was actually recovered.
CREATE OR REPLACE VIEW capstone.vw_review_outcomes AS
SELECT
    f.rule_code,
    f.source_module,
    COUNT(*)                                                    AS reviewed_count,
    COUNT(*) FILTER (WHERE cr.decision = 'FALSE_POSITIVE')      AS false_positive_count,
    COUNT(*) FILTER (WHERE cr.decision IN ('VALID_EXCEPTION', 'ACTION_APPROVED', 'RESOLVED'))
                                                                AS confirmed_count,
    SUM(f.risk_amount)                                          AS estimated_exposure,
    SUM(COALESCE(cr.confirmed_loss, 0))                         AS confirmed_loss,
    SUM(COALESCE(cr.recovered_amount, 0))                       AS recovered_amount
FROM capstone.fact_findings f
JOIN capstone.vw_current_review cr ON cr.finding_key = f.finding_key
GROUP BY f.rule_code, f.source_module;


-- ---------------------------------------------------------------- guards

-- Detectors may only ever create OPEN findings. This trigger blocks any insert
-- path -- including a future script written by someone who skipped the docs --
-- from smuggling a pre-decided case into the table.
CREATE OR REPLACE FUNCTION capstone.enforce_detector_status()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status <> 'OPEN' THEN
        RAISE EXCEPTION
            'findings must be inserted with status OPEN (got %); status changes '
            'belong to the validated review importer, not to detection',
            NEW.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_findings_insert_open ON capstone.fact_findings;
CREATE TRIGGER trg_findings_insert_open
    BEFORE INSERT ON capstone.fact_findings
    FOR EACH ROW EXECUTE FUNCTION capstone.enforce_detector_status();


-- Detected facts are immutable. Mutable: `status` and `owner` (workflow),
-- plus `last_seen_run_id`, `times_seen`, `days_open` and `priority_score`
-- (recurrence -- a case seen again is older and may have climbed the queue).
--
-- Everything else being updated means something is trying to rewrite history,
-- which is the one thing an audit trail must never allow. Note `run_id` is
-- protected: first detection is a fact, and letting a later run overwrite it
-- would silently reset the case's age.
CREATE OR REPLACE FUNCTION capstone.protect_finding_immutability()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.finding_id     IS DISTINCT FROM OLD.finding_id
    OR NEW.run_id         IS DISTINCT FROM OLD.run_id
    OR NEW.rule_code      IS DISTINCT FROM OLD.rule_code
    OR NEW.entity_id      IS DISTINCT FROM OLD.entity_id
    OR NEW.risk_amount    IS DISTINCT FROM OLD.risk_amount
    OR NEW.severity       IS DISTINCT FROM OLD.severity
    OR NEW.evidence_json  IS DISTINCT FROM OLD.evidence_json
    OR NEW.evidence_hash  IS DISTINCT FROM OLD.evidence_hash
    OR NEW.detected_at    IS DISTINCT FROM OLD.detected_at
    OR NEW.business_date  IS DISTINCT FROM OLD.business_date
    THEN
        RAISE EXCEPTION
            'fact_findings is immutable: attempted to modify detected facts on %. '
            'Mutable columns are status, owner, last_seen_run_id, times_seen, '
            'days_open and priority_score.', OLD.finding_id;
    END IF;

    IF NEW.times_seen < OLD.times_seen THEN
        RAISE EXCEPTION
            'times_seen cannot decrease on % (% -> %)',
            OLD.finding_id, OLD.times_seen, NEW.times_seen;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_findings_immutable ON capstone.fact_findings;
CREATE TRIGGER trg_findings_immutable
    BEFORE UPDATE ON capstone.fact_findings
    FOR EACH ROW EXECUTE FUNCTION capstone.protect_finding_immutability();


-- Human decisions are append-only. No edits, no deletes -- a correction is a
-- new review row, which is exactly how the audit trail stays honest.
CREATE OR REPLACE FUNCTION capstone.reviews_are_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'fact_reviews is append-only (attempted %). To correct a decision, '
        'insert a new review; vw_current_review resolves the latest.', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_reviews_append_only ON capstone.fact_reviews;
CREATE TRIGGER trg_reviews_append_only
    BEFORE UPDATE OR DELETE ON capstone.fact_reviews
    FOR EACH ROW EXECUTE FUNCTION capstone.reviews_are_append_only();


-- ---------------------------------------------------------------- seed

INSERT INTO capstone.dim_owner (owner_id, owner_name, team, is_approver) VALUES
    ('OWN-001', 'Revenue Assurance Analyst 1', 'Revenue Assurance', FALSE),
    ('OWN-002', 'Revenue Assurance Analyst 2', 'Revenue Assurance', FALSE),
    ('OWN-003', 'Reconciliation Analyst 1',    'Finance Operations', FALSE),
    ('OWN-004', 'Finance Operations Manager',  'Finance Operations', TRUE),
    ('OWN-005', 'Financial Controller',        'Controllership',     TRUE)
ON CONFLICT (owner_id) DO NOTHING;

-- Calendar covering the dataset plus headroom for backlog trending.
INSERT INTO capstone.dim_date (
    date_key, year, quarter, month, month_name,
    day_of_month, day_of_week, day_name, is_weekend, iso_week
)
SELECT
    d::date,
    EXTRACT(YEAR    FROM d)::int,
    EXTRACT(QUARTER FROM d)::int,
    EXTRACT(MONTH   FROM d)::int,
    TRIM(TO_CHAR(d, 'Month')),
    EXTRACT(DAY FROM d)::int,
    EXTRACT(ISODOW FROM d)::int,
    TRIM(TO_CHAR(d, 'Day')),
    EXTRACT(ISODOW FROM d)::int >= 6,
    EXTRACT(WEEK FROM d)::int
FROM generate_series('2026-01-01'::date, '2027-12-31'::date, INTERVAL '1 day') AS d
ON CONFLICT (date_key) DO NOTHING;
