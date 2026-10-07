-- ============================================================================
-- Source landing zone.
--
-- Deliberately unconstrained: no foreign keys, no CHECK constraints, no unique
-- indexes beyond a loader-side guard. Staging is a faithful copy of the source
-- system INCLUDING its defects.
--
-- This is the point most people get wrong. If staging enforced referential
-- integrity, the orphan order, the negative amount and the duplicate refund
-- would be rejected at load time -- and the data-quality controls whose entire
-- job is to find and quantify them would have nothing to report. The defects
-- would not disappear; they would just stop being visible, which is worse.
--
-- Constraints live where they belong: in the control layer (02_quality_and_kpis)
-- as detections, and in the case model (04_case_model) as guarantees about
-- findings and reviews.
--
-- Run first. Idempotent.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS staging;

-- Created here, in the first script, because 02_quality_and_kpis.sql defines
-- views in this schema and runs before the case model does. Declaring it only
-- in 04 worked on an existing database and failed on every fresh install --
-- the kind of ordering bug that stays invisible until someone else clones the
-- repo.
CREATE SCHEMA IF NOT EXISTS capstone;


-- ---------------------------------------------------------------- masters

CREATE TABLE IF NOT EXISTS staging.customers (
    customer_id      VARCHAR(32),
    customer_name    VARCHAR(200),
    customer_segment VARCHAR(30),
    region           VARCHAR(30),
    signup_date      DATE,
    risk_level       VARCHAR(10)
);

CREATE TABLE IF NOT EXISTS staging.product_prices (
    product_id     VARCHAR(32),
    product_name   VARCHAR(200),
    category       VARCHAR(50),
    approved_price NUMERIC(14, 2),
    minimum_price  NUMERIC(14, 2),
    effective_from DATE,
    effective_to   DATE
);

COMMENT ON TABLE staging.product_prices IS
    'Temporal price list. A product has one row per effective window, so pricing '
    'checks must match on the order date falling inside that window -- comparing '
    'against the current price would flag every historical order.';

CREATE TABLE IF NOT EXISTS staging.fee_rules (
    payment_method    VARCHAR(30),
    standard_fee_rate NUMERIC(8, 5),
    maximum_fee_rate  NUMERIC(8, 5),
    effective_from    DATE
);


-- ---------------------------------------------------------------- transactions

CREATE TABLE IF NOT EXISTS staging.orders (
    order_id        VARCHAR(32),
    customer_id     VARCHAR(32),
    order_date      DATE,
    order_status    VARCHAR(20),
    gross_amount    NUMERIC(14, 2),
    discount_amount NUMERIC(14, 2),
    tax_amount      NUMERIC(14, 2),
    final_amount    NUMERIC(14, 2),
    discount_code   VARCHAR(40),
    sales_channel   VARCHAR(30),
    region          VARCHAR(30)
);

CREATE TABLE IF NOT EXISTS staging.order_items (
    order_item_id  VARCHAR(32),
    order_id       VARCHAR(32),
    product_id     VARCHAR(32),
    quantity       INTEGER,
    unit_price     NUMERIC(14, 2),
    approved_price NUMERIC(14, 2),
    item_discount  NUMERIC(14, 2)
);

CREATE TABLE IF NOT EXISTS staging.payments (
    payment_id            VARCHAR(32),
    order_id              VARCHAR(32),
    payment_date          TIMESTAMP,
    payment_status        VARCHAR(20),
    payment_amount        NUMERIC(14, 2),
    payment_method        VARCHAR(30),
    gateway_fee           NUMERIC(14, 2),
    transaction_reference VARCHAR(40)
);

CREATE TABLE IF NOT EXISTS staging.refunds (
    refund_id     VARCHAR(32),
    order_id      VARCHAR(32),
    payment_id    VARCHAR(32),
    refund_date   TIMESTAMP,
    refund_amount NUMERIC(14, 2),
    refund_reason VARCHAR(100),
    refund_status VARCHAR(20),
    approved_by   VARCHAR(30)
);

CREATE TABLE IF NOT EXISTS staging.shipments (
    shipment_id     VARCHAR(32),
    order_id        VARCHAR(32),
    shipment_date   DATE,
    delivery_date   DATE,
    shipment_status VARCHAR(20),
    courier         VARCHAR(50)
);


-- ---------------------------------------------------------------- settlement

-- Bank statement lines, as the bank reports them. The bank knows nothing about
-- order IDs -- matching to the ledger happens on reference and amount, which is
-- exactly why reconciliation breaks exist at all.
CREATE TABLE IF NOT EXISTS staging.bank_transactions (
    bank_txn_id    VARCHAR(32),
    value_date     DATE,
    amount         NUMERIC(14, 2),
    bank_reference VARCHAR(40),
    txn_type       VARCHAR(20),
    narration      VARCHAR(200)
);

CREATE TABLE IF NOT EXISTS staging.ledger_transactions (
    ledger_txn_id    VARCHAR(32),
    posting_date     DATE,
    amount           NUMERIC(14, 2),
    ledger_reference VARCHAR(40),
    account_code     VARCHAR(20),
    txn_type         VARCHAR(20),
    source_entity_id VARCHAR(32)
);

COMMENT ON COLUMN staging.ledger_transactions.ledger_reference IS
    'The join key to bank_transactions.bank_reference. Both sides carry the '
    'gateway transaction reference, which is the only identifier the bank and '
    'the ledger genuinely share.';


-- ---------------------------------------------------------------- targets

CREATE TABLE IF NOT EXISTS staging.daily_kpi_targets (
    metric_date                 DATE,
    target_revenue              NUMERIC(14, 2),
    target_payment_success_rate NUMERIC(6, 4),
    target_order_count          INTEGER,
    max_refund_rate_pct         NUMERIC(6, 4)
);

COMMENT ON TABLE staging.daily_kpi_targets IS
    'Must cover every date present in staging.orders. A target row missing for a '
    'trading day turns the KPI comparison into a null join, which silently hides '
    'the variance rather than reporting it -- 02_quality_and_kpis raises a '
    'MISSING_TARGET warning instead of letting that pass.';


-- ---------------------------------------------------------------- load control

-- Which run loaded which file, and how many rows. Needed to prove a dashboard
-- total traces back to a specific file at a specific volume.
CREATE TABLE IF NOT EXISTS staging.load_audit (
    load_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      VARCHAR(20)  NOT NULL,
    table_name  VARCHAR(64)  NOT NULL,
    source_file VARCHAR(200) NOT NULL,
    row_count   INTEGER      NOT NULL,
    loaded_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_load_audit_run ON staging.load_audit(run_id);


-- ---------------------------------------------------------------- indexes

-- Non-unique on purpose. These are the join keys the control layer hits
-- repeatedly; uniqueness is a thing the controls TEST for, not something the
-- landing zone may assume.
CREATE INDEX IF NOT EXISTS idx_stg_orders_id        ON staging.orders(order_id);
CREATE INDEX IF NOT EXISTS idx_stg_orders_customer  ON staging.orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_stg_orders_date      ON staging.orders(order_date);
CREATE INDEX IF NOT EXISTS idx_stg_items_order      ON staging.order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_stg_items_product    ON staging.order_items(product_id);
CREATE INDEX IF NOT EXISTS idx_stg_payments_order   ON staging.payments(order_id);
CREATE INDEX IF NOT EXISTS idx_stg_payments_id      ON staging.payments(payment_id);
CREATE INDEX IF NOT EXISTS idx_stg_payments_ref     ON staging.payments(transaction_reference);
CREATE INDEX IF NOT EXISTS idx_stg_refunds_order    ON staging.refunds(order_id);
CREATE INDEX IF NOT EXISTS idx_stg_refunds_payment  ON staging.refunds(payment_id);
CREATE INDEX IF NOT EXISTS idx_stg_shipments_order  ON staging.shipments(order_id);
CREATE INDEX IF NOT EXISTS idx_stg_customers_id     ON staging.customers(customer_id);
CREATE INDEX IF NOT EXISTS idx_stg_bank_ref         ON staging.bank_transactions(bank_reference);
CREATE INDEX IF NOT EXISTS idx_stg_ledger_ref       ON staging.ledger_transactions(ledger_reference);
