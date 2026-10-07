"""Verify the case model's guarantees hold in the database, not just on paper.

Every test here maps to a claim in docs/finding_contract.md. If a claim stops
being true, the corresponding test fails rather than the dashboard quietly
starting to lie.

Run: pytest tests/test_case_model.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from contract import evidence_hash, make_run_id  # noqa: E402
from db import connect  # noqa: E402

TEST_RUN_ID = "RUN-29991231-999"


def _evidence(**overrides) -> dict:
    base = {
        "original_refund_id": "REF-T-001",
        "duplicate_refund_id": "REF-T-002",
        "payment_id": "PAY-T-001",
        "refund_amount": 2500.0,
        "original_refund_date": "2026-08-12 14:00:00",
        "duplicate_refund_date": "2026-08-13 09:30:00",
        "days_between": 0.81,
    }
    base.update(overrides)
    return base


@pytest.fixture()
def conn():
    """A connection whose work is always rolled back, so tests leave no residue."""
    connection = connect()
    connection.autocommit = False
    yield connection
    connection.rollback()
    connection.close()


@pytest.fixture()
def run(conn):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO capstone.fact_pipeline_runs
                   (run_id, run_date, started_at, status, contract_version)
               VALUES (%s, '2999-12-31', now(), 'RUNNING', '1.0.0')""",
            (TEST_RUN_ID,),
        )
    return TEST_RUN_ID


def insert_finding(conn, run_id, evidence=None, **overrides) -> int:
    """Insert a finding. `evidence` is a dict; serialisation and hashing happen
    here so a caller can never pass evidence that disagrees with its own hash."""
    evidence = _evidence() if evidence is None else evidence
    fields = {
        "finding_id": "LK-29991231-00001",
        "run_id": run_id,
        "last_seen_run_id": run_id,
        "source_module": "LEAKAGE",
        "rule_code": "DUPLICATE_REFUND",
        "entity_type": "REFUND",
        "entity_id": "REF-T-002",
        "order_id": "ORD-T-001",
        "customer_id": "CUS-T-001",
        "detected_at": "2999-12-31T00:00:00Z",
        "business_date": "2999-12-31",
        "severity": "MEDIUM",
        "risk_amount": 2500.00,
        "priority_score": 50.0,
        "days_open": 1,
        "evidence_json": json.dumps(evidence) if isinstance(evidence, (dict, list)) else evidence,
        "evidence_hash": evidence_hash(evidence) if isinstance(evidence, dict) else "0" * 64,
        "recommended_action": "Review gateway settlement",
        "approval_required": True,
        "status": "OPEN",
        "due_date": "2999-12-31",
        "contract_version": "1.0.0",
    }
    fields.update(overrides)
    columns = ", ".join(fields)
    placeholders = ", ".join(f"%({key})s" for key in fields)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO capstone.fact_findings ({columns}) "
            f"VALUES ({placeholders}) RETURNING finding_key",
            fields,
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------- registry

def test_every_contract_rule_is_registered(conn):
    """dim_rule must mirror contract.json, or a finding can reference a rule
    the dashboard cannot explain."""
    from contract import RULES_BY_CODE

    with conn.cursor() as cur:
        cur.execute("SELECT rule_code FROM capstone.dim_rule")
        in_db = {row[0] for row in cur.fetchall()}

    assert set(RULES_BY_CODE) == in_db


def test_unregistered_rule_code_is_rejected(conn, run):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insert_finding(conn, run, rule_code="INVENTED_RULE")


# ---------------------------------------------------------------- detection guards

def test_detector_may_insert_open_finding(conn, run):
    assert insert_finding(conn, run) > 0


@pytest.mark.parametrize(
    "status", ["UNDER_REVIEW", "VALID_EXCEPTION", "ACTION_APPROVED", "RESOLVED"]
)
def test_detector_cannot_insert_pre_decided_finding(conn, run, status):
    """Detection may only ever produce OPEN. A pre-decided case would bypass
    human review entirely."""
    with pytest.raises(psycopg.errors.RaiseException, match="must be inserted with status OPEN"):
        insert_finding(conn, run, status=status)


def test_negative_risk_amount_is_rejected(conn, run):
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_finding(conn, run, risk_amount=-1.00)


def test_evidence_must_be_an_object(conn, run):
    """A JSON array is valid JSON but not a valid evidence bag -- required-key
    checks and the Excel evidence sheet both assume an object."""
    with pytest.raises(psycopg.errors.CheckViolation, match="evidence_is_object"):
        insert_finding(conn, run, evidence=[1, 2, 3])


# ---------------------------------------------------------------- idempotency

def test_same_exception_twice_is_blocked(conn, run):
    """The natural key is what makes a rerun safe. finding_id is only a label,
    so the constraint must fire even when the label differs -- and it must fire
    regardless of run, because a case is the exception rather than the run that
    noticed it."""
    insert_finding(conn, run)
    with pytest.raises(psycopg.errors.UniqueViolation, match="findings_natural_key"):
        insert_finding(conn, run, finding_id="LK-29991231-09999")


def test_different_evidence_is_a_different_finding(conn, run):
    insert_finding(conn, run, evidence=_evidence())
    key_b = insert_finding(
        conn, run, evidence=_evidence(refund_amount=9999.0),
        finding_id="LK-29991231-00002",
    )
    assert key_b > 0


# ---------------------------------------------------------------- immutability

def test_status_and_owner_may_change(conn, run):
    key = insert_finding(conn, run)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE capstone.fact_findings SET status='UNDER_REVIEW', owner='OWN-001' "
            "WHERE finding_key=%s",
            (key,),
        )
        cur.execute("SELECT status, owner FROM capstone.fact_findings WHERE finding_key=%s", (key,))
        assert cur.fetchone() == ("UNDER_REVIEW", "OWN-001")


@pytest.mark.parametrize(
    "column, value",
    [
        ("risk_amount", 999999.00),
        ("severity", "CRITICAL"),
        ("rule_code", "PRICING_ERROR"),
        ("evidence_hash", "0" * 64),
        ("detected_at", "2026-01-01T00:00:00Z"),
    ],
)
def test_detected_facts_cannot_be_rewritten(conn, run, column, value):
    """Rewriting a detected fact destroys the audit trail. Inflating risk_amount
    after the fact is the specific abuse this blocks."""
    key = insert_finding(conn, run)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            cur.execute(
                f"UPDATE capstone.fact_findings SET {column}=%s WHERE finding_key=%s",
                (value, key),
            )


# ---------------------------------------------------------------- events

def test_ai_cannot_be_an_event_actor(conn, run):
    key = insert_finding(conn, run)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="ai_is_never_an_actor"):
            cur.execute(
                """INSERT INTO capstone.fact_finding_events
                       (finding_key, finding_id, run_id, event_seq, from_status,
                        to_status, event_type, actor, actor_type)
                   VALUES (%s, 'LK-29991231-00001', %s, 2, 'OPEN', 'UNDER_REVIEW',
                           'REVIEWED', 'ai_investigator', 'SYSTEM')""",
                (key, run),
            )


def test_only_detection_has_no_prior_status(conn, run):
    key = insert_finding(conn, run)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="detection_is_first"):
            cur.execute(
                """INSERT INTO capstone.fact_finding_events
                       (finding_key, finding_id, run_id, event_seq, from_status,
                        to_status, event_type, actor, actor_type)
                   VALUES (%s, 'LK-29991231-00001', %s, 2, NULL, 'UNDER_REVIEW',
                           'REVIEWED', 'analyst.one', 'HUMAN')""",
                (key, run),
            )


# ---------------------------------------------------------------- reviews

def _insert_review(conn, key, **overrides):
    fields = {
        "finding_key": key,
        "finding_id": "LK-29991231-00001",
        "decision": "VALID_EXCEPTION",
        "reviewer": "analyst.one",
        "reviewed_at": "2999-12-31T10:00:00Z",
        "reviewer_notes": "Confirmed duplicate against gateway statement",
        "confirmed_loss": 2500.00,
        "source_file": "test.csv",
    }
    fields.update(overrides)
    columns = ", ".join(fields)
    placeholders = ", ".join(f"%({k})s" for k in fields)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO capstone.fact_reviews ({columns}) "
            f"VALUES ({placeholders}) RETURNING review_id",
            fields,
        )
        return cur.fetchone()[0]


def test_review_can_be_recorded(conn, run):
    key = insert_finding(conn, run)
    assert _insert_review(conn, key) > 0


def test_reviews_cannot_be_edited(conn, run):
    """Append-only is what makes human judgement auditable. A correction is a
    new row, not an overwrite."""
    key = insert_finding(conn, run)
    review_id = _insert_review(conn, key)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            cur.execute(
                "UPDATE capstone.fact_reviews SET reviewer_notes='changed' WHERE review_id=%s",
                (review_id,),
            )


def test_reviews_cannot_be_deleted(conn, run):
    key = insert_finding(conn, run)
    review_id = _insert_review(conn, key)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            cur.execute("DELETE FROM capstone.fact_reviews WHERE review_id=%s", (review_id,))


def test_false_positive_needs_a_reason(conn, run):
    key = insert_finding(conn, run)
    with pytest.raises(psycopg.errors.CheckViolation, match="false_positive_needs_reason"):
        _insert_review(conn, key, decision="FALSE_POSITIVE", confirmed_loss=None)


def test_false_positive_cannot_carry_a_loss(conn, run):
    key = insert_finding(conn, run)
    with pytest.raises(psycopg.errors.CheckViolation, match="false_positive_has_no_loss"):
        _insert_review(
            conn, key, decision="FALSE_POSITIVE",
            false_positive_reason="Gateway retry, not a duplicate",
            confirmed_loss=2500.00,
        )


def test_cannot_recover_more_than_confirmed_loss(conn, run):
    key = insert_finding(conn, run)
    with pytest.raises(psycopg.errors.CheckViolation, match="recovery_within_confirmed_loss"):
        _insert_review(conn, key, confirmed_loss=1000.00, recovered_amount=5000.00)


def test_current_review_resolves_to_the_latest_decision(conn, run):
    """A re-review supersedes without erasing. Both rows survive; the view shows
    the newer one."""
    key = insert_finding(conn, run)
    _insert_review(conn, key, decision="VALID_EXCEPTION", confirmed_loss=2500.00,
                   reviewed_at="2999-12-31T10:00:00Z")
    _insert_review(conn, key, decision="FALSE_POSITIVE", confirmed_loss=None,
                   false_positive_reason="Escalation found a gateway retry",
                   reviewed_at="2999-12-31T18:00:00Z")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT decision FROM capstone.vw_current_review WHERE finding_key=%s", (key,)
        )
        assert cur.fetchone()[0] == "FALSE_POSITIVE"

        cur.execute(
            "SELECT count(*) FROM capstone.fact_reviews WHERE finding_key=%s", (key,)
        )
        assert cur.fetchone()[0] == 2, "the superseded review must still exist"


# ---------------------------------------------------------------- exposure

def test_deduplicated_exposure_does_not_double_count_an_order(conn, run):
    """One order tripping two rules must not add both amounts to the headline
    figure -- that would overstate exposure with money counted twice."""
    discount_evidence = {
        "gross_amount": 50000.0,
        "actual_discount_amount": 30000.0,
        "actual_discount_pct": 60.0,
        "max_permitted_discount_pct": 20.0,
        "discount_code": "UNAUTH60",
        "excess_discount_amount": 20000.0,
    }
    unpaid_evidence = {
        "order_final_amount": 50000.0,
        "successful_payment_amount": 0.0,
        "shortfall_amount": 50000.0,
        "delivery_date": "2999-12-30",
        "courier": "TestCourier",
    }
    insert_finding(
        conn, run, evidence=discount_evidence,
        finding_id="LK-29991231-00010", rule_code="EXCESSIVE_DISCOUNT",
        entity_type="ORDER", entity_id="ORD-SHARED", order_id="ORD-SHARED",
        risk_amount=20000.00,
    )
    insert_finding(
        conn, run, evidence=unpaid_evidence,
        finding_id="LK-29991231-00011", rule_code="DELIVERED_UNPAID",
        entity_type="ORDER", entity_id="ORD-SHARED", order_id="ORD-SHARED",
        risk_amount=50000.00,
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT gross_exposure, deduplicated_exposure "
            "FROM capstone.vw_run_exposure WHERE run_id=%s",
            (run,),
        )
        gross, dedup = cur.fetchone()

    assert gross == 70000.00, "gross intentionally double-counts the shared order"
    assert dedup == 50000.00, "deduplicated keeps only the worst finding per order"


# ---------------------------------------------------------------- run metadata

def test_failed_run_must_explain_itself(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match="failed_run_has_reason"):
            cur.execute(
                """INSERT INTO capstone.fact_pipeline_runs
                       (run_id, run_date, started_at, status, contract_version)
                   VALUES ('RUN-29991231-998', '2999-12-31', now(), 'FAILED', '1.0.0')"""
            )


def test_malformed_run_id_is_rejected(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """INSERT INTO capstone.fact_pipeline_runs
                       (run_id, run_date, started_at, status, contract_version)
                   VALUES ('run-1', '2999-12-31', now(), 'RUNNING', '1.0.0')"""
            )


def test_make_run_id_matches_the_database_constraint(conn):
    """The Python generator and the SQL CHECK must agree, or valid runs get
    rejected at insert time."""
    generated = make_run_id(7)
    with conn.cursor() as cur:
        cur.execute("SELECT %s ~ '^RUN-[0-9]{8}-[0-9]{3}$'", (generated,))
        assert cur.fetchone()[0] is True
