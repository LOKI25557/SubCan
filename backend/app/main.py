"""SubCan FastAPI Starter Application.

Phase 0: Foundation & Team Independence.
Minimal, modular FastAPI setup exposing system health check and interactive documentation.
"""

from datetime import datetime, timezone
from fastapi import FastAPI
from backend.app.models import HealthCheckResponse

app = FastAPI(
    title="SubCan: An AI-Powered Subscription Cancellation Agent",
    description=(
        "Detect. Decide. Approve. Cancel. Verify.\n\n"
        "SubCan is an AI-assisted FinTech workflow that identifies streaming "
        "subscriptions potentially unused for at least three calendar months, collects "
        "evidence, explains cancellation consequences, requests explicit user approval, "
        "executes only approved actions, and independently verifies the final renewal state."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


@app.get(
    "/health",
    response_model=HealthCheckResponse,
    tags=["System"],
    summary="Application Health Check",
    description="Returns the operational health status, application title, version, phase, and timestamp.",
)
def get_health() -> HealthCheckResponse:
    """Return health check response."""
    return HealthCheckResponse(
        status="healthy",
        app_title="SubCan: An AI-Powered Subscription Cancellation Agent",
        version="0.1.0",
        phase="Phase 0: Foundation & Team Independence",
        timestamp=datetime.now(timezone.utc),
    )
