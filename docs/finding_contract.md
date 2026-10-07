# Shared Finding Contract v1.0.0

Every exception in this system — whether detected by SQL controls, the Python leakage
engine, or the reconciliation module — is expressed in one structure. This document is
the authority. Code and SQL must match it, not the other way round.

The machine-readable form lives in `config/contract.json`. That file is what validators
and loaders read; this document explains the reasoning.

---

## 1. Run identity

Every pipeline execution gets one `run_id`, generated once by the orchestrator and passed
down to every module.

```
Format:  RUN-YYYYMMDD-NNN
Example: RUN-20260912-001
Type:    VARCHAR(20)
```

`NNN` is a zero-padded sequence within the calendar day, starting at `001`. The
orchestrator derives it by counting existing runs for that date in
`capstone.fact_pipeline_runs`, so a second run on the same day becomes `-002`.

**Why a string and not an identity integer.** The two source projects disagreed:
`SQL_Project` used `BIGINT GENERATED ALWAYS AS IDENTITY`, `python_project` used a string.
A string wins because the run_id must be generated *before* any database write (the
leakage engine runs on CSVs and may never reach Postgres if validation fails), and
because it is human-readable in filenames, JSON exports, and Power BI slicers. An
identity column cannot be known ahead of the insert.

**Rerun semantics.** Re-executing an existing `run_id` is explicitly supported and must
not duplicate findings. See §7.

---

## 2. Finding identity

```
Format:  <PREFIX>-<YYYYMMDD>-<NNNNN>
Example: LK-20260912-00001
Type:    VARCHAR(32)
```

Prefix encodes the detecting module so a finding's origin is legible without a join:

| Prefix | Source module | Detector |
|---|---|---|
| `KPI` | `KPI` | SQL target-variance controls |
| `DQ` | `DATA_QUALITY` | SQL data-quality controls |
| `LK` | `LEAKAGE` | Python revenue-leakage rules |
| `RC` | `RECONCILIATION` | SQL bank-to-ledger controls |

`NNNNN` is a per-module, per-run counter starting at `00001`.

**Why this replaces `RL-2026-00001`.** The old python_project scheme hardcoded the year
and carried no date, so two runs in different months could collide on counter reuse. It
also had no room for the three other modules. The new scheme is collision-free across
modules and days.

### Natural key (the real uniqueness guarantee)

`finding_id` is a *label*, not the deduplication key. The counter depends on row
iteration order, so the same underlying exception can receive a different `finding_id`
on a rerun. Idempotency is enforced instead on:

```
(run_id, rule_code, entity_type, entity_id, evidence_hash)
```

where `evidence_hash` is the SHA-256 of the canonical JSON serialization of
`evidence_json` (sorted keys, no whitespace, `detected_at` excluded). This is a UNIQUE
constraint in `fact_findings`. Loaders use `ON CONFLICT DO NOTHING` against it.

`detected_at` is excluded from the hash deliberately: a rerun produces a new timestamp
but the same exception, and hashing the timestamp would defeat the whole mechanism.

---

## 3. Severity

Four levels, uppercase:

```
CRITICAL | HIGH | MEDIUM | LOW
```

Uppercase is inherited from `python_project`, which already emits uppercase across six
rules. `SQL_Project` used lowercase (`low`/`medium`/`high`) and had no `CRITICAL`, so the
SQL side is the one that adapts — a smaller change than rewriting the leakage engine.

Amount thresholds stay in `config/policies.json` (unchanged from python_project, so its
existing rule behaviour is preserved):

| Severity | Rule |
|---|---|
| CRITICAL | `risk_amount >= 100000` **or** an explicit rule escalation |
| HIGH | `risk_amount >= 25000` |
| MEDIUM | `risk_amount >= 5000` |
| LOW | below 5000 |

Rule escalations that bypass the amount ladder (also unchanged): a `DELIVERED_UNPAID`
finding against an Enterprise customer is always CRITICAL, because the credit exposure
matters more than the invoice size.

KPI and reconciliation findings map to this ladder through their own rule definitions in
`config/contract.json`, since a KPI variance has no single "risk amount" in the same
sense — see §5.

---

## 4. Case status workflow

```
OPEN
  └─> ASSIGNED
        └─> UNDER_REVIEW
              ├─> FALSE_POSITIVE ─────────> RESOLVED
              │                    └──────> UNDER_REVIEW   (re-examine)
              ├─> MORE_INFORMATION_REQUIRED ─> UNDER_REVIEW
              └─> VALID_EXCEPTION
                    ├─> ACTION_APPROVED ──> RESOLVED
                    ├─> MORE_INFORMATION_REQUIRED          (revise)
                    └─> FALSE_POSITIVE                     (revise)
```

Legal transitions are enumerated in `config/contract.json` under `status_transitions`.
Any transition not listed is rejected by the decision importer before it touches the
database.

**A decision may be revised until it is approved or resolved.** Reviewers get things
wrong, and a graph that forbids correction forces them either to approve a case they
have come to doubt or to leave it stuck — both worse outcomes than an audited change of
mind. Correction is safe precisely because `fact_reviews` is append-only: the original
judgement survives next to the revision, so the trail shows what was thought, when, and
what changed it.

`ACTION_APPROVED` remains reachable only from `VALID_EXCEPTION`. Approval is a real
control gate, so it cannot be skipped — a single spreadsheet row must not be able to both
raise a finding and approve its own remediation.

**Implicit pickup.** The workbook has no "claim this case" step, so a reviewer submitting
a decision on an `OPEN` case is normal rather than a violation. The importer records
`OPEN → UNDER_REVIEW` as its own event before applying the decision, so the history reads
as a real sequence instead of a case leaping from untouched to decided. Only that one hop
is inferred; nothing else about the chain is assumed.

Rules that hold regardless of who is asking:

- Detection modules may only ever write `OPEN`.
- The AI investigator may write **no** status. Not one. It has no write path at all.
- Only a validated human decision moves a case out of `OPEN`.
- `RESOLVED` is terminal. Reopening means a new finding, not an edited one.
- `FALSE_POSITIVE` still requires a reason string — an unexplained dismissal is how
  control systems quietly rot.

Status lives in `fact_findings.status` as the current value, and every transition is
appended to `fact_finding_events`. The events table is the audit trail; the column is
the convenience.

---

## 5. Rule registry

Every `rule_code` is registered in `config/contract.json` with its module, default
severity, entity type, and whether approval is mandatory. Detection code may not invent a
`rule_code` at runtime — an unregistered code is a load-time error, which prevents
silent drift between what the engine emits and what the dashboard can explain.

### Leakage rules (6, ported unchanged from python_project)

| rule_code | entity_type | Exposure basis |
|---|---|---|
| `DUPLICATE_REFUND` | `REFUND` | Refund amount issued twice |
| `REFUND_EXCEEDS_PAYMENT` | `ORDER` | Refunds minus collected payments |
| `EXCESSIVE_DISCOUNT` | `ORDER` | Discount above policy ceiling |
| `DELIVERED_UNPAID` | `ORDER` | Delivered value minus collected payments |
| `PRICING_ERROR` | `ORDER_ITEM` | (minimum price − charged price) × quantity |
| `UNUSUAL_GATEWAY_FEE` | `PAYMENT` | Actual fee minus standard-rate fee |

### Reconciliation rules (5, net new)

| rule_code | entity_type | Exposure basis |
|---|---|---|
| `BANK_ONLY_TRANSACTION` | `BANK_TXN` | Full bank amount (cash in, unexplained) |
| `LEDGER_ONLY_TRANSACTION` | `LEDGER_TXN` | Full ledger amount (booked, not banked) |
| `AMOUNT_MISMATCH` | `BANK_TXN` | Absolute difference |
| `DATE_MISMATCH` | `BANK_TXN` | `0` — timing break, not a loss |
| `DUPLICATE_SETTLEMENT` | `BANK_TXN` | Value of the extra settlement |

`DATE_MISMATCH` carrying `risk_amount = 0` is intentional. A settlement that landed two
days late is a control failure worth investigating and an SLA input, but booking it as
financial exposure would inflate the headline "revenue at risk" number with money that
was never actually lost. Being able to defend that distinction in an interview is worth
more than a bigger number on a dashboard.

### Data-quality rules (SQL, ported)

| rule_code | entity_type | Exposure basis |
|---|---|---|
| `ORDER_WITHOUT_PAYMENT` | `ORDER` | Order final amount |
| `PAYMENT_AMOUNT_MISMATCH` | `PAYMENT` | Absolute difference |
| `ORPHAN_PAYMENT` | `PAYMENT` | Payment amount |
| `ORPHAN_REFUND` | `REFUND` | Refund amount |
| `NEGATIVE_AMOUNT` | varies | Absolute value |
| `DUPLICATE_PRIMARY_KEY` | varies | `0` |
| `UNKNOWN_CUSTOMER_REFERENCE` | `ORDER` | `0` |

### KPI rules (SQL, ported)

| rule_code | entity_type | Exposure basis |
|---|---|---|
| `REVENUE_BELOW_TARGET` | `KPI_DATE` | Shortfall vs target |
| `PAYMENT_SUCCESS_BELOW_TARGET` | `KPI_DATE` | `0` — rate gap, not an amount |
| `ORDER_COUNT_BELOW_TARGET` | `KPI_DATE` | `0` |
| `REFUND_RATE_ABOVE_THRESHOLD` | `KPI_DATE` | Refund value above threshold |

`entity_id` for a KPI finding is the metric date in `YYYY-MM-DD` form. A KPI exception is
about a *day*, not a record, and forcing it into a fake order ID would break drill-through.

---

## 6. Finding record

```jsonc
{
  "finding_id":        "LK-20260912-00001",   // label, VARCHAR(32)
  "run_id":            "RUN-20260912-001",     // VARCHAR(20), FK -> fact_pipeline_runs
  "source_module":     "LEAKAGE",              // KPI|DATA_QUALITY|LEAKAGE|RECONCILIATION
  "rule_code":         "DUPLICATE_REFUND",     // must exist in contract.json
  "entity_type":       "REFUND",               // see rule registry
  "entity_id":         "REF-000412",           // the affected record
  "order_id":          "ORD-00193",            // nullable context
  "customer_id":       "CUS-0015",             // nullable context
  "detected_at":       "2026-09-12T10:39:13Z", // UTC, ISO 8601, Z-suffixed
  "business_date":     "2026-09-11",           // date the exception belongs to
  "severity":          "HIGH",
  "risk_amount":       2500.00,                // estimated exposure, NEVER confirmed loss
  "currency":          "INR",
  "priority_score":    78.4,                   // 0-100
  "days_open":         1,
  "evidence_json":     { },                    // see below
  "evidence_hash":     "9f2b...",              // SHA-256, computed by the emitter
  "recommended_action":"Review gateway settlement and initiate duplicate reversal",
  "approval_required": true,
  "status":            "OPEN",                 // detectors may only write OPEN
  "owner":             null,                   // assigned later by a human
  "due_date":          "2026-09-15"            // detected_at + rule SLA days
}
```

### `risk_amount` is estimated exposure, not loss

This is the single most important semantic rule in the system, and the one most likely to
be probed in an interview.

- `risk_amount` — what the rule *thinks* might be at risk. Automated. Unconfirmed.
- `confirmed_loss` — what a human confirmed after review. Lives in `fact_reviews`, never
  in `fact_findings`.
- `recovered_amount` — what was actually clawed back. Also `fact_reviews`.

They are never summed together and never displayed in the same measure. A dashboard tile
labelled "Revenue at Risk" reads from `risk_amount`; one labelled "Confirmed Loss" reads
from `fact_reviews`. Conflating them is how a control dashboard starts lying.

### `evidence_json`

Free-form per rule, but every emitter must satisfy three requirements:

1. **Sufficient to reproduce the calculation by hand.** A reviewer who reads only the
   evidence must be able to arrive at the same `risk_amount`. If it contains a
   difference, it contains both operands.
2. **Flat scalars only** — strings, numbers, booleans, ISO dates. No nested objects, no
   arrays. This keeps the hash stable and the Excel evidence sheet renderable.
3. **No PII beyond IDs.** Customer *names*, emails, and addresses never enter evidence,
   because evidence is exactly the payload that gets shipped to an external AI API.

Required keys per rule are declared in `config/contract.json` under
`rules[].required_evidence_keys`, and are enforced at load time. A finding missing a
required key is rejected rather than loaded half-formed — this is also what powers the AI
investigator's `flag_missing_evidence` action.

### Exposure deduplication

`risk_amount` sums are double-counted by construction: one order can trip
`EXCESSIVE_DISCOUNT` and `DELIVERED_UNPAID` at once, and adding both overstates the
exposure. python_project already solved this by taking the max `risk_amount` per
`order_id`. That logic is preserved and formalised:

```
gross_exposure         = SUM(risk_amount)                      -- every finding
deduplicated_exposure  = SUM(MAX(risk_amount) per order_id)     -- the defensible figure
```

Power BI headline tiles use `deduplicated_exposure`. `gross_exposure` is shown only in
rule-level breakdowns, where double-counting is expected and understood.

---

## 7. Idempotency

Re-running the same `run_id` must be safe. Three mechanisms:

1. `fact_findings` has `UNIQUE (run_id, rule_code, entity_type, entity_id, evidence_hash)`
   and loaders insert with `ON CONFLICT DO NOTHING`.
2. `fact_pipeline_runs` is upserted on `run_id`, not blindly inserted.
3. `fact_reviews` is **append-only and never deduplicated** — a second review of the same
   finding is a legitimate event (escalation, correction), not a duplicate. Current state
   is derived by taking the latest `reviewed_at` per finding, not by overwriting rows.

A rerun therefore converges to the same finding set without erasing human decisions
already recorded against it. That combination — deterministic detection, preserved
judgement — is the property that makes the audit trail worth anything.

---

## 8. What the AI investigator may do

Allowed actions, and nothing else:

```
draft_commentary
recommend_next_check
request_review
flag_missing_evidence
group_related_cases
prepare_management_summary
```

Hard prohibitions, enforced by architecture rather than by prompt instructions:

- No database connection. The investigator process receives a JSON file path and writes
  Markdown. It has no driver, no credentials, no socket.
- No mutation of `risk_amount`, `severity`, `priority_score`, or `status`.
- No recalculation of official KPIs — those are SQL's output and SQL's alone.
- No claim that estimated exposure is confirmed loss.
- No invented evidence. Its input is the evidence file; anything not in it does not exist.

AI output lands in `outputs/executive_brief.md` and
`outputs/ai_commentary.json`, both of which are *annotations*. Nothing downstream treats
them as facts, and Power BI never reads them.

Unavailability is a normal condition, not an error. Missing API key, network failure,
rate limit, malformed response — each degrades to a deterministic template brief built
from the findings themselves. The pipeline exit code does not change. A finance control
system that stops controlling because a language model is rate-limited is not a control
system.

---

## 9. Canonical dataset

`python_project`'s schema is canonical, because it is the richer superset — it carries
`order_items`, `refunds`, `shipments`, `fee_rules`, `product_prices`, and `gateway_fee`,
none of which exist in SQL_Project. The SQL KPI and quality logic is re-pointed at these
column names; the logic itself is preserved.

| Concept | Canonical | Was in SQL_Project |
|---|---|---|
| Order value before discount | `gross_amount` | `order_amount` |
| Order value after discount+tax | `final_amount` | *(derived)* |
| Customer segment | `customer_segment` | `segment` |
| Payment success literal | `'Successful'` | `'success'` |
| Order completion literal | `'Completed'` | `'completed'` |
| Refund processed literal | `'Processed'` | *(absent)* |

Files: `customers`, `orders`, `order_items`, `payments`, `refunds`, `shipments`,
`product_prices`, `fee_rules` come from python_project unchanged.
`daily_kpi_targets` comes from SQL_Project but **must be regenerated** — its existing
rows cover January 2026 while the canonical order data sits in September 2026, so
every KPI comparison would otherwise be a null join.
`bank_transactions` and `ledger_transactions` are net new.

---

## 10. Changing this contract

The contract is versioned. `config/contract.json` carries `version`, and
`fact_findings.contract_version` records which version produced each row.

Additive changes (a new rule, a new optional field) bump the minor version. Anything that
changes the meaning of an existing field, the status graph, or the natural key bumps the
major version and requires backfill reasoning, because historical findings were written
under the old meaning and silently reinterpreting them corrupts the trend lines that make
the dashboard useful.
