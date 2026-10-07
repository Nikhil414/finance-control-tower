"""End-to-end pipeline test and the three failure demonstrations.

These tests run the real orchestrator against the real database. They are the
ones that answer "does the whole thing actually work", as distinct from the unit
tests that answer "does each part behave".

The three failure demonstrations are as important as the happy path. A control
system is defined as much by how it fails as by what it computes:

  1. a missing source file HALTS processing before any financial figure exists
  2. an AI outage does NOT halt anything
  3. a rerun duplicates nothing and destroys nothing

Run: pytest tests/test_end_to_end.py -v
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

import run_pipeline  # noqa: E402
from db import connect  # noqa: E402
from load_source import RAW  # noqa: E402

EXPECTED_MODULES = {"KPI", "DATA_QUALITY", "LEAKAGE", "RECONCILIATION"}

# Seeded into the dataset on purpose; see src/generate_settlement_data.py.
EXPECTED_RECONCILIATION_BREAKS = 15
EXPECTED_LEAKAGE_RULES = 6


@pytest.fixture(scope="module")
def conn():
    """Autocommit, deliberately.

    These tests observe a pipeline that runs on its own connection, and the
    loader TRUNCATEs the staging tables -- which needs an ACCESS EXCLUSIVE lock.
    A non-autocommit observer holds a transaction open from its first SELECT, so
    that TRUNCATE would block forever and the suite would hang rather than fail.
    """
    connection = connect(autocommit=True)
    yield connection
    connection.close()


def scalar(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


@pytest.fixture(scope="module")
def pipeline_run():
    """One real pipeline execution, shared by every test in this module."""
    return run_pipeline.run(apply_schema=True, use_ai=True, build_workbook=True)


# ---------------------------------------------------------------- happy path

def test_pipeline_completes(pipeline_run):
    assert pipeline_run["run_id"].startswith("RUN-")
    failed = [
        stage for stage in pipeline_run["stages"]
        if stage["status"] == "FAILED"
    ]
    assert failed == [], f"stages failed: {failed}"


def test_every_stage_ran(pipeline_run):
    stages = {stage["stage"] for stage in pipeline_run["stages"]}
    assert {
        "validate source files", "load staging", "detect leakage",
        "persist findings", "ai investigator", "build review workbook",
    } <= stages


def test_all_four_detection_modules_produced_findings(pipeline_run):
    """If a module silently stops emitting, a whole category of exception
    vanishes from the dashboard without anything looking broken."""
    assert set(pipeline_run["by_module"]) == EXPECTED_MODULES
    for module, data in pipeline_run["by_module"].items():
        assert data["findings"] > 0, f"{module} produced nothing"


def test_all_six_leakage_rules_are_exercised(conn, pipeline_run):
    """The dataset seeds a trigger for each rule. Fewer than six means a rule
    stopped firing and its regression would otherwise go unnoticed."""
    rules = scalar(conn, """
        SELECT COUNT(DISTINCT rule_code) FROM capstone.fact_findings
        WHERE source_module = 'LEAKAGE'
    """)
    assert rules == EXPECTED_LEAKAGE_RULES


def test_all_five_reconciliation_break_types_are_detected(conn):
    breaks = dict(_rows(conn, """
        SELECT rule_code, COUNT(*) FROM capstone.fact_findings
        WHERE source_module = 'RECONCILIATION' GROUP BY rule_code
    """))
    assert set(breaks) == {
        "BANK_ONLY_TRANSACTION", "LEDGER_ONLY_TRANSACTION", "AMOUNT_MISMATCH",
        "DATE_MISMATCH", "DUPLICATE_SETTLEMENT",
    }
    assert sum(breaks.values()) == EXPECTED_RECONCILIATION_BREAKS


def _rows(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def test_outputs_were_written(pipeline_run):
    for relative in (
        "outputs/executive_brief.md",
        "outputs/ai_commentary.json",
        "outputs/findings.json",
        "outputs/run_summary.json",
        "outputs/pipeline_summary.json",
        "excel/Finance_Review_Workbook.xlsx",
    ):
        path = BASE_DIR / relative
        assert path.exists(), f"{relative} not produced"
        assert path.stat().st_size > 0, f"{relative} is empty"


def test_dashboard_totals_reconcile_with_the_database(conn, pipeline_run):
    """The Definition of Done requires Power BI totals to match PostgreSQL.
    rpt_control_totals is the baseline both sides compare against, so it must
    agree with the facts it summarises."""
    totals = pipeline_run["control_totals"]

    assert totals["findings_total"] == scalar(
        conn, "SELECT COUNT(*) FROM capstone.fact_findings"
    )
    assert totals["gross_exposure"] == float(scalar(
        conn, "SELECT COALESCE(SUM(risk_amount), 0) FROM capstone.fact_findings"
    ))
    assert totals["deduplicated_exposure"] == float(scalar(
        conn, "SELECT COALESCE(SUM(deduplicated_risk_amount), 0) FROM capstone.rpt_fact_findings"
    ))
    assert totals["net_revenue"] == float(scalar(
        conn, "SELECT COALESCE(SUM(net_revenue), 0) FROM capstone.vw_daily_kpis"
    ))


def test_deduplicated_exposure_is_below_gross(pipeline_run):
    totals = pipeline_run["control_totals"]
    assert totals["deduplicated_exposure"] < totals["gross_exposure"], (
        "no deduplication occurred -- the headline figure is double-counting "
        "orders flagged by more than one rule"
    )


def test_estimated_exposure_and_confirmed_loss_stay_separate(conn):
    """The semantic guarantee the whole system rests on. A detector must never
    be able to assert a loss."""
    assert scalar(conn, """
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema = 'capstone' AND table_name = 'fact_findings'
          AND column_name IN ('confirmed_loss', 'recovered_amount')
    """) == 0


def test_every_finding_is_traceable_to_a_run_and_a_rule(conn):
    """Traceability: every dashboard number must lead back to a pipeline run and
    a rule definition."""
    assert scalar(conn, """
        SELECT COUNT(*) FROM capstone.fact_findings f
        LEFT JOIN capstone.fact_pipeline_runs r ON r.run_id = f.run_id
        LEFT JOIN capstone.dim_rule d ON d.rule_code = f.rule_code
        WHERE r.run_id IS NULL OR d.rule_code IS NULL
    """) == 0


# ------------------------------------------------- demo 1: source file failure

def test_missing_source_file_halts_financial_processing(conn):
    """FAILURE DEMONSTRATION 1.

    A required source file is removed. The pipeline must stop BEFORE computing
    any KPI or exposure figure, and must record the failure.

    This is the deliberate design choice that matters most: a half-loaded
    dataset produces numbers that look entirely plausible and are wrong. No
    number is safer than a confident wrong one.
    """
    victim = RAW / "payments.csv"
    backup = RAW / "payments.csv.e2e-backup"

    findings_before = scalar(conn, "SELECT COUNT(*) FROM capstone.fact_findings")

    shutil.move(victim, backup)
    try:
        with pytest.raises(run_pipeline.PipelineFailed) as failure:
            run_pipeline.run(apply_schema=False, use_ai=False, build_workbook=False)

        assert "payments.csv" in str(failure.value)
        assert "halted" in str(failure.value).lower()

        # The run is recorded, not silently abandoned.
        status, message = _rows(conn, """
            SELECT status, error_message FROM capstone.fact_pipeline_runs
            ORDER BY started_at DESC LIMIT 1
        """)[0]
        assert status == "VALIDATION_FAILED"
        assert "payments.csv" in message

        # And nothing was written to the case model.
        assert scalar(conn, "SELECT COUNT(*) FROM capstone.fact_findings") == findings_before
    finally:
        shutil.move(backup, victim)

    # Recovery: with the file restored, the pipeline works again.
    recovered = run_pipeline.run(apply_schema=False, use_ai=False, build_workbook=False)
    assert recovered["control_totals"]["findings_total"] == findings_before


# ------------------------------------------------------- demo 2: AI outage

def test_ai_outage_does_not_stop_the_pipeline(conn, monkeypatch):
    """FAILURE DEMONSTRATION 2.

    The AI call is forced to fail. Every deterministic output must still be
    produced, the run must still succeed, and the failure must be recorded
    rather than hidden.

    A finance control system that stops controlling because a language model is
    rate-limited is not a control system.
    """
    import ai_investigator

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-deliberately-invalid")

    def explode(*args, **kwargs):
        raise TimeoutError("simulated outage")

    monkeypatch.setattr(ai_investigator.AIInvestigator, "_draft_commentary", explode)

    summary = run_pipeline.run(apply_schema=False, use_ai=True, build_workbook=False)

    assert summary["ai_status"] == "DEGRADED"
    assert "TimeoutError" in summary["ai_reason"]

    # The pipeline itself succeeded.
    assert [s for s in summary["stages"] if s["status"] == "FAILED"] == []
    assert summary["control_totals"]["findings_total"] > 0

    # AI status is recorded on the run without changing the run's own status.
    status, ai_status, reason = _rows(conn, """
        SELECT status, ai_status, ai_failure_reason
        FROM capstone.fact_pipeline_runs WHERE run_id = %s
    """, (summary["run_id"],))[0]
    assert status == "SUCCESS"
    assert ai_status == "DEGRADED"
    assert "TimeoutError" in reason

    # The brief still exists and still carries real figures.
    brief = (BASE_DIR / "outputs" / "executive_brief.md").read_text(encoding="utf-8")
    assert "Revenue Assurance Daily Brief" in brief
    assert "Priority review queue" in brief


def test_pipeline_runs_with_ai_switched_off(conn):
    summary = run_pipeline.run(apply_schema=False, use_ai=False, build_workbook=False)
    assert summary["ai_status"] == "NOT_ATTEMPTED"
    assert summary["control_totals"]["findings_total"] > 0


# ------------------------------------------------------ demo 3: idempotency

def test_rerunning_the_same_run_id_changes_nothing(conn):
    """FAILURE DEMONSTRATION 3.

    The same run_id is executed twice. Nothing is created, nothing is
    destroyed, and no counter advances.
    """
    first = run_pipeline.run(apply_schema=False, use_ai=False, build_workbook=False)
    run_id = first["run_id"]

    before = _rows(conn, """
        SELECT COUNT(*), COALESCE(SUM(risk_amount), 0), COALESCE(SUM(times_seen), 0)
        FROM capstone.fact_findings
    """)[0]

    repeat = run_pipeline.run(run_id=run_id, apply_schema=False, use_ai=False,
                              build_workbook=False)

    after = _rows(conn, """
        SELECT COUNT(*), COALESCE(SUM(risk_amount), 0), COALESCE(SUM(times_seen), 0)
        FROM capstone.fact_findings
    """)[0]

    assert repeat["findings_created"] == 0
    assert repeat["findings_skipped"] > 0
    assert after == before, (
        "re-running one run_id must be a no-op, including the recurrence counter"
    )


def test_a_new_run_recognises_recurring_cases(conn):
    """A fresh run over unchanged data must find the SAME cases, not new ones.

    This is what makes aging, SLA and backlog meaningful. If each run opened its
    own copy of every unresolved exception, exposure would accumulate without
    anything getting worse, and a review recorded yesterday would be orphaned
    beside a fresh duplicate.
    """
    before_count = scalar(conn, "SELECT COUNT(*) FROM capstone.fact_findings")
    before_seen = scalar(conn, "SELECT COALESCE(SUM(times_seen), 0) FROM capstone.fact_findings")

    summary = run_pipeline.run(apply_schema=False, use_ai=False, build_workbook=False)

    after_count = scalar(conn, "SELECT COUNT(*) FROM capstone.fact_findings")
    after_seen = scalar(conn, "SELECT COALESCE(SUM(times_seen), 0) FROM capstone.fact_findings")

    assert summary["findings_created"] == 0, "unchanged data must not create cases"
    assert after_count == before_count, "case count must be stable across runs"
    assert after_seen == before_seen + before_count, (
        "every case should have been seen once more"
    )

    # First detection is preserved; last-seen moves forward.
    first_seen, last_seen = _rows(conn, """
        SELECT MIN(run_id), MAX(last_seen_run_id) FROM capstone.fact_findings
    """)[0]
    assert last_seen == summary["run_id"]
    assert first_seen < last_seen, "first detection must not be overwritten"


def test_exposure_does_not_accumulate_across_runs(conn):
    """The bug this model exists to prevent: exposure inflating simply because
    the pipeline ran again."""
    exposure_now = float(scalar(
        conn, "SELECT COALESCE(SUM(risk_amount), 0) FROM capstone.fact_findings"
    ))

    run_pipeline.run(apply_schema=False, use_ai=False, build_workbook=False)

    exposure_after = float(scalar(
        conn, "SELECT COALESCE(SUM(risk_amount), 0) FROM capstone.fact_findings"
    ))
    assert exposure_after == exposure_now


def test_human_decisions_survive_a_later_run(conn):
    """A review recorded today must still be attached to its case tomorrow.

    Under the previous model -- where run_id was part of the case identity --
    the next run opened a fresh duplicate and left the decision stranded on an
    older copy. This is the regression test for that.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT finding_key, finding_id FROM capstone.fact_findings
            WHERE status = 'OPEN' ORDER BY priority_score DESC LIMIT 1
        """)
        finding_key, finding_id = cur.fetchone()

        cur.execute("""
            INSERT INTO capstone.fact_reviews (
                finding_key, finding_id, decision, reviewer, reviewed_at,
                reviewer_notes, confirmed_loss, source_file
            ) VALUES (%s, %s, 'VALID_EXCEPTION', 'e2e.reviewer', now(),
                      'Recorded by the end-to-end test', 1234.56, 'test_end_to_end.py')
        """, (finding_key, finding_id))

    try:
        run_pipeline.run(apply_schema=False, use_ai=False, build_workbook=False)

        decision, loss = _rows(conn, """
            SELECT decision, confirmed_loss FROM capstone.vw_current_review
            WHERE finding_key = %s
        """, (finding_key,))[0]

        assert decision == "VALID_EXCEPTION"
        assert float(loss) == 1234.56

        # The case still exists exactly once.
        assert scalar(conn, """
            SELECT COUNT(*) FROM capstone.fact_findings WHERE finding_key = %s
        """, (finding_key,)) == 1
    finally:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE capstone.fact_reviews DISABLE TRIGGER trg_reviews_append_only")
            cur.execute(
                "DELETE FROM capstone.fact_reviews WHERE source_file = 'test_end_to_end.py'"
            )
            cur.execute("ALTER TABLE capstone.fact_reviews ENABLE TRIGGER trg_reviews_append_only")
