"""Verify the AI investigator stays inside its box.

The tests that matter most here are not about output quality -- they are about
capability. An investigator that produces a slightly worse brief is a minor
problem; one that can reach the database is a different system entirely.

Run: pytest tests/test_ai_investigator.py
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from ai_investigator import DEGRADED, SUCCESS, UNAVAILABLE, AIInvestigator  # noqa: E402
from contract import CONTRACT  # noqa: E402


def finding(**overrides) -> dict:
    base = {
        "finding_id": "LK-20260912-00001",
        "run_id": "RUN-20260912-001",
        "source_module": "LEAKAGE",
        "rule_code": "DUPLICATE_REFUND",
        "entity_type": "REFUND",
        "entity_id": "REF-000412",
        "order_id": "ORD-00193",
        "customer_id": "CUS-0015",
        "business_date": "2026-09-11",
        "severity": "HIGH",
        "risk_amount": 25000.0,
        "priority_score": 60.0,
        "days_open": 3,
        "due_date": "2026-09-15",
        "approval_required": True,
        "evidence_json": {
            "original_refund_id": "REF-000412",
            "duplicate_refund_id": "REF-000413",
            "payment_id": "PAY-000301",
            "refund_amount": 25000.0,
            "original_refund_date": "2026-08-12 14:00:00",
            "duplicate_refund_date": "2026-08-13 09:30:00",
            "days_between": 0.81,
        },
    }
    base.update(overrides)
    return base


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """An isolated output directory with a findings file, and no AI configured."""
    for variable in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
                     "AI_MODEL", "AI_DISABLED"):
        monkeypatch.delenv(variable, raising=False)

    findings = [
        finding(),
        finding(finding_id="LK-20260912-00002", rule_code="DELIVERED_UNPAID",
                entity_type="ORDER", entity_id="ORD-00193", order_id="ORD-00193",
                risk_amount=295000.0, severity="CRITICAL", priority_score=80.0,
                evidence_json={
                    "order_final_amount": 295000.0,
                    "successful_payment_amount": 0.0,
                    "shortfall_amount": 295000.0,
                    "delivery_date": "2026-08-30",
                    "courier": "Delhivery",
                }),
    ]

    (tmp_path / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
    (tmp_path / "run_summary.json").write_text(json.dumps({
        "run_id": "RUN-20260912-001",
        "findings_count": 2,
        "gross_exposure": 320000.0,
        "deduplicated_exposure": 295000.0,
    }), encoding="utf-8")

    return tmp_path


# ------------------------------------------------------------ capability limits

def test_module_cannot_reach_the_database():
    """The hard architectural guarantee, asserted against the source itself.

    The investigator must have no database import at all. A prompt-injection
    payload hidden in an evidence field then has nothing to reach: there is no
    connection object, no driver, and no credentials in the process. Asserted
    statically so the property survives future edits by anyone who has not read
    the contract.
    """
    tree = ast.parse((SRC / "ai_investigator.py").read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"psycopg", "psycopg2", "sqlalchemy", "db", "load_findings",
                 "load_source", "asyncpg", "pyodbc"}
    assert not (imported & forbidden), (
        f"ai_investigator must not import {imported & forbidden} -- it is "
        "forbidden a database path by architecture, not by instruction"
    )


def test_module_holds_no_credentials_for_the_database(monkeypatch):
    """Nor may it read DATABASE_URL, which would let it build its own connection."""
    source = (SRC / "ai_investigator.py").read_text(encoding="utf-8")
    assert "DATABASE_URL" not in source
    assert "connection_string" not in source


def test_only_contract_approved_actions_are_reported(workspace):
    investigator = AIInvestigator(output_dir=workspace)
    investigator.investigate()

    commentary = json.loads((workspace / "ai_commentary.json").read_text(encoding="utf-8"))
    assert set(commentary["actions_taken"]) <= set(CONTRACT["ai_investigator"]["allowed_actions"])
    assert commentary["allowed_actions"] == CONTRACT["ai_investigator"]["allowed_actions"]


# ------------------------------------------------------------ failure modes

def test_runs_without_any_api_key(workspace):
    """AI is optional by design. No key must still produce a usable brief."""
    result = AIInvestigator(output_dir=workspace).investigate()

    assert result["ai_status"] == UNAVAILABLE
    brief = (workspace / "executive_brief.md").read_text(encoding="utf-8")
    assert "Revenue Assurance Daily Brief" in brief
    assert "Priority review queue" in brief
    assert "295,000.00" in brief, "the brief must still carry the real numbers"


def test_ai_disabled_flag_is_respected(workspace, monkeypatch):
    """The switch used to demonstrate the outage path on demand."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("AI_DISABLED", "1")

    result = AIInvestigator(output_dir=workspace).investigate()
    assert result["ai_status"] == UNAVAILABLE


def test_api_failure_degrades_instead_of_raising(workspace, monkeypatch):
    """A control system must not stop controlling because a model is down."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")

    def explode(*args, **kwargs):
        raise TimeoutError("connection timed out")

    monkeypatch.setattr(AIInvestigator, "_draft_commentary", explode)

    result = AIInvestigator(output_dir=workspace).investigate()

    assert result["ai_status"] == DEGRADED
    assert "TimeoutError" in result["reason"]
    brief = (workspace / "executive_brief.md").read_text(encoding="utf-8")
    assert "295,000.00" in brief, "deterministic content must survive an AI outage"


def test_empty_model_response_is_treated_as_failure(workspace, monkeypatch):
    """A blank answer is a failure, not a brief with no commentary."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key")
    monkeypatch.setattr(
        AIInvestigator, "_draft_commentary",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("model returned an empty response")),
    )
    result = AIInvestigator(output_dir=workspace).investigate()
    assert result["ai_status"] == DEGRADED


def test_model_narrative_is_included_when_available(workspace, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setattr(
        AIInvestigator, "_draft_commentary",
        lambda *args, **kwargs: "## Management Summary\nSynthetic commentary.",
    )
    result = AIInvestigator(output_dir=workspace).investigate()

    assert result["ai_status"] == SUCCESS
    assert "Synthetic commentary." in (workspace / "executive_brief.md").read_text(encoding="utf-8")


def test_missing_findings_file_is_handled(tmp_path):
    result = AIInvestigator(output_dir=tmp_path).investigate()
    assert result["ai_status"] == UNAVAILABLE


# ------------------------------------------------------------ Jev (typesafe.ai)

def test_jev_disabled_by_default(workspace):
    """No TYPESAFE_API_KEY -- the second AI step is optional, same as the
    narrative provider."""
    investigator = AIInvestigator(output_dir=workspace)
    investigator.investigate()

    commentary = json.loads((workspace / "ai_commentary.json").read_text(encoding="utf-8"))
    assert commentary["jev_status"] == UNAVAILABLE
    assert commentary["false_positive_likelihood"] == {}
    assert "score_false_positive_likelihood" not in commentary["actions_taken"]


def test_jev_total_failure_degrades_instead_of_raising(workspace, monkeypatch):
    """A control system must not stop controlling because Jev is unreachable."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-not-a-real-key")

    def explode(*args, **kwargs):
        raise TimeoutError("connection timed out")

    monkeypatch.setattr(AIInvestigator, "_score_false_positive_likelihood", explode)

    investigator = AIInvestigator(output_dir=workspace)
    result = investigator.investigate()
    assert result["ai_status"] in (UNAVAILABLE, DEGRADED, SUCCESS)  # unaffected by Jev

    commentary = json.loads((workspace / "ai_commentary.json").read_text(encoding="utf-8"))
    assert commentary["jev_status"] == DEGRADED
    assert "TimeoutError" in commentary["jev_reason"]
    assert commentary["false_positive_likelihood"] == {}


def test_jev_success_is_recorded(workspace, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-not-a-real-key")
    monkeypatch.setattr(
        AIInvestigator, "_score_false_positive_likelihood",
        lambda self, api_key, findings: {"LK-20260912-00001": 0.12},
    )

    investigator = AIInvestigator(output_dir=workspace)
    investigator.investigate()

    commentary = json.loads((workspace / "ai_commentary.json").read_text(encoding="utf-8"))
    assert commentary["jev_status"] == SUCCESS
    assert commentary["false_positive_likelihood"] == {"LK-20260912-00001": 0.12}
    assert "score_false_positive_likelihood" in commentary["actions_taken"]


# ------------------------------------------------------------ deterministic work

def test_related_cases_are_grouped_and_deduplicated():
    """Two rules on one order form one cluster, and its exposure must not add
    the same money twice."""
    groups = AIInvestigator.group_related_cases([
        finding(finding_id="A", risk_amount=25000.0),
        finding(finding_id="B", rule_code="DELIVERED_UNPAID", entity_type="ORDER",
                risk_amount=295000.0, severity="CRITICAL"),
    ])

    assert len(groups) == 1
    group = groups[0]
    assert group["group_key"] == "ORD-00193"
    assert sorted(group["finding_ids"]) == ["A", "B"]
    assert group["gross_risk_amount"] == 320000.0
    assert group["deduplicated_risk_amount"] == 295000.0
    assert group["highest_severity"] == "CRITICAL"


def test_unrelated_findings_are_not_grouped():
    groups = AIInvestigator.group_related_cases([
        finding(finding_id="A", order_id="ORD-1"),
        finding(finding_id="B", order_id="ORD-2"),
    ])
    assert groups == []


def test_missing_evidence_is_flagged():
    """A reviewer who cannot reproduce the amount must not be asked to confirm it."""
    incomplete = finding()
    del incomplete["evidence_json"]["payment_id"]

    gaps = AIInvestigator.flag_missing_evidence([incomplete])

    assert len(gaps) == 1
    assert gaps[0]["issue"] == "missing required evidence"
    assert gaps[0]["missing_keys"] == ["payment_id"]


def test_empty_evidence_values_are_flagged():
    """A key present but blank is as unreviewable as one absent -- this is what
    caught the original UNKNOWN customer_id bug class."""
    hollow = finding()
    hollow["evidence_json"]["payment_id"] = "UNKNOWN"

    gaps = AIInvestigator.flag_missing_evidence([hollow])
    assert any(gap["issue"] == "evidence present but empty" for gap in gaps)


def test_complete_evidence_raises_no_gap():
    assert AIInvestigator.flag_missing_evidence([finding()]) == []


def test_unregistered_rule_is_flagged():
    gaps = AIInvestigator.flag_missing_evidence([finding(rule_code="INVENTED")])
    assert gaps[0]["issue"] == "unregistered rule_code"


def test_review_queue_is_ordered_by_priority():
    queue = AIInvestigator.request_review([
        finding(finding_id="low", priority_score=10.0, risk_amount=100.0),
        finding(finding_id="high", priority_score=90.0, risk_amount=500000.0),
        finding(finding_id="mid", priority_score=50.0, risk_amount=20000.0),
    ])
    assert [case["finding_id"] for case in queue] == ["high", "mid", "low"]


def test_next_checks_cover_every_rule_present():
    checks = AIInvestigator.recommend_next_check([
        finding(),
        finding(rule_code="DELIVERED_UNPAID", entity_type="ORDER"),
    ])
    assert {item["rule_code"] for item in checks} == {"DUPLICATE_REFUND", "DELIVERED_UNPAID"}
    assert all(item["next_check"] for item in checks)


def test_every_registered_rule_has_a_playbook_entry():
    """A rule with no next check leaves an analyst with a case and no procedure."""
    from contract import RULES_BY_CODE

    checks = AIInvestigator.recommend_next_check([
        finding(rule_code=code) for code in RULES_BY_CODE
    ])
    generic = "Review the evidence and confirm the calculation by hand."
    missing = [item["rule_code"] for item in checks if item["next_check"] == generic]
    assert not missing, f"rules without a specific next check: {missing}"


# ------------------------------------------------------------ output discipline

def test_brief_states_exposure_is_not_confirmed_loss(workspace):
    """The distinction a finance reviewer will look for first."""
    AIInvestigator(output_dir=workspace).investigate()
    brief = (workspace / "executive_brief.md").read_text(encoding="utf-8")

    assert "estimated exposure" in brief.lower()
    assert "not confirmed loss" in brief.lower() or "none of it is confirmed loss" in brief.lower()


def test_brief_declares_the_investigator_has_no_write_path(workspace):
    AIInvestigator(output_dir=workspace).investigate()
    brief = (workspace / "executive_brief.md").read_text(encoding="utf-8")
    assert "no database connection" in brief.lower()
    assert "cannot change a case" in brief.lower()


def test_commentary_carries_an_advisory_disclaimer(workspace):
    AIInvestigator(output_dir=workspace).investigate()
    commentary = json.loads((workspace / "ai_commentary.json").read_text(encoding="utf-8"))
    assert "Advisory only" in commentary["disclaimer"]
    assert "never confirmed loss" in commentary["disclaimer"]


def test_prompt_sends_no_pii(workspace):
    """Evidence is PII-free by contract, which is what makes sending it to a
    third-party API acceptable. Verify the prompt honours that."""
    investigator = AIInvestigator(output_dir=workspace)
    findings = json.loads((workspace / "findings.json").read_text(encoding="utf-8"))
    analysis = {
        "request_review": investigator.request_review(findings),
        "group_related_cases": investigator.group_related_cases(findings),
        "flag_missing_evidence": investigator.flag_missing_evidence(findings),
    }
    prompt = investigator._build_prompt(findings, {}, analysis)

    for key in CONTRACT["evidence_rules"]["forbidden_evidence_keys"]:
        assert key not in prompt


def test_prompt_forbids_recalculation_and_status_change(workspace):
    investigator = AIInvestigator(output_dir=workspace)
    findings = json.loads((workspace / "findings.json").read_text(encoding="utf-8"))
    analysis = {
        "request_review": investigator.request_review(findings),
        "group_related_cases": investigator.group_related_cases(findings),
        "flag_missing_evidence": investigator.flag_missing_evidence(findings),
    }
    # Collapse whitespace: the constraints are line-wrapped in the prompt, so a
    # raw substring match would fail on the wrap rather than on the content.
    prompt = " ".join(investigator._build_prompt(findings, {}, analysis).lower().split())

    assert "do not compute" in prompt
    assert "never describe it as confirmed loss" in prompt
    assert "no authority over case status" in prompt
    assert "do not invent evidence" in prompt
