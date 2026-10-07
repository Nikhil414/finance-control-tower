"""Verify the findings loader: idempotency, audit completeness, and that the
totals in the case model agree with the control views that produced them.

Requires a completed load:
    python src/load_source.py && python src/leakage_engine.py && python src/load_findings.py

Run: pytest tests/test_load_findings.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from contract import CONTRACT  # noqa: E402
from db import connect  # noqa: E402
from load_findings import load_all  # noqa: E402


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


@pytest.fixture(scope="module")
def loaded_run(conn):
    run_id = scalar(conn, """
        SELECT run_id FROM capstone.fact_pipeline_runs
        WHERE status = 'SUCCESS' ORDER BY started_at DESC LIMIT 1
    """)
    if run_id is None:
        pytest.skip("no successful run loaded -- see this module's docstring")
    return run_id


# ---------------------------------------------------------------- idempotency

def test_reloading_the_same_run_creates_nothing(conn, loaded_run):
    """The central guarantee. A rerun must converge on the same finding set
    without inserting duplicates -- and without erasing anything, because human
    decisions may already be attached to these findings."""
    before = scalar(conn, "SELECT COUNT(*) FROM capstone.fact_findings WHERE last_seen_run_id = %s",
                    (loaded_run,))

    result = load_all(loaded_run, conn=conn)

    after = scalar(conn, "SELECT COUNT(*) FROM capstone.fact_findings WHERE last_seen_run_id = %s",
                   (loaded_run,))

    assert result["findings_created"] == 0
    assert result["findings_skipped"] == result["findings_offered"]
    assert after == before

    conn.rollback()


def test_natural_key_is_unique_within_a_run(conn, loaded_run):
    duplicates = scalar(conn, """
        SELECT COUNT(*) FROM (
            SELECT rule_code, entity_type, entity_id, evidence_hash
            FROM capstone.fact_findings WHERE last_seen_run_id = %s
            GROUP BY 1, 2, 3, 4 HAVING COUNT(*) > 1
        ) d
    """, (loaded_run,))
    assert duplicates == 0


def test_evidence_hash_is_a_full_sha256(conn, loaded_run):
    """A truncated or absent hash would silently weaken the natural key."""
    bad = scalar(conn, """
        SELECT COUNT(*) FROM capstone.fact_findings
        WHERE last_seen_run_id = %s AND (evidence_hash IS NULL OR LENGTH(evidence_hash) <> 64)
    """, (loaded_run,))
    assert bad == 0


# ---------------------------------------------------------------- audit trail

def test_every_finding_has_exactly_one_detection_event(conn, loaded_run):
    """Without a detection event the case has no beginning, and its history
    cannot be reconstructed from the events table alone."""
    orphans = scalar(conn, """
        SELECT COUNT(*) FROM capstone.fact_findings f
        WHERE f.last_seen_run_id = %s AND NOT EXISTS (
            SELECT 1 FROM capstone.fact_finding_events e
            WHERE e.finding_key = f.finding_key AND e.event_type = 'DETECTED'
        )
    """, (loaded_run,))
    assert orphans == 0

    doubled = scalar(conn, """
        SELECT COUNT(*) FROM (
            SELECT e.finding_key FROM capstone.fact_finding_events e
            JOIN capstone.fact_findings f USING (finding_key)
            WHERE f.last_seen_run_id = %s AND e.event_type = 'DETECTED'
            GROUP BY e.finding_key HAVING COUNT(*) > 1
        ) d
    """, (loaded_run,))
    assert doubled == 0


def test_detection_only_ever_creates_open_cases(conn, loaded_run):
    """Detection may not pre-decide a case.

    Asserted against the DETECTED event rather than the current status: once
    reviewers work the queue, findings legitimately hold other statuses, so
    checking current state would test the review loop rather than the detector.
    """
    bad = scalar(conn, """
        SELECT COUNT(*) FROM capstone.fact_finding_events e
        JOIN capstone.fact_findings f USING (finding_key)
        WHERE f.last_seen_run_id = %s
          AND e.event_type = 'DETECTED'
          AND (e.to_status <> 'OPEN' OR e.from_status IS NOT NULL
               OR e.actor_type <> 'SYSTEM')
    """, (loaded_run,))
    assert bad == 0


def test_status_changes_only_ever_come_from_humans(conn, loaded_run):
    """Anything past detection must be attributed to a person. An automated
    status change would mean something decided a case without review."""
    bad = scalar(conn, """
        SELECT COUNT(*) FROM capstone.fact_finding_events e
        JOIN capstone.fact_findings f USING (finding_key)
        WHERE f.last_seen_run_id = %s
          AND e.event_type <> 'DETECTED'
          AND e.actor_type <> 'HUMAN'
    """, (loaded_run,))
    assert bad == 0


def test_current_status_agrees_with_the_latest_event(conn, loaded_run):
    """fact_findings.status is a convenience column; fact_finding_events is the
    history. If they disagree, one of them is lying."""
    mismatched = rows(conn, """
        SELECT f.finding_id, f.status, latest.to_status
        FROM capstone.fact_findings f
        JOIN (
            SELECT DISTINCT ON (finding_key) finding_key, to_status
            FROM capstone.fact_finding_events
            ORDER BY finding_key, event_seq DESC
        ) latest USING (finding_key)
        WHERE f.last_seen_run_id = %s AND f.status <> latest.to_status
    """, (loaded_run,))
    assert mismatched == []


def test_no_event_actor_is_an_ai(conn):
    ai_actors = scalar(conn, """
        SELECT COUNT(*) FROM capstone.fact_finding_events
        WHERE actor ILIKE 'ai%' OR actor ILIKE '%investigator%'
    """)
    assert ai_actors == 0


# ---------------------------------------------------------------- reconciliation

def test_all_four_modules_contributed(conn, loaded_run):
    """If a module stops emitting, the dashboard loses a whole category of
    exception silently. Fail loudly instead."""
    modules = {
        row[0] for row in rows(conn, """
            SELECT DISTINCT source_module FROM capstone.fact_findings WHERE last_seen_run_id = %s
        """, (loaded_run,))
    }
    assert modules == set(CONTRACT["source_modules"])


def test_loaded_exposure_matches_the_control_views(conn, loaded_run):
    """The case model must not alter the amounts the controls computed. Any gap
    here means a dashboard figure cannot be traced back to its source query."""
    for module, view in (
        ("RECONCILIATION", "capstone.vw_reconciliation_findings"),
    ):
        from_table = scalar(conn, """
            SELECT COALESCE(SUM(risk_amount), 0) FROM capstone.fact_findings
            WHERE last_seen_run_id = %s AND source_module = %s
        """, (loaded_run, module))
        from_view = scalar(conn, f"SELECT COALESCE(SUM(risk_amount), 0) FROM {view}")
        assert from_table == from_view, module

    sql_modules = scalar(conn, """
        SELECT COALESCE(SUM(risk_amount), 0) FROM capstone.fact_findings
        WHERE last_seen_run_id = %s AND source_module IN ('KPI', 'DATA_QUALITY')
    """, (loaded_run,))
    sql_view = scalar(conn, "SELECT COALESCE(SUM(risk_amount), 0) FROM capstone.vw_sql_findings")
    assert sql_modules == sql_view


def test_every_reconciliation_break_links_to_its_finding(conn, loaded_run):
    """A break with no finding cannot be investigated; a matched row with a
    finding means something was classified twice."""
    unlinked_breaks = scalar(conn, """
        SELECT COUNT(*) FROM capstone.fact_reconciliation_results
        WHERE run_id = %s AND match_status <> 'MATCHED' AND finding_key IS NULL
    """, (loaded_run,))
    assert unlinked_breaks == 0

    matched_with_finding = scalar(conn, """
        SELECT COUNT(*) FROM capstone.fact_reconciliation_results
        WHERE run_id = %s AND match_status = 'MATCHED' AND finding_key IS NOT NULL
    """, (loaded_run,))
    assert matched_with_finding == 0


def test_reconciliation_results_cover_every_transaction(conn, loaded_run):
    persisted = scalar(conn, """
        SELECT COUNT(*) FROM capstone.fact_reconciliation_results WHERE run_id = %s
    """, (loaded_run,))
    classified = scalar(conn, "SELECT COUNT(*) FROM capstone.vw_reconciliation_matches")
    assert persisted == classified


# ---------------------------------------------------------------- exposure

def test_deduplicated_exposure_is_never_above_gross(conn, loaded_run):
    gross, dedup = rows(conn, """
        SELECT gross_exposure, deduplicated_exposure
        FROM capstone.vw_run_exposure WHERE run_id = %s
    """, (loaded_run,))[0]
    assert dedup <= gross


def test_deduplication_actually_removes_double_counting(conn, loaded_run):
    """Several modules can flag the same order -- ORD-TEST-005 trips both
    DELIVERED_UNPAID and PAYMENT_AMOUNT_MISMATCH. If dedup equalled gross, the
    headline exposure would be counting that money twice."""
    shared_orders = scalar(conn, """
        SELECT COUNT(*) FROM (
            SELECT order_id FROM capstone.fact_findings
            WHERE last_seen_run_id = %s AND order_id IS NOT NULL
            GROUP BY order_id HAVING COUNT(*) > 1
        ) s
    """, (loaded_run,))
    assert shared_orders > 0, "no shared orders makes this test vacuous"

    gross, dedup = rows(conn, """
        SELECT gross_exposure, deduplicated_exposure
        FROM capstone.vw_run_exposure WHERE run_id = %s
    """, (loaded_run,))[0]
    assert dedup < gross


def test_severity_never_falls_below_its_rule_floor(conn, loaded_run):
    """dim_rule advertises a floor per rule; a finding below it would make the
    registry a lie."""
    order = CONTRACT["severity_levels"]
    offenders = rows(conn, """
        SELECT f.finding_id, f.rule_code, f.severity, r.default_severity
        FROM capstone.fact_findings f
        JOIN capstone.dim_rule r USING (rule_code)
        WHERE f.last_seen_run_id = %s
    """, (loaded_run,))
    for finding_id, rule_code, severity, floor in offenders:
        assert order.index(severity) <= order.index(floor), (
            f"{finding_id} ({rule_code}) is {severity}, below its {floor} floor"
        )


def test_sla_due_dates_follow_severity(conn, loaded_run):
    """A CRITICAL case given a 14-day deadline would make the SLA breach rate
    meaningless."""
    for severity, expected_days in CONTRACT["sla_days_by_severity"].items():
        bad = scalar(conn, """
            SELECT COUNT(*) FROM capstone.fact_findings
            WHERE last_seen_run_id = %s AND severity = %s
              AND due_date <> (detected_at::date + %s)
        """, (loaded_run, severity, expected_days))
        assert bad == 0, f"{severity} findings have the wrong due date"


def test_run_row_records_what_was_loaded(conn, loaded_run):
    created, skipped, orders_loaded = rows(conn, """
        SELECT findings_created, findings_skipped, orders_loaded
        FROM capstone.fact_pipeline_runs WHERE run_id = %s
    """, (loaded_run,))[0]

    actual = scalar(conn, "SELECT COUNT(*) FROM capstone.fact_findings WHERE last_seen_run_id = %s",
                    (loaded_run,))
    staged_orders = scalar(conn, "SELECT COUNT(*) FROM staging.orders")

    assert created + skipped == actual or created == 0, (
        "a rerun reports 0 created and all skipped; a first load reports the full count"
    )
    assert orders_loaded == staged_orders
