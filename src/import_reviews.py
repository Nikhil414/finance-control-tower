"""Validate reviewer decisions and import them into the case model.

This module is a gate. Its job is to refuse bad input, and it validates the
entire file before writing anything.

All-or-nothing is a deliberate choice. A partially applied decision file leaves
the case model in a state nobody intended and nobody can easily identify: some
cases moved, some did not, and the reviewer's spreadsheet no longer describes
reality. Rejecting the whole file with a precise list of problems is far cheaper
to recover from than unpicking a half-import.

What gets checked, in order:

  1. structure   -- required columns present
  2. references  -- every finding_id exists in the target run
  3. vocabulary  -- decisions are contract values, spelled exactly
  4. transitions -- the implied status change is legal from the current status
  5. completeness-- the fields that decision requires are filled in
  6. arithmetic  -- amounts are non-negative; recovery cannot exceed loss
  7. semantics   -- a false positive cannot carry a confirmed loss

Run: python src/import_reviews.py [path/to/review_decisions.csv]
"""

from __future__ import annotations

import csv
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg

from contract import CONTRACT
from db import connect

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CSV = BASE_DIR / "excel" / "review_decisions.csv"

logger = logging.getLogger("capstone.import_reviews")

REQUIRED_COLUMNS = {"finding_id", "decision", "reviewer"}

OPTIONAL_COLUMNS = {
    "reviewed_at", "reviewer_notes", "approved_action", "confirmed_loss",
    "recovered_amount", "false_positive_reason", "resolution_date",
}

MONEY_COLUMNS = ("confirmed_loss", "recovered_amount")
DATE_COLUMNS = ("resolution_date",)

# A decision names the status the case moves to. Same vocabulary either way, so
# the status graph in the contract validates the transition directly.
DECISION_TO_STATUS = {
    "FALSE_POSITIVE": "FALSE_POSITIVE",
    "VALID_EXCEPTION": "VALID_EXCEPTION",
    "MORE_INFORMATION_REQUIRED": "MORE_INFORMATION_REQUIRED",
    "ACTION_APPROVED": "ACTION_APPROVED",
    "RESOLVED": "RESOLVED",
}

DECISION_TO_EVENT = {
    "FALSE_POSITIVE": "REVIEWED",
    "VALID_EXCEPTION": "REVIEWED",
    "MORE_INFORMATION_REQUIRED": "REVIEWED",
    "ACTION_APPROVED": "APPROVED",
    "RESOLVED": "RESOLVED",
}

# Statuses from which submitting a decision implies the reviewer picked the case
# up. Recording a decision on an OPEN case is not a contract violation -- it
# means the pickup was never logged separately, because the workbook has no
# "claim this case" step. The importer logs OPEN -> UNDER_REVIEW explicitly so
# the event history stays a truthful sequence rather than showing a case leaping
# from untouched to decided.
#
# Only this one hop is bridged. Nothing further is inferred: ACTION_APPROVED
# still requires a prior VALID_EXCEPTION decision, because approval is a real
# control gate and auto-walking the chain would let a single spreadsheet entry
# both raise and approve its own remediation.
AUTO_PICKUP_FROM = ("OPEN", "ASSIGNED")
PICKUP_STATUS = "UNDER_REVIEW"


def effective_status(current_status: str) -> str:
    """The status a decision is validated against, after implicit pickup."""
    return PICKUP_STATUS if current_status in AUTO_PICKUP_FROM else current_status


class ReviewFileRejected(ValueError):
    """The decision file is invalid. Nothing was imported."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(
            f"{len(problems)} problem(s) found; nothing imported:\n  - "
            + "\n  - ".join(problems)
        )


@dataclass
class Decision:
    row_number: int
    finding_id: str
    decision: str
    reviewer: str
    reviewed_at: datetime
    reviewer_notes: str | None = None
    approved_action: str | None = None
    confirmed_loss: Decimal | None = None
    recovered_amount: Decimal | None = None
    false_positive_reason: str | None = None
    resolution_date: date | None = None
    problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- parsing


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _parse_money(raw: str | None, column: str, row_number: int,
                 problems: list[str]) -> Decimal | None:
    text = _clean(raw)
    if text is None:
        return None
    try:
        amount = Decimal(text.replace(",", ""))
    except InvalidOperation:
        problems.append(f"row {row_number}: {column} '{text}' is not a number")
        return None
    if amount < 0:
        problems.append(f"row {row_number}: {column} cannot be negative ({amount})")
        return None
    return amount


def _parse_date(raw: str | None, column: str, row_number: int,
                problems: list[str]) -> date | None:
    text = _clean(raw)
    if text is None:
        return None
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    problems.append(
        f"row {row_number}: {column} '{text}' is not a recognised date "
        "(use YYYY-MM-DD)"
    )
    return None


def read_decisions(path: Path) -> tuple[list[Decision], list[str]]:
    if not path.exists():
        raise ReviewFileRejected([f"decision file not found: {path}"])

    problems: list[str] = []

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = set(reader.fieldnames or ())

        missing = REQUIRED_COLUMNS - header
        if missing:
            raise ReviewFileRejected(
                [f"missing required column(s): {', '.join(sorted(missing))}"]
            )

        unknown = header - REQUIRED_COLUMNS - OPTIONAL_COLUMNS
        if unknown:
            # A warning, not a rejection: an extra column is harmless, and
            # Excel exports often carry a stray blank one.
            logger.warning("ignoring unrecognised column(s): %s", sorted(unknown))

        decisions: list[Decision] = []
        seen: dict[str, int] = {}

        for row_number, row in enumerate(reader, start=2):
            finding_id = _clean(row.get("finding_id"))
            decision_value = _clean(row.get("decision"))

            # A row with no decision is an un-reviewed case, not an error. The
            # template ships one row per open finding and reviewers work down it.
            if finding_id is None and decision_value is None:
                continue
            if decision_value is None:
                continue

            if finding_id is None:
                problems.append(f"row {row_number}: decision given with no finding_id")
                continue

            if decision_value not in DECISION_TO_STATUS:
                problems.append(
                    f"row {row_number}: '{decision_value}' is not a valid decision "
                    f"(expected one of {', '.join(sorted(DECISION_TO_STATUS))})"
                )
                continue

            reviewer = _clean(row.get("reviewer"))
            if reviewer is None:
                problems.append(
                    f"row {row_number}: reviewer is required -- an anonymous "
                    "decision is not auditable"
                )
                continue

            if finding_id in seen:
                problems.append(
                    f"row {row_number}: {finding_id} already decided on row "
                    f"{seen[finding_id]}. Submit one decision per finding per file; "
                    "to supersede an earlier decision, import a later file."
                )
                continue
            seen[finding_id] = row_number

            reviewed_at = _parse_date(row.get("reviewed_at"), "reviewed_at",
                                      row_number, problems)

            decisions.append(Decision(
                row_number=row_number,
                finding_id=finding_id,
                decision=decision_value,
                reviewer=reviewer,
                reviewed_at=datetime.combine(
                    reviewed_at, datetime.min.time(), tzinfo=timezone.utc
                ) if reviewed_at else datetime.now(timezone.utc),
                reviewer_notes=_clean(row.get("reviewer_notes")),
                approved_action=_clean(row.get("approved_action")),
                confirmed_loss=_parse_money(row.get("confirmed_loss"), "confirmed_loss",
                                            row_number, problems),
                recovered_amount=_parse_money(row.get("recovered_amount"),
                                              "recovered_amount", row_number, problems),
                false_positive_reason=_clean(row.get("false_positive_reason")),
                resolution_date=_parse_date(row.get("resolution_date"),
                                            "resolution_date", row_number, problems),
            ))

    return decisions, problems


# ---------------------------------------------------------------- validation


def validate(conn: psycopg.Connection, run_id: str,
             decisions: list[Decision]) -> list[str]:
    problems: list[str] = []
    if not decisions:
        return problems

    with conn.cursor() as cur:
        cur.execute("""
            SELECT finding_id, finding_key, status, risk_amount, approval_required
            FROM capstone.fact_findings WHERE last_seen_run_id = %s
        """, (run_id,))
        known = {
            row[0]: {"key": row[1], "status": row[2],
                     "risk_amount": row[3], "approval_required": row[4]}
            for row in cur.fetchall()
        }

    transitions = CONTRACT["status_transitions"]
    requirements = CONTRACT["decision_requirements"]

    for decision in decisions:
        finding = known.get(decision.finding_id)
        if finding is None:
            problems.append(
                f"row {decision.row_number}: {decision.finding_id} is not a finding "
                f"in run {run_id}"
            )
            continue

        target_status = DECISION_TO_STATUS[decision.decision]
        from_status = effective_status(finding["status"])
        allowed = transitions.get(from_status, [])
        if target_status not in allowed:
            picked_up = from_status != finding["status"]
            problems.append(
                f"row {decision.row_number}: {decision.finding_id} is "
                f"{finding['status']}"
                + (f" (under review once picked up)" if picked_up else "")
                + f"; cannot move to {target_status}. "
                f"Legal next: {', '.join(allowed) or 'none (terminal)'}"
                + (". ACTION_APPROVED requires the case to be recorded as a "
                   "VALID_EXCEPTION first -- approval is a separate control step"
                   if target_status == "ACTION_APPROVED" else "")
            )
            continue

        for required in requirements.get(decision.decision, []):
            if required == "reviewer":
                continue
            if getattr(decision, required, None) in (None, ""):
                problems.append(
                    f"row {decision.row_number}: {decision.decision} requires "
                    f"'{required}'"
                )

        if decision.decision == "FALSE_POSITIVE" and decision.confirmed_loss:
            problems.append(
                f"row {decision.row_number}: a FALSE_POSITIVE cannot carry a "
                f"confirmed_loss ({decision.confirmed_loss}). If a loss occurred, "
                "the finding was not a false positive."
            )

        if (decision.recovered_amount is not None
                and decision.confirmed_loss is not None
                and decision.recovered_amount > decision.confirmed_loss):
            problems.append(
                f"row {decision.row_number}: recovered_amount "
                f"({decision.recovered_amount}) exceeds confirmed_loss "
                f"({decision.confirmed_loss}) -- you cannot recover more than was lost"
            )

        if (decision.recovered_amount is not None
                and decision.confirmed_loss is None
                and decision.recovered_amount > 0):
            problems.append(
                f"row {decision.row_number}: recovered_amount given with no "
                "confirmed_loss -- record what was lost before what was recovered"
            )

    return problems


# ---------------------------------------------------------------- import


def apply_decisions(conn: psycopg.Connection, run_id: str,
                    decisions: list[Decision], source_file: str) -> dict:
    """Write validated decisions. Reviews append; findings move status.

    fact_reviews is append-only, so a re-review never destroys the earlier
    judgement -- capstone.vw_current_review resolves the latest. The status
    column on the finding is a convenience; fact_finding_events is the history.
    """
    applied = 0

    with conn.cursor() as cur:
        for decision in decisions:
            cur.execute("""
                SELECT finding_key, status FROM capstone.fact_findings
                WHERE last_seen_run_id = %s AND finding_id = %s
            """, (run_id, decision.finding_id))
            finding_key, current_status = cur.fetchone()
            target_status = DECISION_TO_STATUS[decision.decision]

            cur.execute("""
                INSERT INTO capstone.fact_reviews (
                    finding_key, finding_id, decision, reviewer, reviewed_at,
                    reviewer_notes, approved_action, confirmed_loss,
                    recovered_amount, false_positive_reason, resolution_date,
                    source_file, run_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
            """, (
                finding_key, decision.finding_id, decision.decision,
                decision.reviewer, decision.reviewed_at, decision.reviewer_notes,
                decision.approved_action, decision.confirmed_loss,
                decision.recovered_amount, decision.false_positive_reason,
                decision.resolution_date, source_file, run_id,
            ))

            cur.execute("""
                SELECT COALESCE(MAX(event_seq), 0) + 1
                FROM capstone.fact_finding_events WHERE finding_key = %s
            """, (finding_key,))
            next_seq = cur.fetchone()[0]

            # Log the pickup as its own event when it was never recorded
            # separately, so the history reads as a real sequence instead of a
            # case jumping from untouched to decided.
            if current_status in AUTO_PICKUP_FROM:
                cur.execute("""
                    INSERT INTO capstone.fact_finding_events (
                        finding_key, finding_id, run_id, event_seq, from_status,
                        to_status, event_type, actor, actor_type, note
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'ASSIGNED', %s, 'HUMAN', %s)
                """, (
                    finding_key, decision.finding_id, run_id, next_seq,
                    current_status, PICKUP_STATUS, decision.reviewer,
                    "Picked up for review on decision submission",
                ))
                next_seq += 1
                current_status = PICKUP_STATUS

            cur.execute("""
                INSERT INTO capstone.fact_finding_events (
                    finding_key, finding_id, run_id, event_seq, from_status,
                    to_status, event_type, actor, actor_type, note
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'HUMAN', %s)
            """, (
                finding_key, decision.finding_id, run_id, next_seq,
                current_status, target_status,
                DECISION_TO_EVENT[decision.decision],
                decision.reviewer,
                decision.reviewer_notes or decision.false_positive_reason,
            ))

            cur.execute("""
                UPDATE capstone.fact_findings
                SET status = %s, owner = COALESCE(owner, NULL)
                WHERE finding_key = %s
            """, (target_status, finding_key))

            applied += 1

    return {"decisions_applied": applied}


# ---------------------------------------------------------------- entry point


def import_file(path: Path = DEFAULT_CSV, run_id: str | None = None,
                conn: psycopg.Connection | None = None) -> dict:
    owns_connection = conn is None
    connection = conn or connect()

    try:
        if run_id is None:
            with connection.cursor() as cur:
                cur.execute("""
                    SELECT run_id FROM capstone.fact_pipeline_runs
                    WHERE status = 'SUCCESS' ORDER BY started_at DESC LIMIT 1
                """)
                row = cur.fetchone()
                if row is None:
                    raise ReviewFileRejected(["no successful run to import against"])
                run_id = row[0]

        decisions, parse_problems = read_decisions(path)
        validation_problems = validate(connection, run_id, decisions)

        problems = parse_problems + validation_problems
        if problems:
            connection.rollback()
            raise ReviewFileRejected(problems)

        if not decisions:
            logger.info("no decisions to import from %s", path)
            return {"run_id": run_id, "decisions_applied": 0,
                    "note": "file contained no completed decisions"}

        result = apply_decisions(connection, run_id, decisions, path.name)
        if owns_connection:
            connection.commit()

        logger.info("imported %d decision(s) for %s", result["decisions_applied"], run_id)
        return {"run_id": run_id, **result}

    except Exception:
        connection.rollback()
        raise
    finally:
        if owns_connection:
            connection.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV

    try:
        print(json.dumps(import_file(path), indent=2, default=str))
    except ReviewFileRejected as rejection:
        print("REVIEW FILE REJECTED -- nothing was imported\n")
        for problem in rejection.problems:
            print(f"  - {problem}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
