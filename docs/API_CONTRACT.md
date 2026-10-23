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
| `/health` | `GET` | **IMPLEMENTED** | Loki (Backend Starter) |
| `/ready` | `GET` | **IMPLEMENTED** | Loki (Kubernetes / Container Readiness Probe) |
| `/subscriptions` | `GET` | **IMPLEMENTED** | Chinmay / Dadu / Loki (Inventory Filtering & Pagination) |
| `/subscriptions` | `POST` | **IMPLEMENTED** | Loki / Dadu (Manual Inventory with Duplicate Guard) |
| `/subscriptions/{id}` | `GET` | **IMPLEMENTED** | Loki / Dadu (Tenant-Scoped Details) |
| `/subscriptions/{id}` | `PUT` | **IMPLEMENTED** | Loki / Dadu (Manual Inventory & Field Edits) |
| `/subscriptions/{id}` | `DELETE` | **IMPLEMENTED** | Loki / Dadu (Soft-Delete / Archive) |
| `/runs` | `POST` | **IMPLEMENTED** | Loki (Agent Orchestrator) |
| `/runs/{run_id}` | `GET` | **IMPLEMENTED** | Loki / Dadu |
| `/runs/{run_id}/subscriptions` | `GET` | **IMPLEMENTED** | Dadu (Persistence / Queries) |
| `/runs/{run_id}/report` | `GET` | **IMPLEMENTED** | Dadu / Loki (Reporting) |
| `/runs/{run_id}/audit` | `GET` | **IMPLEMENTED** | Dadu / Loki (Audit Log) |
| `/plans/{plan_id}/approve` | `POST` | **IMPLEMENTED** | Loki (Approvals) |
| `/plans/{plan_id}/decline` | `POST` | **IMPLEMENTED** | Loki (Approvals) |
| `/actions/{action_id}/verify` | `POST` | **IMPLEMENTED** | Loki (Verification) |
| `/api/email/oauth/url` | `GET` | **IMPLEMENTED** | Loki (Email Discovery & PKCE) |
| `/api/email/oauth/callback` | `GET` | **IMPLEMENTED** | Loki (Email Discovery Token Exchange) |
| `/api/email/status` | `GET` | **IMPLEMENTED** | Loki / Dadu (Email Account Status) |
| `/api/email/disconnect` | `POST` | **IMPLEMENTED** | Loki (OAuth Revocation & Clean Disconnect) |
| `/api/email/sync` | `POST` | **IMPLEMENTED** | Loki (Billing Email Ingestion / Deduplication) |
| `/api/email/candidates` | `GET` | **IMPLEMENTED** | Loki / Dadu (Candidate Ingestion Queue) |
| `/api/email/candidates/{id}/confirm` | `POST` | **IMPLEMENTED** | Loki / Dadu (Promote Candidate to Inventory) |
| `/api/email/candidates/{id}/dismiss` | `POST` | **IMPLEMENTED** | Loki / Dadu (Dismiss Candidate) |
| `/api/user/export` | `GET` | **IMPLEMENTED** | Dadu / Loki (GDPR/CCPA User Data Portability JSON) |
| `/api/user/data` | `DELETE` | **IMPLEMENTED** | Dadu / Loki (GDPR Right-to-be-Forgotten Data Purge) |
| `/services/{service_id}/manual-guide` | `GET` | **IMPLEMENTED** | Loki (Restricted Provider Guidance) |
| `/api/test-report` | `GET` | **IMPLEMENTED** | Loki (Live Acceptance Scorecard) |

> [!IMPORTANT]
> The dashboard is available at `/dashboard/`. All workflow state is persisted in SQLite configured by `DATABASE_URL`; only synthetic fixtures and mock actions are supported.

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

### 3.1a. `GET /ready`

**Status:** IMPLEMENTED.

- **Purpose:** Container, Kubernetes, and load-balancer readiness probe. Validates database session connectivity, connector registry readiness, and reports OAuth mode.
- **Response (200 OK):**
  ```json
  {
    "status": "ready",
    "database": "connected",
    "connectors": "initialized",
    "oauth_mode": "mock_demo",
    "timestamp": "2026-10-23T19:20:00Z"
  }
  ```

---

### 3.2. `POST /runs`

**Status:** IMPLEMENTED (Phase 3 Orchestrator).

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

### 3.2a. `GET /subscriptions`

**Status:** IMPLEMENTED.

- **Purpose:** Returns the unified subscription inventory with optional filtering and pagination.
- **Query Parameters:**
  - `status` (optional string): Filter by status (`active`, `cancelled`, `unknown`).
  - `include_archived` (optional boolean, default `false`): Include soft-deleted/archived records.
  - `limit` (optional integer): Bounded pagination limit.
  - `offset` (optional integer, default `0`): Pagination offset.
- **Headers:** `X-User-ID` (optional, default: `user_default`).
- **Response (200 OK):** An array of canonical `Subscription` objects.
- **Dashboard:** The static review UI is served at `GET /dashboard/`.

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

**Status:** IMPLEMENTED.

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

---

### 3.9. Manual Subscription Inventory Endpoints

#### `POST /subscriptions`
- **Purpose:** Adds a user-defined manual subscription into durable inventory.
- **Headers:** `X-User-ID` (optional, defaults to `user_default`).
- **Body:** `SubscriptionCreateRequest`
  ```json
  {
    "service_name": "Notion Plus",
    "monthly_cost": 10.0,
    "currency": "USD",
    "billing_cycle": "monthly",
    "renewal_date": "2026-11-01",
    "category": "software",
    "notes": "Work workspace"
  }
  ```
- **Response (201 Created):** Full `Subscription` entity with `discovery_source: "manual"`.

#### `GET /subscriptions/{id}`
- **Purpose:** Retrieve details for a specific subscription.
- **Headers:** `X-User-ID` (optional, enforces tenant isolation).
- **Response (200 OK):** `Subscription` object.
- **Error:** `404 Not Found` if nonexistent or owned by another tenant.

#### `PUT /subscriptions/{id}`
- **Purpose:** Update fields of an existing user-owned subscription.
- **Headers:** `X-User-ID` (optional).
- **Body:** `SubscriptionUpdateRequest`
- **Response (200 OK):** Updated `Subscription` object.

#### `DELETE /subscriptions/{id}`
- **Purpose:** Soft-delete (archive) a user-owned subscription.
- **Headers:** `X-User-ID` (optional).
- **Response (200 OK):**
  ```json
  {
    "status": "archived",
    "subscription_id": "sub_man_12345"
  }
  ```

---

### 3.10. Email Account & Discovery Endpoints

#### `GET /api/email/oauth/url`
- **Purpose:** Generates Google OAuth 2.0 PKCE authorization URL with state CSRF protection (or returns safe mock demo URL if live OAuth credentials are not configured).
- **Headers:** `X-User-ID` (optional).
- **Response (200 OK):**
  ```json
  {
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?...",
    "state": "csrf_token_hex",
    "mode": "live"
  }
  ```

#### `GET /api/email/oauth/callback`
- **Purpose:** Handles Google OAuth authorization code redirect, performs one-time CSRF state verification, exchanges code for refresh/access tokens, and stores linked account in SQLite.
- **Query Params:** `code`, `state`.
- **Response (307 Temporary Redirect):** Redirects browser back to `/dashboard/?email_connected=true`.

#### `GET /api/email/status`
- **Purpose:** Returns connection and sync status for the current user's email account.
- **Headers:** `X-User-ID` (optional).
- **Response (200 OK):** `EmailAccountStatus`
  ```json
  {
    "connected": true,
    "email": "user@example.com",
    "provider": "google",
    "last_synced_at": "2026-10-23T10:00:00Z",
    "candidate_count": 3,
    "pending_candidate_count": 2
  }
  ```

#### `POST /api/email/disconnect`
- **Purpose:** Revokes OAuth token with Google and removes tokens and account from database.
- **Headers:** `X-User-ID` (optional).
- **Response (200 OK):** `{"status": "disconnected"}`

#### `POST /api/email/sync`
- **Purpose:** Scans inbox using targeted queries for subscription billing receipts, extracts merchant details and renewal dates, sanitizes HTML against XSS, and records candidates in review queue.
- **Headers:** `X-User-ID` (optional).
- **Response (200 OK):**
  ```json
  {
    "synced_count": 3,
    "candidates": [ ... ]
  }
  ```

#### `GET /api/email/candidates`
- **Purpose:** Retrieves discovered candidates for review.
- **Headers:** `X-User-ID` (optional).
- **Query Params:** `status` (optional, defaults to `pending`).
- **Response (200 OK):** Array of `SubscriptionCandidate` objects.

#### `POST /api/email/candidates/{id}/confirm`
- **Purpose:** User confirms a discovered candidate; promotes it into the unified `Subscription` inventory and attaches original email excerpt as `Evidence`.
- **Headers:** `X-User-ID` (optional).
- **Response (200 OK):** Newly promoted `Subscription` object.

#### `POST /api/email/candidates/{id}/dismiss`
- **Purpose:** User dismisses candidate without creating a subscription record.
- **Headers:** `X-User-ID` (optional).
- **Response (200 OK):** `{"status": "dismissed", "candidate_id": "cand_..."}`

---

### 3.11. Restricted Provider Guidance Endpoints

#### `GET /services/{service_id}/manual-guide`
- **Purpose:** Returns official safe cancellation guidance and direct URLs for providers requiring human-in-the-loop cancellation (e.g. Bloomberg phone requirement, Adobe retention flow, gym certified letter / in-person).
- **Path Parameters:** `service_id` (string).
- **Response (200 OK):**
  ```json
  {
    "service_id": "bloomberg",
    "service_name": "Bloomberg Terminal / Professional",
    "cancellation_type": "phone_or_account_rep",
    "official_url": "https://service.bloomberg.com",
    "support_phone": "1-212-318-2000",
    "steps": [ ... ],
    "warnings": [ ... ],
    "requires_in_person_or_certified_mail": false
  }
  ```

---

### 3.12. Live Test Suite Scorecard Endpoint

#### `GET /api/test-report`
- **Purpose:** Executes the official acceptance test scenarios (TC1 - TC4 and edge cases) live and returns a granular JSON scorecard.
- **Response (200 OK):**
  ```json
  {
    "total_tests": 9,
    "passed": 9,
    "failed": 0,
    "all_passed": true,
    "results": [ ... ]
  }
  ```

---

### 3.13. User Privacy & Compliance Endpoints (GDPR / CCPA)

#### `GET /api/user/export`
- **Purpose:** Exports all user-scoped subscriptions, candidate records, and redacted email account metadata in portable JSON format (GDPR Article 20 / CCPA).
- **Headers:** `X-User-ID` (optional, default: `user_default`).
- **Response (200 OK):**
  ```json
  {
    "user_id": "user_alice",
    "exported_at": "2026-10-23T19:30:00Z",
    "subscription_count": 2,
    "candidate_count": 1,
    "email_account": {
      "user_id": "user_alice",
      "email_address": "alice@subcan-demo.com",
      "is_connected": true,
      "provider": "gmail"
    },
    "subscriptions": [ ... ],
    "candidates": [ ... ]
  }
  ```

#### `DELETE /api/user/data`
- **Purpose:** Permanently deletes user-scoped subscriptions, discovered candidates, and OAuth access/refresh tokens (GDPR Article 17 "Right to be Forgotten").
- **Headers:** `X-User-ID` (optional, default: `user_default`).
- **Response (200 OK):**
  ```json
  {
    "status": "purged",
    "user_id": "user_alice",
    "deleted_subscriptions": 2,
    "deleted_candidates": 1,
    "tokens_purged": true
  }
  ```
