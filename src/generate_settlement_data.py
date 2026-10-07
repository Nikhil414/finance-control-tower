"""Generate the three datasets the capstone needs that neither source project had:
daily_kpi_targets, bank_transactions, ledger_transactions.

Deterministic by design. A fixed seed means the same inputs always produce the
same findings and the same financial totals -- which is what lets the end-to-end
test assert on exact numbers, and what "reproducible" has to mean for a control
system.

Run: python src/generate_settlement_data.py
"""

from __future__ import annotations

import random
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
RAW = BASE_DIR / "data" / "raw"

SEED = 20260912

# Settlement conventions. The gateway pays out net of its fee, and both the bank
# statement and the ledger record that same net figure -- so a difference between
# them is a genuine break rather than an expected fee deduction.
SETTLEMENT_LAG_DAYS = 1
DATE_TOLERANCE_DAYS = 1


def load_source() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    orders = pd.read_csv(RAW / "orders.csv", parse_dates=["order_date"])
    payments = pd.read_csv(RAW / "payments.csv", parse_dates=["payment_date"])
    refunds = pd.read_csv(RAW / "refunds.csv", parse_dates=["refund_date"])
    return orders, payments, refunds


# ---------------------------------------------------------------- calibration


FAILED_ATTEMPT_PREFIX = "PAY-FAIL-"
FAILED_ATTEMPT_RATE = 0.06


def add_failed_attempts(payments: pd.DataFrame) -> pd.DataFrame:
    """Add failed first attempts for orders that later paid successfully.

    The source data carries 1 failed payment in 306, which makes the daily
    payment-success rate sit at 100% and turns the KPI page into a flat green
    line that demonstrates nothing. Real gateway traffic declines a few percent
    of attempts and customers retry.

    Modelled as ADDITIONAL attempt rows rather than by flipping existing
    payments to Failed. That distinction matters: flipping a successful payment
    would leave its order delivered-but-unpaid and manufacture leakage findings
    that the seeded test cases did not intend. Adding a failed attempt alongside
    a successful one changes the attempt count and nothing else -- every order's
    collected total, and therefore every leakage and data-quality finding, is
    exactly as before.

    Idempotent: previously generated attempts are dropped before new ones are
    added, so running the generator repeatedly does not compound them.
    """
    rng = random.Random(SEED + 2)

    original = payments[
        ~payments["payment_id"].astype(str).str.startswith(FAILED_ATTEMPT_PREFIX)
    ].copy()

    successful = original[original["payment_status"] == "Successful"]
    successful = successful.sort_values("payment_id")

    target_count = int(len(successful) * FAILED_ATTEMPT_RATE)
    victims = rng.sample(list(successful.index), target_count)

    attempts = []
    for counter, index in enumerate(sorted(victims), start=1):
        row = original.loc[index]
        attempts.append({
            "payment_id": f"{FAILED_ATTEMPT_PREFIX}{counter:04d}",
            "order_id": row["order_id"],
            # The declined attempt precedes the successful retry.
            "payment_date": (row["payment_date"] - pd.Timedelta(minutes=7)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "payment_status": "Failed",
            # A declined attempt collects nothing and is charged no fee. Any
            # non-zero value here would leak into the revenue definition.
            "payment_amount": 0.0,
            "payment_method": row["payment_method"],
            "gateway_fee": 0.0,
            "transaction_reference": f"TXN-DECLINED-{counter:04d}",
        })

    combined = pd.concat([original, pd.DataFrame(attempts)], ignore_index=True)
    # The appended rows carry string dates, which downgrades the column to
    # object dtype and breaks every .dt accessor downstream. Re-parse once here.
    combined["payment_date"] = pd.to_datetime(combined["payment_date"])
    combined = combined.sort_values(["payment_date", "payment_id"]).reset_index(drop=True)

    collected_before = original.loc[
        original["payment_status"] == "Successful", "payment_amount"
    ].sum()
    collected_after = combined.loc[
        combined["payment_status"] == "Successful", "payment_amount"
    ].sum()
    assert abs(collected_before - collected_after) < 0.01, \
        "calibration changed collected revenue -- it must not"

    return combined


# ---------------------------------------------------------------- KPI targets


def build_kpi_targets(orders: pd.DataFrame, payments: pd.DataFrame,
                      refunds: pd.DataFrame) -> pd.DataFrame:
    """Targets covering every trading day in the order data.

    Targets are derived from actuals with a seeded daily swing, so the KPI page
    shows a realistic mix of days that beat target and days that miss. Deriving
    from actuals is circular for synthetic data, but the alternative -- flat
    round numbers -- produces a variance chart that is either all green or all
    red and demonstrates nothing.

    Revenue follows the official definition used everywhere in this system:
    successful payments minus processed refunds minus gateway fees.
    """
    rng = random.Random(SEED)

    successful = payments[payments["payment_status"] == "Successful"].copy()
    successful["day"] = successful["payment_date"].dt.date
    daily_paid = successful.groupby("day")["payment_amount"].sum()
    daily_fees = successful.groupby("day")["gateway_fee"].sum()

    processed = refunds[refunds["refund_status"] == "Processed"].copy()
    processed["day"] = processed["refund_date"].dt.date
    daily_refunds = processed.groupby("day")["refund_amount"].sum()

    orders_by_day = orders.assign(day=orders["order_date"].dt.date).groupby("day")
    daily_orders = orders_by_day["order_id"].count()

    all_days = pd.date_range(orders["order_date"].min(), orders["order_date"].max(), freq="D")

    rows = []
    for stamp in all_days:
        day = stamp.date()
        actual_revenue = (
            float(daily_paid.get(day, 0.0))
            - float(daily_refunds.get(day, 0.0))
            - float(daily_fees.get(day, 0.0))
        )
        actual_orders = int(daily_orders.get(day, 0))

        # Swing centred slightly above actual, so misses are common but not universal.
        revenue_factor = rng.uniform(0.94, 1.09)
        order_factor = rng.uniform(0.92, 1.06)

        rows.append({
            "metric_date": day.isoformat(),
            "target_revenue": round(max(actual_revenue, 0.0) * revenue_factor, 2),
            "target_payment_success_rate": round(rng.uniform(0.960, 0.995), 4),
            "target_order_count": max(1, int(round(actual_orders * order_factor))),
            "max_refund_rate_pct": 2.0,
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------- settlement


def build_settlement(payments: pd.DataFrame,
                     refunds: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build matching bank and ledger sides, then inject the five break types.

    Every successful payment and processed refund settles cleanly by default.
    Breaks are introduced deliberately and in known quantities so the
    reconciliation controls have something real to find and the end-to-end test
    can assert the exact count.
    """
    rng = random.Random(SEED + 1)

    bank: list[dict] = []
    ledger: list[dict] = []

    successful = payments[payments["payment_status"] == "Successful"].copy()
    successful = successful.sort_values("payment_id").reset_index(drop=True)

    # The source data contains genuinely colliding transaction references
    # (PAY-000033 and PAY-000179 both carry TXN-859782 with different amounts).
    # That collision is a real defect and the DUPLICATE_TRANSACTION_REFERENCE
    # data-quality rule reports it. Here it must also be disambiguated, because
    # reconciliation matches on reference: left as-is, one reference matching two
    # candidates on each side fans out into four match rows and silently inflates
    # both the match count and the break count. Real gateways suffix a sequence
    # for exactly this reason.
    collisions = (
        successful["transaction_reference"]
        .value_counts()
        .loc[lambda counts: counts > 1]
        .index
    )
    suffix_counter: dict[str, int] = {}

    def settlement_reference(raw_reference: str) -> str:
        if raw_reference not in set(collisions):
            return raw_reference
        suffix_counter[raw_reference] = suffix_counter.get(raw_reference, 0) + 1
        return f"{raw_reference}-{chr(ord('A') + suffix_counter[raw_reference] - 1)}"

    # ---- clean base: inflow settlements
    for position, row in successful.iterrows():
        reference = settlement_reference(str(row["transaction_reference"]))
        net = round(float(row["payment_amount"]) - float(row["gateway_fee"]), 2)
        value_date = (row["payment_date"] + pd.Timedelta(days=SETTLEMENT_LAG_DAYS)).date()
        posting_date = row["payment_date"].date()

        bank.append({
            "bank_txn_id": f"BNK-{position + 1:06d}",
            "value_date": value_date.isoformat(),
            "amount": net,
            "bank_reference": reference,
            "txn_type": "CREDIT",
            "narration": f"GATEWAY SETTLEMENT {reference}",
        })
        ledger.append({
            "ledger_txn_id": f"LED-{position + 1:06d}",
            "posting_date": posting_date.isoformat(),
            "amount": net,
            "ledger_reference": reference,
            "account_code": "1100-CASH",
            "txn_type": "CREDIT",
            "source_entity_id": str(row["payment_id"]),
        })

    # ---- clean base: refund outflows
    processed = refunds[refunds["refund_status"] == "Processed"].copy()
    processed = processed.sort_values("refund_id").reset_index(drop=True)
    offset = len(successful)

    for position, row in processed.iterrows():
        reference = f"RFD-{row['refund_id']}"
        amount = -round(float(row["refund_amount"]), 2)
        bank.append({
            "bank_txn_id": f"BNK-{offset + position + 1:06d}",
            "value_date": (row["refund_date"] + pd.Timedelta(days=SETTLEMENT_LAG_DAYS)).date().isoformat(),
            "amount": amount,
            "bank_reference": reference,
            "txn_type": "DEBIT",
            "narration": f"REFUND PAYOUT {row['refund_id']}",
        })
        ledger.append({
            "ledger_txn_id": f"LED-{offset + position + 1:06d}",
            "posting_date": row["refund_date"].date().isoformat(),
            "amount": amount,
            "ledger_reference": reference,
            "account_code": "4200-REFUNDS",
            "txn_type": "DEBIT",
            "source_entity_id": str(row["refund_id"]),
        })

    bank_df = pd.DataFrame(bank)
    ledger_df = pd.DataFrame(ledger)

    # Pick distinct, disjoint victims for each break type so one row never
    # carries two breaks -- overlapping breaks make the expected counts in the
    # end-to-end test ambiguous.
    #
    # Victims are identified by bank_txn_id, never by positional index: the
    # LEDGER_ONLY step drops rows and reindexes, so a position captured
    # beforehand would afterwards point at a different transaction.
    bank_df = bank_df.set_index("bank_txn_id", drop=False)
    eligible = sorted(bank_df.loc[bank_df["txn_type"] == "CREDIT", "bank_txn_id"])
    rng.shuffle(eligible)

    def take(count: int) -> list[str]:
        taken, eligible[:] = eligible[:count], eligible[count:]
        return taken

    amount_mismatch_ids = take(4)
    date_mismatch_ids = take(3)
    ledger_only_ids = take(3)
    duplicate_ids = take(2)

    breaks_expected = {
        "AMOUNT_MISMATCH": len(amount_mismatch_ids),
        "DATE_MISMATCH": len(date_mismatch_ids),
        "LEDGER_ONLY_TRANSACTION": len(ledger_only_ids),
        "DUPLICATE_SETTLEMENT": len(duplicate_ids),
        "BANK_ONLY_TRANSACTION": 3,
    }

    # 1. AMOUNT_MISMATCH -- the bank credited less than the ledger booked.
    for txn_id in amount_mismatch_ids:
        original_amount = float(bank_df.at[txn_id, "amount"])
        shortfall = round(original_amount * 0.03 + 25.0, 2)
        bank_df.at[txn_id, "amount"] = round(original_amount - shortfall, 2)
        bank_df.at[txn_id, "narration"] = bank_df.at[txn_id, "narration"] + " (PARTIAL)"

    # 2. DATE_MISMATCH -- settlement landed outside the tolerance window. No
    #    money is missing, so these findings carry zero exposure by contract.
    for txn_id in date_mismatch_ids:
        late = pd.Timestamp(bank_df.at[txn_id, "value_date"]) + pd.Timedelta(
            days=DATE_TOLERANCE_DAYS + 3
        )
        bank_df.at[txn_id, "value_date"] = late.date().isoformat()

    # 3. DUPLICATE_SETTLEMENT -- the same reference credited twice. Built before
    #    the drop below, while every chosen id is still present.
    duplicates = []
    for counter, txn_id in enumerate(duplicate_ids, start=1):
        original = bank_df.loc[txn_id].to_dict()
        duplicates.append({
            **original,
            "bank_txn_id": f"BNK-DUP-{counter:03d}",
            "value_date": (
                pd.Timestamp(original["value_date"]) + pd.Timedelta(days=1)
            ).date().isoformat(),
            "narration": original["narration"] + " (REPRESENTED)",
        })

    # 4. LEDGER_ONLY -- revenue was booked but the cash never arrived. Drop the
    #    bank side entirely.
    ledger_only_refs = [bank_df.at[txn_id, "bank_reference"] for txn_id in ledger_only_ids]
    bank_df = bank_df.drop(index=ledger_only_ids).reset_index(drop=True)

    # 5. BANK_ONLY -- cash arrived with no ledger entry to explain it.
    unexplained = [
        {
            "bank_txn_id": "BNK-UNK-001",
            "value_date": "2026-08-19",
            "amount": 48500.00,
            "bank_reference": "NEFT-UNIDENTIFIED-8891",
            "txn_type": "CREDIT",
            "narration": "NEFT INWARD UNIDENTIFIED REMITTER",
        },
        {
            "bank_txn_id": "BNK-UNK-002",
            "value_date": "2026-08-27",
            "amount": 12750.00,
            "bank_reference": "NEFT-UNIDENTIFIED-9043",
            "txn_type": "CREDIT",
            "narration": "NEFT INWARD NO REMITTANCE ADVICE",
        },
        {
            "bank_txn_id": "BNK-UNK-003",
            "value_date": "2026-09-02",
            "amount": 91200.00,
            "bank_reference": "RTGS-SUSPENSE-1174",
            "txn_type": "CREDIT",
            "narration": "RTGS CREDIT PENDING IDENTIFICATION",
        },
    ]

    bank_df = pd.concat(
        [bank_df, pd.DataFrame(duplicates), pd.DataFrame(unexplained)],
        ignore_index=True,
    )

    bank_df = bank_df.sort_values(["value_date", "bank_txn_id"]).reset_index(drop=True)
    ledger_df = ledger_df.sort_values(["posting_date", "ledger_txn_id"]).reset_index(drop=True)

    return bank_df, ledger_df, breaks_expected, ledger_only_refs


# ---------------------------------------------------------------- entry point


def main() -> None:
    orders, payments, refunds = load_source()

    payments = add_failed_attempts(payments)
    payments.to_csv(RAW / "payments.csv", index=False)
    failed = int((payments["payment_status"] == "Failed").sum())
    print(f"payments.csv             {len(payments):>4} rows  "
          f"({failed} failed attempts, "
          f"{100 * (1 - failed / len(payments)):.1f}% success)")

    targets = build_kpi_targets(orders, payments, refunds)
    targets.to_csv(RAW / "daily_kpi_targets.csv", index=False)

    bank_df, ledger_df, expected, ledger_only_refs = build_settlement(payments, refunds)
    bank_df.to_csv(RAW / "bank_transactions.csv", index=False)
    ledger_df.to_csv(RAW / "ledger_transactions.csv", index=False)

    print(f"daily_kpi_targets.csv    {len(targets):>4} rows  "
          f"({targets['metric_date'].min()} -> {targets['metric_date'].max()})")
    print(f"bank_transactions.csv    {len(bank_df):>4} rows")
    print(f"ledger_transactions.csv  {len(ledger_df):>4} rows")
    print("\ndeliberate reconciliation breaks seeded:")
    for rule, count in sorted(expected.items()):
        print(f"  {rule:<28} {count}")
    print(f"\ntotal expected breaks: {sum(expected.values())}")

    _verify(bank_df, ledger_df, expected, ledger_only_refs)


def _verify(bank_df: pd.DataFrame, ledger_df: pd.DataFrame,
            expected: dict, ledger_only_refs: list[str]) -> None:
    """Confirm the generator produced exactly the breaks it claims.

    Without this the reconciliation controls could be validated against a
    dataset that does not actually contain what we think it does, and a passing
    test would prove nothing.
    """
    bank_refs = set(bank_df["bank_reference"])
    ledger_refs = set(ledger_df["ledger_reference"])

    bank_only = bank_refs - ledger_refs
    ledger_only = ledger_refs - bank_refs

    assert len(bank_only) == expected["BANK_ONLY_TRANSACTION"], \
        f"bank-only mismatch: {sorted(bank_only)}"
    assert len(ledger_only) == expected["LEDGER_ONLY_TRANSACTION"], \
        f"ledger-only mismatch: {sorted(ledger_only)}"
    assert set(ledger_only) == set(ledger_only_refs), "ledger-only refs drifted"

    duplicate_refs = bank_df[bank_df.duplicated("bank_reference", keep=False)]
    assert len(duplicate_refs) == expected["DUPLICATE_SETTLEMENT"] * 2, \
        f"expected {expected['DUPLICATE_SETTLEMENT']} duplicate pairs, got {len(duplicate_refs)} rows"

    # No bank row may be blank or unreferenced -- an unkeyed row would land in
    # the break pile for the wrong reason.
    assert bank_df["bank_reference"].notna().all()
    assert bank_df["amount"].notna().all()
    assert ledger_df["ledger_reference"].notna().all()

    print("generator self-check passed")


if __name__ == "__main__":
    main()
