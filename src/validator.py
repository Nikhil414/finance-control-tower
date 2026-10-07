"""
Validation Engine for Revenue Leakage and Exception Triage Agent.
Verifies data integrity, schemas, foreign keys, non-negative monetary constraints,
and produces an auditable data_quality_report.csv.
"""

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

logger = logging.getLogger("RevenueLeakage.Validator")


class DataValidator:
    def __init__(self, data_dir: Path, output_dir: Path):
        self.data_dir = data_dir
        self.output_dir = output_dir
        self.issues = []

    def log_issue(self, severity: str, table_name: str, record_id: str, field: str, issue_description: str):
        """Record a data quality issue."""
        self.issues.append({
            "severity": severity,  # CRITICAL, WARNING, INFO
            "table_name": table_name,
            "record_id": record_id,
            "field": field,
            "issue_description": issue_description,
            "detected_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        })

    def validate_all(self):
        """Run complete validation suite across all 8 datasets."""
        self.issues.clear()
        
        # 1. Load DataFrames
        try:
            customers = pd.read_csv(self.data_dir / "customers.csv")
            orders = pd.read_csv(self.data_dir / "orders.csv")
            order_items = pd.read_csv(self.data_dir / "order_items.csv")
            payments = pd.read_csv(self.data_dir / "payments.csv")
            refunds = pd.read_csv(self.data_dir / "refunds.csv")
            shipments = pd.read_csv(self.data_dir / "shipments.csv")
            products = pd.read_csv(self.data_dir / "product_prices.csv")
            fee_rules = pd.read_csv(self.data_dir / "fee_rules.csv")
        except Exception as e:
            self.log_issue("CRITICAL", "SYSTEM", "ALL", "FILE_READ", f"Failed to load required datasets: {e}")
            self.export_report()
            return False, {}

        # 2. Check Primary Key Uniqueness & Nulls
        self._check_pk(customers, "customers", "customer_id")
        self._check_pk(orders, "orders", "order_id")
        self._check_pk(payments, "payments", "payment_id")
        self._check_pk(refunds, "refunds", "refund_id")
        self._check_pk(shipments, "shipments", "shipment_id")

        # 3. Check Foreign Key Integrity
        valid_customers = set(customers["customer_id"].dropna())
        for _, row in orders.iterrows():
            if row["customer_id"] not in valid_customers:
                self.log_issue("CRITICAL", "orders", str(row["order_id"]), "customer_id",
                               f"Orphan order: customer_id '{row['customer_id']}' not in customers master")

        valid_orders = set(orders["order_id"].dropna())
        for _, row in payments.iterrows():
            if row["order_id"] not in valid_orders:
                self.log_issue("WARNING", "payments", str(row["payment_id"]), "order_id",
                               f"Orphan payment: order_id '{row['order_id']}' not found in orders")

        for _, row in refunds.iterrows():
            if row["order_id"] not in valid_orders:
                self.log_issue("CRITICAL", "refunds", str(row["refund_id"]), "order_id",
                               f"Orphan refund: order_id '{row['order_id']}' not found in orders")

        # 4. Monetary Sanity Checks (Non-negative values)
        for _, row in orders.iterrows():
            if row["gross_amount"] < 0 or row["final_amount"] < 0:
                self.log_issue("CRITICAL", "orders", str(row["order_id"]), "amount",
                               f"Negative amount detected: gross={row['gross_amount']}, final={row['final_amount']}")

        # 5. Temporal Validity (Products)
        for _, row in products.iterrows():
            try:
                start = datetime.strptime(str(row["effective_from"]), "%Y-%m-%d")
                end = datetime.strptime(str(row["effective_to"]), "%Y-%m-%d")
                if end < start:
                    self.log_issue("CRITICAL", "product_prices", str(row["product_id"]), "dates",
                                   f"Price end date {row['effective_to']} is before start date {row['effective_from']}")
            except Exception as e:
                self.log_issue("WARNING", "product_prices", str(row["product_id"]), "dates", f"Date parse error: {e}")

        # Export report
        self.export_report()

        # Clean quarantined records for downstream execution
        clean_orders = orders[
            orders["customer_id"].isin(valid_customers) &
            (orders["gross_amount"] >= 0) &
            (orders["final_amount"] >= 0)
        ].copy()
        valid_clean_orders = set(clean_orders["order_id"])

        # Orphan refunds were already flagged CRITICAL above; drop them here too,
        # otherwise they still poison downstream duplicate/excess-refund calculations.
        clean_refunds = refunds[refunds["order_id"].isin(valid_clean_orders)].copy()

        datasets = {
            "customers": customers,
            "orders": clean_orders,
            "order_items": order_items,
            "payments": payments,
            "refunds": clean_refunds,
            "shipments": shipments,
            "products": products,
            "fee_rules": fee_rules,
            "raw_orders_count": len(orders),
            "clean_orders_count": len(clean_orders)
        }

        return True, datasets

    def export_report(self):
        """Write data_quality_report.csv."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.output_dir / "data_quality_report.csv"
        
        if not self.issues:
            # Create empty report with headers
            with open(report_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["severity", "table_name", "record_id", "field", "issue_description", "detected_at"])
            return

        fieldnames = ["severity", "table_name", "record_id", "field", "issue_description", "detected_at"]
        with open(report_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self.issues)

    def _check_pk(self, df: pd.DataFrame, table_name: str, pk_col: str):
        if pk_col not in df.columns:
            self.log_issue("CRITICAL", table_name, "SCHEMA", pk_col, f"Primary key column '{pk_col}' missing")
            return
        
        nulls = df[df[pk_col].isna()]
        if len(nulls) > 0:
            self.log_issue("CRITICAL", table_name, "NULL_ID", pk_col, f"Found {len(nulls)} rows with NULL primary keys")

        dups = df[df.duplicated(subset=[pk_col], keep=False)]
        if len(dups) > 0:
            self.log_issue("CRITICAL", table_name, "DUP_ID", pk_col, f"Found {len(dups)} rows with duplicate primary keys")
