"""Pipeline orchestrator. One command, one run_id, one deterministic workflow.

    python src/run_pipeline.py

Stage order is not arbitrary -- each stage depends on the last:

    1. schema      apply sql/*.sql, sync the rule registry
    2. validate    check every required source file exists
    3. load        CSVs into the staging landing zone
    4. detect      SQL controls, reconciliation, Python leakage rules
    5. persist     unified findings into the case model (idempotent)
    6. export      structured JSON for the AI investigator
    7. investigate AI commentary, or a deterministic brief if unavailable
    8. workbook    Excel review queue for human reviewers

Two failure policies, and the difference is the whole design:

  * A missing source file or a critical validation failure STOPS the run before
    any financial figure is computed. Half-loaded data produces KPIs that look
    plausible and are wrong, which is worse than no KPIs at all.

  * AI unavailability NEVER stops the run. It is recorded on the run row and the
    brief falls back to a deterministic template. A control system that stops
    controlling because a language model is rate-limited is not a control system.

Flags:
    --run-id ID       reuse a specific run_id (demonstrates idempotency)
    --skip-schema     skip DDL application when the schema is known current
    --no-ai           skip the AI step (demonstrates the outage path)
    --no-workbook     skip Excel generation
    --fail-fast       raise instead of recording failure and exiting non-zero
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")  # DATABASE_URL and the AI key, if configured

sys.path.insert(0, str(BASE_DIR / "src"))

import ai_investigator  # noqa: E402
import build_review_workbook  # noqa: E402
import db  # noqa: E402
import load_findings  # noqa: E402
import load_source  # noqa: E402
from contract import CONTRACT, make_run_id  # noqa: E402
from leakage_engine import LeakageEngine, ValidationFailed  # noqa: E402

logger = logging.getLogger("capstone.pipeline")

OUTPUT_DIR = BASE_DIR / "outputs"


class PipelineFailed(RuntimeError):
    """A stage that must not be bypassed has failed. No financial output produced."""


def next_run_id(conn) -> str:
    """Allocate today's next sequence number.

    Derived from what the database already holds rather than from a counter in
    memory, so a second run on the same day becomes -002 even across separate
    invocations.
    """
    today = datetime.now(timezone.utc).date()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM capstone.fact_pipeline_runs WHERE run_date = %s",
            (today,),
        )
        used = cur.fetchone()[0]
    return make_run_id(used + 1, today)


class Stage:
    """Records timing and outcome per stage so the run summary is honest about
    where time went and what actually ran."""

    def __init__(self, results: list[dict], name: str, critical: bool = True):
        self.results = results
        self.name = name
        self.critical = critical
        self.started = 0.0

    def __enter__(self):
        self.started = time.perf_counter()
        logger.info("--> %s", self.name)
        return self

    def __exit__(self, exc_type, exc, traceback):
        elapsed = round(time.perf_counter() - self.started, 2)
        if exc is None:
            self.results.append({"stage": self.name, "status": "OK", "seconds": elapsed})
            return False

        self.results.append({
            "stage": self.name,
            "status": "FAILED" if self.critical else "SKIPPED",
            "seconds": elapsed,
            "error": f"{type(exc).__name__}: {exc}",
        })

        if self.critical:
            logger.error("%s failed: %s", self.name, exc)
            return False  # propagate

        # Non-critical stage: log and continue. This is the AI path.
        logger.warning("%s unavailable, continuing: %s", self.name, exc)
        return True


def run(run_id: str | None = None, apply_schema: bool = True,
        use_ai: bool = True, build_workbook: bool = True) -> dict:
    started = time.perf_counter()
    stages: list[dict] = []
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if apply_schema:
        with Stage(stages, "apply schema"):
            db.apply_all()

    with db.connect() as conn:
        if run_id is None:
            run_id = next_run_id(conn)
        logger.info("run_id %s (contract v%s)", run_id, CONTRACT["version"])

        # ---- gate: source files must all be present before anything else.
        # Recording the run first means a validation failure is still visible in
        # fact_pipeline_runs rather than vanishing without trace.
        load_findings.open_run(conn, run_id)
        conn.commit()

        try:
            with Stage(stages, "validate source files"):
                load_source.check_files()

            with Stage(stages, "load staging"):
                counts = load_source.load_all(run_id, conn=conn)
                conn.commit()
                logger.info("staged %d rows across %d tables",
                            sum(counts.values()), len(counts))

            with Stage(stages, "detect leakage"):
                engine = LeakageEngine(run_id=run_id)
                leakage_summary = engine.run()
                logger.info("leakage: %d findings, %s exposure",
                            leakage_summary["findings_count"],
                            f"{leakage_summary['deduplicated_exposure']:,.2f}")

            with Stage(stages, "persist findings"):
                load_result = load_findings.load_all(run_id, conn=conn)
                conn.commit()
                logger.info("findings: %d created, %d already present",
                            load_result["findings_created"],
                            load_result["findings_skipped"])

        except (load_source.MissingSourceFile, ValidationFailed) as error:
            # The deliberate stop. No KPI, no exposure figure, nothing that
            # could be mistaken for a real number.
            load_findings.close_run(
                conn, run_id, "VALIDATION_FAILED", error_message=str(error)
            )
            conn.commit()
            raise PipelineFailed(
                f"source validation failed, financial processing halted: {error}"
            ) from error

        except Exception as error:
            load_findings.close_run(conn, run_id, "FAILED", error_message=str(error))
            conn.commit()
            raise

        # ---- AI: explicitly non-critical.
        ai_status, ai_reason = "NOT_ATTEMPTED", None
        if use_ai:
            with Stage(stages, "ai investigator", critical=False):
                result = ai_investigator.AIInvestigator(OUTPUT_DIR).investigate()
                ai_status, ai_reason = result["ai_status"], result.get("reason")
        else:
            ai_status, ai_reason = "NOT_ATTEMPTED", "skipped via --no-ai"
            stages.append({"stage": "ai investigator", "status": "SKIPPED",
                           "seconds": 0.0})

        load_findings.set_ai_status(conn, run_id, ai_status, ai_reason)
        conn.commit()

        if build_workbook:
            with Stage(stages, "build review workbook", critical=False):
                build_review_workbook.build(run_id=run_id)

        summary = _summarise(conn, run_id, load_result, stages,
                             ai_status, ai_reason, started)

    (OUTPUT_DIR / "pipeline_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    return summary


def _summarise(conn, run_id: str, load_result: dict, stages: list[dict],
               ai_status: str, ai_reason: str | None, started: float) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM capstone.rpt_control_totals")
        columns = [description.name for description in cur.description]
        totals = dict(zip(columns, cur.fetchone()))

        cur.execute("""
            SELECT source_module, COUNT(*), COALESCE(SUM(risk_amount), 0)
            FROM capstone.fact_findings WHERE last_seen_run_id = %s
            GROUP BY source_module ORDER BY source_module
        """, (run_id,))
        by_module = {
            row[0]: {"findings": row[1], "gross_exposure": float(row[2])}
            for row in cur.fetchall()
        }

        cur.execute("""
            SELECT severity, COUNT(*) FROM capstone.fact_findings
            WHERE last_seen_run_id = %s GROUP BY severity
        """, (run_id,))
        by_severity = {row[0]: row[1] for row in cur.fetchall()}

    return {
        "run_id": run_id,
        "contract_version": CONTRACT["version"],
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_seconds": round(time.perf_counter() - started, 2),
        "stages": stages,
        "findings_created": load_result["findings_created"],
        "findings_skipped": load_result["findings_skipped"],
        "by_module": by_module,
        "by_severity": by_severity,
        "control_totals": {key: float(value) if value is not None else 0.0
                           for key, value in totals.items()},
        "ai_status": ai_status,
        "ai_reason": ai_reason,
    }


def _print_report(summary: dict) -> None:
    totals = summary["control_totals"]
    money = lambda amount: f"{amount:>16,.2f}"  # noqa: E731

    print()
    print("=" * 66)
    print(f"  RUN {summary['run_id']}    contract v{summary['contract_version']}"
          f"    {summary['duration_seconds']}s")
    print("=" * 66)

    print("\n  stages")
    for stage in summary["stages"]:
        marker = {"OK": "ok", "SKIPPED": "--", "FAILED": "XX"}[stage["status"]]
        print(f"    [{marker}] {stage['stage']:<28} {stage['seconds']:>6.2f}s")
        if stage.get("error"):
            print(f"         {stage['error']}")

    print("\n  findings by module")
    for module, data in summary["by_module"].items():
        print(f"    {module:<16} {data['findings']:>4} cases   "
              f"{money(data['gross_exposure'])}")

    print("\n  severity")
    severities = summary["by_severity"]
    print("    " + "   ".join(
        f"{level}: {severities.get(level, 0)}"
        for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
    ))

    print("\n  exposure")
    print(f"    gross (double-counts shared orders) {money(totals['gross_exposure'])}")
    print(f"    deduplicated (defensible)           {money(totals['deduplicated_exposure'])}")
    print(f"    confirmed loss (human-verified)     {money(totals['confirmed_loss'])}")
    print(f"    recovered                           {money(totals['recovered_amount'])}")

    print("\n  controls")
    print(f"    net realized revenue                {money(totals['net_revenue'])}")
    print(f"    reconciliation match rate           {totals['match_rate_pct']:>15.2f}%")
    print(f"    data quality score                  {totals['data_quality_score_pct']:>15.2f}%")
    print(f"    open cases                          {int(totals['open_cases']):>16}")
    print(f"    critical cases                      {int(totals['critical_cases']):>16}")

    print(f"\n  findings created {summary['findings_created']}, "
          f"already present {summary['findings_skipped']}")
    if summary["findings_created"] == 0 and summary["findings_skipped"] > 0:
        print("    (rerun of an existing run_id -- idempotency held, nothing duplicated)")

    print(f"\n  ai: {summary['ai_status']}"
          + (f" ({summary['ai_reason']})" if summary["ai_reason"] else ""))
    print()
    print("  outputs/executive_brief.md")
    print("  excel/Finance_Review_Workbook.xlsx")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Finance Analytics Agent OS pipeline")
    parser.add_argument("--run-id", help="reuse a run_id (demonstrates idempotency)")
    parser.add_argument("--skip-schema", action="store_true", help="skip DDL application")
    parser.add_argument("--no-ai", action="store_true", help="skip the AI investigator")
    parser.add_argument("--no-workbook", action="store_true", help="skip Excel generation")
    parser.add_argument("--fail-fast", action="store_true",
                        help="raise on failure instead of exiting non-zero")
    parser.add_argument("--quiet", action="store_true", help="log warnings only")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    try:
        summary = run(
            run_id=args.run_id,
            apply_schema=not args.skip_schema,
            use_ai=not args.no_ai,
            build_workbook=not args.no_workbook,
        )
    except PipelineFailed as failure:
        if args.fail_fast:
            raise
        print(f"\nPIPELINE HALTED\n\n  {failure}\n")
        print("  No financial figures were produced. The run is recorded as")
        print("  VALIDATION_FAILED in capstone.fact_pipeline_runs.\n")
        raise SystemExit(2)

    _print_report(summary)


if __name__ == "__main__":
    main()
