"""Verify the SQL control layer: KPI definitions, data-quality controls, and
reconciliation classification.

These tests assert on the invariants that make the numbers trustworthy, not on
incidental values. Where an exact figure is asserted it is because the dataset
seeds that exact defect on purpose.

Requires staging to be loaded: python src/load_source.py

Run: pytest tests/test_controls.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from contract import RULES_BY_CODE  # noqa: E402
from db import connect  # noqa: E402

# Breaks the settlement generator seeds deliberately. Any drift here means the
# generator and the controls have diverged.
EXPECTED_BREAKS = {
    "BANK_ONLY_TRANSACTION": 3,
    "LEDGER_ONLY_TRANSACTION": 3,
    "AMOUNT_MISMATCH": 4,
    "DATE_MISMATCH": 3,
    "DUPLICATE_SETTLEMENT": 2,
}

# A break that has a counterpart on the other side consumes one source row from
# each side; a one-sided break consumes only one.
TWO_SIDED_STATUSES = ("MATCHED", "AMOUNT_MISMATCH", "DATE_MISMATCH")


@pytest.fixture(scope="module")
def conn():
    connection = connect()
    yield connection
    connection.close()


def scalar(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def rows(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


@pytest.fixture(scope="module", autouse=True)
def require_loaded_staging(conn):
    if scalar(conn, "SELECT COUNT(*) FROM staging.orders") == 0:
        pytest.skip("staging is empty -- run: python src/load_source.py")


# ---------------------------------------------------------------- KPI layer

def test_net_revenue_matches_its_definition(conn):
    """net_revenue must equal successful payments - processed refunds - gateway
    fees, recomputed independently from staging. If the view and the definition
    ever disagree, every revenue figure in the system is wrong."""
    view_total = scalar(conn, "SELECT SUM(net_revenue) FROM capstone.vw_daily_kpis")

    independent = scalar(conn, """
        SELECT
            (SELECT COALESCE(SUM(payment_amount), 0) FROM staging.payments
              WHERE payment_status = 'Successful')
          - (SELECT COALESCE(SUM(refund_amount), 0) FROM staging.refunds
              WHERE refund_status = 'Processed')
          - (SELECT COALESCE(SUM(gateway_fee), 0) FROM staging.payments
              WHERE payment_status = 'Successful')
    """)

    assert view_total == independent


def test_kpi_view_does_not_multiply_rows(conn):
    """One row per date. Joining orders, payments and refunds without
    pre-aggregating each grain is the classic way this view silently doubles
    every amount."""
    total, distinct = rows(conn, """
        SELECT COUNT(*), COUNT(DISTINCT metric_date) FROM capstone.vw_daily_kpis
    """)[0]
    assert total == distinct


def test_order_count_matches_staging(conn):
    from_view = scalar(conn, "SELECT SUM(order_count) FROM capstone.vw_daily_kpis")
    from_staging = scalar(conn, """
        SELECT COUNT(*) FROM staging.orders
        WHERE order_date IS NOT NULL AND COALESCE(order_status, '') <> 'Cancelled'
    """)
    assert from_view == from_staging


def test_rates_are_null_not_zero_when_undefined(conn):
    """A day with no payment attempts has an undefined success rate. Reporting
    it as 0% would fabricate a failure that never occurred."""
    bad = scalar(conn, """
        SELECT COUNT(*) FROM capstone.vw_daily_kpis
        WHERE payment_attempts = 0 AND payment_success_rate IS NOT NULL
    """)
    assert bad == 0


def test_payment_success_rate_has_real_variance(conn):
    """The KPI page is pointless if this metric is a flat line. The dataset is
    calibrated with failed first attempts specifically to avoid that."""
    low, high = rows(conn, """
        SELECT MIN(payment_success_rate), MAX(payment_success_rate)
        FROM capstone.vw_daily_kpis WHERE payment_success_rate IS NOT NULL
    """)[0]
    assert low < Decimal("95"), f"no meaningful downside variance (min {low})"
    assert high > low


def test_every_trading_day_has_a_target(conn):
    """A missing target turns the variance comparison into a null join, which
    hides the breach rather than reporting it."""
    missing = scalar(conn, """
        SELECT COUNT(*) FROM capstone.vw_daily_kpis
        WHERE target_missing AND order_count > 0
    """)
    assert missing == 0


# ---------------------------------------------------------------- exposure semantics

def test_zero_exposure_rules_emit_zero_exposure(conn):
    """Rules flagged zero-exposure-by-design must never contribute to revenue at
    risk. A rate gap or a duplicate key is a control failure, not lost money."""
    zero_rules = [
        code for code, rule in RULES_BY_CODE.items()
        if rule.get("zero_exposure_by_design")
    ]
    offenders = rows(conn, """
        SELECT rule_code, SUM(risk_amount)
        FROM (
            SELECT * FROM capstone.vw_sql_findings
            UNION ALL
            SELECT * FROM capstone.vw_reconciliation_findings
        ) f
        WHERE rule_code = ANY(%s) AND risk_amount <> 0
        GROUP BY rule_code
    """, (zero_rules,))
    assert offenders == []


def test_no_control_emits_negative_exposure(conn):
    negative = scalar(conn, """
        SELECT COUNT(*) FROM (
            SELECT * FROM capstone.vw_sql_findings
            UNION ALL
            SELECT * FROM capstone.vw_reconciliation_findings
        ) f WHERE risk_amount < 0
    """)
    assert negative == 0


def test_every_emitted_rule_is_registered(conn):
    """An unregistered rule_code would appear on a dashboard with no definition
    behind it, and would fail the foreign key at load time."""
    emitted = {
        row[0] for row in rows(conn, """
            SELECT DISTINCT rule_code FROM (
                SELECT rule_code FROM capstone.vw_sql_findings
                UNION ALL
                SELECT rule_code FROM capstone.vw_reconciliation_findings
            ) f
        """)
    }
    assert emitted <= set(RULES_BY_CODE), f"unregistered: {emitted - set(RULES_BY_CODE)}"


def test_every_finding_carries_its_required_evidence(conn):
    """Evidence must be sufficient to reproduce the calculation by hand. A
    missing key means a reviewer cannot verify the amount they are asked to
    confirm."""
    failures = []
    for rule_code, evidence_keys in rows(conn, """
        SELECT rule_code, ARRAY(SELECT jsonb_object_keys(evidence_json))
        FROM (
            SELECT rule_code, evidence_json FROM capstone.vw_sql_findings
            UNION ALL
            SELECT rule_code, evidence_json FROM capstone.vw_reconciliation_findings
        ) f
    """):
        required = set(RULES_BY_CODE[rule_code]["required_evidence_keys"])
        present = set(evidence_keys)
        if not required <= present:
            failures.append((rule_code, sorted(required - present)))

    assert not failures, f"findings missing required evidence: {failures}"


def test_evidence_contains_no_pii(conn):
    """Evidence is the exact payload shipped to an external AI API, so customer
    names and contact details must never appear in it."""
    from contract import CONTRACT

    forbidden = CONTRACT["evidence_rules"]["forbidden_evidence_keys"]
    leaked = rows(conn, """
        SELECT DISTINCT rule_code, k
        FROM (
            SELECT rule_code, evidence_json FROM capstone.vw_sql_findings
            UNION ALL
            SELECT rule_code, evidence_json FROM capstone.vw_reconciliation_findings
        ) f, jsonb_object_keys(f.evidence_json) k
        WHERE k = ANY(%s)
    """, (forbidden,))
    assert leaked == []


# ---------------------------------------------------------------- data quality

def test_seeded_defects_are_all_detected(conn):
    """The dataset carries known defects. Each must be found exactly as often as
    it was planted -- under-detection hides a problem, over-detection means the
    control is firing on clean records."""
    found = dict(rows(conn, """
        SELECT rule_code, COUNT(*) FROM capstone.vw_data_quality_findings
        GROUP BY rule_code
    """))

    assert found.get("UNKNOWN_CUSTOMER_REFERENCE") == 1, "ORD-INVALID-999"
    assert found.get("NEGATIVE_AMOUNT") == 1
    assert found.get("DUPLICATE_TRANSACTION_REFERENCE") == 1, "TXN-859782"
    assert found.get("ORDER_WITHOUT_PAYMENT") == 4
    assert "DUPLICATE_PRIMARY_KEY" not in found, "dataset has no duplicate keys"


def test_payment_mismatch_aggregates_before_comparing(conn):
    """An order may legitimately be paid in instalments. Comparing each payment
    individually against the order total would flag every instalment as a
    mismatch."""
    multi_payment_orders = scalar(conn, """
        SELECT COUNT(*) FROM (
            SELECT order_id FROM staging.payments
            WHERE payment_status = 'Successful'
            GROUP BY order_id HAVING COUNT(*) > 1
        ) m
    """)
    flagged_multi = scalar(conn, """
        SELECT COUNT(*) FROM capstone.vw_data_quality_findings f
        WHERE f.rule_code = 'PAYMENT_AMOUNT_MISMATCH'
          AND f.order_id IN (
              SELECT order_id FROM staging.payments
              WHERE payment_status = 'Successful'
              GROUP BY order_id HAVING COUNT(*) > 1
          )
    """)
    # Whatever the instalment count, a mismatch is raised at most once per order.
    assert flagged_multi <= multi_payment_orders


def test_calibration_attempts_carry_no_money(conn):
    """The failed attempts added for KPI calibration must collect nothing and be
    charged no fee, so they change the attempt count and nothing else.

    Scoped to the PAY-FAIL- prefix on purpose. The source data contains its own
    failed payment, PAY-000303, which legitimately carries the full attempted
    amount -- it is the seeded enterprise delivered-but-unpaid case. Asserting
    over every failed payment would wrongly condemn that fixture."""
    bad = scalar(conn, """
        SELECT COUNT(*) FROM staging.payments
        WHERE payment_id LIKE 'PAY-FAIL-%'
          AND (COALESCE(payment_amount, 0) <> 0 OR COALESCE(gateway_fee, 0) <> 0)
    """)
    assert bad == 0

    assert scalar(conn, """
        SELECT COUNT(*) FROM staging.payments WHERE payment_id LIKE 'PAY-FAIL-%'
    """) > 0, "calibration attempts are absent -- regenerate the dataset"


def test_failed_payments_never_enter_revenue(conn):
    """PAY-000303 is Failed but carries 295,000. The revenue definition filters
    on Successful, so that amount must not appear in net_revenue -- otherwise a
    failed payment would be reported as collected cash."""
    failed_value = scalar(conn, """
        SELECT COALESCE(SUM(payment_amount), 0) FROM staging.payments
        WHERE payment_status <> 'Successful'
    """)
    assert failed_value > 0, "no failed value in the dataset makes this vacuous"

    revenue = scalar(conn, "SELECT SUM(net_revenue) FROM capstone.vw_daily_kpis")
    successful_only = scalar(conn, """
        SELECT COALESCE(SUM(payment_amount), 0) - COALESCE(SUM(gateway_fee), 0)
        FROM staging.payments WHERE payment_status = 'Successful'
    """)
    refunds = scalar(conn, """
        SELECT COALESCE(SUM(refund_amount), 0) FROM staging.refunds
        WHERE refund_status = 'Processed'
    """)
    assert revenue == successful_only - refunds


# ---------------------------------------------------------------- reconciliation

def test_every_transaction_is_classified_exactly_once(conn):
    """The core reconciliation invariant. Source rows must be fully accounted
    for: two-sided outcomes consume one row from each side, one-sided outcomes
    consume one. If this drifts, the match rate and the break count are both
    wrong and disagree with each other."""
    source_rows = scalar(conn, """
        SELECT (SELECT COUNT(*) FROM staging.bank_transactions)
             + (SELECT COUNT(*) FROM staging.ledger_transactions)
    """)

    classified = scalar(conn, "SELECT COUNT(*) FROM capstone.vw_reconciliation_matches")
    two_sided = scalar(conn, """
        SELECT COUNT(*) FROM capstone.vw_reconciliation_matches
        WHERE match_status = ANY(%s)
    """, (list(TWO_SIDED_STATUSES),))

    assert classified + two_sided == source_rows, (
        f"{classified} classified + {two_sided} second sides != {source_rows} source rows"
    )


def test_no_transaction_appears_in_two_classifications(conn):
    """Mutual exclusivity, checked directly rather than assumed from the CASE."""
    duplicated_bank = scalar(conn, """
        SELECT COUNT(*) FROM (
            SELECT bank_txn_id FROM capstone.vw_reconciliation_matches
            WHERE bank_txn_id IS NOT NULL
            GROUP BY bank_txn_id HAVING COUNT(*) > 1
        ) d
    """)
    duplicated_ledger = scalar(conn, """
        SELECT COUNT(*) FROM (
            SELECT ledger_txn_id FROM capstone.vw_reconciliation_matches
            WHERE ledger_txn_id IS NOT NULL
            GROUP BY ledger_txn_id HAVING COUNT(*) > 1
        ) d
    """)
    assert duplicated_bank == 0
    assert duplicated_ledger == 0


def test_duplicate_settlement_does_not_inflate_matches(conn):
    """A re-presented reference must be classified as a duplicate, never counted
    as a second successful match. This is the join-multiplication guard."""
    matched_refs, distinct_refs = rows(conn, """
        SELECT COUNT(*), COUNT(DISTINCT reference)
        FROM capstone.vw_reconciliation_matches
        WHERE match_status = 'MATCHED'
    """)[0]
    assert matched_refs == distinct_refs


def test_seeded_breaks_are_detected_exactly(conn):
    found = dict(rows(conn, """
        SELECT rule_code, COUNT(*) FROM capstone.vw_reconciliation_findings
        GROUP BY rule_code
    """))
    assert found == EXPECTED_BREAKS


def test_match_rate_reconciles_with_the_break_count(conn):
    summary = rows(conn, """
        SELECT total_transactions, matched, breaks, match_rate_pct
        FROM capstone.vw_reconciliation_summary
    """)[0]
    total, matched, breaks, rate = summary

    assert matched + breaks == total
    assert rate == round(Decimal(matched) / Decimal(total) * 100, 2)

    finding_count = scalar(conn, "SELECT COUNT(*) FROM capstone.vw_reconciliation_findings")
    assert finding_count == breaks, "every break must raise exactly one finding"


def test_break_exposure_agrees_between_summary_and_findings(conn):
    """The summary tile and the findings detail must never show different
    numbers for the same concept."""
    summary_exposure = scalar(
        conn, "SELECT break_exposure FROM capstone.vw_reconciliation_summary"
    )
    findings_exposure = scalar(
        conn, "SELECT SUM(risk_amount) FROM capstone.vw_reconciliation_findings"
    )
    assert summary_exposure == findings_exposure


def test_date_mismatch_carries_no_exposure(conn):
    """Late money is not lost money."""
    exposure = scalar(conn, """
        SELECT COALESCE(SUM(risk_amount), 0) FROM capstone.vw_reconciliation_findings
        WHERE rule_code = 'DATE_MISMATCH'
    """)
    assert exposure == 0
    assert scalar(conn, """
        SELECT COUNT(*) FROM capstone.vw_reconciliation_findings
        WHERE rule_code = 'DATE_MISMATCH'
    """) > 0, "the assertion above is meaningless if no date mismatches exist"
