"""Load source CSVs into the staging landing zone.

Staging is replace-on-load, not append: a run represents the current state of the
source files, so accumulating loads across runs would double every amount. The
load_audit table keeps the history of what was loaded when.

Rows are loaded verbatim, defects included. Rejecting a bad row here would hide
the very defect the data-quality controls exist to quantify.

Run: python src/load_source.py
"""

from __future__ import annotations

import csv
from pathlib import Path

import psycopg

from db import connect

BASE_DIR = Path(__file__).resolve().parent.parent
RAW = BASE_DIR / "data" / "raw"

# Load order is irrelevant to correctness because staging has no foreign keys,
# but masters first keeps the audit log readable.
TABLES: dict[str, tuple[str, ...]] = {
    "customers": (
        "customer_id", "customer_name", "customer_segment", "region",
        "signup_date", "risk_level",
    ),
    "product_prices": (
        "product_id", "product_name", "category", "approved_price",
        "minimum_price", "effective_from", "effective_to",
    ),
    "fee_rules": (
        "payment_method", "standard_fee_rate", "maximum_fee_rate", "effective_from",
    ),
    "daily_kpi_targets": (
        "metric_date", "target_revenue", "target_payment_success_rate",
        "target_order_count", "max_refund_rate_pct",
    ),
    "orders": (
        "order_id", "customer_id", "order_date", "order_status", "gross_amount",
        "discount_amount", "tax_amount", "final_amount", "discount_code",
        "sales_channel", "region",
    ),
    "order_items": (
        "order_item_id", "order_id", "product_id", "quantity", "unit_price",
        "approved_price", "item_discount",
    ),
    "payments": (
        "payment_id", "order_id", "payment_date", "payment_status",
        "payment_amount", "payment_method", "gateway_fee", "transaction_reference",
    ),
    "refunds": (
        "refund_id", "order_id", "payment_id", "refund_date", "refund_amount",
        "refund_reason", "refund_status", "approved_by",
    ),
    "shipments": (
        "shipment_id", "order_id", "shipment_date", "delivery_date",
        "shipment_status", "courier",
    ),
    "bank_transactions": (
        "bank_txn_id", "value_date", "amount", "bank_reference", "txn_type",
        "narration",
    ),
    "ledger_transactions": (
        "ledger_txn_id", "posting_date", "amount", "ledger_reference",
        "account_code", "txn_type", "source_entity_id",
    ),
}


class MissingSourceFile(FileNotFoundError):
    """A required source file is absent. Halts financial processing by design."""


def check_files() -> None:
    """Fail before touching the database if any required file is missing.

    A partial load is worse than no load: KPIs computed from a half-present
    dataset look plausible and are wrong. This is the check the deliberate
    failure demonstration exercises.
    """
    missing = [name for name in TABLES if not (RAW / f"{name}.csv").exists()]
    if missing:
        raise MissingSourceFile(
            "required source files absent: "
            + ", ".join(f"{name}.csv" for name in sorted(missing))
            + f" (looked in {RAW})"
        )


def _load_table(conn: psycopg.Connection, run_id: str, table: str,
                columns: tuple[str, ...]) -> int:
    path = RAW / f"{table}.csv"

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = set(reader.fieldnames or ())
        expected = set(columns)
        if not expected.issubset(header):
            raise MissingSourceFile(
                f"{path.name} is missing expected columns: "
                f"{sorted(expected - header)} (found {sorted(header)})"
            )
        rows = [
            tuple(None if row[column] in ("", None) else row[column] for column in columns)
            for row in reader
        ]

    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE staging.{table}")
        if rows:
            placeholders = ", ".join(["%s"] * len(columns))
            cur.executemany(
                f"INSERT INTO staging.{table} ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                rows,
            )
        cur.execute(
            """INSERT INTO staging.load_audit (run_id, table_name, source_file, row_count)
               VALUES (%s, %s, %s, %s)""",
            (run_id, table, path.name, len(rows)),
        )
    return len(rows)


def load_all(run_id: str, conn: psycopg.Connection | None = None) -> dict[str, int]:
    check_files()

    owns_connection = conn is None
    connection = conn or connect()
    try:
        counts = {
            table: _load_table(connection, run_id, table, columns)
            for table, columns in TABLES.items()
        }
        if owns_connection:
            connection.commit()
        return counts
    finally:
        if owns_connection:
            connection.close()


def main() -> None:
    from contract import make_run_id

    run_id = make_run_id(1)
    counts = load_all(run_id)
    print(f"loaded into staging under {run_id}")
    for table, count in counts.items():
        print(f"  {table:<22} {count:>5} rows")
    print(f"  {'TOTAL':<22} {sum(counts.values()):>5} rows")


if __name__ == "__main__":
    main()
