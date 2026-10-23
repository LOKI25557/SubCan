# SubCan: An AI-Assisted Subscription Cancellation Agent

> **Detect. Decide. Approve. Cancel. Verify.**

SubCan is an auditable, AI-assisted FinTech workflow engine that identifies unused recurring streaming subscriptions (&ge; 3 calendar months inactive), gathers and normalizes usage logs and cancellation terms, determines cancellation eligibility using deterministic rules, presents consequences and data-loss warnings, strictly enforces an explicit per-subscription human approval gate, coordinates safe cancellation actions, and independently verifies provider renewal states.

---

## 1. Architecture Overview

```
                      +------------------------------------------------+
                      |         SubCan Integrated Agent Workflow       |
                      +------------------------------------------------+
                                              |
 1. DETECT    [Chinmay]    Ingest mock streaming subscriptions & collect usage/policy evidence
                           via thread-safe ConnectorRegistry (5 mock provider connectors)
                                              |
                                              v
 2. DECIDE    [Loki]       Evaluate strict 3-calendar-month inactivity rule with day clamping;
                           fail closed on missing, contradictory, or future-dated logs
                                              |
                                              v
 3. APPROVE   [Loki/Dadu]  Versioned plan construction; explicit disclosure of data-loss warnings
                           and pause alternatives; strict per-subscription approval gate
                                              |
                                              v
 4. CANCEL    [Loki]       Execute authorized cancellation steps ONLY for approved plans;
                           enforce idempotent replay protection across process restarts
                                              |
                                              v
 5. VERIFY    [Loki]       Independently poll & verify provider renewal state (ActionStatus.SUCCEEDED
                           is NEVER treated as cancellation; pauses are NEVER reported cancelled)
                                              |
                                              v
 6. PERSIST   [Dadu]       Durable SQLite storage for runs, subscriptions, audit events,
                           action records, and official test results (restores on restart)
```

### Safety Invariants
1. **Never cancel without explicit user approval:** No blanket, auto-pilot, or preselected approvals. Approvals are strictly bound to the exact subscription ID, service name, and plan version fingerprint.
2. **Action execution success is not cancellation:** An action status of `succeeded` indicates only that the execution script ran without crashing. Independent verification of provider renewal state is mandatory before marking a subscription as `verified_cancelled`.
3. **No fabricated activity dates:** Missing usage history is strictly represented as `null`/`unknown`, failing closed to manual review (`needs_review`).
4. **Pause alternatives remain distinct:** A pause or billing freeze option is strictly recognized as distinct from cancellation; a paused subscription is never reported as cancelled.
5. **Synthetic and mock safety:** The system only interacts with synthetic test fixtures and organizer-authorized mock sites. Real user credentials, passwords, or payment details are never stored or bypassed.

---

## 2. Team Module Responsibilities & Integration

| Member | Primary Modules | Key Responsibilities & Deliverables |
|---|---|---|
| **Loki** | `backend/app/agent.py`<br>`backend/app/rules.py`<br>`backend/app/planner.py`<br>`backend/app/approvals.py`<br>`backend/app/actions.py`<br>`backend/app/verification.py`<br>`backend/app/main.py`<br>`backend/app/acceptance.py` | Orchestration pipeline, calendar-month inactivity rules, plan generation, fail-closed approval gate, safe action coordination, independent renewal verification, FastAPI REST endpoints, and official acceptance test suite. |
| **Chinmay** | `backend/app/connectors/` (`base.py`, `registry.py`, `services.py`, `http.py`, `fixture.py`, `normalization.py`, `exceptions.py`) | Mock streaming connectors (CineVault, AudioCloud, FitStream, MovieMax, GameStream), ISO timestamp normalization, evidence extraction, retry/timeout error handling, and `load_connector_data()` ingestion interface. |
| **Dadu** | `backend/app/database.py`<br>`frontend/` (`index.html`, `app.js`, `styles.css`) | Durable SQLite persistence (`runs`, `subscriptions`, `run_subscriptions`, `audit_events`, `action_records`, `test_results`), restart recovery, and control center dashboard UI. |

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
├── .env.example                       # Environment configuration template
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
    │   ├── database.py                # Durable SQLite persistence & event logger
    │   ├── acceptance.py              # Official acceptance test suite runner (TC1-TC4)
    │   └── connectors/                # Chinmay's data connectors & normalization
    └── tests/
        ├── test_health.py             # System health & OpenAPI docs tests (3)
        ├── test_models.py             # Schema validation, enums, & fixture compliance (19)
        ├── test_rules.py              # 3-month calendar rules, boundary, contradictory logs (22)
        ├── test_planner.py            # Proposal construction & consequence disclosures (14)
        ├── test_approvals.py          # Human approval gate fail-closed validation (17)
        ├── test_actions.py            # Action execution & idempotency tests (16)
        ├── test_verification.py       # Independent verification separation tests (11)
        ├── test_agent.py              # Workflow orchestration & lifecycle tests (14)
        ├── test_connectors.py         # Mock connectors, retries, extraction tests (36)
        ├── test_database.py           # SQLite persistence & restart recovery tests (4)
        ├── test_api.py                # FastAPI REST endpoint integration tests (15)
        └── test_official_acceptance.py # Official TC1-TC4 and edge cases acceptance test (1)
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

---

## 6. Running the Application

### Start Backend API & Dashboard

```bash
uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

- **Interactive Dashboard:** [http://127.0.0.1:8000/dashboard/](http://127.0.0.1:8000/dashboard/)
- **Interactive Swagger Docs:** [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **ReDoc Documentation:** [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)
- **System Health:** [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

---

## 7. REST API Endpoints Specification

| Method | Endpoint | Description | Primary Safety Rule |
|---|---|---|---|
| `GET` | `/health` | Service health status and metadata | Verifies operational readiness |
| `GET` | `/subscriptions` | Ingest connector-provided subscriptions | Ingests from registry; persists standalone records |
| `POST` | `/runs` | Initiate new AI-agent orchestration run | Analysis only; NEVER triggers cancellation |
| `GET` | `/runs/{run_id}` | Retrieve run status and aggregated summary | Inspects orchestration lifecycle state |
| `GET` | `/runs/{run_id}/subscriptions` | Detailed subscription context, evidence, plans | Returns complete review items for UI |
| `POST` | `/plans/{plan_id}/approve` | Explicit human approval for cancellation | Validates exact service, plan version, and sub ID |
| `POST` | `/plans/{plan_id}/decline` | Decline proposed plan | Keeps subscription active; zero action taken |
| `POST` | `/actions/{action_id}/verify` | Trigger independent renewal verification | Action success != verification; re-checks renewal |
| `GET` | `/runs/{run_id}/report` | Audit and financial reconciliation report | Calculates verified savings and item status |
| `GET` | `/runs/{run_id}/audit` | Immutable SQLite audit events log | Chronological timeline of decisions & actions |
| `GET` | `/api/test-report` | Live scorecard for official test scenarios | Executes TC1-TC4 and edge cases |

---

## 8. Official Acceptance Test Scenarios Walkthrough

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

## 9. Test Suite Execution & Verification

To run the complete automated test suite across all 12 modules:

```bash
pytest -v
```

### Actual Verified Test Results

```
============================= test session starts =============================
platform win32 -- Python 3.13.3, pytest-9.0.3, pluggy-1.6.0
rootdir: C:\Users\Lokesh\OneDrive\Desktop\Projects\SubCan
configfile: pytest.ini
testpaths: backend/tests
plugins: anyio-4.13.0, asyncio-1.4.0

backend/tests/test_actions.py ................                           [  9%]
backend/tests/test_agent.py ..............                               [ 17%]
backend/tests/test_api.py ...............                                [ 26%]
backend/tests/test_approvals.py .................                        [ 36%]
backend/tests/test_connectors.py ....................................    [ 57%]
backend/tests/test_database.py ....                                      [ 59%]
backend/tests/test_health.py ...                                         [ 61%]
backend/tests/test_models.py ...................                         [ 72%]
backend/tests/test_official_acceptance.py .                              [ 73%]
backend/tests/test_planner.py ..............                             [ 81%]
backend/tests/test_rules.py ......................                       [ 94%]
backend/tests/test_verification.py ...........                           [100%]

============================ 172 passed in 10.00s =============================
```

**Total Tests:** 172  
**Passed:** 172 (100%)  
**Failed:** 0  
**Skipped:** 0  

---

## 10. Step-by-Step Live Demonstration Walkthrough

1. **Start the API:** Run `uvicorn backend.app.main:app --reload` and navigate to [http://127.0.0.1:8000/dashboard/](http://127.0.0.1:8000/dashboard/).
2. **Review Ingested Accounts:** Notice all 5 connector-discovered subscriptions (CineVault, AudioCloud, FitStream, MovieMax, GameStream) populated with statuses and renewal dates.
3. **Start an Orchestration Review Run:** Click **Start review run**. Observe the metrics update: 4 candidates identified (&ge; 3 calendar months inactive).
4. **Inspect Consequence Warnings (TC2):** Click **Inspect** on `AudioCloud Mock Streaming`. Observe the red callout: `"CRITICAL CONSEQUENCE: IMMEDIATE DATA DELETION"`.
5. **Decline Cancellation (Preserve Data):** Click **Keep Subscription**. Notice status immediately changes to `Kept Active`, with zero actions executed.
6. **Approve Eligible Plan (TC1):** Click **Inspect** on `CineVault Mock Streaming`. Click **Approve Cancellation**. Observe the action executes and transitions to `Action Executed (Pending Verif)`.
7. **Trigger Independent Verification:** Click **Trigger Independent Renewal Verification Check**. Observe independent provider polling confirms termination; status transitions to **Verified Cancelled**.
8. **Verify Isolation (TC4):** Notice that `GameStream Mock Pass` remains completely untouched and pending approval.
9. **Inspect Audit & Reconciliation:** Switch to the **Audit & Reconciliation** tab. View the calculated recurring monthly savings ($14.99/mo) and inspect the immutable chronological SQLite audit trail.
10. **Run Acceptance Scorecard (Screen E):** Switch to the **Acceptance Test Suite** tab and click **Execute Official Acceptance Suite**. Watch live execution pass all 9 test scenarios (TC1 - TC4 and edge cases) with 100% green checkmarks.
