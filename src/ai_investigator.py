"""Controlled AI investigator.

Reads a structured findings file and writes commentary. That is the whole of its
power.

Deliberately has NO database import. Not "does not write to the database" as a
matter of prompt instruction -- it has no driver, no credentials and no
connection available to it, so a prompt injection in an evidence field has
nothing to reach. tests/test_ai_investigator.py asserts this module imports
nothing database-related, so the guarantee survives future edits.

The division of labour matters more than the model:

  * Deterministic code does the analysis -- grouping related cases, detecting
    missing evidence, ranking the queue. These are decisions with correct
    answers, and a language model that gets one wrong is worse than useless
    because the error looks authoritative.
  * The model writes prose -- explaining findings in business language and
    drafting a management summary. That is judgement about wording, not about
    money.

Unavailability is a normal operating condition, not an error. Missing key,
network failure, rate limit, malformed response: each degrades to a
deterministic brief built from the findings themselves, and the pipeline's exit
code does not change.

Run: python src/ai_investigator.py
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from contract import CONTRACT, RULES_BY_CODE

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "outputs"

logger = logging.getLogger("capstone.ai")

ALLOWED_ACTIONS = CONTRACT["ai_investigator"]["allowed_actions"]

# Status values written to fact_pipeline_runs.ai_status.
SUCCESS = "SUCCESS"        # model produced the brief
DEGRADED = "DEGRADED"      # model was configured but failed; template used
UNAVAILABLE = "UNAVAILABLE"  # no model configured; template used

REQUEST_TIMEOUT_SECONDS = 90
MAX_FINDINGS_TO_MODEL = 12

TYPESAFE_API_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_MODEL = "jev-latest"
MAX_FINDINGS_TO_SCORE = 12


class AIInvestigator:
    def __init__(self, output_dir: Path = OUTPUT_DIR):
        self.output_dir = output_dir
        self.findings_path = output_dir / "findings.json"
        self.summary_path = output_dir / "run_summary.json"
        self.brief_path = output_dir / "executive_brief.md"
        self.commentary_path = output_dir / "ai_commentary.json"

    # ------------------------------------------------------------ entry point

    def investigate(self) -> dict:
        if not self.findings_path.exists():
            logger.warning("no findings file at %s -- nothing to investigate",
                           self.findings_path)
            return {"ai_status": UNAVAILABLE, "reason": "findings file absent"}

        findings = json.loads(self.findings_path.read_text(encoding="utf-8"))
        summary = (
            json.loads(self.summary_path.read_text(encoding="utf-8"))
            if self.summary_path.exists() else {}
        )

        # Deterministic analysis first. This happens whether or not a model is
        # reachable, so the operational value of the brief never depends on an
        # external API being up.
        analysis = {
            "group_related_cases": self.group_related_cases(findings),
            "flag_missing_evidence": self.flag_missing_evidence(findings),
            "request_review": self.request_review(findings),
            "recommend_next_check": self.recommend_next_check(findings),
        }

        provider = self._configured_provider()
        narrative = None
        ai_status = UNAVAILABLE
        reason: str | None = None

        if provider is None:
            reason = "no API key configured (AI is optional by design)"
            logger.info("AI unavailable: %s", reason)
        else:
            try:
                narrative = self._draft_commentary(provider, findings, summary, analysis)
                ai_status = SUCCESS
                logger.info("AI commentary generated via %s", provider["name"])
            except Exception as error:
                ai_status = DEGRADED
                reason = f"{type(error).__name__}: {error}"
                logger.warning(
                    "AI call failed (%s) -- falling back to deterministic brief", reason
                )

        brief = self._build_brief(findings, summary, analysis, narrative, ai_status, reason)
        self.brief_path.write_text(brief, encoding="utf-8")

        typesafe_key = self._configured_typesafe_key()
        jev_status = UNAVAILABLE
        jev_reason: str | None = None
        false_positive_likelihood: dict[str, float] = {}

        if typesafe_key is None:
            jev_reason = "no TYPESAFE_API_KEY configured (optional by design)"
        else:
            try:
                false_positive_likelihood = self._score_false_positive_likelihood(
                    typesafe_key, findings
                )
                jev_status = SUCCESS
            except Exception as error:
                jev_status = DEGRADED
                jev_reason = f"{type(error).__name__}: {error}"
                logger.warning(
                    "Jev scoring failed (%s) -- continuing without it", jev_reason
                )

        actions_taken = set(analysis) | (
            {"draft_commentary", "prepare_management_summary"} if narrative else set()
        )
        if jev_status == SUCCESS:
            actions_taken.add("score_false_positive_likelihood")

        self.commentary_path.write_text(json.dumps({
            "run_id": summary.get("run_id"),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ai_status": ai_status,
            "reason": reason,
            "provider": provider["name"] if provider else None,
            "jev_status": jev_status,
            "jev_reason": jev_reason,
            "allowed_actions": ALLOWED_ACTIONS,
            "actions_taken": sorted(actions_taken),
            "analysis": analysis,
            "narrative": narrative,
            "false_positive_likelihood": false_positive_likelihood,
            "disclaimer": (
                "Advisory only. Every amount here is estimated exposure produced by a "
                "deterministic rule, never confirmed loss. This file is not read by "
                "Power BI and cannot change a case's status. false_positive_likelihood "
                "is a triage hint from Jev, not a decision -- it never sets a finding's "
                "status and a reviewer must still confirm or reject every case."
            ),
        }, indent=2, default=str), encoding="utf-8")

        logger.info("brief written to %s (ai_status=%s)", self.brief_path, ai_status)
        return {"ai_status": ai_status, "reason": reason,
                "brief_path": str(self.brief_path)}

    # ------------------------------------------------------ deterministic work

    @staticmethod
    def group_related_cases(findings: list[dict]) -> list[dict]:
        """Group findings that share an order or a customer.

        Deterministic on purpose. Whether two findings concern the same order is
        a fact, and asking a model to infer it would introduce errors into
        something a GROUP BY answers exactly. Grouping matters because one root
        cause often trips several rules, and a reviewer should see the cluster
        rather than five apparently unrelated cases.
        """
        by_order: dict[str, list[dict]] = {}
        for finding in findings:
            key = finding.get("order_id") or finding.get("entity_id")
            by_order.setdefault(key, []).append(finding)

        groups = []
        for key, members in by_order.items():
            if len(members) < 2:
                continue
            groups.append({
                "group_key": key,
                "finding_ids": [f["finding_id"] for f in members],
                "rule_codes": sorted({f["rule_code"] for f in members}),
                "modules": sorted({f["source_module"] for f in members}),
                "gross_risk_amount": round(sum(float(f["risk_amount"]) for f in members), 2),
                # The defensible figure for the cluster: the same money must not
                # be counted once per rule that noticed it.
                "deduplicated_risk_amount": round(
                    max(float(f["risk_amount"]) for f in members), 2
                ),
                "highest_severity": min(
                    (f["severity"] for f in members),
                    key=CONTRACT["severity_levels"].index,
                ),
            })

        return sorted(groups, key=lambda g: -g["deduplicated_risk_amount"])

    @staticmethod
    def flag_missing_evidence(findings: list[dict]) -> list[dict]:
        """Report findings whose evidence cannot support the amount claimed.

        Checked against each rule's required_evidence_keys in the contract, not
        judged by a model. A reviewer asked to confirm a loss must be able to
        recompute it from the evidence; if a key is absent the case is not
        reviewable and should be sent back rather than approved.
        """
        gaps = []
        for finding in findings:
            rule = RULES_BY_CODE.get(finding["rule_code"])
            if rule is None:
                gaps.append({
                    "finding_id": finding["finding_id"],
                    "issue": "unregistered rule_code",
                    "detail": finding["rule_code"],
                })
                continue

            evidence = finding.get("evidence_json") or {}
            missing = sorted(set(rule["required_evidence_keys"]) - set(evidence))
            if missing:
                gaps.append({
                    "finding_id": finding["finding_id"],
                    "rule_code": finding["rule_code"],
                    "issue": "missing required evidence",
                    "missing_keys": missing,
                })

            empty = sorted(
                key for key, value in evidence.items()
                if value in (None, "", "NULL", "UNKNOWN")
            )
            if empty:
                gaps.append({
                    "finding_id": finding["finding_id"],
                    "rule_code": finding["rule_code"],
                    "issue": "evidence present but empty",
                    "empty_keys": empty,
                })

        return gaps

    @staticmethod
    def request_review(findings: list[dict], limit: int = 10) -> list[dict]:
        """Rank the review queue. Ordering only -- no case is decided here."""
        ranked = sorted(
            findings,
            key=lambda f: (-float(f.get("priority_score", 0)), -float(f["risk_amount"])),
        )
        return [{
            "finding_id": f["finding_id"],
            "rule_code": f["rule_code"],
            "severity": f["severity"],
            "priority_score": f.get("priority_score"),
            "risk_amount": float(f["risk_amount"]),
            "order_id": f.get("order_id"),
            "due_date": f.get("due_date"),
            "approval_required": f.get("approval_required", True),
        } for f in ranked[:limit]]

    @staticmethod
    def recommend_next_check(findings: list[dict]) -> list[dict]:
        """Suggest the next verification step per rule present in this run.

        A fixed mapping rather than generated text: these are standard finance
        procedures, and a model inventing a plausible-sounding but wrong
        procedure is a real risk when an analyst may follow it.
        """
        playbook = {
            "DUPLICATE_REFUND": "Pull the gateway settlement report for the payment and confirm whether two payouts actually left the account.",
            "REFUND_EXCEEDS_PAYMENT": "Reconstruct the order's full payment and refund ledger; check for a refund raised against a cancelled or reversed capture.",
            "EXCESSIVE_DISCOUNT": "Retrieve the discount approval record and confirm whether the promo code was authorised for this customer tier.",
            "DELIVERED_UNPAID": "Confirm delivery with the courier's proof of delivery, then check whether payment settled under a different transaction reference.",
            "PRICING_ERROR": "Compare the catalogue's effective price window against the order date and check for a recent price-list sync failure.",
            "UNUSUAL_GATEWAY_FEE": "Match the charged fee against the current merchant agreement rate card and raise a billing dispute if it exceeds contract.",
            "BANK_ONLY_TRANSACTION": "Trace the remitter via the bank narration and identify which invoice or customer the credit belongs to.",
            "LEDGER_ONLY_TRANSACTION": "Confirm whether the settlement is genuinely outstanding or landed in a suspense account under a different reference.",
            "AMOUNT_MISMATCH": "Check for a bank charge, FX adjustment or partial settlement explaining the shortfall.",
            "DATE_MISMATCH": "Confirm the settlement eventually cleared and review whether the gateway is consistently outside its contracted cycle.",
            "DUPLICATE_SETTLEMENT": "Verify with the bank whether the second credit is a genuine re-presentment, then prepare a reversal instruction.",
            "ORDER_WITHOUT_PAYMENT": "Determine whether the order was abandoned, paid offline, or the payment record failed to load.",
            "PAYMENT_AMOUNT_MISMATCH": "Check for part payments, a price adjustment after capture, or a tax recalculation.",
            "ORPHAN_PAYMENT": "Identify the order the payment belongs to; a missing order record may indicate a failed upstream load.",
            "ORPHAN_REFUND": "Escalate immediately -- money left the business against a record that does not exist.",
            "NEGATIVE_AMOUNT": "Trace the source of the negative value; likely a sign error or an uncorrected reversal.",
            "DUPLICATE_PRIMARY_KEY": "Resolve at source before trusting any aggregate that touches this table.",
            "UNKNOWN_CUSTOMER_REFERENCE": "Check whether the customer master load is stale or the customer was deleted.",
            "DUPLICATE_TRANSACTION_REFERENCE": "Confirm the gateway reused a reference, and verify reconciliation did not fan out across the ambiguous key.",
            "MISSING_KPI_TARGET": "Load the missing target so the day's variance stops being hidden by a null join.",
            "REVENUE_BELOW_TARGET": "Break the shortfall down by region and channel before treating it as a demand problem.",
            "PAYMENT_SUCCESS_BELOW_TARGET": "Check decline reasons by payment method for a gateway or issuer outage.",
            "ORDER_COUNT_BELOW_TARGET": "Compare against the same weekday historically before reacting to a single day.",
            "REFUND_RATE_ABOVE_THRESHOLD": "Group refund reasons to separate a product-quality problem from a fulfilment one.",
        }

        present = sorted({f["rule_code"] for f in findings})
        return [{
            "rule_code": code,
            "finding_count": sum(1 for f in findings if f["rule_code"] == code),
            "next_check": playbook.get(code, "Review the evidence and confirm the calculation by hand."),
        } for code in present]

    # ------------------------------------------------------------ model call

    @staticmethod
    def _configured_provider() -> dict | None:
        """Resolve a provider from environment variables only.

        Never scans files for credentials. The original implementation searched
        several .env files plus APIKEY.txt and inferred the provider from the
        key's prefix, which both widened the blast radius of a leaked key and
        made it easy to ship one by accident.
        """
        if os.getenv("AI_DISABLED", "0") == "1":
            return None

        model_override = os.getenv("AI_MODEL") or None

        if os.getenv("ANTHROPIC_API_KEY"):
            return {
                "name": "anthropic",
                "key": os.environ["ANTHROPIC_API_KEY"],
                "url": "https://api.anthropic.com/v1/messages",
                "model": model_override or "claude-sonnet-5",
            }
        if os.getenv("OPENAI_API_KEY"):
            return {
                "name": "openai",
                "key": os.environ["OPENAI_API_KEY"],
                "url": "https://api.openai.com/v1/chat/completions",
                "model": model_override or "gpt-4o-mini",
            }
        if os.getenv("DEEPSEEK_API_KEY"):
            return {
                "name": "deepseek",
                "key": os.environ["DEEPSEEK_API_KEY"],
                "url": os.getenv("DEEPSEEK_BASE_URL",
                                 "https://api.deepseek.com/chat/completions"),
                "model": model_override or "deepseek-chat",
            }
        return None

    @staticmethod
    def _configured_typesafe_key() -> str | None:
        """Jev (typesafe.ai) is a second, independent AI step -- structured
        classification rather than prose. It shares the same discipline as the
        narrative provider: environment variable only, optional, and any
        failure degrades to an empty result rather than raising.
        """
        if os.getenv("AI_DISABLED", "0") == "1":
            return None
        return os.getenv("TYPESAFE_API_KEY") or None

    def _score_false_positive_likelihood(self, api_key: str,
                                         findings: list[dict]) -> dict[str, float]:
        """Ask Jev's Noul primitive how likely each finding is a false positive.

        Purely advisory: the result lands in ai_commentary.json as a triage hint
        for the reviewer and is never written back to a finding's status, exactly
        like the narrative -- ai_investigator has no database credentials at all.
        Scored one finding per call (bounded by MAX_FINDINGS_TO_SCORE) so one bad
        evidence payload cannot break the whole batch.
        """
        scored = findings[:MAX_FINDINGS_TO_SCORE]
        results: dict[str, float] = {}
        failures = 0
        for finding in scored:
            state = json.dumps({
                "rule_code": finding["rule_code"],
                "severity": finding["severity"],
                "risk_amount": float(finding["risk_amount"]),
                "evidence": finding.get("evidence_json"),
            }, default=str)

            payload = {
                "state": state,
                "model": TYPESAFE_MODEL,
                "questions": {
                    "is_false_positive": {
                        "type": "noul",
                        "instructions": (
                            "Given this evidence, the flagged finding is actually a "
                            "false positive rather than a genuine exception"
                        ),
                    }
                },
            }
            request = urllib.request.Request(
                TYPESAFE_API_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {api_key}",
                },
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=REQUEST_TIMEOUT_SECONDS
                ) as response:
                    data = json.loads(response.read().decode("utf-8"))
                results[finding["finding_id"]] = float(
                    data["answers"]["is_false_positive"]["noul"]
                )
            except Exception as error:
                failures += 1
                logger.warning(
                    "Jev scoring failed for %s: %s", finding["finding_id"], error
                )

        if scored and failures == len(scored):
            raise RuntimeError(f"all {failures} Jev scoring call(s) failed")
        return results

    @staticmethod
    def _build_prompt(findings: list[dict], summary: dict, analysis: dict) -> str:
        """Build the prompt. Only evidence-bearing fields are sent.

        Evidence is already PII-free by contract -- forbidden keys are rejected
        at validation -- which is what makes shipping it to a third-party API
        acceptable in the first place.
        """
        queue = analysis["request_review"]
        top = [{
            "finding_id": f["finding_id"],
            "rule_code": f["rule_code"],
            "severity": f["severity"],
            "risk_amount": float(f["risk_amount"]),
            "order_id": f.get("order_id"),
            "evidence": f.get("evidence_json"),
        } for f in findings[:MAX_FINDINGS_TO_MODEL]]

        return f"""You are a senior revenue assurance analyst writing a daily brief for a Head of Finance.

DETERMINISTIC RESULTS (already computed -- treat as fact):
{json.dumps(summary, indent=2, default=str)}

TOP CASES BY PRIORITY:
{json.dumps(queue, indent=2, default=str)}

CASE EVIDENCE:
{json.dumps(top, indent=2, default=str)}

RELATED CASE CLUSTERS:
{json.dumps(analysis["group_related_cases"][:5], indent=2, default=str)}

EVIDENCE GAPS:
{json.dumps(analysis["flag_missing_evidence"][:10], indent=2, default=str)}

Write two Markdown sections and nothing else:

## Management Summary
Three to five sentences for an executive who will not read the detail. What is
the picture, what is the biggest single concern, what decision is needed.

## Case Commentary
For each of the top five cases: one short paragraph explaining in business
language why it was flagged and what it probably means. Reference the evidence.

HARD CONSTRAINTS -- violating any of these makes the output unusable:
1. Use ONLY the numbers given above. Do not compute, re-derive, total, or
   estimate any figure. If you want a number that is not present, omit it.
2. Every amount is ESTIMATED EXPOSURE from a deterministic rule. Never describe
   it as confirmed loss, actual loss, or money lost. It is unconfirmed until a
   human reviews it.
3. Do not recommend that any case be closed, approved, resolved, or dismissed.
   You have no authority over case status.
4. Do not invent evidence, customer names, causes, or facts absent above.
5. Where evidence is missing, say the case cannot yet be concluded.
"""

    def _draft_commentary(self, provider: dict, findings: list[dict],
                          summary: dict, analysis: dict) -> str:
        prompt = self._build_prompt(findings, summary, analysis)
        system = (
            "You are a precise financial control analyst. You ground every "
            "statement in supplied evidence, never calculate new figures, and "
            "never treat estimated exposure as confirmed loss."
        )

        if provider["name"] == "anthropic":
            payload = {
                "model": provider["model"],
                "max_tokens": 2000,
                "temperature": 0.2,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            }
            headers = {
                "content-type": "application/json",
                "x-api-key": provider["key"],
                "anthropic-version": "2023-06-01",
            }
            extract = lambda data: data["content"][0]["text"]  # noqa: E731
        else:
            payload = {
                "model": provider["model"],
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            }
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {provider['key']}",
            }
            extract = lambda data: data["choices"][0]["message"]["content"]  # noqa: E731

        request = urllib.request.Request(
            provider["url"],
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )

        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))

        text = extract(data)
        if not text or not text.strip():
            raise ValueError("model returned an empty response")
        return text.strip()

    # ------------------------------------------------------------ brief

    def _build_brief(self, findings: list[dict], summary: dict, analysis: dict,
                     narrative: str | None, ai_status: str,
                     reason: str | None) -> str:
        money = lambda amount: f"INR {float(amount):,.2f}"  # noqa: E731
        severity_counts = {level: 0 for level in CONTRACT["severity_levels"]}
        for finding in findings:
            severity_counts[finding["severity"]] += 1

        gross = sum(float(f["risk_amount"]) for f in findings)
        worst_per_order: dict[str, float] = {}
        for finding in findings:
            key = finding.get("order_id") or finding.get("entity_id")
            worst_per_order[key] = max(
                worst_per_order.get(key, 0.0), float(finding["risk_amount"])
            )
        deduplicated = sum(worst_per_order.values())

        lines = [
            "# Revenue Assurance Daily Brief",
            "",
            f"**Run ID:** `{summary.get('run_id', 'unknown')}`  ",
            f"**Generated:** {datetime.now(timezone.utc).strftime('%d %B %Y %H:%M UTC')}  ",
            f"**Commentary source:** {self._describe_status(ai_status, reason)}  ",
            "",
            "---",
            "",
            "## Exposure",
            "",
            f"- **Estimated revenue at risk (deduplicated):** **{money(deduplicated)}**",
            f"- Gross flagged exposure: {money(gross)}",
            f"- Open cases: {len(findings)}",
            f"  - CRITICAL: {severity_counts['CRITICAL']}",
            f"  - HIGH: {severity_counts['HIGH']}",
            f"  - MEDIUM: {severity_counts['MEDIUM']}",
            f"  - LOW: {severity_counts['LOW']}",
            "",
            "> Every figure above is **estimated exposure** from a deterministic rule.",
            "> None of it is confirmed loss. Confirmed loss exists only after a human",
            "> review is recorded, and is reported separately.",
            "",
            "---",
            "",
        ]

        if narrative:
            lines += [narrative, "", "---", ""]

        lines += ["## Priority review queue", "",
                  "| # | Finding | Rule | Severity | Exposure | Due |",
                  "|---|---|---|---|---:|---|"]
        for position, case in enumerate(analysis["request_review"], start=1):
            lines.append(
                f"| {position} | `{case['finding_id']}` | {case['rule_code']} | "
                f"{case['severity']} | {money(case['risk_amount'])} | {case['due_date']} |"
            )

        clusters = analysis["group_related_cases"]
        if clusters:
            lines += ["", "---", "", "## Related case clusters", "",
                      "One root cause often trips several rules. Exposure per cluster is",
                      "deduplicated, because the same money must not be counted once per",
                      "rule that noticed it.", ""]
            for cluster in clusters[:5]:
                lines.append(
                    f"- **{cluster['group_key']}** — {len(cluster['finding_ids'])} cases "
                    f"({', '.join(cluster['rule_codes'])}); "
                    f"deduplicated exposure {money(cluster['deduplicated_risk_amount'])} "
                    f"(gross {money(cluster['gross_risk_amount'])})"
                )

        gaps = analysis["flag_missing_evidence"]
        lines += ["", "---", "", "## Evidence completeness", ""]
        if gaps:
            lines.append(
                f"{len(gaps)} case(s) cannot be concluded as they stand — a reviewer "
                "cannot reproduce the amount from the evidence supplied:"
            )
            lines.append("")
            for gap in gaps[:10]:
                detail = gap.get("missing_keys") or gap.get("empty_keys") or gap.get("detail")
                lines.append(f"- `{gap['finding_id']}` — {gap['issue']}: {detail}")
        else:
            lines.append("All findings carry the evidence their rule requires.")

        lines += ["", "---", "", "## Recommended next checks", ""]
        for item in analysis["recommend_next_check"]:
            lines.append(
                f"- **{item['rule_code']}** ({item['finding_count']} case(s)) — "
                f"{item['next_check']}"
            )

        lines += [
            "",
            "---",
            "",
            "## Governance",
            "",
            "This brief is advisory. The investigator that produced it:",
            "",
            "- has no database connection, credentials, or write path of any kind",
            "- cannot change a case's status, severity, exposure, or owner",
            "- cannot approve an action, issue a refund, or contact a customer",
            "- did not compute any figure above; all amounts come from SQL and",
            "  Python controls and are reproducible from their evidence",
            "",
            "Every case requiring action must be reviewed in the Excel review",
            "workbook. Decisions are validated before import and recorded",
            "append-only, so the original finding and the human judgement on it",
            "both survive.",
        ]

        return "\n".join(lines)

    @staticmethod
    def _describe_status(ai_status: str, reason: str | None) -> str:
        if ai_status == SUCCESS:
            return "AI-drafted commentary over deterministic analysis"
        if ai_status == DEGRADED:
            return f"deterministic template — AI call failed ({reason})"
        return f"deterministic template — {reason or 'AI not configured'}"


def main() -> None:
    # Loaded here, not at module level: this module must stay importable and
    # usable as a library without env-file side effects. The orchestrator loads
    # .env itself before importing this module; standalone invocation needs it
    # here instead.
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = AIInvestigator().investigate()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
