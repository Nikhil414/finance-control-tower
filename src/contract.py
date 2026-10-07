"""Shared finding contract: identity generation, evidence hashing, validation.

Every detection module routes its findings through validate_finding() before they reach
Postgres. See docs/finding_contract.md for the reasoning behind each rule.

Run `python src/contract.py` to execute the self-check.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONTRACT_PATH = BASE_DIR / "config" / "contract.json"
POLICIES_PATH = BASE_DIR / "config" / "policies.json"


class ContractViolation(ValueError):
    """A finding or transition that the contract forbids. Never silently tolerated."""


def load_contract(path: Path = CONTRACT_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


CONTRACT = load_contract()
RULES_BY_CODE = {rule["rule_code"]: rule for rule in CONTRACT["rules"]}

with open(POLICIES_PATH, "r", encoding="utf-8") as _handle:
    POLICIES = json.load(_handle)


# ---------------------------------------------------------------- identity


def make_run_id(sequence: int, on_date: date | None = None) -> str:
    """Build a run_id. Sequence is the Nth run of that calendar day, starting at 1."""
    if not 1 <= sequence <= 999:
        raise ContractViolation(f"run sequence must be 1-999, got {sequence}")
    stamp = (on_date or datetime.now(timezone.utc).date()).strftime("%Y%m%d")
    run_id = f"RUN-{stamp}-{sequence:03d}"
    if not re.match(CONTRACT["run_id"]["pattern"], run_id):
        raise ContractViolation(f"generated run_id failed its own pattern: {run_id}")
    return run_id


def make_finding_id(source_module: str, counter: int, on_date: date | None = None) -> str:
    """Build a finding_id label. Counter is per-module, per-run, starting at 1."""
    modules = CONTRACT["source_modules"]
    if source_module not in modules:
        raise ContractViolation(f"unknown source_module: {source_module}")
    if not 1 <= counter <= 99999:
        raise ContractViolation(f"finding counter must be 1-99999, got {counter}")
    prefix = modules[source_module]["finding_prefix"]
    stamp = (on_date or datetime.now(timezone.utc).date()).strftime("%Y%m%d")
    return f"{prefix}-{stamp}-{counter:05d}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------- evidence


def evidence_hash(evidence: dict) -> str:
    """SHA-256 of canonical evidence JSON.

    Canonical means sorted keys and no whitespace, so two structurally identical
    evidence dicts hash identically regardless of insertion order. detected_at is
    excluded because a rerun of the same exception carries a new timestamp, and
    hashing it would defeat idempotency entirely.
    """
    payload = {key: value for key, value in evidence.items() if key != "detected_at"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_evidence(rule: dict, evidence: dict) -> list[str]:
    errors: list[str] = []
    rules = CONTRACT["evidence_rules"]

    missing = [key for key in rule["required_evidence_keys"] if key not in evidence]
    if missing:
        errors.append(f"missing required evidence keys: {sorted(missing)}")

    forbidden = [key for key in evidence if key in rules["forbidden_evidence_keys"]]
    if forbidden:
        errors.append(f"evidence contains PII keys: {sorted(forbidden)}")

    for key, value in evidence.items():
        if isinstance(value, (dict, list, tuple, set)):
            errors.append(f"evidence['{key}'] must be a flat scalar, got {type(value).__name__}")

    return errors


# ---------------------------------------------------------------- severity


def _more_severe(left: str, right: str) -> str:
    order = CONTRACT["severity_levels"]  # most severe first
    return left if order.index(left) <= order.index(right) else right


def resolve_severity(risk_amount: float, rule_code: str, context: dict | None = None) -> str:
    """Resolve severity from the amount ladder, the rule's floor, and escalations.

    Three inputs, in increasing order of authority:

    1. The amount ladder -- bigger exposure is more severe.
    2. The rule's `default_severity`, applied as a FLOOR. Some rules are serious
       regardless of amount: REFUND_EXCEEDS_PAYMENT means more was refunded than
       was ever collected, which is a total control failure whether it is 4,000
       or 400,000. Without the floor the amount ladder alone rated that LOW,
       which no finance reviewer would accept -- and it contradicted dim_rule,
       which advertised the rule as CRITICAL.
    3. A rule escalation, which wins outright. An unpaid enterprise delivery is
       a credit-exposure problem, not a small-invoice problem.

    Note this deliberately differs from the original leakage agent, which
    declared `severity_default` per rule in policies.json and then never read it.
    """
    rule = RULES_BY_CODE.get(rule_code)
    if rule is None:
        raise ContractViolation(f"unregistered rule_code: {rule_code}")

    context = context or {}
    for escalation in rule.get("escalations", []):
        if escalation["when"] == "customer_segment == 'Enterprise'":
            if context.get("customer_segment") == "Enterprise":
                return escalation["severity"]

    thresholds = CONTRACT["severity_thresholds"]
    amount = abs(float(risk_amount))
    from_amount = "LOW"
    for level in CONTRACT["severity_levels"]:
        if amount >= thresholds[level]:
            from_amount = level
            break

    return _more_severe(from_amount, rule["default_severity"])


def due_date_for(severity: str, detected_at: datetime) -> str:
    days = CONTRACT["sla_days_by_severity"][severity]
    return (detected_at + timedelta(days=days)).date().isoformat()


SEVERITY_BASE_SCORE = {"CRITICAL": 90, "HIGH": 75, "MEDIUM": 50, "LOW": 25}

# Aging reaches a full score at this age; beyond it, age stops differentiating.
AGING_SATURATION_DAYS = 20


def priority_score(severity: str, days_open: int, customer_segment: str = "Retail",
                   risk_amount: float = 0.0) -> float:
    """Queue order for human review.

    Priority answers "what should someone open first", which is a different
    question from severity. Severity is a category; priority is a ranking within
    and across categories.

    Weights come from config/policies.json `scoring_weights`: financial exposure
    0.50, rule severity 0.25, aging 0.15, customer segment 0.10.

    Those weights were declared in the original project and never read -- the
    implementation used a fixed 0.7 severity / 0.3 aging split with NO exposure
    term at all. With few findings that was merely odd; across 68 it produced a
    visibly wrong queue, ranking a 4,000 CRITICAL above a 523,035 CRITICAL,
    because once severity saturates there is nothing left to rank on. Exposure
    is the single most important input to "what first", so it now carries the
    weight the config always claimed it had.

    Lives here rather than in any one detector so SQL, leakage and
    reconciliation findings share one scale. Two modules ranking differently
    would make the merged queue's ordering meaningless.
    """
    weights = POLICIES["scoring_weights"]

    # Exposure is normalised against a reference amount that sits deliberately
    # ABOVE the CRITICAL severity threshold. Normalising against the threshold
    # itself made every case over 100,000 tie at a full score, so the queue
    # stopped ranking by money precisely where money matters most -- a 523,035
    # break sorted below a 110,725 one on an aging tiebreak. The reference is a
    # tuning knob in policies.json, not a constant, because the right value
    # depends on the scale of a given business's routine exceptions.
    reference = float(POLICIES["priority_exposure_reference_amount"])
    exposure_component = min(100.0, abs(float(risk_amount)) / reference * 100.0)

    severity_component = float(SEVERITY_BASE_SCORE[severity])

    aging_component = min(100.0, max(0, days_open) / AGING_SATURATION_DAYS * 100.0)

    multipliers = POLICIES["customer_segment_multiplier"]
    segment_component = (
        multipliers.get(customer_segment, 1.0) / max(multipliers.values()) * 100.0
    )

    score = (
        weights["financial_exposure_weight"] * exposure_component
        + weights["rule_severity_weight"] * severity_component
        + weights["aging_weight"] * aging_component
        + weights["customer_segment_weight"] * segment_component
    )
    return min(100.0, round(score, 1))


# ---------------------------------------------------------------- validation


REQUIRED_FIELDS = (
    "finding_id",
    "run_id",
    "source_module",
    "rule_code",
    "entity_type",
    "entity_id",
    "detected_at",
    "business_date",
    "severity",
    "risk_amount",
    "evidence_json",
    "status",
)


def validate_finding(finding: dict) -> dict:
    """Validate a finding against the contract, returning it with evidence_hash set.

    Raises ContractViolation listing every problem at once rather than the first one,
    so a broken emitter surfaces all its faults in a single run instead of one per fix.
    """
    errors: list[str] = []

    for field in REQUIRED_FIELDS:
        if field not in finding:
            errors.append(f"missing required field: {field}")
    if errors:
        raise ContractViolation("; ".join(errors))

    rule = RULES_BY_CODE.get(finding["rule_code"])
    if rule is None:
        errors.append(
            f"unregistered rule_code '{finding['rule_code']}' -- "
            "add it to config/contract.json before emitting it"
        )

    if not re.match(CONTRACT["run_id"]["pattern"], str(finding["run_id"])):
        errors.append(f"run_id does not match contract pattern: {finding['run_id']}")

    if not re.match(CONTRACT["finding_id"]["pattern"], str(finding["finding_id"])):
        errors.append(f"finding_id does not match contract pattern: {finding['finding_id']}")

    if finding["source_module"] not in CONTRACT["source_modules"]:
        errors.append(f"unknown source_module: {finding['source_module']}")

    if finding["severity"] not in CONTRACT["severity_levels"]:
        errors.append(f"unknown severity: {finding['severity']}")

    if finding["entity_type"] not in CONTRACT["entity_types"]:
        errors.append(f"unknown entity_type: {finding['entity_type']}")
    elif finding["entity_type"] == "VARIES":
        errors.append(
            "'VARIES' is a rule-level declaration, not a finding value -- a "
            "finding must name the concrete entity type it was raised against"
        )

    if finding["status"] not in CONTRACT["detector_writable_statuses"]:
        errors.append(
            f"detectors may only emit {CONTRACT['detector_writable_statuses']}, "
            f"got '{finding['status']}' -- status changes belong to human review"
        )

    if rule is not None:
        # A rule declaring VARIES fires against whatever table tripped it -- a
        # duplicate key or a negative amount is not specific to one entity --
        # so only rules pinned to a single entity type are checked for a match.
        if rule["entity_type"] != "VARIES" and finding["entity_type"] != rule["entity_type"]:
            errors.append(
                f"rule {rule['rule_code']} expects entity_type "
                f"{rule['entity_type']}, got {finding['entity_type']}"
            )

        risk = float(finding["risk_amount"])
        if risk < 0:
            errors.append(f"risk_amount must be >= 0, got {risk}")
        if rule.get("zero_exposure_by_design") and risk != 0:
            errors.append(
                f"rule {rule['rule_code']} is zero-exposure by design but carries "
                f"risk_amount {risk} -- this would inflate revenue-at-risk"
            )

        evidence = finding["evidence_json"]
        if not isinstance(evidence, dict):
            errors.append(f"evidence_json must be an object, got {type(evidence).__name__}")
        else:
            errors.extend(_check_evidence(rule, evidence))

    if "confirmed_loss" in finding or "recovered_amount" in finding:
        errors.append(
            "confirmed_loss and recovered_amount belong to fact_reviews, never to a "
            "finding -- a detector cannot confirm a loss"
        )

    if errors:
        raise ContractViolation(f"{finding.get('finding_id', '<no id>')}: " + "; ".join(errors))

    enriched = dict(finding)
    enriched["evidence_hash"] = evidence_hash(finding["evidence_json"])
    enriched["contract_version"] = CONTRACT["version"]
    enriched.setdefault("currency", CONTRACT["currency"])
    enriched.setdefault("approval_required", rule["approval_required"])
    return enriched


def natural_key(finding: dict) -> tuple:
    return tuple(finding[field] for field in CONTRACT["natural_key"])


def validate_transition(current: str, target: str) -> None:
    allowed = CONTRACT["status_transitions"].get(current)
    if allowed is None:
        raise ContractViolation(f"unknown current status: {current}")
    if target not in allowed:
        raise ContractViolation(
            f"illegal transition {current} -> {target}; allowed: {allowed or 'none (terminal)'}"
        )


# ---------------------------------------------------------------- self-check


def _demo() -> None:
    """Self-check. Fails loudly if the contract's guarantees stop holding."""
    good = {
        "finding_id": make_finding_id("LEAKAGE", 1, date(2026, 9, 12)),
        "run_id": make_run_id(1, date(2026, 9, 12)),
        "source_module": "LEAKAGE",
        "rule_code": "DUPLICATE_REFUND",
        "entity_type": "REFUND",
        "entity_id": "REF-000412",
        "detected_at": "2026-09-12T10:39:13Z",
        "business_date": "2026-09-11",
        "severity": "HIGH",
        "risk_amount": 25000.0,
        "evidence_json": {
            "original_refund_id": "REF-000412",
            "duplicate_refund_id": "REF-000413",
            "payment_id": "PAY-000301",
            "refund_amount": 25000.0,
            "original_refund_date": "2026-08-12 14:00:00",
            "duplicate_refund_date": "2026-08-13 09:30:00",
            "days_between": 0.81,
        },
        "status": "OPEN",
    }

    assert good["finding_id"] == "LK-20260912-00001", good["finding_id"]
    assert good["run_id"] == "RUN-20260912-001", good["run_id"]

    validated = validate_finding(good)
    assert len(validated["evidence_hash"]) == 64
    assert validated["approval_required"] is True
    assert validated["currency"] == "INR"

    # Evidence hash must ignore key order, or idempotency breaks on dict reordering.
    shuffled = dict(reversed(list(good["evidence_json"].items())))
    assert evidence_hash(shuffled) == evidence_hash(good["evidence_json"])

    # Evidence hash must ignore detected_at, or every rerun looks like a new finding.
    with_ts = dict(good["evidence_json"], detected_at="2026-09-13T00:00:00Z")
    assert evidence_hash(with_ts) == evidence_hash(good["evidence_json"])

    # Same exception, different run -> SAME natural key. A case is the exception,
    # not the run that noticed it, so tomorrow's run updates this case rather
    # than opening a rival beside it and orphaning its review history.
    other_run = validate_finding(dict(good, run_id=make_run_id(2, date(2026, 9, 12))))
    assert natural_key(other_run) == natural_key(validated)
    assert "run_id" not in CONTRACT["natural_key"]

    # Different evidence is genuinely a different case.
    changed = validate_finding(
        dict(good, evidence_json=dict(good["evidence_json"], refund_amount=9999.0))
    )
    assert natural_key(changed) != natural_key(validated)

    def rejects(finding: dict, expect: str) -> None:
        try:
            validate_finding(finding)
        except ContractViolation as error:
            assert expect in str(error), f"expected '{expect}' in: {error}"
        else:
            raise AssertionError(f"contract failed to reject: {expect}")

    rejects(dict(good, status="RESOLVED"), "detectors may only emit")
    rejects(dict(good, rule_code="MADE_UP_RULE"), "unregistered rule_code")
    rejects(dict(good, entity_type="ORDER"), "expects entity_type")
    rejects(dict(good, risk_amount=-1.0), "must be >= 0")
    rejects(dict(good, confirmed_loss=500.0), "belong to fact_reviews")
    rejects(
        dict(good, evidence_json={k: v for k, v in good["evidence_json"].items() if k != "payment_id"}),
        "missing required evidence keys",
    )
    rejects(
        dict(good, evidence_json=dict(good["evidence_json"], customer_name="Acme Ltd")),
        "PII keys",
    )
    rejects(
        dict(good, evidence_json=dict(good["evidence_json"], nested={"a": 1})),
        "flat scalar",
    )

    # Zero-exposure rules must stay at zero, or "revenue at risk" inflates with
    # money that was never lost.
    date_mismatch = {
        "finding_id": make_finding_id("RECONCILIATION", 1, date(2026, 9, 12)),
        "run_id": "RUN-20260912-001",
        "source_module": "RECONCILIATION",
        "rule_code": "DATE_MISMATCH",
        "entity_type": "BANK_TXN",
        "entity_id": "BNK-000021",
        "detected_at": "2026-09-12T10:39:13Z",
        "business_date": "2026-09-11",
        "severity": "LOW",
        "risk_amount": 0.0,
        "evidence_json": {
            "bank_txn_id": "BNK-000021",
            "ledger_txn_id": "LED-000019",
            "bank_value_date": "2026-09-11",
            "ledger_posting_date": "2026-09-09",
            "day_difference": 2,
            "tolerance_days": 1,
        },
        "status": "OPEN",
    }
    validate_finding(date_mismatch)
    rejects(dict(date_mismatch, risk_amount=5000.0), "zero-exposure by design")

    # Severity: amount ladder, rule floor, and escalation.
    # EXCESSIVE_DISCOUNT has a MEDIUM floor, so the ladder can only raise it.
    assert resolve_severity(150000, "EXCESSIVE_DISCOUNT") == "CRITICAL"
    assert resolve_severity(30000, "EXCESSIVE_DISCOUNT") == "HIGH"
    assert resolve_severity(6000, "EXCESSIVE_DISCOUNT") == "MEDIUM"
    assert resolve_severity(100, "EXCESSIVE_DISCOUNT") == "MEDIUM", "floor must hold"

    # UNUSUAL_GATEWAY_FEE also floors at MEDIUM; a trivial overcharge is still
    # a billing defect worth a case.
    assert resolve_severity(10, "UNUSUAL_GATEWAY_FEE") == "MEDIUM"

    # REFUND_EXCEEDS_PAYMENT is CRITICAL at any amount: more went out than ever
    # came in. This is the case the floor exists for.
    assert resolve_severity(1, "REFUND_EXCEEDS_PAYMENT") == "CRITICAL"
    assert resolve_severity(500000, "REFUND_EXCEEDS_PAYMENT") == "CRITICAL"

    # DATE_MISMATCH floors at LOW, so the ladder governs it freely -- and it
    # always carries zero exposure, so it stays LOW.
    assert resolve_severity(0, "DATE_MISMATCH") == "LOW"

    # Escalation outranks both ladder and floor.
    assert resolve_severity(100, "DELIVERED_UNPAID", {"customer_segment": "Enterprise"}) == "CRITICAL"
    assert resolve_severity(100, "DELIVERED_UNPAID", {"customer_segment": "Retail"}) == "HIGH", \
        "DELIVERED_UNPAID floors at HIGH"

    # Severity must never fall below a rule's declared floor, or dim_rule
    # advertises one thing while findings say another.
    for code, rule in RULES_BY_CODE.items():
        floor = rule["default_severity"]
        for amount in (0, 1, 4999, 25000, 100000, 10_000_000):
            resolved = resolve_severity(amount, code)
            assert _more_severe(resolved, floor) == resolved, (
                f"{code} at {amount} resolved {resolved}, below its {floor} floor"
            )

    # Priority must be driven primarily by exposure, or the review queue ranks
    # a trivial case above a major one once severity saturates.
    big = priority_score("CRITICAL", 1, "Retail", 523_035.00)
    small = priority_score("CRITICAL", 15, "Retail", 4_000.00)
    assert big > small, f"large exposure ranked below small ({big} vs {small})"

    # Exposure dominates, but does not erase the other inputs.
    assert priority_score("CRITICAL", 1, "Retail", 50_000) > priority_score(
        "LOW", 1, "Retail", 50_000
    ), "severity must still count"
    assert priority_score("HIGH", 20, "Retail", 10_000) > priority_score(
        "HIGH", 0, "Retail", 10_000
    ), "aging must still count"
    assert priority_score("HIGH", 5, "Enterprise", 10_000) > priority_score(
        "HIGH", 5, "Retail", 10_000
    ), "customer segment must still count"

    # Bounded regardless of input.
    assert priority_score("CRITICAL", 9999, "Enterprise", 10**12) <= 100.0
    assert priority_score("LOW", 0, "Retail", 0.0) >= 0.0

    # Exposure must keep ranking above the CRITICAL threshold. If it saturated
    # there, these two would tie and an aging tiebreak would decide -- which is
    # the bug that put a 110,725 case above a 523,035 one.
    assert (
        priority_score("CRITICAL", 1, "Retail", 523_035)
        > priority_score("CRITICAL", 1, "Retail", 110_725)
    ), "exposure stops differentiating above the CRITICAL threshold"

    # The reference must sit above the CRITICAL threshold, or saturation returns.
    assert (
        POLICIES["priority_exposure_reference_amount"]
        > CONTRACT["severity_thresholds"]["CRITICAL"]
    ), "priority_exposure_reference_amount must exceed the CRITICAL threshold"

    # Status graph.
    validate_transition("OPEN", "UNDER_REVIEW")
    validate_transition("UNDER_REVIEW", "VALID_EXCEPTION")
    validate_transition("VALID_EXCEPTION", "ACTION_APPROVED")
    validate_transition("ACTION_APPROVED", "RESOLVED")
    for bad_from, bad_to in [
        ("OPEN", "RESOLVED"),
        ("RESOLVED", "UNDER_REVIEW"),
        ("UNDER_REVIEW", "ACTION_APPROVED"),
        ("FALSE_POSITIVE", "VALID_EXCEPTION"),
    ]:
        try:
            validate_transition(bad_from, bad_to)
        except ContractViolation:
            pass
        else:
            raise AssertionError(f"allowed illegal transition {bad_from} -> {bad_to}")

    # Every registered rule must declare evidence keys and a valid entity type,
    # otherwise a rule can be registered that nothing could ever satisfy.
    for code, rule in RULES_BY_CODE.items():
        assert rule["required_evidence_keys"], f"{code} declares no evidence keys"
        assert rule["entity_type"] in CONTRACT["entity_types"], code
        assert rule["default_severity"] in CONTRACT["severity_levels"], code
        assert rule["source_module"] in CONTRACT["source_modules"], code

    # A VARIES rule accepts any concrete entity type, but never VARIES itself.
    varies = {
        "finding_id": make_finding_id("DATA_QUALITY", 1, date(2026, 9, 12)),
        "run_id": "RUN-20260912-001",
        "source_module": "DATA_QUALITY",
        "rule_code": "DUPLICATE_PRIMARY_KEY",
        "entity_type": "PAYMENT",
        "entity_id": "PAY-000033",
        "detected_at": "2026-09-12T10:39:13Z",
        "business_date": "2026-09-11",
        "severity": "CRITICAL",
        "risk_amount": 0.0,
        "evidence_json": {
            "table_name": "staging.payments",
            "key_column": "payment_id",
            "duplicate_value": "PAY-000033",
            "occurrence_count": 2,
        },
        "status": "OPEN",
    }
    validate_finding(varies)
    validate_finding(dict(varies, entity_type="ORDER", entity_id="ORD-00001"))
    rejects(dict(varies, entity_type="VARIES"), "not a finding value")

    print(f"contract v{CONTRACT['version']} self-check passed")
    print(f"  {len(RULES_BY_CODE)} rules registered across {len(CONTRACT['source_modules'])} modules")
    for module in CONTRACT["source_modules"]:
        codes = [c for c, r in RULES_BY_CODE.items() if r["source_module"] == module]
        print(f"    {module:<16} {len(codes)} rules")


if __name__ == "__main__":
    _demo()
