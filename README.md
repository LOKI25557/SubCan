# SubCan: An AI-Powered Subscription Cancellation Agent

> **Detect. Decide. Approve. Cancel. Verify.**

SubCan is an AI-assisted FinTech workflow that identifies streaming subscriptions potentially unused for at least three calendar months, collects evidence, explains cancellation consequences, requests explicit user approval, executes only approved actions, and independently verifies the final renewal state.

---

## Phase 0: Foundation & Team Independence

This repository is currently at **Phase 0**. The objective of Phase 0 is to establish a rock-solid, shared development foundation, standardized Pydantic data schemas, API contracts, and synthetic fixtures so that teammates can develop their components in parallel without blocking each other.

> [!NOTE]
> During Phase 0, the full cancellation agent is **not yet implemented**. The application provides a minimal, working FastAPI foundation (`GET /health`), fully validated shared data models, four comprehensive synthetic test fixtures, and verified unit tests.

---

## Architecture Overview

```
                      +---------------------------------------+
                      |     SubCan Core Workflow Pipeline     |
                      +---------------------------------------+
                                          |
 1. DETECT    [Chinmay]    Ingest mock subscriptions & collect usage evidence
                                          |
                                          v
 2. DECIDE    [Loki]       Evaluate 3-month inactivity rule (>= 90 days) & consequences
                                          |
                                          v
 3. APPROVE   [Loki/Dadu]  Present plan with warnings; require explicit per-service approval
                                          |
                                          v
 4. CANCEL    [Loki]       Execute cancellation steps ONLY for approved plans
                                          |
                                          v
 5. VERIFY    [Loki]       Independently verify provider renewal state (NEVER assume success)
```

### Safety Invariants
1. **Never cancel without explicit user approval:** No generic, blanket, or auto-pilot cancellations. Approvals are strictly bound to the exact service name and plan version.
2. **Action success is not verification:** An action status of `succeeded` indicates only that the execution script ran without crashes. Independent verification of the provider renewal state is mandatory before marking a subscription as `verified_cancelled`.
3. **No fabricated activity dates:** Missing usage history is strictly represented as `null`/`unknown`, never as an invented timestamp.
4. **Synthetic and mock safety:** The system only interacts with synthetic test fixtures or organizer-authorized mock sites. Real user credentials must never be used or committed.

---

## Technology Stack

- **Language:** Python 3.11+ (tested on Python 3.13)
- **API Framework:** FastAPI
- **ASGI Server:** Uvicorn
- **Validation & Schemas:** Pydantic V2
- **Testing:** pytest, pytest-asyncio, HTTPX
- **Version Control:** Git & GitHub

*(Phase 0 does not introduce React, SQLite connections, browser automation, LLM APIs, or external dependencies.)*

---

## Repository Structure

```
SubCan/
├── .env.example              # Environment variables template
├── .gitignore                # Git hygiene rules
├── README.md                 # Project documentation & team guidelines
├── requirements.txt          # Python dependencies with version bounds
├── pytest.ini                # Pytest configuration
├── docs/
│   ├── API_CONTRACT.md       # API specification (implemented + planned)
│   └── DATA_CONTRACT.md      # Canonical Pydantic schemas & enum reference
├── fixtures/
│   └── subscriptions.json    # 4 synthetic test scenarios (TC1 - TC4)
├── frontend/
│   └── README.md             # Guidelines for Dadu's dashboard module
├── reports/
│   └── .gitkeep              # Destination directory for run audit reports
└── backend/
    ├── app/
    │   ├── __init__.py       # Package definition
    │   ├── main.py           # FastAPI entry point & GET /health
    │   ├── models.py         # Shared Pydantic domain models & enums
    │   ├── agent.py          # AI-agent orchestrator placeholder (Loki)
    │   ├── rules.py          # Inactivity & decision rules placeholder (Loki)
    │   ├── planner.py        # Cancellation plan builder placeholder (Loki)
    │   ├── approvals.py      # Human-in-the-loop approval validator placeholder (Loki)
    │   ├── actions.py        # Action execution coordinator placeholder (Loki)
    │   ├── verification.py   # Independent verification placeholder (Loki)
    │   ├── database.py       # Persistence layer placeholder (Dadu)
    │   └── connectors/
    │       └── __init__.py   # Mock site connectors placeholder (Chinmay)
    └── tests/
        ├── __init__.py
        ├── test_health.py    # Health check & documentation tests
        └── test_models.py    # Model validation, enums, & fixture compliance tests
```

---

## Team Roles & Independence

To enable parallel, unblocked development across the team:

| Member | Primary Responsibility | Target Directory | Dedicated Branch |
|---|---|---|---|
| **Loki** | Backend orchestration, decision rules, planner, approval validation, safe actions, verification | `backend/app/` (`agent.py`, `rules.py`, `planner.py`, `approvals.py`, `actions.py`, `verification.py`) | `feature/loki-backend` |
| **Chinmay** | Data retrieval, mock-site connectors, usage history extraction, cancellation terms, evidence collection | `backend/app/connectors/` | `feature/chinmay-connectors` |
| **Dadu** | Database schema, persistence, dashboard frontend, test harnesses, audit reporting | `backend/app/database.py`, `frontend/`, `reports/` | `feature/dadu-database-ui` |

### Collaboration Guidelines
1. **Adhere to Contracts:** All connector outputs produced by Chinmay and UI payloads consumed by Dadu must conform strictly to [docs/DATA_CONTRACT.md](file:///docs/DATA_CONTRACT.md) and [docs/API_CONTRACT.md](file:///docs/API_CONTRACT.md).
2. **Feature Branches:** Work on isolated feature branches (`feature/<member>-<scope>`). Never push directly to `main`.
3. **Pull Requests:** Merge features exclusively via reviewed Pull Requests.
4. **Secret Hygiene:** Never commit `.env` files, API keys, or credentials. Use `.env.example` as a template.

---

## Setup & Execution

### 1. Prerequisites
- Python 3.11 or higher
- Git

### 2. Clone and Setup Environment

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

# Install Phase 0 dependencies
pip install -r requirements.txt
```

### 3. Run the FastAPI Application

```bash
uvicorn backend.app.main:app --reload
```

- Application runs at: `http://127.0.0.1:8000`
- Interactive Swagger UI: `http://127.0.0.1:8000/docs`
- ReDoc alternative docs: `http://127.0.0.1:8000/redoc`

### 4. Run the Test Suite

```bash
pytest -v
```

All 22 deterministic tests should pass without external network calls.

---

## Test Scenarios in `fixtures/subscriptions.json`

The fixture file contains four officially specified test cases:

- **TC1 (Standard Inactive):** `sub_tc1_cinevault` — Active subscription with no streaming activity for > 3 calendar months (130+ days). Standard cancellation terms preserved.
- **TC2 (Data Deletion Warning):** `sub_tc2_audiocloud` — Active subscription where cancellation immediately purges cloud playlists and audio stems. Requires explicit UI consequence disclosure before approval.
- **TC3 (Pause Alternative):** `sub_tc3_fitstream` — Active subscription offering a 90-day freeze alternative. Must never be misreported as cancelled if paused.
- **TC4 (Approval Isolation):** `sub_tc4_moviemax` & `sub_tc4_gamestream` — Multiple subscriptions evaluated simultaneously. Approving cancellation for one must strictly isolate from the other.

---

## Current Limitations & Roadmap

### Phase 0 Limitations
- Only `GET /health` is implemented as an active endpoint.
- Future endpoints (`/runs`, `/plans/{id}/approve`, etc.) are documented contracts.
- Connectors and database layers are documented placeholders.

### Upcoming Phases
- **Phase 1:** Decision rules (3-month inactivity detection), mock site connector ingestion, and plan generation.
- **Phase 2:** Human approval endpoint enforcement, mock action execution, and independent renewal verification.
- **Phase 3:** Full AI-agent orchestration pipeline, Dadu's dashboard UI integration, and end-to-end reconciliation reports.
