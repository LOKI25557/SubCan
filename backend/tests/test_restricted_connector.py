"""Tests for restricted website connector handling and manual cancellation guides."""

import pytest
from fastapi.testclient import TestClient
from backend.app.main import app

client = TestClient(app)


def test_restricted_service_guide_for_known_providers():
    """Verify restricted services return official account URLs, steps, and consequence warnings without bypassing security."""
    restricted_services = [
        ("bloomberg", "Bloomberg Terminal / Professional"),
        ("adobe", "Adobe Creative Cloud"),
        ("wsj", "The Wall Street Journal"),
        ("gym_in_person", "Physical Health Club / Gym"),
    ]

    for service_id, expected_name in restricted_services:
        resp = client.get(f"/services/{service_id}/manual-guide")
        assert resp.status_code == 200
        guide = resp.json()

        assert guide["service_name"] == expected_name
        assert guide["requires_manual_action"] is True
        assert guide["official_url"].startswith("https://")
        assert len(guide["steps"]) >= 3
        assert guide["data_loss_warning"] is not None

        # Confirm no bypass or unauthorized scraping instructions
        full_text = " ".join(guide["steps"]).lower()
        assert "bypass" not in full_text
        assert "hack" not in full_text
        assert "scrape" not in full_text


def test_restricted_service_guide_fallback_for_arbitrary_service():
    """Verify arbitrary service names return a safe generic portal checklist."""
    resp = client.get("/services/custom_software_saas/manual-guide")
    assert resp.status_code == 200
    guide = resp.json()

    assert guide["service_name"] == "custom_software_saas"
    assert guide["requires_manual_action"] is True
    assert len(guide["steps"]) >= 4
    assert any("billing" in s.lower() or "subscription" in s.lower() for s in guide["steps"])
