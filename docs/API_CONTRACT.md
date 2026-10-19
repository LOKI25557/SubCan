# SubCan API Contract

**Project Title:** SubCan: An AI-Powered Subscription Cancellation Agent  
**Tagline:** Detect. Decide. Approve. Cancel. Verify.  
**Version:** 0.1.0 (Phase 0: Foundation & Team Independence)  
**Base URL:** `http://127.0.0.1:8000`  
**Interactive Docs:** `/docs` (Swagger UI), `/redoc` (ReDoc)

---

## 1. Scope & Implementation Status

| Endpoint | Method | Phase Status | Primary Module Owner |
|---|---|---|---|
| `/health` | `GET` | **IMPLEMENTED (Phase 0)** | Loki (Backend Starter) |
| `/runs` | `POST` | *Planned (Phase 3)* | Loki (Agent Orchestrator) |
| `/runs/{run_id}` | `GET` | *Planned (Phase 3)* | Loki / Dadu |
| `/runs/{run_id}/subscriptions` | `GET` | *Planned (Phase 3)* | Dadu (Persistence / Queries) |
| `/plans/{plan_id}/approve` | `POST` | *Planned (Phase 1)* | Loki (Approvals) |
| `/plans/{plan_id}/decline` | `POST` | *Planned (Phase 1)* | Loki (Approvals) |
| `/actions/{action_id}/verify` | `POST` | *Planned (Phase 2)* | Loki (Verification) |
| `/runs/{run_id}/report` | `GET` | *Planned (Phase 3)* | Dadu / Loki (Reporting) |

> [!IMPORTANT]
> During Phase 0, only `GET /health` is functional. All other endpoints are planned contracts designed to allow Chinmay (connectors) and Dadu (database/UI) to build their respective integrations with zero ambiguity.

---

## 2. Standard Error Envelope

All API errors return a consistent RFC-7807 compatible error envelope:

```json
{
  "error": {
    "code": "RESOURCE_NOT_FOUND",
    "message": "Human-readable description of what went wrong.",
    "details": {},
    "timestamp": "2026-10-19T10:00:00Z"
  }
}
```

Standard HTTP status codes:
- `200 OK`: Request succeeded.
- `201 Created`: Resource successfully created.
- `400 Bad Request`: Validation failure or malformed payload.
- `404 Not Found`: Specified identifier does not exist.
- `409 Conflict`: Invalid state transition (e.g. approving an already invalidated plan).
- `422 Unprocessable Entity`: Pydantic schema validation error.

---

## 3. Endpoints Specification

### 3.1. `GET /health`

**Status:** IMPLEMENTED in Phase 0.

- **Purpose:** Verifies backend service availability, version, and operational health.
- **Request:**
  - Headers: `Accept: application/json`
  - Parameters: None
  - Body: None
- **Response (200 OK):**
  ```json
  {
    "status": "healthy",
    "app_title": "SubCan: An AI-Powered Subscription Cancellation Agent",
    "version": "0.1.0",
    "phase": "Phase 0: Foundation & Team Independence",
    "timestamp": "2026-10-19T09:30:00.123456Z"
  }
  ```

---

### 3.2. `POST /runs`

**Status:** Planned (Phase 3).

- **Purpose:** Initiates a new AI-agent orchestration run across identified subscriptions.
- **Request Body:**
  ```json
  {
    "subscription_ids": ["sub_tc1_cinevault", "sub_tc2_audiocloud"],
    "rule_version": "1.0.0",
    "dry_run": false
  }
  ```
- **Response (201 Created):**
  ```json
  {
    "run_id": "run_20261019_001",
    "started_at": "2026-10-19T09:31:00Z",
    "status": "pending",
    "subscription_ids": ["sub_tc1_cinevault", "sub_tc2_audiocloud"],
    "final_results": {}
  }
  ```
- **Error Behavior:**
  - `400 Bad Request` if `subscription_ids` is empty or invalid.

---

### 3.3. `GET /runs/{run_id}`

**Status:** Planned (Phase 3).

- **Purpose:** Retrieves the current execution status and aggregated summary of an agent run.
- **Path Parameters:**
  - `run_id` (string, required): Unique run identifier.
- **Response (200 OK):**
  ```json
  {
    "run_id": "run_20261019_001",
    "started_at": "2026-10-19T09:31:00Z",
    "completed_at": null,
    "status": "in_progress",
    "subscription_ids": ["sub_tc1_cinevault"],
    "final_results": {
      "sub_tc1_cinevault": "pending"
    },
    "summary": "1 candidate awaiting user review."
  }
  ```
- **Error Behavior:**
  - `404 Not Found` if `run_id` does not exist.

---

### 3.4. `GET /runs/{run_id}/subscriptions`

**Status:** Planned (Phase 3).

- **Purpose:** Returns detailed subscription states, evidence, evaluations, and plan details for a specific run (for Dadu's dashboard).
- **Path Parameters:**
  - `run_id` (string, required): Run identifier.
- **Response (200 OK):**
  ```json
  {
    "run_id": "run_20261019_001",
    "items": [
      {
        "subscription": {
          "subscription_id": "sub_tc1_cinevault",
          "service_name": "CineVault Mock Streaming",
          "status": "active",
          "renewal_date": "2026-11-15",
          "last_used_at": "2026-06-10T20:30:00Z"
        },
        "decision": {
          "decision_id": "dec_001",
          "eligibility": "candidate",
          "reason": "Unused for 131 days (> 90 day threshold)",
          "uncertainty": 0.05,
          "rule_version": "1.0.0"
        },
        "plan": {
          "plan_id": "plan_tc1_01",
          "plan_version": "v1.0",
          "status": "awaiting_approval",
          "consequences_warning": null
        }
      }
    ]
  }
  ```

---

### 3.5. `POST /plans/{plan_id}/approve`

**Status:** Planned (Phase 1).

> [!CAUTION]
> **Core Safety Invariant:** Approval must validate against the **exact service name and plan version**. Generic or multi-service approvals are strictly rejected.

- **Purpose:** Explicitly approves a single, specific cancellation plan for execution.
- **Path Parameters:**
  - `plan_id` (string, required): Targeted plan identifier.
- **Request Body:**
  ```json
  {
    "plan_id": "plan_tc1_01",
    "subscription_id": "sub_tc1_cinevault",
    "service_name": "CineVault Mock Streaming",
    "plan_version": "v1.0",
    "approved_by": "user_loki_001",
    "notes": "Confirmed unused; cancel immediately."
  }
  ```
- **Response (200 OK):**
  ```json
  {
    "approval_id": "appr_98765",
    "plan_id": "plan_tc1_01",
    "subscription_id": "sub_tc1_cinevault",
    "service_name": "CineVault Mock Streaming",
    "plan_version": "v1.0",
    "status": "approved",
    "approved_by": "user_loki_001",
    "decided_at": "2026-10-19T09:40:00Z",
    "notes": "Confirmed unused; cancel immediately."
  }
  ```
- **Error Behavior:**
  - `400 Bad Request`: If `plan_version` or `service_name` in request body does not match the stored plan.
  - `404 Not Found`: If `plan_id` does not exist.
  - `409 Conflict`: If plan is not in `awaiting_approval` status.

---

### 3.6. `POST /plans/{plan_id}/decline`

**Status:** Planned (Phase 1).

- **Purpose:** Declines cancellation of a plan, keeping the subscription active.
- **Path Parameters:**
  - `plan_id` (string, required): Targeted plan identifier.
- **Request Body:**
  ```json
  {
    "plan_id": "plan_tc1_01",
    "plan_version": "v1.0",
    "declined_by": "user_loki_001",
    "notes": "Plan to resume watching next week."
  }
  ```
- **Response (200 OK):**
  ```json
  {
    "approval_id": "appr_98766",
    "plan_id": "plan_tc1_01",
    "subscription_id": "sub_tc1_cinevault",
    "status": "declined",
    "decided_at": "2026-10-19T09:42:00Z",
    "notes": "Plan to resume watching next week."
  }
  ```

---

### 3.7. `POST /actions/{action_id}/verify`

**Status:** Planned (Phase 2).

> [!IMPORTANT]
> **Core Safety Invariant:** Action execution success (`ActionStatus.SUCCEEDED`) is NEVER treated as proof of cancellation. Independent verification check must occur.

- **Purpose:** Triggers independent verification of the subscription status following an action execution.
- **Path Parameters:**
  - `action_id` (string, required): Unique action identifier.
- **Response (200 OK):**
  ```json
  {
    "verification_id": "verif_5501",
    "action_id": "act_8801",
    "subscription_id": "sub_tc1_cinevault",
    "status": "verified",
    "method": "renewal_status_check",
    "verified_at": "2026-10-19T09:45:00Z",
    "final_state_observed": "cancelled",
    "details": "Mock provider API reports billing status 'terminated'."
  }
  ```
- **Error Behavior:**
  - `404 Not Found` if `action_id` does not exist.
  - `400 Bad Request` if action has not completed.

---

### 3.8. `GET /runs/{run_id}/report`

**Status:** Planned (Phase 3).

- **Purpose:** Generates a full audit trail and reconciliation report for a completed run.
- **Path Parameters:**
  - `run_id` (string, required): Run identifier.
- **Response (200 OK):**
  ```json
  {
    "run_id": "run_20261019_001",
    "generated_at": "2026-10-19T09:50:00Z",
    "total_evaluated": 1,
    "total_cancelled_verified": 1,
    "total_kept": 0,
    "total_monthly_savings_usd": 14.99,
    "items": [
      {
        "subscription_id": "sub_tc1_cinevault",
        "service_name": "CineVault Mock Streaming",
        "final_status": "verified_cancelled",
        "evidence_count": 2,
        "approval_id": "appr_98765",
        "action_id": "act_8801",
        "verification_id": "verif_5501"
      }
    ]
  }
  ```
