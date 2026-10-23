"""Tests for SubCan manual subscription entry, CRUD lifecycle, and user isolation."""

import pytest
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.database import initialize_schema

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    initialize_schema()
    from backend.app.database import delete_user_data
    delete_user_data("test_user_manual_1")
    delete_user_data("test_user_manual_2")
    delete_user_data("test_user_manual_3")


def test_create_manual_subscription_success():
    """Verify creating a manual subscription with valid data persists and returns correct fields."""
    payload = {
        "service_name": "ProtonMail Unlimited",
        "monthly_cost": 9.99,
        "currency": "USD",
        "billing_cycle": "monthly",
        "renewal_date": "2026-11-15",
        "last_used_at": "2026-06-01T10:00:00",
        "category": "privacy",
        "status": "active",
        "notes": "Annual privacy bundle",
        "manual_action_url": "https://account.proton.me/u/0/mail",
    }
    response = client.post("/subscriptions", json=payload, headers={"X-User-ID": "test_user_manual_1"})
    assert response.status_code == 201
    data = response.json()

    assert data["subscription_id"].startswith("sub_man_")
    assert data["service_name"] == "ProtonMail Unlimited"
    assert data["monthly_cost"] == 9.99
    assert data["currency"] == "USD"
    assert data["billing_cycle"] == "monthly"
    assert data["renewal_date"] == "2026-11-15"
    assert data["category"] == "privacy"
    assert data["status"] == "active"
    assert data["discovery_source"] == "manual"
    assert data["notes"] == "Annual privacy bundle"
    assert data["manual_action_url"] == "https://account.proton.me/u/0/mail"
    assert data["user_id"] == "test_user_manual_1"
    assert data["is_archived"] is False


def test_create_manual_subscription_with_unknown_and_missing_optional_fields():
    """Verify that unknown price, missing dates, and null optional fields are preserved honestly."""
    payload = {
        "service_name": "Legacy Gym Membership",
        "monthly_cost": None,
        "renewal_date": None,
        "last_used_at": None,
        "status": "unknown",
    }
    response = client.post("/subscriptions", json=payload, headers={"X-User-ID": "test_user_manual_2"})
    assert response.status_code == 201
    data = response.json()

    assert data["service_name"] == "Legacy Gym Membership"
    assert data["monthly_cost"] is None
    assert data["renewal_date"] is None
    assert data["last_used_at"] is None
    assert data["status"] == "unknown"
    assert data["confidence"] == 1.0  # User entered directly


def test_create_manual_subscription_validation_failures():
    """Verify input validation rejects empty service name and negative costs."""
    # 1. Empty service name
    resp = client.post("/subscriptions", json={"service_name": "   "})
    assert resp.status_code == 422

    # 2. Negative monthly cost
    resp2 = client.post("/subscriptions", json={"service_name": "Test Sub", "monthly_cost": -12.50})
    assert resp2.status_code == 422


def test_get_and_update_manual_subscription():
    """Verify updating a manual subscription updates fields without altering stable subscription_id."""
    create_resp = client.post(
        "/subscriptions",
        json={"service_name": "Figma Professional", "monthly_cost": 12.00, "billing_cycle": "monthly"},
        headers={"X-User-ID": "test_user_update"},
    )
    assert create_resp.status_code == 201
    sub_id = create_resp.json()["subscription_id"]

    # Update price and notes
    update_payload = {
        "monthly_cost": 15.00,
        "notes": "Upgraded to Organization plan",
        "renewal_date": "2026-12-01",
    }
    upd_resp = client.put(f"/subscriptions/{sub_id}", json=update_payload, headers={"X-User-ID": "test_user_update"})
    assert upd_resp.status_code == 200
    updated = upd_resp.json()

    assert updated["subscription_id"] == sub_id
    assert updated["service_name"] == "Figma Professional"
    assert updated["monthly_cost"] == 15.00
    assert updated["notes"] == "Upgraded to Organization plan"
    assert updated["renewal_date"] == "2026-12-01"


def test_archive_manual_subscription():
    """Verify archiving a subscription soft-deletes it and excludes it from the default listing."""
    create_resp = client.post(
        "/subscriptions",
        json={"service_name": "Archived Streaming Service", "monthly_cost": 5.99},
        headers={"X-User-ID": "test_user_archive"},
    )
    sub_id = create_resp.json()["subscription_id"]

    # Delete (archive)
    del_resp = client.delete(f"/subscriptions/{sub_id}", headers={"X-User-ID": "test_user_archive"})
    assert del_resp.status_code == 200
    assert del_resp.json()["archived"] is True

    # Confirm it is excluded from list
    list_resp = client.get("/subscriptions", headers={"X-User-ID": "test_user_archive"})
    assert list_resp.status_code == 200
    subs = list_resp.json()
    assert not any(s["subscription_id"] == sub_id for s in subs)


def test_tenant_and_user_isolation_for_subscriptions():
    """Verify User A cannot access, view, modify, or archive User B's subscriptions."""
    # User A creates a subscription
    resp_a = client.post(
        "/subscriptions",
        json={"service_name": "User A Private Service", "monthly_cost": 50.00},
        headers={"X-User-ID": "user_alice"},
    )
    sub_id_a = resp_a.json()["subscription_id"]

    # User B lists subscriptions -> should NOT see User A's subscription
    resp_b_list = client.get("/subscriptions", headers={"X-User-ID": "user_bob"})
    assert resp_b_list.status_code == 200
    b_subs = resp_b_list.json()
    assert not any(s["subscription_id"] == sub_id_a for s in b_subs)

    # User B attempts to GET User A's subscription directly
    resp_b_get = client.get(f"/subscriptions/{sub_id_a}", headers={"X-User-ID": "user_bob"})
    assert resp_b_get.status_code == 404

    # User B attempts to UPDATE User A's subscription
    resp_b_put = client.put(
        f"/subscriptions/{sub_id_a}",
        json={"monthly_cost": 0.01},
        headers={"X-User-ID": "user_bob"},
    )
    assert resp_b_put.status_code == 404

    # User B attempts to DELETE (archive) User A's subscription
    resp_b_del = client.delete(f"/subscriptions/{sub_id_a}", headers={"X-User-ID": "user_bob"})
    assert resp_b_del.status_code == 404
