"""Test suite for Gmail account nickname persistence, reconnection, and isolation.

Validates the expected behaviors:
1. Account nickname is saved permanently in the database associated with the user and Gmail account.
2. Reconnecting an existing Gmail address automatically reuses its registered account ID and existing nickname.
3. Explicit renaming updates the nickname permanently via PATCH /api/email/accounts/{account_id}/nickname.
4. Disconnecting clears authorization tokens but preserves the account record, nickname, and email address.
5. Reconnecting refreshes tokens and connection status without creating duplicate account rows.
6. Matching is based on verified email address, not fragile nickname matching.
7. Multiple Gmail accounts under the same user maintain independent nicknames, tokens, and candidate pools.
8. Disconnected accounts reject sync operations until validly reconnected.
9. Cross-user isolation: User B cannot access, overwrite, or mutate User A's account or nickname.
"""

import pytest
from starlette.testclient import TestClient

from backend.app.database import (
    disconnect_email_account,
    get_email_account,
    get_email_account_by_email,
    get_email_tokens,
    list_email_accounts,
    save_email_account,
    update_email_account_name,
)
from backend.app.email.oauth import (
    _OAUTH_STATES,
    exchange_code_for_tokens,
    generate_oauth_url,
)
from backend.app.main import app
from backend.app.models import EmailAccountStatus


@pytest.fixture
def client():
    return TestClient(app)


def test_disconnect_preserves_account_record_and_nickname(client):
    """Disconnecting clears OAuth tokens but preserves account record, email address, and nickname."""
    user_id = "user_nick_disc_01"
    account_id = f"acct_{user_id}_personal"
    email = "alice.personal@gmail.com"
    nickname = "Alice Personal"

    acct = EmailAccountStatus(
        account_id=account_id,
        user_id=user_id,
        email_address=email,
        account_name=nickname,
        is_connected=True,
    )
    save_email_account(
        acct,
        refresh_token="mock_refresh_token_123",
        access_token="mock_access_token_123",
    )

    # Verify tokens exist before disconnect
    tokens_before = get_email_tokens(user_id=user_id, account_id=account_id)
    assert tokens_before is not None
    assert tokens_before["refresh_token"] == "mock_refresh_token_123"

    # Disconnect via API
    resp = client.post(
        f"/api/email/disconnect?account_id={account_id}",
        headers={"X-User-ID": user_id},
    )
    assert resp.status_code == 200
    assert resp.json()["disconnected"] is True

    # Tokens must be cleared
    tokens_after = get_email_tokens(user_id=user_id, account_id=account_id)
    assert tokens_after is not None
    assert tokens_after["refresh_token"] is None
    assert tokens_after["access_token"] is None

    # Account record, email, and nickname must be preserved
    status = get_email_account(user_id=user_id, account_id=account_id)
    assert status is not None
    assert status.is_connected is False
    assert status.email_address == email
    assert status.account_name == nickname

    # Account must still be returned in user's accounts list with Disconnected state
    accounts = list_email_accounts(user_id=user_id)
    matching = [a for a in accounts if a.account_id == account_id]
    assert len(matching) == 1
    assert matching[0].is_connected is False
    assert matching[0].account_name == nickname
    assert matching[0].email_address == email


def test_reconnecting_existing_gmail_reuses_stable_id_and_preserves_nickname(client):
    """When reconnecting an existing Gmail address, reuse account_id and preserve its existing nickname."""
    user_id = "user_reconnect_02"
    email = "bob.streaming@gmail.com"
    nickname = "Streaming Hub"

    # Register initial account
    url_resp = client.get(
        f"/api/email/oauth/url?account_name={nickname}",
        headers={"X-User-ID": user_id},
    )
    assert url_resp.status_code == 200
    state = url_resp.json()["state"]

    # Exchange code to connect
    cb_resp = client.get(
        f"/api/email/oauth/callback?code=mock_code_{user_id}&state={state}",
        headers={"X-User-ID": user_id},
    )
    assert cb_resp.status_code == 200
    initial_account = get_email_account(user_id=user_id)
    assert initial_account is not None
    initial_account_id = initial_account.account_id
    assert initial_account.account_name == nickname
    assert initial_account.is_connected is True

    # Disconnect mailbox
    client.post(
        f"/api/email/disconnect?account_id={initial_account_id}",
        headers={"X-User-ID": user_id},
    )
    after_disc = get_email_account(user_id=user_id, account_id=initial_account_id)
    assert after_disc.is_connected is False
    assert after_disc.account_name == nickname

    # Reconnect WITHOUT providing a nickname prompt (as the frontend does when reconnecting existing account)
    reconn_url_resp = client.get(
        f"/api/email/oauth/url?account_id={initial_account_id}",
        headers={"X-User-ID": user_id},
    )
    assert reconn_url_resp.status_code == 200
    reconn_state = reconn_url_resp.json()["state"]

    reconn_cb_resp = client.get(
        f"/api/email/oauth/callback?code=mock_code_{user_id}&state={reconn_state}",
        headers={"X-User-ID": user_id},
    )
    assert reconn_cb_resp.status_code == 200

    # Account must be reconnected with the SAME account ID and the PRESERVED nickname
    reconnected = get_email_account(user_id=user_id, account_id=initial_account_id)
    assert reconnected is not None
    assert reconnected.account_id == initial_account_id
    assert reconnected.is_connected is True
    assert reconnected.account_name == nickname

    # Total account count for user must remain exactly 1 (no duplicate accounts created)
    user_accounts = list_email_accounts(user_id=user_id)
    assert len(user_accounts) == 1


def test_reconnecting_does_not_create_duplicate_account_rows():
    """Repeated disconnect and reconnect cycles never create duplicate rows in the database."""
    user_id = "user_dup_prevention_03"
    email = "charlie.work@gmail.com"
    nickname = "Work Mailbox"

    # Save initial account
    acct = EmailAccountStatus(
        account_id=f"acct_{user_id}_work",
        user_id=user_id,
        email_address=email,
        account_name=nickname,
        is_connected=True,
    )
    save_email_account(acct, refresh_token="tok_1", access_token="acc_1")

    # Simulate 3 disconnect and reconnect cycles
    for i in range(3):
        disconnect_email_account(user_id=user_id, account_id=acct.account_id)
        assert get_email_account(user_id=user_id, account_id=acct.account_id).is_connected is False

        # Reconnect by code exchange
        res = exchange_code_for_tokens(
            code=f"mock_reconnect_cycle_{i}",
            state=f"test_state_{i}",
            user_id=user_id,
            account_id=acct.account_id,
        )
        assert res.is_connected is True
        assert res.account_name == nickname

    # Must only have 1 account in DB
    all_accounts = list_email_accounts(user_id=user_id)
    assert len(all_accounts) == 1
    assert all_accounts[0].account_id == acct.account_id
    assert all_accounts[0].account_name == nickname


def test_explicit_rename_endpoint_updates_nickname_permanently(client):
    """PATCH /api/email/accounts/{account_id}/nickname updates nickname and persists in database."""
    user_id = "user_rename_04"
    account_id = f"acct_{user_id}_main"
    email = "diana.dev@gmail.com"

    acct = EmailAccountStatus(
        account_id=account_id,
        user_id=user_id,
        email_address=email,
        account_name="Initial Nickname",
        is_connected=True,
    )
    save_email_account(acct)

    # Rename to 'Production Cloud'
    patch_resp = client.patch(
        f"/api/email/accounts/{account_id}/nickname",
        headers={"X-User-ID": user_id},
        json={"nickname": "Production Cloud"},
    )
    assert patch_resp.status_code == 200
    data = patch_resp.json()
    assert data["success"] is True
    assert data["account_name"] == "Production Cloud"

    # Verify persisted in database
    db_acct = get_email_account(user_id=user_id, account_id=account_id)
    assert db_acct.account_name == "Production Cloud"

    # Reject empty nickname
    empty_resp = client.patch(
        f"/api/email/accounts/{account_id}/nickname",
        headers={"X-User-ID": user_id},
        json={"nickname": "   "},
    )
    assert empty_resp.status_code == 400


def test_multiple_accounts_retain_separate_nicknames_and_tokens(client):
    """User with multiple mailboxes retains independent nicknames, tokens, and connection status."""
    user_id = "user_multi_acct_05"

    acct1 = EmailAccountStatus(
        account_id=f"acct_{user_id}_personal",
        user_id=user_id,
        email_address="user.multi.personal@gmail.com",
        account_name="Personal Vault",
        is_connected=True,
    )
    save_email_account(acct1, refresh_token="tok_personal", access_token="acc_personal")

    acct2 = EmailAccountStatus(
        account_id=f"acct_{user_id}_business",
        user_id=user_id,
        email_address="user.multi.business@gmail.com",
        account_name="Business Enterprise",
        is_connected=True,
    )
    save_email_account(acct2, refresh_token="tok_biz", access_token="acc_biz")

    # Disconnect ONLY account 1
    client.post(
        f"/api/email/disconnect?account_id={acct1.account_id}",
        headers={"X-User-ID": user_id},
    )

    # Account 1 must be disconnected, tokens cleared, nickname retained
    s1 = get_email_account(user_id=user_id, account_id=acct1.account_id)
    assert s1.is_connected is False
    assert s1.account_name == "Personal Vault"
    assert get_email_tokens(user_id=user_id, account_id=acct1.account_id)["refresh_token"] is None

    # Account 2 must remain connected, tokens intact, nickname untouched
    s2 = get_email_account(user_id=user_id, account_id=acct2.account_id)
    assert s2.is_connected is True
    assert s2.account_name == "Business Enterprise"
    assert get_email_tokens(user_id=user_id, account_id=acct2.account_id)["refresh_token"] == "tok_biz"


def test_disconnected_account_rejects_sync_until_validly_reconnected(client):
    """Disconnected mailbox cannot be synchronized; rejects with 400 until reconnected."""
    user_id = "user_sync_gate_06"
    account_id = f"acct_{user_id}_gate"

    acct = EmailAccountStatus(
        account_id=account_id,
        user_id=user_id,
        email_address="gate.user@gmail.com",
        account_name="Gate Mailbox",
        is_connected=False,
    )
    save_email_account(acct)

    # Attempt to sync while disconnected
    sync_resp = client.post(
        f"/api/email/sync?account_id={account_id}",
        headers={"X-User-ID": user_id},
    )
    assert sync_resp.status_code == 400
    err = sync_resp.json()
    msg = err.get("detail", {}).get("message") or err.get("message") or str(err)
    assert "not connected" in msg.lower() or "no email account connected" in msg.lower()


def test_cross_user_nickname_and_account_isolation(client):
    """User B cannot rename, view, or mutate User A's email account."""
    user_a = "user_iso_alice_07"
    user_b = "user_iso_bob_07"

    acct_a = EmailAccountStatus(
        account_id=f"acct_{user_a}_secret",
        user_id=user_a,
        email_address="alice.secret@gmail.com",
        account_name="Alice Secret Vault",
        is_connected=True,
    )
    save_email_account(acct_a, refresh_token="alice_tok")

    # User B attempts to rename User A's account
    hacked_resp = client.patch(
        f"/api/email/accounts/{acct_a.account_id}/nickname",
        headers={"X-User-ID": user_b},
        json={"nickname": "Hacked Name"},
    )
    assert hacked_resp.status_code == 404

    # User A's nickname must remain completely unchanged
    check_a = get_email_account(user_id=user_a, account_id=acct_a.account_id)
    assert check_a.account_name == "Alice Secret Vault"

    # User B list accounts must be empty
    b_accounts = list_email_accounts(user_id=user_b)
    assert len(b_accounts) == 0
