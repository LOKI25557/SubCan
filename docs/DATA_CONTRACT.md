# SubCan Data Contract

**Project Title:** SubCan: An AI-Powered Subscription Cancellation Agent  
**Version:** 0.1.0 (Phase 0: Foundation & Team Independence)  
**Standard:** Pydantic V2 / JSON Schema Draft 2020-12 / ISO 8601

---

## 1. Architectural Purpose & Module Ownership

The SubCan data contract defines the canonical data models and validation boundaries used across all components. Adherence to this contract ensures that:
- **Chinmay** (Connectors & Evidence) can output structured subscription and evidence data without relying on agent logic.
- **Loki** (Agent Orchestration & Decisions) can deterministically evaluate eligibility, generate cancellation plans, enforce approvals, and coordinate verification.
- **Dadu** (Database & UI) can build SQLite/PostgreSQL schemas and frontend dashboard tables directly against these stable schemas.

### Module Ownership Matrix

| Entity | Primary Producer | Primary Consumer | Shared File Reference |
|---|---|---|---|
| `Subscription` | **Chinmay** (Connectors) | Loki (Rules/Agent), Dadu (DB/UI) | [models.py](file:///backend/app/models.py) |
| `Evidence` | **Chinmay** (Connectors) | Loki (Rules), Dadu (DB/Audit) | [models.py](file:///backend/app/models.py) |
| `Decision` | **Loki** (Rules Engine) | Loki (Planner), Dadu (Dashboard) | [models.py](file:///backend/app/models.py) |
| `CancellationPlan` | **Loki** (Planner) | Loki (Approvals), Dadu (UI Review) | [models.py](file:///backend/app/models.py) |
| `Approval` | **Dadu / User** (UI) | Loki (Approval Validator / Actions) | [models.py](file:///backend/app/models.py) |
| `ActionResult` | **Loki** (Action Coordinator) | Loki (Verification), Dadu (DB) | [models.py](file:///backend/app/models.py) |
| `VerificationResult` | **Loki** (Verification) | Loki (AgentRun), Dadu (Reporting) | [models.py](file:///backend/app/models.py) |
| `AgentRun` | **Loki** (Agent Orchestrator) | Dadu (Reporting & History UI) | [models.py](file:///backend/app/models.py) |

---

## 2. Standard Types, Formats, and Conventions

1. **Date Format:** ISO 8601 calendar date `YYYY-MM-DD` (e.g. `2026-11-15`).
2. **Timestamp Format:** Timezone-aware ISO 8601 string `YYYY-MM-DDTHH:MM:SSZ` or with timezone offset `+HH:MM` (e.g. `2026-10-19T09:30:00Z`).
   - *Requirement:* Naive datetimes without timezone information are strictly rejected by `Evidence` validation.
3. **Missing Data Policy:**
   - **Never invent synthetic dates** for missing usage history. If user playback history is absent, `last_used_at` MUST be `null` (`None`).
   - If subscription state cannot be determined, set `status: "unknown"`.
4. **JSON Serialization:**
   - Serialized via `model.model_dump(mode='json')` in Python.
   - Deserialized via `ModelClass.model_validate(raw_dict)` or `ModelClass.model_validate_json(raw_json)`.

---

## 3. Standardised Status Enums

### 3.1. `SubscriptionStatus`
- `active`: Subscription is currently billed and valid.
- `cancelled`: Subscription has terminated billing and access.
- `unknown`: Unable to confirm status from connector.

### 3.2. `EligibilityStatus`
- `candidate`: Inactive for >= 3 calendar months (approx. 90 days), recommended for cancellation review.
- `not_eligible`: Active usage within 90 days or non-cancellable contract.
- `needs_review`: Ambiguous logs or contradictory evidence requiring human review.

### 3.3. `ApprovalStatus`
- `awaiting_approval`: Plan generated, awaiting explicit human approval.
- `approved`: User explicitly approved cancellation for this exact service and plan version.
- `declined`: User rejected cancellation (subscription kept).
- `invalidated`: Superseded by newer plan or altered conditions.

### 3.4. `ActionStatus`
- `pending`: Action scheduled.
- `executing`: In-flight API or mock interaction.
- `succeeded`: Execution routine finished without errors.
- `failed`: Execution failed.
- `blocked`: Action halted due to safety guard (e.g. missing approval).

> [!CAUTION]
> **Safety Invariant:** `ActionStatus.SUCCEEDED` DOES NOT equate to verified cancellation. Independent verification is always required.

### 3.5. `VerificationStatus`
- `verified`: Independent renewal query confirms cancellation.
- `failed`: Renewal query reveals subscription remains active or renewed.
- `pending`: Verification check queued or awaiting sync.

### 3.6. `FinalResultStatus`
- `verified_cancelled`: Successfully verified as cancelled.
- `kept`: Retained per user decision or policy.
- `blocked`: Execution prevented due to lack of approval or policy tripwire.
- `failed`: Action or verification failed.
- `pending`: Run in progress.

---

## 4. Entity Schemas

### 4.1. `Subscription`
| Field | Type | Required | Description |
|---|---|---|---|
| `subscription_id` | `str` | Yes | Unique identifier (e.g. `sub_tc1_cinevault`). |
| `service_name` | `str` | Yes | Service name (e.g. `CineVault Mock Streaming`). |
| `status` | `SubscriptionStatus` | Yes | Current state (`active`, `cancelled`, `unknown`). |
| `renewal_date` | `date` | No | Next renewal date (ISO `YYYY-MM-DD`). |
| `last_used_at` | `datetime` | No | Last streaming activity timestamp. `null` if unknown. |
| `source_url` | `str` | No | Connector or mock site URL. |
| `source_record_id` | `str` | No | Upstream account or customer ID. |
| `monthly_cost` | `float` | No | Cost per billing cycle. |
| `currency` | `str` | No | Currency code (default: `"USD"`). |
| `billing_cycle` | `str` | No | Cadence (`"monthly"`, `"annual"`). |
| `cancellation_terms_url` | `str` | No | Link to cancellation policy. |
| `data_deletion_warning` | `bool` | No | `true` if cancellation purges user data immediately. |
| `pause_option_available` | `bool` | No | `true` if service offers billing pause. |

### 4.2. `Evidence`
| Field | Type | Required | Description |
|---|---|---|---|
| `evidence_id` | `str` | Yes | Unique ID (e.g. `ev_tc1_01`). |
| `subscription_id` | `str` | Yes | Foreign key to `Subscription.subscription_id`. |
| `evidence_type` | `str` | Yes | Category (`usage_history`, `cancellation_terms`, `pause_policy`). |
| `excerpt` | `str` | Yes | Raw textual excerpt or log quote. |
| `source_url` | `str` | No | Originating source URL. |
| `captured_at` | `datetime` | Yes | Timezone-aware collection timestamp. |
| `confidence` | `float` | Yes | Range `[0.0, 1.0]`. |
| `raw_reference` | `dict` | No | Raw connector payload or clause reference. |

### 4.3. `Decision`
| Field | Type | Required | Description |
|---|---|---|---|
| `decision_id` | `str` | Yes | Unique decision ID. |
| `subscription_id` | `str` | Yes | Foreign key to `Subscription.subscription_id`. |
| `eligibility` | `EligibilityStatus` | Yes | `candidate`, `not_eligible`, `needs_review`. |
| `reason` | `str` | Yes | Detailed rationale. |
| `uncertainty` | `float` | Yes | Range `[0.0, 1.0]`. |
| `rule_version` | `str` | Yes | Rule version (e.g. `"1.0.0"`). |
| `evaluated_at` | `datetime` | Yes | Timestamp of rule evaluation. |
| `data_deletion_risk` | `bool` | No | Disclose if data loss will occur. |
| `pause_alternative_available` | `bool` | No | Disclose if pause option exists. |
| `consequences` | `List[str]` | No | List of explicit consequence warnings. |

### 4.4. `CancellationPlan`
| Field | Type | Required | Description |
|---|---|---|---|
| `plan_id` | `str` | Yes | Unique plan identifier. |
| `subscription_id` | `str` | Yes | Target subscription ID. |
| `service_name` | `str` | Yes | Explicit service name. |
| `plan_version` | `str` | Yes | Plan version string (e.g. `"v1.0"`). |
| `steps` | `List[str]` | Yes | Stepwise execution sequence. |
| `consequences_warning` | `str` | No | Warning banner text (e.g. for TC2). |
| `pause_alternative_available` | `bool` | No | Disclose pause option (for TC3). |
| `status` | `ApprovalStatus` | Yes | Defaults to `awaiting_approval`. |
| `created_at` | `datetime` | Yes | Timestamp plan was generated. |

### 4.5. `Approval`
| Field | Type | Required | Description |
|---|---|---|---|
| `approval_id` | `str` | Yes | Unique approval identifier. |
| `plan_id` | `str` | Yes | Must match `CancellationPlan.plan_id`. |
| `subscription_id` | `str` | Yes | Must match `CancellationPlan.subscription_id`. |
| `service_name` | `str` | Yes | Must match `CancellationPlan.service_name`. |
| `plan_version` | `str` | Yes | Must match `CancellationPlan.plan_version`. |
| `status` | `ApprovalStatus` | Yes | `approved`, `declined`, `invalidated`. |
| `approved_by` | `str` | No | User identifier. |
| `decided_at` | `datetime` | No | Timestamp of human decision. |
| `notes` | `str` | No | User rationale or decline note. |

### 4.6. `ActionResult`
| Field | Type | Required | Description |
|---|---|---|---|
| `action_id` | `str` | Yes | Unique action execution ID. |
| `plan_id` | `str` | Yes | Target plan ID. |
| `subscription_id` | `str` | Yes | Target subscription ID. |
| `status` | `ActionStatus` | Yes | `succeeded`, `failed`, `blocked`, etc. |
| `started_at` | `datetime` | No | Start timestamp. |
| `completed_at` | `datetime` | No | Completion timestamp. |
| `message` | `str` | No | Summary message or error reason. |
| `action_type` | `str` | Yes | Default: `"cancellation"`. |
| `logs` | `List[str]` | Yes | Step execution log lines. |

### 4.7. `VerificationResult`
| Field | Type | Required | Description |
|---|---|---|---|
| `verification_id` | `str` | Yes | Unique verification ID. |
| `action_id` | `str` | Yes | Foreign key to `ActionResult.action_id`. |
| `subscription_id` | `str` | Yes | Foreign key to `Subscription.subscription_id`. |
| `status` | `VerificationStatus` | Yes | `verified`, `failed`, `pending`. |
| `method` | `str` | Yes | Check method (e.g. `renewal_status_check`). |
| `verified_at` | `datetime` | No | Timestamp of check. |
| `final_state_observed` | `str` | No | Status observed on provider (e.g. `cancelled`, `paused`). |
| `details` | `str` | No | Observational notes or error report. |

### 4.8. `AgentRun`
| Field | Type | Required | Description |
|---|---|---|---|
| `run_id` | `str` | Yes | Unique run identifier. |
| `started_at` | `datetime` | Yes | Run start timestamp. |
| `completed_at` | `datetime` | No | Run completion timestamp. |
| `status` | `str` | Yes | `"pending"`, `"in_progress"`, `"completed"`. |
| `subscription_ids` | `List[str]` | Yes | List of subscription IDs in scope. |
| `final_results` | `Dict[str, FinalResultStatus]` | Yes | Map of ID -> FinalResultStatus. |
| `summary` | `str` | No | Human-readable audit summary. |

---

## 5. Handling Missing or Contradictory Evidence

1. **Missing Usage Logs:**
   If a connector cannot retrieve watch/listen history:
   - `Subscription.last_used_at` = `null`.
   - `Decision.eligibility` = `EligibilityStatus.NEEDS_REVIEW`.
   - `Decision.uncertainty` = `1.0`.
   - The system MUST NEVER fabricate an artificial inactivity date.

2. **Contradictory Evidence:**
   When multiple evidence sources yield conflicting information (e.g. billing active vs playback dormant vs recent login):
   - Multiple `Evidence` objects are persisted, preserving each source's URL, timestamp, and confidence.
   - Evaluator records an elevated `uncertainty` score.
   - Requires explicit human resolution before planning.

3. **Preserving Provenance:**
   - Every `Evidence` record maintains `source_url`, `captured_at`, and `raw_reference` so decisions can be audited back to the raw source data collected by Chinmay's connector layer.
