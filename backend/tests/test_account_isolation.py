"""Comprehensive Multi-Account & Multi-User Data Isolation Test Suite.

Verifies the 10 mandatory isolation requirements:
1. User A sees only User A's subscriptions and candidates.
2. User B sees only User B's subscriptions and candidates.
3. User B cannot access or mutate User A's records by altering IDs or headers.
4. Syncing User B's Gmail does not modify User A's records.
5. Identical merchant names across users (e.g. Spotify) do not cause deduplication collisions.
6. Confirming or dismissing a candidate affects only its owner's data.
7. Switching accounts does not reuse the wrong OAuth token or sync cursor.
8. Repeated syncs remain idempotent for each account.
9. Exports and audit endpoints enforce strict tenant isolation.
10. Existing data survives migrations and repair operations without data loss.
"""

from datetime import date, datetime, timezone
import os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.models import (
    CandidateReviewStatus,
    DiscoverySource,
    EmailAccountStatus,
    Subscription,
    SubscriptionCandidate,
    SubscriptionStatus,
)
from backend.app.database import (
    delete_user_data,
    get_email_tokens,
    get_subscription_candidate,
    get_subscription_from_db,
    list_subscription_candidates,
    list_subscriptions_from_db,
    repair_mixed_data,
    save_email_account,
    save_subscription,
    save_subscription_candidate,
)
from backend.app.agent import initialize_orchestration_run


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def cleanup_users():
    """Ensure clean isolated state for test users before and after each test."""
    test_uids = [
        "user_alice_test",
        "user_bob_test",
        "user_sync_a",
        "user_sync_b",
        "user_repair_test",
    ]
    for uid in test_uids:
        delete_user_data(uid)
    yield
    for uid in test_uids:
        delete_user_data(uid)


# ==============================================================================
# Scenario 1 & 2: Independent Visibility for User A and User B
# ==============================================================================

def test_user_a_sees_only_own_subscriptions_and_candidates(client):
    """User A sees only User A's subscriptions and candidates; 0 of Bob's data leaks."""
    user_a = "user_alice_test"
    user_b = "user_bob_test"

    sub_a = Subscription(
        subscription_id=f"sub_email_{user_a}_netflix_001",
        service_name="Netflix",
        status=SubscriptionStatus.ACTIVE,
        user_id=user_a,
        account_id="acct_alice_1",
        account_email="alice@gmail.com",
        discovery_source=DiscoverySource.EMAIL.value,
        monthly_cost=15.49,
    )
    save_subscription(sub_a)

    cand_a = SubscriptionCandidate(
        candidate_id=f"cand_{user_a}_prime_001",
        user_id=user_a,
        account_id="acct_alice_1",
        account_email="alice@gmail.com",
        merchant_name="Amazon Prime",
        estimated_cost=14.99,
        review_status=CandidateReviewStatus.PENDING,
    )
    save_subscription_candidate(cand_a)

    sub_b = Subscription(
        subscription_id=f"sub_email_{user_b}_hulu_001",
        service_name="Hulu",
        status=SubscriptionStatus.ACTIVE,
        user_id=user_b,
        account_id="acct_bob_1",
        account_email="bob@gmail.com",
        discovery_source=DiscoverySource.EMAIL.value,
        monthly_cost=7.99,
    )
    save_subscription(sub_b)

    cand_b = SubscriptionCandidate(
        candidate_id=f"cand_{user_b}_disney_001",
        user_id=user_b,
        account_id="acct_bob_1",
        account_email="bob@gmail.com",
        merchant_name="Disney Plus",
        estimated_cost=10.99,
        review_status=CandidateReviewStatus.PENDING,
    )
    save_subscription_candidate(cand_b)

    # User A requests subscriptions
    resp_subs_a = client.get("/subscriptions", headers={"X-User-ID": user_a})
    assert resp_subs_a.status_code == 200
    subs_a_data = resp_subs_a.json()
    sub_ids_a = {s["subscription_id"] for s in subs_a_data}
    assert sub_a.subscription_id in sub_ids_a
    assert sub_b.subscription_id not in sub_ids_a

    # User A requests candidates
    resp_cands_a = client.get("/api/email/candidates", headers={"X-User-ID": user_a})
    assert resp_cands_a.status_code == 200
    cands_a_data = resp_cands_a.json()
    cand_ids_a = {c["candidate_id"] for c in cands_a_data}
    assert cand_a.candidate_id in cand_ids_a
    assert cand_b.candidate_id not in cand_ids_a


def test_user_b_sees_only_own_subscriptions_and_candidates(client):
    """User B sees only User B's subscriptions and candidates; 0 of Alice's data leaks."""
    user_a = "user_alice_test"
    user_b = "user_bob_test"

    sub_a = Subscription(
        subscription_id=f"sub_email_{user_a}_netflix_001",
        service_name="Netflix",
        status=SubscriptionStatus.ACTIVE,
        user_id=user_a,
        account_id="acct_alice_1",
        account_email="alice@gmail.com",
        discovery_source=DiscoverySource.EMAIL.value,
    )
    save_subscription(sub_a)

    sub_b = Subscription(
        subscription_id=f"sub_email_{user_b}_hulu_001",
        service_name="Hulu",
        status=SubscriptionStatus.ACTIVE,
        user_id=user_b,
        account_id="acct_bob_1",
        account_email="bob@gmail.com",
        discovery_source=DiscoverySource.EMAIL.value,
    )
    save_subscription(sub_b)

    resp_subs_b = client.get("/subscriptions", headers={"X-User-ID": user_b})
    assert resp_subs_b.status_code == 200
    subs_b_data = resp_subs_b.json()
    sub_ids_b = {s["subscription_id"] for s in subs_b_data}
    assert sub_b.subscription_id in sub_ids_b
    assert sub_a.subscription_id not in sub_ids_b


# ==============================================================================
# Scenario 3: Cross-User Tampering & Privilege Escalation Fails Closed
# ==============================================================================

def test_user_b_cannot_access_or_mutate_user_a_records(client):
    """User B cannot view, update, delete, or confirm User A's records by altering IDs."""
    user_a = "user_alice_test"
    user_b = "user_bob_test"

    sub_a = Subscription(
        subscription_id=f"sub_man_{user_a}_secret_001",
        service_name="Private Vault",
        status=SubscriptionStatus.ACTIVE,
        user_id=user_a,
        discovery_source=DiscoverySource.MANUAL.value,
        monthly_cost=100.0,
    )
    save_subscription(sub_a)

    cand_a = SubscriptionCandidate(
        candidate_id=f"cand_{user_a}_secret_001",
        user_id=user_a,
        merchant_name="Alice Secret",
        estimated_cost=50.0,
        review_status=CandidateReviewStatus.PENDING,
    )
    save_subscription_candidate(cand_a)

    # User B attempts to read User A's subscription
    get_res = client.get(f"/subscriptions/{sub_a.subscription_id}", headers={"X-User-ID": user_b})
    assert get_res.status_code == 404

    # User B attempts to edit User A's subscription
    put_res = client.put(
        f"/subscriptions/{sub_a.subscription_id}",
        headers={"X-User-ID": user_b},
        json={"monthly_cost": 0.0},
    )
    assert put_res.status_code == 404

    # User B attempts to archive User A's subscription
    del_res = client.delete(f"/subscriptions/{sub_a.subscription_id}", headers={"X-User-ID": user_b})
    assert del_res.status_code == 404

    # User B attempts to confirm User A's candidate
    conf_res = client.post(
        f"/api/email/candidates/{cand_a.candidate_id}/confirm",
        headers={"X-User-ID": user_b},
    )
    assert conf_res.status_code == 404

    # User B attempts to dismiss User A's candidate
    dism_res = client.post(
        f"/api/email/candidates/{cand_a.candidate_id}/dismiss",
        headers={"X-User-ID": user_b},
    )
    assert dism_res.status_code == 404

    # Verify Alice's records remain untouched
    preserved_sub = get_subscription_from_db(sub_a.subscription_id, user_id=user_a)
    assert preserved_sub is not None
    assert preserved_sub.monthly_cost == 100.0
    assert not preserved_sub.is_archived


# ==============================================================================
# Scenario 4: Mailbox Sync Isolation
# ==============================================================================

def test_syncing_user_b_does_not_modify_user_a_records(client):
    """Syncing User B's mailbox leaves User A's inventory and candidate pool strictly intact."""
    user_a = "user_sync_a"
    user_b = "user_sync_b"

    # Connect User A and sync
    auth_a = client.get("/api/email/oauth/url", headers={"X-User-ID": user_a})
    client.get(f"/api/email/oauth/callback?code=mock_code&state={auth_a.json()['state']}", headers={"X-User-ID": user_a})
    sync_a = client.post("/api/email/sync", headers={"X-User-ID": user_a})
    assert sync_a.status_code == 200

    cands_a_before = client.get("/api/email/candidates", headers={"X-User-ID": user_a}).json()
    assert len(cands_a_before) >= 3

    # Connect User B and sync
    auth_b = client.get("/api/email/oauth/url", headers={"X-User-ID": user_b})
    client.get(f"/api/email/oauth/callback?code=mock_code&state={auth_b.json()['state']}", headers={"X-User-ID": user_b})
    sync_b = client.post("/api/email/sync", headers={"X-User-ID": user_b})
    assert sync_b.status_code == 200

    # User A's candidates must remain identical
    cands_a_after = client.get("/api/email/candidates", headers={"X-User-ID": user_a}).json()
    assert [c["candidate_id"] for c in cands_a_before] == [c["candidate_id"] for c in cands_a_after]


# ==============================================================================
# Scenario 5: Cross-User Merchant Deduplication Safety
# ==============================================================================

def test_identical_merchants_across_users_do_not_collide(client):
    """When User A and User B both have Spotify, deduplication does not merge or overwrite them."""
    user_a = "user_alice_test"
    user_b = "user_bob_test"

    # Simulate sync candidate extraction for both users with identical merchant name
    cand_a = SubscriptionCandidate(
        candidate_id=f"cand_{user_a}_spotify_001",
        user_id=user_a,
        account_id="acct_alice_1",
        account_email="alice@gmail.com",
        merchant_name="Spotify Premium",
        estimated_cost=119.0,
        currency="INR",
        review_status=CandidateReviewStatus.PENDING,
    )
    save_subscription_candidate(cand_a)

    cand_b = SubscriptionCandidate(
        candidate_id=f"cand_{user_b}_spotify_002",
        user_id=user_b,
        account_id="acct_bob_1",
        account_email="bob@gmail.com",
        merchant_name="Spotify Premium",
        estimated_cost=179.0,
        currency="INR",
        review_status=CandidateReviewStatus.PENDING,
    )
    save_subscription_candidate(cand_b)

    # Alice fetches candidates -> sees only hers with cost 119
    cands_a = client.get("/api/email/candidates", headers={"X-User-ID": user_a}).json()
    spotify_a = next((c for c in cands_a if c["merchant_name"] == "Spotify Premium"), None)
    assert spotify_a is not None
    assert spotify_a["candidate_id"] == cand_a.candidate_id
    assert spotify_a["estimated_cost"] == 119.0

    # Bob fetches candidates -> sees only his with cost 179
    cands_b = client.get("/api/email/candidates", headers={"X-User-ID": user_b}).json()
    spotify_b = next((c for c in cands_b if c["merchant_name"] == "Spotify Premium"), None)
    assert spotify_b is not None
    assert spotify_b["candidate_id"] == cand_b.candidate_id
    assert spotify_b["estimated_cost"] == 179.0


# ==============================================================================
# Scenario 6: Candidate Confirmation & Dismissal Isolation
# ==============================================================================

def test_confirm_and_dismiss_affects_only_owner(client):
    """Confirming or dismissing candidate affects only the owner's inventory."""
    user_a = "user_alice_test"
    user_b = "user_bob_test"

    cand_a = SubscriptionCandidate(
        candidate_id=f"cand_{user_a}_youtube_001",
        user_id=user_a,
        account_id="acct_alice_1",
        account_email="alice@gmail.com",
        merchant_name="YouTube Premium",
        estimated_cost=12.99,
        currency="USD",
        review_status=CandidateReviewStatus.PENDING,
        source_message_id="msg_yt_a",
    )
    save_subscription_candidate(cand_a)

    cand_b = SubscriptionCandidate(
        candidate_id=f"cand_{user_b}_youtube_002",
        user_id=user_b,
        account_id="acct_bob_1",
        account_email="bob@gmail.com",
        merchant_name="YouTube Premium",
        estimated_cost=12.99,
        currency="USD",
        review_status=CandidateReviewStatus.PENDING,
        source_message_id="msg_yt_b",
    )
    save_subscription_candidate(cand_b)

    # Alice dismisses her candidate
    dism_res = client.post(f"/api/email/candidates/{cand_a.candidate_id}/dismiss", headers={"X-User-ID": user_a})
    assert dism_res.status_code == 200

    # Bob's candidate must STILL be pending
    bob_cands = client.get("/api/email/candidates", headers={"X-User-ID": user_b}).json()
    bob_yt = next(c for c in bob_cands if c["candidate_id"] == cand_b.candidate_id)
    assert bob_yt["review_status"] == "pending"

    # Bob confirms his candidate
    conf_res = client.post(f"/api/email/candidates/{cand_b.candidate_id}/confirm", headers={"X-User-ID": user_b})
    assert conf_res.status_code == 200
    bob_sub = conf_res.json()
    assert bob_sub["user_id"] == user_b

    # Alice must NOT see Bob's confirmed subscription in her inventory
    alice_subs = client.get("/subscriptions", headers={"X-User-ID": user_a}).json()
    assert not any(s["service_name"] == "YouTube Premium" for s in alice_subs)


# ==============================================================================
# Scenario 7: Multi-Account Token & Identity Isolation
# ==============================================================================

def test_switching_accounts_does_not_reuse_wrong_oauth_token(client):
    """Different email accounts hold independent encrypted credentials and identities."""
    user_id = "user_alice_test"
    acct_1 = "acct_alice_personal"
    acct_2 = "acct_alice_work"

    status_1 = EmailAccountStatus(
        account_id=acct_1,
        user_id=user_id,
        email_address="alice.personal@gmail.com",
        account_name="Personal",
        is_connected=True,
        is_default=True,
    )
    save_email_account(
        status_1,
        access_token="token_personal_secret",
        refresh_token="refresh_personal",
    )

    status_2 = EmailAccountStatus(
        account_id=acct_2,
        user_id=user_id,
        email_address="alice.work@gmail.com",
        account_name="Work",
        is_connected=True,
        is_default=False,
    )
    save_email_account(
        status_2,
        access_token="token_work_secret",
        refresh_token="refresh_work",
    )

    # Verify tokens are strictly separated
    tok_1 = get_email_tokens(user_id, account_id=acct_1)
    tok_2 = get_email_tokens(user_id, account_id=acct_2)
    assert tok_1["access_token"] == "token_personal_secret"
    assert tok_2["access_token"] == "token_work_secret"

    # Verify listing endpoint returns both accounts
    accounts_res = client.get("/api/email/accounts", headers={"X-User-ID": user_id})
    assert accounts_res.status_code == 200
    accounts = accounts_res.json()
    assert len(accounts) == 2
    acct_ids = {a["account_id"] for a in accounts}
    assert acct_ids == {acct_1, acct_2}


# ==============================================================================
# Scenario 8: Idempotency of Repeated Syncs
# ==============================================================================

def test_repeated_syncs_remain_idempotent_per_account(client):
    """Repeated mailbox syncs never duplicate candidates or inventory."""
    user_id = "user_sync_a"

    auth = client.get("/api/email/oauth/url", headers={"X-User-ID": user_id})
    client.get(f"/api/email/oauth/callback?code=mock_code&state={auth.json()['state']}", headers={"X-User-ID": user_id})

    # Sync 1
    res1 = client.post("/api/email/sync", headers={"X-User-ID": user_id})
    assert res1.status_code == 200
    count1 = res1.json()["synced_count"]
    assert count1 >= 3

    # Sync 2
    res2 = client.post("/api/email/sync", headers={"X-User-ID": user_id})
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["duplicates_skipped"] >= count1

    # Database candidate pool count must remain constant
    cands = client.get("/api/email/candidates", headers={"X-User-ID": user_id}).json()
    assert len(cands) == count1


# ==============================================================================
# Scenario 9: Privacy Export and Run Audit Trail Isolation
# ==============================================================================

def test_exports_and_audit_endpoints_enforce_isolation(client):
    """GDPR export and audit trail never leak other tenants' data."""
    user_a = "user_alice_test"
    user_b = "user_bob_test"

    sub_a = Subscription(
        subscription_id=f"sub_man_{user_a}_gym",
        service_name="Alice Gym",
        status=SubscriptionStatus.ACTIVE,
        user_id=user_a,
        discovery_source=DiscoverySource.MANUAL.value,
    )
    save_subscription(sub_a)

    sub_b = Subscription(
        subscription_id=f"sub_man_{user_b}_tennis",
        service_name="Bob Tennis",
        status=SubscriptionStatus.ACTIVE,
        user_id=user_b,
        discovery_source=DiscoverySource.MANUAL.value,
    )
    save_subscription(sub_b)

    # Initialize a run for Alice
    run_a, _ = initialize_orchestration_run(
        subscriptions=[sub_a],
        user_id=user_a,
        as_of=datetime.now(timezone.utc),
    )

    # Bob attempts to read Alice's run status
    run_res = client.get(f"/runs/{run_a.run_id}", headers={"X-User-ID": user_b})
    assert run_res.status_code == 404

    # Bob attempts to read Alice's run report
    rep_res = client.get(f"/runs/{run_a.run_id}/report", headers={"X-User-ID": user_b})
    assert rep_res.status_code == 404

    # User A export
    exp_a = client.get("/api/user/export", headers={"X-User-ID": user_a}).json()
    assert exp_a["user_id"] == user_a
    assert any(s["service_name"] == "Alice Gym" for s in exp_a["subscriptions"])
    assert not any(s["service_name"] == "Bob Tennis" for s in exp_a["subscriptions"])

    # User B export
    exp_b = client.get("/api/user/export", headers={"X-User-ID": user_b}).json()
    assert exp_b["user_id"] == user_b
    assert any(s["service_name"] == "Bob Tennis" for s in exp_b["subscriptions"])
    assert not any(s["service_name"] == "Alice Gym" for s in exp_b["subscriptions"])


# ==============================================================================
# Scenario 10: Migration & Collision Data Repair Safety
# ==============================================================================

def test_existing_data_survives_migrations_and_repair(client):
    """Repair endpoint creates automatic backup and safely reassigns records by verified provenance."""
    user_target = "user_repair_test"

    # Register target user email account
    acct = EmailAccountStatus(
        account_id=f"acct_{user_target}_1",
        user_id=user_target,
        email_address="repair.verified@gmail.com",
        is_connected=True,
    )
    save_email_account(acct)

    # Insert a misattributed candidate (attributed to 'user_default' but provenance points to repair.verified@gmail.com)
    misattributed_cand = SubscriptionCandidate(
        candidate_id="cand_misattributed_001",
        user_id="user_default",
        account_email="repair.verified@gmail.com",
        merchant_name="Reassigned Service",
        estimated_cost=29.99,
        review_status=CandidateReviewStatus.PENDING,
    )
    save_subscription_candidate(misattributed_cand)

    # Dry-run repair
    dry_res = client.post(
        "/api/database/repair",
        headers={"X-User-ID": user_target},
        json={"dry_run": True},
    )
    assert dry_res.status_code == 200
    dry_data = dry_res.json()
    assert dry_data["dry_run"] is True
    assert dry_data["reassigned_candidates"] >= 1

    # Verify cand was NOT mutated during dry-run
    cand_after_dry = get_subscription_candidate("cand_misattributed_001")
    assert cand_after_dry.user_id == "user_default"

    # Execute active repair
    active_res = client.post(
        "/api/database/repair",
        headers={"X-User-ID": user_target},
        json={"dry_run": False},
    )
    assert active_res.status_code == 200
    active_data = active_res.json()
    assert active_data["dry_run"] is False
    assert active_data["backup_created"] is not None
    assert Path(active_data["backup_created"]).exists()

    # Verify candidate is safely reassigned to verified user
    repaired_cand = get_subscription_candidate("cand_misattributed_001", user_id=user_target)
    assert repaired_cand is not None
    assert repaired_cand.user_id == user_target
    assert repaired_cand.account_id == acct.account_id
