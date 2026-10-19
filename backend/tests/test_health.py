"""Health check endpoint tests for SubCan."""

from fastapi.testclient import TestClient
from backend.app.main import app

client = TestClient(app)


def test_health_returns_200():
    """Verify GET /health returns HTTP 200 OK."""
    response = client.get("/health")
    assert response.status_code == 200


def test_health_response_payload():
    """Verify GET /health response payload contains expected status and metadata."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["app_title"] == "SubCan: An AI-Powered Subscription Cancellation Agent"
    assert data["version"] == "0.1.0"
    assert "Phase 0" in data["phase"]
    assert "timestamp" in data


def test_docs_endpoint_accessible():
    """Verify OpenAPI interactive /docs UI is accessible."""
    response = client.get("/docs")
    assert response.status_code == 200
