# SubCan: An AI-Assisted Subscription Cancellation Agent

> **Detect. Decide. Approve. Cancel. Verify.**

SubCan is an auditable, AI-assisted FinTech workflow engine that identifies unused recurring subscriptions (≥ 3 calendar months inactive), gathers and normalizes usage logs and cancellation terms, determines cancellation eligibility using deterministic rules, presents consequences and data-loss warnings, strictly enforces an explicit per-subscription human approval gate, coordinates safe cancellation actions, and independently verifies provider renewal states.

---

## 1. Architecture Overview

```
                      +------------------------------------------------+
                      |         SubCan Integrated Agent Workflow       |
                      +------------------------------------------------+
                                              |
 1. DISCOVER  [Chinmay/Loki] Ingest multi-channel subscriptions:
                             - Thread-safe ConnectorRegistry (5 mock streaming providers)
                             - Email Receipt Discovery via Google OAuth 2.0 PKCE & HTML parser
                             - Manual inventory entry modal with category tagging
                                              |
                                              v
 2. DECIDE    [Loki]         Evaluate strict 3-calendar-month inactivity rule with day clamping;
                             fail closed on missing, contradictory, or future-dated logs
                                              |
                                              v
 3. APPROVE   [Loki/Dadu]    Versioned plan construction; explicit disclosure of data-loss warnings
                             and pause alternatives; strict per-subscription approval gate;
                             safe manual guidance for restricted providers (Bloomberg, Adobe, Gym)
                                              |
                                              v
 4. CANCEL    [Loki]         Execute authorized cancellation steps ONLY for approved plans;
                             enforce idempotent replay protection across process restarts
                                              |
                                              v
 5. VERIFY    [Loki]         Independently poll & verify provider renewal state (ActionStatus.SUCCEEDED
                             is NEVER treated as cancellation; pauses are NEVER reported cancelled)
                                              |
                                              v
 6. PERSIST   [Dadu]         Durable SQLite storage for runs, subscriptions, email accounts,
                             candidates, audit events, action records, and test results
```

### Safety Invariants
1. **Never cancel without explicit user approval:** No blanket, auto-pilot, or preselected approvals. Approvals are strictly bound to the exact subscription ID, service name, and plan version fingerprint.
2. **Action execution success is not cancellation:** An action status of `succeeded` indicates only that the execution script ran without crashing. Independent verification of provider renewal state is mandatory before marking a subscription as `verified_cancelled`.
3. **No fabricated activity dates:** Missing usage history is strictly represented as `null`/`unknown`, failing closed to manual review (`needs_review`).
4. **Pause alternatives remain distinct:** A pause or billing freeze option is strictly recognized as distinct from cancellation; a paused subscription is never reported as cancelled.
5. **Safe restricted provider handling:** For providers requiring phone calls, retention funnels, 2FA/CAPTCHA, or certified mail (e.g. Bloomberg, Gyms, Adobe), SubCan NEVER bypasses security controls; instead, it generates verified manual cancellation guidance.
6. **Synthetic and mock safety:** The system defaults to safe mock fixtures and demo fallbacks. Real user credentials, passwords, or payment details are never stored or bypassed.

---

## 2. Team Module Responsibilities & Integration

| Member | Primary Modules | Key Responsibilities & Deliverables |
|---|---|---|
| **Loki** | `backend/app/agent.py`<br>`backend/app/rules.py`<br>`backend/app/planner.py`<br>`backend/app/approvals.py`<br>`backend/app/actions.py`<br>`backend/app/verification.py`<br>`backend/app/email/` (`oauth.py`, `parser.py`, `service.py`)<br>`backend/app/main.py`<br>`backend/app/acceptance.py` | Orchestration pipeline, calendar-month inactivity rules, plan generation, fail-closed approval gate, safe action coordination, independent renewal verification, Google OAuth 2.0 PKCE email discovery, billing receipt parser, restricted provider guidance, and FastAPI REST endpoints. |
| **Chinmay** | `backend/app/connectors/` (`base.py`, `registry.py`, `services.py`, `http.py`, `fixture.py`, `normalization.py`, `exceptions.py`) | Mock streaming connectors (CineVault, AudioCloud, FitStream, MovieMax, GameStream), ISO timestamp normalization, evidence extraction, retry/timeout error handling, and `load_connector_data()` ingestion interface. |
| **Dadu** | `backend/app/database.py`<br>`frontend/` (`index.html`, `app.js`, `styles.css`) | Durable SQLite persistence (`runs`, `subscriptions`, `email_accounts`, `subscription_candidates`, `audit_events`, `action_records`, `test_results`), restart recovery, and control center dashboard UI. |

---

## 3. Technology Stack

- **Backend Framework:** FastAPI & Starlette
- **Data Validation & Schemas:** Pydantic V2 (`extra="forbid"`)
- **Persistence:** SQLite (`sqlite3` standard library with foreign keys, WAL/timeout configuration, indexed queries)
- **HTTP & Mock Connectors:** HTTPX with retry/backoff transport
- **Frontend Dashboard:** Vanilla HTML5, CSS3 (Modern Tech/Slate design system, Plus Jakarta Sans & JetBrains Mono), Vanilla JavaScript
- **Test Suite:** pytest, pytest-asyncio, HTTPX TestClient

---

## 4. Repository Structure

```
SubCan/
├── .env.example                       # Environment configuration template (with Google OAuth)
├── .gitignore                         # Git hygiene rules
├── README.md                          # Full system documentation & demo instructions
├── pytest.ini                         # Pytest configuration
├── requirements.txt                   # Production & testing dependencies
├── docs/
│   ├── API_CONTRACT.md                # Full REST API specification
│   ├── DATA_CONTRACT.md               # Canonical Pydantic schemas & enum reference
│   └── CONNECTORS.md                  # Connector architecture & service specs
├── fixtures/
│   └── subscriptions.json             # 4 synthetic test scenarios (TC1 - TC4)
├── frontend/
│   ├── README.md                      # Frontend architecture guide
│   ├── index.html                     # Control center dashboard (Screens A - E)
│   ├── app.js                         # Dynamic state management & API interaction
│   └── styles.css                     # Modern tech-forward aesthetic stylesheet
├── reports/
│   └── subcan.db                      # Durable SQLite database (auto-created)
└── backend/
    ├── app/
    │   ├── __init__.py                # Package root
    │   ├── main.py                    # FastAPI application, CORS, static mount, endpoints
    │   ├── models.py                  # Domain models, Pydantic schemas, enums
    │   ├── agent.py                   # Orchestration workflow coordinator
    │   ├── rules.py                   # Calendar-month inactivity & decision rules
    │   ├── planner.py                 # Cancellation proposal builder & fingerprinting
    │   ├── approvals.py               # Human-in-the-loop explicit approval validator
    │   ├── actions.py                 # Safe action coordination & idempotency registry
    │   ├── verification.py            # Independent renewal verification check
    │   ├── database.py                # Durable SQLite persistence & schema migrations
    │   ├── acceptance.py              # Official acceptance test suite runner (TC1-TC4)
    │   ├── connectors/                # Chinmay's data connectors & normalization
    │   └── email/                     # Loki's email discovery subsystem
    │       ├── __init__.py            # Package exports
    │       ├── oauth.py               # Google OAuth 2.0 PKCE flow & demo fallback
    │       ├── parser.py              # Sanitized HTML/text billing receipt parser
    │       └── service.py             # Mailbox sync & candidate promotion service
    └── tests/
        ├── test_actions.py            # Action execution & idempotency tests (16)
        ├── test_agent.py              # Workflow orchestration & lifecycle tests (14)
        ├── test_api.py                # FastAPI REST endpoint integration tests (15)
        ├── test_approvals.py          # Human approval gate fail-closed validation (17)
        ├── test_connectors.py         # Mock connectors, retries, extraction tests (36)
        ├── test_dashboard_ui.py       # Dashboard UI rendering, drawer lifecycle, & XSS tests (12)
        ├── test_database.py           # SQLite persistence & restart recovery tests (4)
        ├── test_e2e_scenarios.py      # End-to-end 17 mandatory acceptance scenario tests (17)
        ├── test_email_discovery.py    # Email OAuth, parser, sanitization, sync tests (8)
        ├── test_health.py             # System health & OpenAPI docs tests (3)
        ├── test_manual_subscriptions.py # Manual entry CRUD, validation, tenant isolation (6)
        ├── test_models.py             # Schema validation, enums, & fixture compliance (19)
        ├── test_official_acceptance.py # Official TC1-TC4 and edge cases acceptance test (1)
        ├── test_planner.py            # Proposal construction & consequence disclosures (14)
        ├── test_restricted_connector.py # Safe guidance for non-standard providers (2)
        ├── test_rules.py              # 3-month calendar rules, boundary, contradictory logs (22)
        └── test_verification.py       # Independent verification separation tests (11)
```

---

## 5. Setup & Execution

### Prerequisites
- Python 3.11+ (Tested on Python 3.13)
- Git

### Installation

```bash
# Clone the repository
git clone https://github.com/LOKI25557/SubCan.git
cd SubCan

# Create and activate a virtual environment
python -m venv .venv

# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Environment Configuration

```bash
# Copy environment template
cp .env.example .env
```

Key environment variables:
- `DATABASE_URL`: SQLite connection URI (default: `sqlite:///./reports/subcan.db`).
- `APP_HOST`: Host interface (default: `127.0.0.1`).
- `APP_PORT`: Server port (default: `8000`).
- `GOOGLE_CLIENT_ID`: Google OAuth 2.0 Client ID (optional; leaves system in Safe Demo Fallback mode if unset).
- `GOOGLE_CLIENT_SECRET`: Google OAuth 2.0 Client Secret (optional).
- `GOOGLE_REDIRECT_URI`: OAuth callback URI (default: `http://127.0.0.1:8000/api/email/oauth/callback`).

#### Google OAuth 2.0 Setup (Live Mode)
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a project and configure the OAuth consent screen with scope `https://www.googleapis.com/auth/gmail.readonly`.
3. Create OAuth 2.0 Client IDs (Web application) with Authorized Redirect URI: `http://127.0.0.1:8000/api/email/oauth/callback`.
4. Populate `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`.
5. *Note:* If left blank or unset, SubCan automatically operates in **Safe Demo Fallback Mode**, presenting realistic synthetic billing emails (e.g. GitHub Copilot, The New York Times, Dropbox Plus) without requiring external credentials.

---

## 6. Running the Application

### Live Deployed Backend (Render)

SubCan's backend and control center dashboard are deployed live at:
- **Production Base URL:** `https://subcan.onrender.com`
- **Interactive Production Dashboard:** [https://subcan.onrender.com/dashboard/](https://subcan.onrender.com/dashboard/)
- **Interactive Swagger Docs:** [https://subcan.onrender.com/docs](https://subcan.onrender.com/docs)
- **ReDoc Documentation:** [https://subcan.onrender.com/redoc](https://subcan.onrender.com/redoc)
- **System Health Endpoint:** [https://subcan.onrender.com/health](https://subcan.onrender.com/health)
- **Acceptance Scorecard API:** [https://subcan.onrender.com/api/test-report](https://subcan.onrender.com/api/test-report)

### Local Development

```bash
uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

- **Interactive Local Dashboard:** [http://127.0.0.1:8000/dashboard/](http://127.0.0.1:8000/dashboard/)
- **Interactive Local Swagger Docs:** [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **Local ReDoc:** [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)
- **Local System Health:** [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

---

## 7. REST API Endpoints Specification

| Method | Endpoint | Description | Primary Safety Rule |
|---|---|---|---|
| `GET` | `/health` | Service health status and metadata | Verifies operational readiness |
| `GET` | `/subscriptions` | Ingest connector & user inventory subscriptions | Merges connector fixtures with user-scoped entries |
| `POST` | `/subscriptions` | Add manual subscription record | User-scoped tenant isolation; validates non-blank name |
| `GET` | `/subscriptions/{id}` | Retrieve subscription details | Tenant-isolated lookup (404 on cross-tenant access) |
| `PUT` | `/subscriptions/{id}` | Update manual subscription fields | Scoped to owner tenant |
| `DELETE` | `/subscriptions/{id}` | Soft-delete / archive subscription | Sets `is_archived=True`; preserves audit integrity |
| `POST` | `/runs` | Initiate new AI-agent orchestration run | Analysis only; NEVER triggers cancellation |
| `GET` | `/runs/{run_id}` | Retrieve run status and aggregated summary | Inspects orchestration lifecycle state |
| `GET` | `/runs/{run_id}/subscriptions` | Detailed subscription context, evidence, plans | Returns complete review items for UI |
| `POST` | `/plans/{plan_id}/approve` | Explicit human approval for cancellation | Validates exact service, plan version, and sub ID |
| `POST` | `/plans/{plan_id}/decline` | Decline proposed plan | Keeps subscription active; zero action taken |
| `POST` | `/actions/{action_id}/verify` | Trigger independent renewal verification | Action success != verification; re-checks renewal |
| `GET` | `/runs/{run_id}/report` | Audit and financial reconciliation report | Calculates verified savings and item status |
| `GET` | `/runs/{run_id}/audit` | Immutable SQLite audit events log | Chronological timeline of decisions & actions |
| `GET` | `/api/email/oauth/url` | Generate Google OAuth 2.0 PKCE auth URL | Includes CSRF state token; demo fallback if unconfigured |
| `GET` | `/api/email/oauth/callback` | OAuth redirect callback handler | Validates CSRF state, exchanges tokens, links account |
| `GET` | `/api/email/status` | Current email connection and sync status | Returns connected email and candidate count |
| `POST` | `/api/email/disconnect` | Revoke tokens and disconnect email | Cleanly revokes Google tokens and drops DB records |
| `POST` | `/api/email/sync` | Sync mailbox for billing receipts | Sanitizes HTML against XSS; extracts candidates |
| `GET` | `/api/email/candidates` | List discovered candidate subscriptions | Filterable by review status (`pending`, `confirmed`) |
| `POST` | `/api/email/candidates/{id}/confirm` | Promote candidate to unified inventory | Promotes candidate and links original email evidence |
| `POST` | `/api/email/candidates/{id}/dismiss` | Dismiss discovered candidate | Marks candidate dismissed without creating subscription |
| `GET` | `/services/{service_id}/manual-guide` | Safe guidance for restricted providers | Delivers portal links, phone numbers, and steps |
| `GET` | `/api/test-report` | Live scorecard for official test scenarios | Executes TC1-TC4 and edge cases live |

---

## 8. Google OAuth 2.0 & Gmail Discovery Configuration

SubCan supports discovery of recurring subscriptions directly from billing, invoice, and renewal receipts in a connected Gmail mailbox.

### Google Cloud Console Setup:
1. **Create or Select Project:** Visit the [Google Cloud Console](https://console.cloud.google.com/) and create a project (e.g. `SubCan-Discovery`).
2. **Enable Gmail API:** Go to **APIs & Services** > **Library**, search for **Gmail API**, and click **Enable**.
3. **Configure OAuth Consent Screen:**
   - Under **APIs & Services** > **OAuth consent screen**, choose **External**.
   - Set **App name** to `SubCan` and provide your support email.
   - Under **Scopes**, click **Add or Remove Scopes** and add:
     `https://www.googleapis.com/auth/gmail.readonly` (Read-only access to messages and metadata).
   - Under **Test Users**, add your personal or test Gmail address.
4. **Create OAuth 2.0 Client Credentials:**
   - Go to **APIs & Services** > **Credentials** > **Create Credentials** > **OAuth client ID**.
   - Select **Application type:** `Web application`.
   - Name: `SubCan Web Client`.
   - Under **Authorized redirect URIs**, add the exact callback URI:
     `http://127.0.0.1:8000/api/email/oauth/callback`
5. **Configure Local Environment:**
   Copy `.env.example` to `.env` in the repository root:
   ```bash
   cp .env.example .env
   ```
   Add your Google OAuth credentials:
   ```ini
   GOOGLE_CLIENT_ID=your_client_id.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=your_client_secret
   GOOGLE_REDIRECT_URI=http://127.0.0.1:8000/api/email/oauth/callback
   ```

### Safe Demo Fallback Mode:
When `GOOGLE_CLIENT_ID` or `GOOGLE_CLIENT_SECRET` are omitted, blank, or placeholder values, SubCan automatically operates in **Safe Demo Fallback mode**:
- Uses realistic synthetic billing emails (e.g. GitHub Copilot, The New York Times, Dropbox Plus).
- Never contacts Google Cloud endpoints or attempts live token exchange.
- Clearly flags accounts and candidates as `Demo Mock` in both the API and dashboard.
- Enables safe local end-to-end evaluation without requiring cloud credentials.

### Security & Privacy Safeguards:
1. **Least Privilege Scope:** Requests strictly `https://www.googleapis.com/auth/gmail.readonly`. SubCan cannot send, modify, or delete emails.
2. **PKCE & CSRF Defense:** Uses high-entropy RFC 7636 S256 PKCE code challenges and cryptographically secure state tokens to prevent CSRF and authorization code interception attacks.
3. **HTML Sanitization:** Raw email content is stripped of scripts, iframes, styles, and event handlers to prevent XSS.
4. **No Inferred Activity:** A billing receipt arrival date establishes billing provenance only; it is **never** used to establish active user streaming or service usage (`detected_last_used` remains `None`).
5. **No Automatic Cancellation:** Subscriptions discovered via email are placed in a Candidate Review inbox requiring explicit human confirmation before promotion to inventory.
6. **No Token Exposure:** Tokens are stored server-side in SQLite and are never logged, printed, or sent to frontend clients.

---

## 9. Official Acceptance Test Scenarios Walkthrough

The system includes automated tests and a live UI scorecard for the four official scenarios in `fixtures/subscriptions.json`:

### TC1 — Approved Cancellation (`sub_tc1_cinevault`)
- **Pre-condition:** Inactive streaming subscription (last streamed 131 days ago, > 3 calendar months).
- **Evaluation:** Evaluated as `CANDIDATE` with standard cancellation terms preserved.
- **Workflow:** User explicitly approves plan; cancellation action executes (`ActionStatus.SUCCEEDED`); independent verification confirms provider renewal is disabled.
- **Outcome:** Final state is verified as `VERIFIED_CANCELLED`.

### TC2 — Immediate Data Deletion Risk (`sub_tc2_audiocloud`)
- **Pre-condition:** Cloud audio subscription with 120+ days inactivity. Policy indicates cloud playlists and audio stems are purged immediately upon cancellation.
- **Evaluation:** Evaluated as `CANDIDATE` with `data_deletion_risk=True`. Plan explicitly discloses critical warning: `"WARNING: Immediate and irreversible data deletion."`
- **Workflow:** User reviews consequence warning in UI and chooses **Keep Subscription** (Decline).
- **Outcome:** Plan marked `DECLINED`, zero action executed, subscription remains active (`KEPT`).

### TC3 — Billing Pause Alternative (`sub_tc3_fitstream`)
- **Pre-condition:** Inactive fitness stream. Provider offers a 90-day freeze/pause alternative.
- **Evaluation:** Evaluated as `CANDIDATE` with `pause_alternative_available=True`. Plan discloses pause option without account deletion.
- **Workflow:** If paused, provider renewal check returns billing status `"paused"`.
- **Outcome:** Independent verification refuses to report a pause as cancelled; outcome remains distinct.

### TC4 — Strict Multi-Service Isolation (`sub_tc4_moviemax` & `sub_tc4_gamestream`)
- **Pre-condition:** Multiple candidate subscriptions evaluated in the same run.
- **Workflow:** Explicit approval submitted exclusively for `sub_tc4_moviemax`.
- **Outcome:** Cancellation executes and verifies ONLY for MovieMax. `sub_tc4_gamestream` remains completely untouched in `AWAITING_APPROVAL` status with zero actions executed.

---

## 10. Test Suite Execution & Verification

To run the complete automated test suite across all 18 modules:

```bash
.\.venv\Scripts\pytest -q
```

### Actual Verified Test Results

```
........................................................................ [ 30%]
........................................................................ [ 61%]
........................................................................ [ 92%]
..................                                                       [100%]
============================== warnings summary ===============================
234 passed, 2 warnings in 215.78s
```

- **Total Tests:** 234
- **Passed:** 234 (100%)
- **Failed:** 0
- **Skipped:** 0
- **Regression Count:** 0

---

## 11. Step-by-Step Live Demonstration Walkthrough

1. **Start the API & Dashboard:** Run `uvicorn backend.app.main:app --reload` and open [http://127.0.0.1:8000/dashboard/](http://127.0.0.1:8000/dashboard/).
2. **Review Ingested Accounts:** Notice all 5 connector-discovered subscriptions (CineVault, AudioCloud, FitStream, MovieMax, GameStream) populated with statuses, renewal dates, and clear `Demo Mock` provenance badges.
3. **Add a Manual Subscription:** Click **+ Add Subscription**. Fill in `Notion Plus`, `$10.00/mo`, category `Software`, and click **Save Subscription**. Observe it instantly appears with a `Manual` provenance badge.
4. **Email Discovery & Receipt Parsing:** Switch to the **Email Discovery** tab.
   - If Google OAuth credentials are configured in `.env`, the button reads **Connect Gmail Account** and redirects to Google's authorization consent screen.
   - If unconfigured, the button reads **Connect Demo Mailbox (Mock)** and connects a simulated demo mailbox.
   - Click **Sync Billing Emails** to scan messages and extract subscription candidates.
   - Review discovered candidates: examine detected amounts, renewal dates, and sanitised email snippets.
   - Click **Confirm** to promote a candidate into the unified inventory (with linked email evidence) or **Dismiss** to reject a false positive.
5. **Start an Orchestration Review Run:** Switch back to the **Active Subscriptions** tab and click **Start review run**. Observe the metrics update: candidates identified (≥ 3 calendar months inactive).
6. **Inspect Consequence Warnings (TC2):** Click **Inspect** on `AudioCloud Mock Streaming`. Observe the red callout: `"CRITICAL CONSEQUENCE: IMMEDIATE DATA DELETION"`.
7. **Decline Cancellation (Preserve Data):** Click **Keep Subscription**. Notice status immediately changes to `Kept Active`, with zero actions executed.
8. **Approve Eligible Plan (TC1):** Click **Inspect** on `CineVault Mock Streaming`. Click **Approve Cancellation**. Observe the action executes and transitions to `Action Executed (Pending Verif)`.
9. **Trigger Independent Verification:** Click **Trigger Independent Renewal Verification Check**. Observe independent provider polling confirms termination; status transitions to **Verified Cancelled**.
10. **Verify Isolation (TC4):** Notice that `GameStream Mock Pass` remains completely untouched and pending approval.
11. **Restricted Provider Guidance:** Click **Inspect** on any restricted service (or call `GET /services/bloomberg/manual-guide`). View the verified step-by-step human guide, support phone number, and direct cancellation portal link.
12. **Inspect Audit & Reconciliation:** Switch to the **Audit & Reconciliation** tab. View the calculated recurring monthly savings and inspect the immutable chronological SQLite audit trail.
13. **Run Acceptance Scorecard (Screen E):** Switch to the **Acceptance Test Suite** tab and click **Execute Official Acceptance Suite**. Watch live execution pass all 9 test scenarios (TC1 - TC4 and edge cases) with 100% green checkmarks.
