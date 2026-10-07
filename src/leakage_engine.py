"""Revenue-leakage detection: six deterministic rules over the order-to-cash flow.

Ported from the standalone Revenue Leakage and Exception Triage Agent. The six
rule calculations are preserved exactly -- they were correct and are the reason
that project exists. What changed is everything around them:

  * findings are emitted in the shared contract's shape and validated against it
  * run_id is supplied by the orchestrator instead of self-generated, so SQL,
    leakage and reconciliation findings all belong to the same run
  * severity comes from src/contract.py rather than a second copy of the ladder
  * aging is measured from a deterministic as-of date, not wall-clock now
  * the AI call is gone; the orchestrator owns that step

One genuine bug was fixed on the way -- see _detect_duplicate_refunds.

Run: python src/leakage_engine.py
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from contract import (
    due_date_for,
    make_finding_id,
    priority_score,
    resolve_severity,
    utc_now_iso,
    validate_finding,
)
from validator import DataValidator

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "policies.json"
DATA_DIR = BASE_DIR / "data" / "raw"
OUTPUT_DIR = BASE_DIR / "outputs"

logger = logging.getLogger("capstone.leakage")

SOURCE_MODULE = "LEAKAGE"


class ValidationFailed(RuntimeError):
    """Source data failed critical validation. Financial processing must stop."""


class LeakageEngine:
    def __init__(self, run_id: str, config_path: Path = CONFIG_PATH,
                 data_dir: Path = DATA_DIR, output_dir: Path = OUTPUT_DIR):
        self.run_id = run_id
        self.config_path = config_path
        self.data_dir = data_dir
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        with open(config_path, "r", encoding="utf-8") as handle:
            self.policies = json.load(handle)

        self.findings: list[dict] = []
        self.as_of_date: pd.Timestamp | None = None
        self._counter = 0

    # ------------------------------------------------------------ helpers

    def _next_finding_id(self) -> str:
        self._counter += 1
        return make_finding_id(SOURCE_MODULE, self._counter)

    def _days_open(self, business_date) -> int:
        """Age a finding against the dataset's as-of date, not wall-clock today.

        Using now() would make priority scores drift every day the pipeline is
        not run, so the same input would stop producing the same output -- and
        reproducibility is the property that lets anyone re-derive a reported
        number months later. The as-of date is the latest activity in the data,
        which is what a real reporting run would anchor to.
        """
        if business_date is None or pd.isna(business_date):
            return 0
        return max(0, (self.as_of_date - pd.Timestamp(business_date)).days)

    def _emit(self, rule_code: str, entity_type: str, entity_id: str,
              business_date, risk_amount: float, evidence: dict,
              recommended_action: str, order_id: str | None = None,
              customer_id: str | None = None,
              customer_segment: str = "Retail") -> dict:
        """Assemble, score and contract-validate one finding.

        Every rule routes through here, so no rule can skip validation or invent
        its own severity -- which is how two detectors end up disagreeing about
        what HIGH means.
        """
        detected_at = datetime.now(timezone.utc)
        risk_amount = round(float(risk_amount), 2)

        severity = resolve_severity(
            risk_amount, rule_code, {"customer_segment": customer_segment}
        )
        days_open = self._days_open(business_date)

        finding = {
            "finding_id": self._next_finding_id(),
            "run_id": self.run_id,
            "source_module": SOURCE_MODULE,
            "rule_code": rule_code,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "order_id": None if order_id is None else str(order_id),
            "customer_id": None if customer_id is None else str(customer_id),
            "detected_at": utc_now_iso(),
            "business_date": pd.Timestamp(business_date).date().isoformat(),
            "severity": severity,
            "risk_amount": risk_amount,
            "priority_score": priority_score(
                severity, days_open, customer_segment, risk_amount
            ),
            "days_open": days_open,
            "evidence_json": evidence,
            "recommended_action": recommended_action,
            "status": "OPEN",
            "due_date": due_date_for(severity, detected_at),
            "customer_segment": customer_segment,
        }

        validated = validate_finding(finding)
        self.findings.append(validated)
        return validated

    # ------------------------------------------------------------ pipeline

    def run(self) -> dict:
        logger.info("leakage detection starting for %s", self.run_id)

        validator = DataValidator(self.data_dir, self.output_dir)
        is_valid, datasets = validator.validate_all()
        if not is_valid:
            raise ValidationFailed(
                "critical validation failure -- see outputs/data_quality_report.csv"
            )

        customers = datasets["customers"]
        orders = datasets["orders"]
        order_items = datasets["order_items"]
        payments = datasets["payments"]
        refunds = datasets["refunds"]
        shipments = datasets["shipments"]
        products = datasets["products"]
        fee_rules = datasets["fee_rules"]

        orders["order_date_dt"] = pd.to_datetime(orders["order_date"])
        payments["payment_date_dt"] = pd.to_datetime(payments["payment_date"])
        refunds["refund_date_dt"] = pd.to_datetime(refunds["refund_date"])
        shipments["delivery_date_dt"] = pd.to_datetime(
            shipments["delivery_date"], errors="coerce"
        )

        self.as_of_date = max(
            orders["order_date_dt"].max(),
            payments["payment_date_dt"].max(),
            refunds["refund_date_dt"].max(),
        ).normalize()
        logger.info("as-of date for aging: %s", self.as_of_date.date())

        # Two distinct lookups, and conflating them was the original bug.
        # segment_by_customer is keyed by customer_id; customer_by_order maps an
        # order to its customer. Rules that start from a refund or a payment have
        # only an order_id in hand and must go through the second map first.
        segment_by_customer = (
            customers.set_index("customer_id")["customer_segment"].to_dict()
        )
        customer_by_order = orders.set_index("order_id")["customer_id"].to_dict()

        context = {
            "segment_by_customer": segment_by_customer,
            "customer_by_order": customer_by_order,
        }

        rules = self.policies["rules"]
        if rules["duplicate_refunds"]["enabled"]:
            self._detect_duplicate_refunds(refunds, context)
        if rules["excess_refunds"]["enabled"]:
            self._detect_excess_refunds(payments, refunds, context)
        if rules["excessive_discounts"]["enabled"]:
            self._detect_excessive_discounts(orders, context)
        if rules["delivered_unpaid"]["enabled"]:
            self._detect_delivered_unpaid(orders, payments, shipments, context)
        if rules["pricing_errors"]["enabled"]:
            self._detect_pricing_errors(orders, order_items, products, context)
        if rules["unusual_gateway_fees"]["enabled"]:
            self._detect_unusual_gateway_fees(orders, payments, fee_rules, context)

        summary = self._summarise(datasets)
        self._export(summary)
        logger.info("leakage detection complete: %d findings", len(self.findings))
        return summary

    def _segment_for_order(self, order_id, context: dict) -> tuple[str | None, str]:
        customer_id = context["customer_by_order"].get(order_id)
        segment = context["segment_by_customer"].get(customer_id, "Retail")
        return customer_id, segment

    # ------------------------------------------------------------ rule 1

    def _detect_duplicate_refunds(self, refunds: pd.DataFrame, context: dict) -> None:
        """Same amount refunded twice against one payment inside the lookback window.

        BUG FIX: the original looked up `cust_map.get(order_id)` against a map
        keyed by customer_id, so every duplicate-refund finding carried
        customer_id "UNKNOWN". That silently broke segment-based severity
        escalation and made the findings unattributable to an account. Refunds
        carry no customer_id, so the order must be resolved to its customer
        first.
        """
        processed = refunds[refunds["refund_status"] == "Processed"].copy()
        if processed.empty:
            return

        lookback_days = self.policies["rules"]["duplicate_refunds"]["lookback_window_days"]
        processed.sort_values(
            by=["order_id", "payment_id", "refund_amount", "refund_date_dt"], inplace=True
        )

        for (order_id, payment_id, amount), group in processed.groupby(
            ["order_id", "payment_id", "refund_amount"]
        ):
            if len(group) < 2:
                continue

            records = group.to_dict(orient="records")
            first = records[0]
            for duplicate in records[1:]:
                gap_days = (
                    duplicate["refund_date_dt"] - first["refund_date_dt"]
                ).total_seconds() / 86400.0
                if gap_days > lookback_days:
                    continue

                customer_id, segment = self._segment_for_order(order_id, context)
                self._emit(
                    rule_code="DUPLICATE_REFUND",
                    entity_type="REFUND",
                    entity_id=duplicate["refund_id"],
                    business_date=duplicate["refund_date_dt"],
                    risk_amount=float(amount),
                    order_id=order_id,
                    customer_id=customer_id,
                    customer_segment=segment,
                    evidence={
                        "original_refund_id": first["refund_id"],
                        "duplicate_refund_id": duplicate["refund_id"],
                        "payment_id": payment_id,
                        "refund_amount": float(amount),
                        "original_refund_date": str(first["refund_date"]),
                        "duplicate_refund_date": str(duplicate["refund_date"]),
                        "days_between": round(gap_days, 2),
                    },
                    recommended_action=(
                        "Review payment gateway settlement and initiate duplicate reversal"
                    ),
                )

    # ------------------------------------------------------------ rule 2

    def _detect_excess_refunds(self, payments: pd.DataFrame, refunds: pd.DataFrame,
                               context: dict) -> None:
        """More refunded than was ever collected on the order."""
        collected = (
            payments[payments["payment_status"] == "Successful"]
            .groupby("order_id")["payment_amount"].sum()
        )
        returned = (
            refunds[refunds["refund_status"] == "Processed"]
            .groupby("order_id")["refund_amount"].sum()
        )
        refund_dates = (
            refunds[refunds["refund_status"] == "Processed"]
            .groupby("order_id")["refund_date_dt"].max()
        )

        combined = pd.DataFrame(
            {"total_paid": collected, "total_refunded": returned}
        ).fillna(0.0)
        excess = combined[combined["total_refunded"] > combined["total_paid"]]

        for order_id, row in excess.iterrows():
            total_paid = float(row["total_paid"])
            total_refunded = float(row["total_refunded"])
            customer_id, segment = self._segment_for_order(order_id, context)

            self._emit(
                rule_code="REFUND_EXCEEDS_PAYMENT",
                entity_type="ORDER",
                entity_id=order_id,
                business_date=refund_dates.get(order_id, self.as_of_date),
                risk_amount=total_refunded - total_paid,
                order_id=order_id,
                customer_id=customer_id,
                customer_segment=segment,
                evidence={
                    "total_collected_payment": round(total_paid, 2),
                    "total_processed_refunds": round(total_refunded, 2),
                    "excess_refund_amount": round(total_refunded - total_paid, 2),
                },
                recommended_action=(
                    "Audit refund ledger approvals and initiate merchant debit adjustment"
                ),
            )

    # ------------------------------------------------------------ rule 3

    def _detect_excessive_discounts(self, orders: pd.DataFrame, context: dict) -> None:
        """Discount above the policy ceiling without an authorised promo code."""
        settings = self.policies["rules"]["excessive_discounts"]
        ceiling = settings["max_permitted_discount_pct"]
        authorised = set(settings["authorized_promo_codes"])

        for _, order in orders.iterrows():
            gross = float(order["gross_amount"])
            discount = float(order["discount_amount"])
            if gross <= 0:
                continue

            discount_pct = discount / gross
            if discount_pct <= ceiling:
                continue

            promo_code = str(order.get("discount_code", "NONE"))
            if promo_code in authorised:
                continue

            allowed = round(gross * ceiling, 2)
            excess = round(discount - allowed, 2)
            segment = context["segment_by_customer"].get(order["customer_id"], "Retail")

            self._emit(
                rule_code="EXCESSIVE_DISCOUNT",
                entity_type="ORDER",
                entity_id=order["order_id"],
                business_date=order["order_date_dt"],
                risk_amount=excess,
                order_id=order["order_id"],
                customer_id=order["customer_id"],
                customer_segment=segment,
                evidence={
                    "gross_amount": gross,
                    "actual_discount_amount": discount,
                    "actual_discount_pct": round(discount_pct * 100, 2),
                    "max_permitted_discount_pct": round(ceiling * 100, 2),
                    "discount_code": promo_code,
                    "excess_discount_amount": excess,
                },
                recommended_action=(
                    "Review sales authorization and verify marketing promo code eligibility"
                ),
            )

    # ------------------------------------------------------------ rule 4

    def _detect_delivered_unpaid(self, orders: pd.DataFrame, payments: pd.DataFrame,
                                 shipments: pd.DataFrame, context: dict) -> None:
        """Goods delivered but the order is not fully paid."""
        delivered = shipments[shipments["shipment_status"] == "Delivered"]
        collected = (
            payments[payments["payment_status"] == "Successful"]
            .groupby("order_id")["payment_amount"].sum().to_dict()
        )
        orders_by_id = orders.set_index("order_id")

        for _, shipment in delivered.iterrows():
            order_id = shipment["order_id"]
            if order_id not in orders_by_id.index:
                continue

            order = orders_by_id.loc[order_id]
            final_amount = float(order["final_amount"])
            paid = float(collected.get(order_id, 0.0))
            if paid >= final_amount:
                continue

            segment = context["segment_by_customer"].get(order["customer_id"], "Retail")
            self._emit(
                rule_code="DELIVERED_UNPAID",
                entity_type="ORDER",
                entity_id=order_id,
                business_date=shipment["delivery_date_dt"]
                if pd.notna(shipment["delivery_date_dt"]) else order["order_date_dt"],
                risk_amount=final_amount - paid,
                order_id=order_id,
                customer_id=order["customer_id"],
                customer_segment=segment,
                evidence={
                    "order_final_amount": final_amount,
                    "successful_payment_amount": paid,
                    "shortfall_amount": round(final_amount - paid, 2),
                    "delivery_date": str(shipment["delivery_date"]),
                    "courier": str(shipment.get("courier", "Unknown")),
                },
                recommended_action=(
                    "Verify bank gateway settlement status and contact accounts receivable"
                ),
            )

    # ------------------------------------------------------------ rule 5

    def _detect_pricing_errors(self, orders: pd.DataFrame, order_items: pd.DataFrame,
                               products: pd.DataFrame, context: dict) -> None:
        """Item sold below the minimum price approved for that date.

        The price list is temporal, so the comparison must use the window that
        contains the order date. Comparing against today's price would flag
        every historical order sold under an older, lower price.
        """
        items = order_items.merge(
            orders[["order_id", "order_date_dt", "customer_id"]], on="order_id"
        )
        catalogue = products.copy()
        catalogue["effective_from_dt"] = pd.to_datetime(catalogue["effective_from"])
        catalogue["effective_to_dt"] = pd.to_datetime(catalogue["effective_to"])

        for _, item in items.iterrows():
            order_date = item["order_date_dt"]
            window = catalogue[
                (catalogue["product_id"] == item["product_id"])
                & (catalogue["effective_from_dt"] <= order_date)
                & (catalogue["effective_to_dt"] >= order_date)
            ]
            if window.empty:
                continue

            price_row = window.iloc[0]
            minimum_price = float(price_row["minimum_price"])
            charged = float(item["unit_price"])
            if charged >= minimum_price:
                continue

            quantity = int(item["quantity"])
            segment = context["segment_by_customer"].get(item["customer_id"], "Retail")

            self._emit(
                rule_code="PRICING_ERROR",
                entity_type="ORDER_ITEM",
                entity_id=item["order_item_id"],
                business_date=order_date,
                risk_amount=(minimum_price - charged) * quantity,
                order_id=item["order_id"],
                customer_id=item["customer_id"],
                customer_segment=segment,
                evidence={
                    "product_id": item["product_id"],
                    "quantity": quantity,
                    "actual_unit_price": charged,
                    "approved_price": float(price_row["approved_price"]),
                    "minimum_permitted_price": minimum_price,
                    "pricing_shortfall_per_unit": round(minimum_price - charged, 2),
                },
                recommended_action=(
                    "Audit product pricing catalog change log and invoice adjustments"
                ),
            )

    # ------------------------------------------------------------ rule 6

    def _detect_unusual_gateway_fees(self, orders: pd.DataFrame, payments: pd.DataFrame,
                                     fee_rules: pd.DataFrame, context: dict) -> None:
        """Gateway charged above its contracted maximum rate."""
        rules_by_method = fee_rules.set_index("payment_method").to_dict(orient="index")
        buffer_pct = self.policies["rules"]["unusual_gateway_fees"][
            "fee_rate_threshold_buffer_pct"
        ]
        successful = payments[payments["payment_status"] == "Successful"]

        for _, payment in successful.iterrows():
            method = payment["payment_method"]
            amount = float(payment["payment_amount"])
            actual_fee = float(payment["gateway_fee"])
            if amount <= 0 or method not in rules_by_method:
                continue

            rule = rules_by_method[method]
            ceiling_rate = float(rule["maximum_fee_rate"]) + buffer_pct
            actual_rate = actual_fee / amount
            if actual_rate <= ceiling_rate:
                continue

            expected_fee = round(amount * float(rule["standard_fee_rate"]), 2)
            customer_id, segment = self._segment_for_order(payment["order_id"], context)

            self._emit(
                rule_code="UNUSUAL_GATEWAY_FEE",
                entity_type="PAYMENT",
                entity_id=payment["payment_id"],
                business_date=payment["payment_date_dt"],
                risk_amount=actual_fee - expected_fee,
                order_id=payment["order_id"],
                customer_id=customer_id,
                customer_segment=segment,
                evidence={
                    "payment_id": payment["payment_id"],
                    "payment_method": method,
                    "payment_amount": amount,
                    "actual_gateway_fee": actual_fee,
                    "actual_fee_rate_pct": round(actual_rate * 100, 3),
                    "max_permitted_fee_rate_pct": round(ceiling_rate * 100, 3),
                    "excess_fee_amount": round(actual_fee - expected_fee, 2),
                },
                recommended_action=(
                    "Dispute payment gateway billing invoice and request merchant fee credit"
                ),
            )

    # ------------------------------------------------------------ summary

    def _summarise(self, datasets: dict) -> dict:
        gross = round(sum(f["risk_amount"] for f in self.findings), 2)

        # Deduplicate by order: one order can trip several rules, and adding
        # every amount would count the same money more than once. The worst
        # finding per order is the defensible figure.
        worst_per_order: dict[str, float] = {}
        for finding in self.findings:
            key = finding["order_id"] or finding["entity_id"]
            worst_per_order[key] = max(worst_per_order.get(key, 0.0), finding["risk_amount"])
        deduplicated = round(sum(worst_per_order.values()), 2)

        severity_counts = {level: 0 for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW")}
        rule_counts: dict[str, int] = {}
        for finding in self.findings:
            severity_counts[finding["severity"]] += 1
            rule_counts[finding["rule_code"]] = rule_counts.get(finding["rule_code"], 0) + 1

        payments = datasets["payments"]
        collected = float(
            payments[payments["payment_status"] == "Successful"]["payment_amount"].sum()
        )

        return {
            "run_id": self.run_id,
            "executed_at": utc_now_iso(),
            "as_of_date": self.as_of_date.date().isoformat(),
            "orders_evaluated": datasets["clean_orders_count"],
            "findings_count": len(self.findings),
            "gross_exposure": gross,
            "deduplicated_exposure": deduplicated,
            "successful_payment_value": round(collected, 2),
            "leakage_risk_rate_pct": round(deduplicated / collected * 100, 3)
            if collected > 0 else 0.0,
            "severity_breakdown": severity_counts,
            "rule_breakdown": rule_counts,
        }

    def _export(self, summary: dict) -> None:
        (self.output_dir / "leakage_findings.json").write_text(
            json.dumps(self.findings, indent=2, default=str), encoding="utf-8"
        )
        (self.output_dir / "leakage_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    from contract import make_run_id

    engine = LeakageEngine(run_id=make_run_id(1))
    summary = engine.run()
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
