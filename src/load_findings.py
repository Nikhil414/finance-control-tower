"""Load findings from every detection module into the case model.

Three sources, one destination:

  * SQL control findings      -- capstone.vw_sql_findings
  * reconciliation findings   -- capstone.vw_reconciliation_findings
  * leakage findings          -- outputs/leakage_findings.json

The SQL views emit raw facts only: rule, entity, business date, exposure and
evidence. Identity, severity, priority and SLA are applied here through
src/contract.py, so those decisions exist in exactly one place rather than being
reimplemented in SQL and in Python where they could drift apart.

Idempotency is the whole point of this module. Re-running a run_id inserts
nothing new and destroys nothing, because uniqueness rests on what an exception
IS -- rule, entity and evidence hash -- not on the label it was given.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from contract import (
    CONTRACT,
    RULES_BY_CODE,
    due_date_for,
    evidence_hash,
    make_finding_id,
    priority_score,
    resolve_severity,
    utc_now_iso,
    validate_finding,
)
from db import connect

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "outputs"

logger = logging.getLogger("capstone.load_findings")

SQL_FINDING_VIEWS = (
    "capstone.vw_sql_findings",
    "capstone.vw_reconciliation_findings",
)


# ---------------------------------------------------------------- run record


def open_run(conn: psycopg.Connection, run_id: str) -> None:
    """Upsert the run row. Upsert, not insert, so a rerun resumes rather than
    colliding on the primary key.

    finished_at must be cleared on reopen. Leaving the previous run's finish
    time in place alongside a fresh started_at produces a row that finished
    before it started -- which the run_finished_after_started constraint
    correctly refuses.
    """
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO capstone.fact_pipeline_runs
                   (run_id, run_date, started_at, status, contract_version)
               VALUES (%s, %s, %s, 'RUNNING', %s)
               ON CONFLICT (run_id) DO UPDATE SET
                   started_at = EXCLUDED.started_at,
                   finished_at = NULL,
                   status = 'RUNNING',
                   error_message = NULL""",
            (
                run_id,
                datetime.strptime(run_id[4:12], "%Y%m%d").date(),
                datetime.now(timezone.utc),
                CONTRACT["version"],
            ),
        )


def close_run(conn: psycopg.Connection, run_id: str, status: str,
              created: int = 0, skipped: int = 0,
              error_message: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE capstone.fact_pipeline_runs SET
                   finished_at = %s,
                   status = %s,
                   findings_created = %s,
                   findings_skipped = %s,
                   error_message = %s,
                   orders_loaded    = (SELECT COUNT(*) FROM staging.orders),
                   payments_loaded  = (SELECT COUNT(*) FROM staging.payments),
                   refunds_loaded   = (SELECT COUNT(*) FROM staging.refunds),
                   bank_txns_loaded = (SELECT COUNT(*) FROM staging.bank_transactions),
                   ledger_txns_loaded = (SELECT COUNT(*) FROM staging.ledger_transactions)
               WHERE run_id = %s""",
            (datetime.now(timezone.utc), status, created, skipped, error_message, run_id),
        )


def set_ai_status(conn: psycopg.Connection, run_id: str, status: str,
                  reason: str | None = None) -> None:
    """Record how the AI step went. Never changes the run's own status: AI is an
    annotation layer, and its failure is not a pipeline failure."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE capstone.fact_pipeline_runs
               SET ai_status = %s, ai_failure_reason = %s
               WHERE run_id = %s""",
            (status, reason, run_id),
        )


# ---------------------------------------------------------------- enrichment


def _segment_lookup(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT customer_id, customer_segment FROM staging.customers")
        return {row[0]: row[1] for row in cur.fetchall()}


def _as_of_date(conn: psycopg.Connection):
    """The dataset's latest activity, used to age findings deterministically.

    Wall-clock now() would make priority scores and SLA dates drift every day
    the pipeline is not run, so identical input would stop producing identical
    output.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT GREATEST(
                (SELECT MAX(order_date)          FROM staging.orders),
                (SELECT MAX(payment_date)::date  FROM staging.payments),
                (SELECT MAX(refund_date)::date   FROM staging.refunds)
            )
        """)
        return cur.fetchone()[0]


def collect_sql_findings(conn: psycopg.Connection, run_id: str) -> list[dict]:
    """Read the SQL control views and enrich each row into a full finding."""
    segments = _segment_lookup(conn)
    as_of = _as_of_date(conn)
    detected_at = datetime.now(timezone.utc)
    detected_at_iso = utc_now_iso()

    findings: list[dict] = []
    counters: dict[str, int] = {}

    for view in SQL_FINDING_VIEWS:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT source_module, rule_code, entity_type, entity_id,
                       order_id, customer_id, business_date, risk_amount,
                       evidence_json
                FROM {view}
                ORDER BY rule_code, entity_id
            """)
            columns = [description.name for description in cur.description]
            raw_rows = [dict(zip(columns, row)) for row in cur.fetchall()]

        for raw in raw_rows:
            module = raw["source_module"]
            counters[module] = counters.get(module, 0) + 1

            segment = segments.get(raw["customer_id"], "Retail")
            risk_amount = float(raw["risk_amount"] or 0)
            severity = resolve_severity(
                risk_amount, raw["rule_code"], {"customer_segment": segment}
            )
            business_date = raw["business_date"]
            days_open = max(0, (as_of - business_date).days) if business_date else 0

            evidence = {
                key: (float(value) if hasattr(value, "quantize") else value)
                for key, value in raw["evidence_json"].items()
            }

            findings.append(validate_finding({
                "finding_id": make_finding_id(module, counters[module]),
                "run_id": run_id,
                "source_module": module,
                "rule_code": raw["rule_code"],
                "entity_type": raw["entity_type"],
                "entity_id": str(raw["entity_id"]),
                "order_id": raw["order_id"],
                "customer_id": raw["customer_id"],
                "detected_at": detected_at_iso,
                "business_date": business_date.isoformat(),
                "severity": severity,
                "risk_amount": risk_amount,
                "priority_score": priority_score(
                    severity, days_open, segment, risk_amount
                ),
                "days_open": days_open,
                "evidence_json": evidence,
                "recommended_action": RULES_BY_CODE[raw["rule_code"]]["exposure_basis"],
                "status": "OPEN",
                "due_date": due_date_for(severity, detected_at),
            }))

    return findings


def load_leakage_findings(path: Path = OUTPUT_DIR / "leakage_findings.json") -> list[dict]:
    """Read leakage findings, which arrive already contract-shaped.

    Re-validated anyway. The file is an artifact on disk that something else
    could have touched between engine and loader, and trusting an unverified
    input is how bad data reaches a financial table.
    """
    if not path.exists():
        logger.warning("no leakage findings file at %s -- skipping", path)
        return []

    raw = json.loads(path.read_text(encoding="utf-8"))
    return [validate_finding({
        key: value for key, value in finding.items()
        if key not in ("evidence_hash", "contract_version", "customer_segment",
                       "approval_required", "currency")
    }) for finding in raw]


# ---------------------------------------------------------------- insert


INSERT_FINDING = """
INSERT INTO capstone.fact_findings (
    finding_id, run_id, last_seen_run_id, source_module, rule_code, entity_type,
    entity_id, order_id, customer_id, detected_at, business_date, severity,
    risk_amount, currency, priority_score, days_open, evidence_json,
    evidence_hash, recommended_action, approval_required, status, due_date,
    contract_version
) VALUES (
    %(finding_id)s, %(run_id)s, %(run_id)s, %(source_module)s, %(rule_code)s,
    %(entity_type)s, %(entity_id)s, %(order_id)s, %(customer_id)s,
    %(detected_at)s, %(business_date)s, %(severity)s, %(risk_amount)s,
    %(currency)s, %(priority_score)s, %(days_open)s, %(evidence_json)s,
    %(evidence_hash)s, %(recommended_action)s, %(approval_required)s,
    %(status)s, %(due_date)s, %(contract_version)s
)
ON CONFLICT (rule_code, entity_type, entity_id, evidence_hash)
DO UPDATE SET
    last_seen_run_id = EXCLUDED.last_seen_run_id,
    -- Only count a recurrence when a DIFFERENT run sees the case again.
    -- Re-running the same run_id must leave every column untouched, or
    -- idempotency would quietly inflate the recurrence counter.
    times_seen = capstone.fact_findings.times_seen
                 + CASE WHEN capstone.fact_findings.last_seen_run_id
                             IS DISTINCT FROM EXCLUDED.last_seen_run_id
                        THEN 1 ELSE 0 END,
    days_open = EXCLUDED.days_open,
    priority_score = EXCLUDED.priority_score
-- xmax is 0 only on a genuine INSERT and non-zero on the UPDATE path, which is
-- the reliable way to tell the two apart. Inspecting times_seen instead cannot
-- distinguish a brand-new case from a same-run rerun of one: both leave the
-- counter at 1.
RETURNING finding_key, (xmax = 0) AS is_new
"""


def insert_findings(conn: psycopg.Connection, findings: list[dict]) -> tuple[int, int]:
    """Upsert findings. New exceptions are created; recurring ones are updated.

    A case is identified by what the exception IS, not by the run that noticed
    it, so an unresolved problem detected again updates the existing case rather
    than opening a rival beside it. That is what lets aging, SLA and backlog
    mean anything across runs -- and what stops a review recorded yesterday from
    being orphaned by today's run.

    Re-running the SAME run_id changes nothing at all: the recurrence counter
    only advances when a different run sees the case, so "0 created" on a rerun
    remains positive proof that idempotency held.
    """
    created = 0
    skipped = 0

    with conn.cursor() as cur:
        for finding in findings:
            payload = dict(finding)
            payload["evidence_json"] = json.dumps(finding["evidence_json"], default=str)
            cur.execute(INSERT_FINDING, payload)
            finding_key, is_new = cur.fetchone()

            if not is_new:
                skipped += 1
                continue

            created += 1
            # Detection is the first event in a case's life, and the only one an
            # automated actor may write.
            cur.execute(
                """INSERT INTO capstone.fact_finding_events
                       (finding_key, finding_id, run_id, event_seq, from_status,
                        to_status, event_type, actor, actor_type, note)
                   VALUES (%s, %s, %s, 1, NULL, 'OPEN', 'DETECTED', %s, 'SYSTEM', %s)""",
                (
                    finding_key,
                    finding["finding_id"],
                    finding["run_id"],
                    f"{finding['source_module'].lower()}_detector",
                    f"Detected by rule {finding['rule_code']}",
                ),
            )

    return created, skipped


def load_reconciliation_results(conn: psycopg.Connection, run_id: str) -> int:
    """Persist every reconciliation outcome, matched rows included.

    The match rate needs a denominator: a table of breaks alone cannot say what
    share of settlement reconciled. Breaks are linked back to the finding they
    raised so a dashboard can drill from a match-rate tile into the open case.
    """
    with conn.cursor() as cur:
        # Snapshot table, not a log: rpt_fact_reconciliation reads it with no
        # run_id filter, so a stale prior run's rows would double-count
        # forever instead of just this run's. Purge everything, not just
        # this run_id, before reloading.
        cur.execute("DELETE FROM capstone.fact_reconciliation_results")
        cur.execute("""
            INSERT INTO capstone.fact_reconciliation_results (
                run_id, bank_txn_id, ledger_txn_id, match_status, matched_on,
                bank_amount, ledger_amount, amount_difference,
                bank_value_date, ledger_posting_date, day_difference,
                business_date, aging_bucket, finding_key
            )
            SELECT
                %(run_id)s, m.bank_txn_id, m.ledger_txn_id, m.match_status,
                m.matched_on, m.bank_amount, m.ledger_amount, m.amount_difference,
                m.bank_value_date, m.ledger_posting_date, m.day_difference,
                m.business_date, m.aging_bucket,
                f.finding_key
            FROM capstone.vw_reconciliation_matches m
            LEFT JOIN capstone.fact_findings f
                   ON f.last_seen_run_id = %(run_id)s
                  AND f.source_module = 'RECONCILIATION'
                  AND f.entity_id = COALESCE(m.bank_txn_id, m.ledger_txn_id)
            WHERE m.match_status = 'MATCHED' OR f.finding_key IS NOT NULL
        """, {"run_id": run_id})
        return cur.rowcount


# ---------------------------------------------------------------- export


def export_findings_json(conn: psycopg.Connection, run_id: str,
                         output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    """Write the run's findings and summary to JSON for the AI investigator.

    This file IS the investigator's entire world. It receives a path and nothing
    else -- no connection, no credentials -- so the boundary is enforced by what
    the process is given rather than by what it is told not to do.

    Only evidence-bearing fields are exported. Internal keys stay in the
    database because there is no reason to ship them to a third-party API.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT finding_id, run_id, source_module, rule_code, entity_type,
                   entity_id, order_id, customer_id, detected_at, business_date,
                   severity, risk_amount, currency, priority_score, days_open,
                   evidence_json, recommended_action, approval_required, status,
                   due_date
            FROM capstone.fact_findings
            WHERE last_seen_run_id = %s
            ORDER BY priority_score DESC, risk_amount DESC
        """, (run_id,))
        columns = [description.name for description in cur.description]
        findings = [dict(zip(columns, row)) for row in cur.fetchall()]

        cur.execute("""
            SELECT findings_count, gross_exposure, deduplicated_exposure,
                   critical_count, high_count, medium_count, low_count
            FROM capstone.vw_run_exposure WHERE run_id = %s
        """, (run_id,))
        exposure_row = cur.fetchone()
        exposure_columns = [description.name for description in cur.description]

        cur.execute("""
            SELECT match_rate_pct, matched, breaks, break_exposure
            FROM capstone.vw_reconciliation_summary
        """)
        recon = cur.fetchone()

        cur.execute("SELECT data_quality_score_pct FROM capstone.vw_data_quality_score")
        dq_score = cur.fetchone()[0]

    summary = {
        "run_id": run_id,
        "contract_version": CONTRACT["version"],
        **(dict(zip(exposure_columns, exposure_row)) if exposure_row else {}),
        "reconciliation_match_rate_pct": recon[0] if recon else None,
        "reconciliation_breaks": recon[2] if recon else None,
        "reconciliation_break_exposure": recon[3] if recon else None,
        "data_quality_score_pct": dq_score,
    }

    findings_path = output_dir / "findings.json"
    summary_path = output_dir / "run_summary.json"
    findings_path.write_text(
        json.dumps(findings, indent=2, default=str), encoding="utf-8"
    )
    summary_path.write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    return findings_path, summary_path


# ---------------------------------------------------------------- entry point


def load_all(run_id: str, conn: psycopg.Connection | None = None) -> dict:
    owns_connection = conn is None
    connection = conn or connect()

    try:
        open_run(connection, run_id)

        findings = collect_sql_findings(connection, run_id)
        findings.extend(load_leakage_findings())

        created, skipped = insert_findings(connection, findings)
        recon_rows = load_reconciliation_results(connection, run_id)

        close_run(connection, run_id, "SUCCESS", created, skipped)
        export_findings_json(connection, run_id)
        if owns_connection:
            connection.commit()

        return {
            "run_id": run_id,
            "findings_offered": len(findings),
            "findings_created": created,
            "findings_skipped": skipped,
            "reconciliation_rows": recon_rows,
        }
    except Exception as error:
        connection.rollback()
        open_run(connection, run_id)
        close_run(connection, run_id, "FAILED", error_message=str(error)[:2000])
        if owns_connection:
            connection.commit()
        raise
    finally:
        if owns_connection:
            connection.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    from contract import make_run_id

    result = load_all(make_run_id(1))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
