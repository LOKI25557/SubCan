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

### 3.7. `DiscoverySource`
- `connector`: Ingested via mock/direct provider connector.
- `email`: Discovered from scanned email receipts/invoices.
- `manual`: Manually registered by user through inventory dashboard.

### 3.8. `CandidateReviewStatus`
- `pending`: Discovered email candidate awaiting user review.
- `confirmed`: Accepted by user and promoted into main subscription inventory.
- `dismissed`: Rejected/ignored by user without creating a subscription record.

---

## 4. Entity Schemas

### 4.1. `Subscription`
| Field | Type | Required | Description |
|---|---|---|---|
| `subscription_id` | `str` | Yes | Unique identifier (e.g. `sub_tc1_cinevault` or `sub_man_...`). |
| `service_name` | `str` | Yes | Service name (e.g. `CineVault Mock Streaming`). |
| `status` | `SubscriptionStatus` | Yes | Current state (`active`, `cancelled`, `unknown`). |
| `renewal_date` | `date` | No | Next renewal date (ISO `YYYY-MM-DD`). |
| `last_used_at` | `datetime` | No | Last activity timestamp. `null` if unknown. |
| `source_url` | `str` | No | Connector, portal, or receipt reference URL. |
| `source_record_id` | `str` | No | Upstream account, email message, or customer ID. |
| `monthly_cost` | `float` | No | Cost per billing cycle (>= 0). |
| `currency` | `str` | No | Currency code (default: `"USD"`). |
| `billing_cycle` | `str` | No | Cadence (`"monthly"`, `"annual"`, `"quarterly"`). |
| `cancellation_terms_url` | `str` | No | Link to cancellation policy. |
| `data_deletion_warning` | `bool` | No | `true` if cancellation purges user data immediately. |
| `pause_option_available` | `bool` | No | `true` if service offers billing pause. |
| `user_id` | `str` | No | Owner user ID for tenant isolation (`user_default` fallback). |
| `category` | `str` | No | Subscription category (`streaming`, `software`, `utility`, `news`, `gym`, `other`). |
| `discovery_source` | `DiscoverySource` | No | Discovery origin (`connector`, `email`, `manual`). |
| `confidence` | `float` | No | Extraction confidence score (`0.0` - `1.0`). |
| `notes` | `str` | No | User-provided notes or parsing remarks. |
| `is_archived` | `bool` | No | Soft-delete status (`false` by default). |
| `manual_action_url` | `str` | No | Official direct portal cancellation URL for restricted services. |
| `manual_action_instructions` | `str` | No | Safe human guidance steps for restricted/in-person services. |
| `created_at` | `datetime` | No | Creation timestamp. |
| `updated_at` | `datetime` | No | Last modification timestamp. |

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

### 4.9. `SubscriptionCandidate`
| Field | Type | Required | Description |
|---|---|---|---|
| `candidate_id` | `str` | Yes | Unique candidate identifier (`cand_<uid>_<svc>_<msgid>`). |
| `user_id` | `str` | Yes | Scoped owner user ID. |
| `email_account` | `str` | Yes | Connected email address. |
| `service_name` | `str` | Yes | Extracted service / merchant name. |
| `detected_amount` | `float` | No | Extracted billing amount (>= 0). |
| `currency` | `str` | No | Billing currency code (default: `"USD"`). |
| `billing_cycle` | `str` | No | Inferred interval (`"monthly"`, `"annual"`). |
| `renewal_date` | `date` | No | Detected next renewal or cycle date. |
| `sender_email` | `str` | No | Source email address of sender. |
| `subject_line` | `str` | No | Email subject line. |
| `message_date` | `datetime` | No | Email received timestamp. |
| `confidence` | `float` | Yes | Extraction confidence (`0.0` - `1.0`). |
| `status` | `CandidateReviewStatus`| Yes | Review state (`pending`, `confirmed`, `dismissed`). |
| `evidence_snippet` | `str` | No | Sanitized contextual text excerpt from email body. |
| `raw_message_id` | `str` | No | Email provider message ID. |
| `created_at` | `datetime` | No | Discovery timestamp. |

### 4.10. `EmailAccountStatus`
| Field | Type | Required | Description |
|---|---|---|---|
| `connected` | `bool` | Yes | `true` if an account is linked with valid tokens. |
| `email` | `str` | No | Linked email address. |
| `provider` | `str` | No | OAuth provider (`"google"`). |
| `last_synced_at` | `datetime` | No | Timestamp of most recent mailbox sync. |
| `candidate_count` | `int` | No | Total candidates discovered. |
| `pending_candidate_count`| `int` | No | Discovered candidates awaiting confirmation. |

### 4.11. `SubscriptionCreateRequest`
| Field | Type | Required | Description |
|---|---|---|---|
| `service_name` | `str` | Yes | Non-blank service name (min length: 1). |
| `monthly_cost` | `float` | No | Monthly cost (>= 0.0). |
| `currency` | `str` | No | Currency code (default: `"USD"`). |
| `billing_cycle` | `str` | No | Cadence (`"monthly"`, `"annual"`, `"quarterly"`). |
| `renewal_date` | `date` | No | Next renewal date (`YYYY-MM-DD`). |
| `last_used_at` | `datetime` | No | Last usage activity date. |
| `category` | `str` | No | Category (`"streaming"`, `"software"`, `"utility"`, etc.). |
| `cancellation_terms_url` | `str` | No | Policy URL. |
| `notes` | `str` | No | User notes. |
| `manual_action_url` | `str` | No | Custom or official portal cancellation URL. |
| `manual_action_instructions` | `str` | No | Guidance notes for cancellation. |

### 4.12. `SubscriptionUpdateRequest`
| Field | Type | Required | Description |
|---|---|---|---|
| `service_name` | `str` | No | Updated service name. |
| `status` | `SubscriptionStatus` | No | Updated status (`active`, `cancelled`, `unknown`). |
| `renewal_date` | `date` | No | Updated renewal date. |
| `last_used_at` | `datetime` | No | Updated last activity timestamp. |
| `monthly_cost` | `float` | No | Updated cost (>= 0.0). |
| `currency` | `str` | No | Updated currency. |
| `billing_cycle` | `str` | No | Updated billing cycle. |
| `category` | `str` | No | Updated category. |
| `notes` | `str` | No | Updated notes. |
| `manual_action_url` | `str` | No | Updated manual URL. |
| `manual_action_instructions` | `str` | No | Updated instructions. |

### 4.13. `SubscriptionCandidate`
| Field | Type | Required | Description |
|---|---|---|---|
| `candidate_id` | `str` | Yes | Unique ID (e.g. `cand_user_netflix_001`). |
| `user_id` | `str` | Yes | Tenant or user owning this candidate. |
| `merchant_name` | `str` | Yes | Extracted merchant or service name. |
| `product_name` | `str` | No | Detected product or tier (e.g. Premium). |
| `estimated_cost` | `float` | No | Extracted recurring billing cost. |
| `currency` | `str` | No | Extracted currency (default `"USD"`). |
| `billing_interval` | `str` | No | Detected cadence (`monthly`, `annual`, etc.). |
| `detected_renewal_date` | `date` | No | Extracted next renewal date. |
| `detected_last_used` | `datetime` | No | Detected date of receipt email. |
| `confidence` | `float` | Yes | Extraction confidence (0.0 to 1.0). |
| `review_status` | `str` | Yes | Status (`pending`, `confirmed`, `dismissed`). |
| `source_message_id` | `str` | Yes | Provenance Gmail message ID. |
| `source_subject` | `str` | Yes | Sanitized subject line of receipt. |
| `source_sender` | `str` | Yes | Sanitized sender address. |
| `email_snippet` | `str` | Yes | XSS-sanitized body evidence excerpt. |
| `cancellation_signal` | `bool` | Yes | Flag indicating receipt is a cancellation/refund. |

### 4.14. `EmailAccountStatus`
| Field | Type | Required | Description |
|---|---|---|---|
| `user_id` | `str` | Yes | Tenant or user identifier. |
| `email_address` | `str` | No | Connected Gmail address. |
| `is_connected` | `bool` | Yes | Whether OAuth token is active and valid. |
| `provider` | `str` | Yes | Provider name (`"gmail"`). |
| `last_synced_at` | `datetime` | No | Timestamp of latest mailbox sync. |
| `sync_status` | `str` | Yes | Status (`"idle"`, `"syncing"`, `"failed"`). |
| `candidate_count` | `int` | Yes | Total discovered candidates in review queue. |
| `error_message` | `str` | No | Sanitized error message if sync failed. |

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
