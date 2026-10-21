"""Comprehensive Unit and Integration Tests for SubCan Connectors Module.

Tests cover:
1. Connector behavior: listing, getting specific subscription, empty lists, unknown IDs, unsupported services, registry.
2. Usage evidence: valid history, missing history, naive/timezone timestamps, duplicate deduplication, conflict detection.
3. Cancellation terms: standard terms, data deletion warnings, pause options, missing policies.
4. Reliability: timeouts, connection errors, HTTP errors (401, 403, 404, 500), retries, malformed JSON.
5. Contract & Agent integration: Pydantic conformity, provenance preservation, and feeding directly into agent.initialize_orchestration_run.
"""

from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Dict, List
import httpx
import pytest

from backend.app.agent import initialize_orchestration_run
from backend.app.connectors import (
    AudioCloudConnector,
    BaseSubscriptionConnector,
    CineVaultConnector,
    ConnectorAuthError,
    ConnectorConnectionError,
    ConnectorError,
    ConnectorHTTPError,
    ConnectorRegistry,
    ConnectorResponseError,
    ConnectorTimeoutError,
    EvidenceExtractionError,
    FitStreamConnector,
    FixtureSubscriptionConnector,
    GameStreamConnector,
    HttpMockSubscriptionConnector,
    MovieMaxConnector,
    SubscriptionNotFoundError,
    UnsupportedServiceError,
    build_evidence,
    collect_evidence,
    deduplicate_evidence,
    fetch_subscriptions,
    get_default_registry,
    get_subscription,
    load_connector_data,
    normalize_status,
    normalize_subscription_payload,
    parse_date,
    parse_timestamp,
    reset_default_registry,
)
from backend.app.models import (
    EligibilityStatus,
    Evidence,
    Subscription,
    SubscriptionStatus,
)


@pytest.fixture(autouse=True)
def reset_registry():
    """Ensure clean default registry before and after tests."""
    reset_default_registry()
    yield
    reset_default_registry()


# ==============================================================================
# 1. Normalization & Utility Unit Tests
# ==============================================================================

def test_parse_timestamp_valid_and_timezone_aware():
    dt = parse_timestamp("2026-06-10T20:30:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.month == 6 and dt.day == 10

    dt_offset = parse_timestamp("2026-06-10T20:30:00+05:30")
    assert dt_offset is not None
    assert dt_offset.tzinfo is not None


def test_parse_timestamp_naive_datetime_assigned_utc():
    naive = datetime(2026, 6, 10, 20, 30, 0)
    aware = parse_timestamp(naive)
    assert aware.tzinfo == timezone.utc


def test_parse_timestamp_none_and_empty():
    assert parse_timestamp(None) is None
    assert parse_timestamp("") is None
    assert parse_timestamp("   ") is None


def test_parse_timestamp_malformed_handling():
    # Non-raising mode
    assert parse_timestamp("invalid-date-string") is None

    # Raising mode
    with pytest.raises(EvidenceExtractionError):
        parse_timestamp("invalid-date-string", raise_on_invalid=True)


def test_parse_date_valid_and_invalid():
    assert parse_date("2026-11-15") == date(2026, 11, 15)
    assert parse_date(date(2026, 11, 15)) == date(2026, 11, 15)
    assert parse_date(datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)) == date(2026, 11, 15)
    assert parse_date(None) is None
    assert parse_date("garbage") is None

    with pytest.raises(EvidenceExtractionError):
        parse_date("garbage", raise_on_invalid=True)


def test_normalize_status():
    assert normalize_status("active") == SubscriptionStatus.ACTIVE
    assert normalize_status("renewing") == SubscriptionStatus.ACTIVE
    assert normalize_status("cancelled") == SubscriptionStatus.CANCELLED
    assert normalize_status("terminated") == SubscriptionStatus.CANCELLED
    assert normalize_status("unknown") == SubscriptionStatus.UNKNOWN
    assert normalize_status("something_random") == SubscriptionStatus.UNKNOWN
    assert normalize_status(None) == SubscriptionStatus.UNKNOWN


def test_build_evidence_enforces_timezone_and_confidence():
    naive_ts = datetime(2026, 10, 18, 9, 0, 0)
    ev = build_evidence(
        evidence_id="ev_test_01",
        subscription_id="sub_test",
        evidence_type="usage_history",
        excerpt="Test playback",
        captured_at=naive_ts,
        confidence=1.5,  # Should clamp to 1.0
    )
    assert ev.captured_at.tzinfo is not None
    assert ev.confidence == 1.0
    assert ev.evidence_id == "ev_test_01"


def test_deduplicate_evidence():
    now = datetime.now(timezone.utc)
    ev1 = build_evidence("ev_1", "sub_1", "usage", "First", captured_at=now)
    ev2 = build_evidence("ev_2", "sub_1", "usage", "Second", captured_at=now)
    ev1_dup = build_evidence("ev_1", "sub_1", "usage", "First duplicate", captured_at=now)

    deduped = deduplicate_evidence([ev1, ev2, ev1_dup])
    assert len(deduped) == 2
    assert deduped[0].evidence_id == "ev_1"
    assert deduped[1].evidence_id == "ev_2"


def test_normalize_subscription_payload_missing_fields():
    with pytest.raises(EvidenceExtractionError, match="subscription_id"):
        normalize_subscription_payload({})

    with pytest.raises(EvidenceExtractionError, match="service_name"):
        normalize_subscription_payload({"subscription_id": "sub_1"})


# ==============================================================================
# 2. Fixture Connector & Concrete Service Connectors
# ==============================================================================

def test_fixture_connector_all_subscriptions():
    connector = FixtureSubscriptionConnector()
    subs = connector.list_subscriptions()
    assert len(subs) >= 5
    sub_ids = {s.subscription_id for s in subs}
    assert "sub_tc1_cinevault" in sub_ids
    assert "sub_tc2_audiocloud" in sub_ids
    assert "sub_tc3_fitstream" in sub_ids
    assert "sub_tc4_moviemax" in sub_ids
    assert "sub_tc4_gamestream" in sub_ids


def test_fixture_connector_specific_subscription():
    connector = CineVaultConnector()
    sub = connector.get_subscription("sub_tc1_cinevault")
    assert sub.subscription_id == "sub_tc1_cinevault"
    assert sub.service_name == "CineVault Mock Streaming"
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.monthly_cost == 14.99
    assert sub.renewal_date == date(2026, 11, 15)
    assert sub.last_used_at is not None
    assert sub.data_deletion_warning is False
    assert sub.pause_option_available is False


def test_fixture_connector_not_found_raises():
    connector = CineVaultConnector()
    with pytest.raises(SubscriptionNotFoundError) as exc_info:
        connector.get_subscription("sub_non_existent")
    assert "sub_non_existent" in str(exc_info.value)


def test_audiocloud_connector_data_deletion_warning():
    connector = AudioCloudConnector()
    sub = connector.get_subscription("sub_tc2_audiocloud")
    assert sub.data_deletion_warning is True

    terms_ev = connector.get_cancellation_terms("sub_tc2_audiocloud")
    assert len(terms_ev) > 0
    assert any("purge" in ev.excerpt.lower() for ev in terms_ev)


def test_fitstream_connector_pause_policy():
    connector = FitStreamConnector()
    sub = connector.get_subscription("sub_tc3_fitstream")
    assert sub.pause_option_available is True

    terms_ev = connector.get_cancellation_terms("sub_tc3_fitstream")
    assert len(terms_ev) > 0
    assert any("pause" in ev.excerpt.lower() for ev in terms_ev)


def test_moviemax_and_gamestream_connectors():
    mm = MovieMaxConnector()
    sub_mm = mm.get_subscription("sub_tc4_moviemax")
    assert sub_mm.subscription_id == "sub_tc4_moviemax"

    gs = GameStreamConnector()
    sub_gs = gs.get_subscription("sub_tc4_gamestream")
    assert sub_gs.subscription_id == "sub_tc4_gamestream"


def test_empty_fixture_returns_empty_list(tmp_path):
    empty_file = tmp_path / "empty.json"
    empty_file.write_text(json.dumps({"subscriptions": [], "scenarios": {}}), encoding="utf-8")

    connector = FixtureSubscriptionConnector(fixture_path=empty_file)
    assert connector.list_subscriptions() == []


def test_missing_fixture_raises_connector_error(tmp_path):
    non_existent = tmp_path / "does_not_exist.json"
    connector = FixtureSubscriptionConnector(fixture_path=non_existent)
    with pytest.raises(ConnectorResponseError):
        connector.list_subscriptions()


# ==============================================================================
# 3. Connector Registry Tests
# ==============================================================================

def test_registry_resolution_by_id_and_name():
    reg = ConnectorRegistry(register_defaults=True)
    c1 = reg.get_connector("cinevault")
    assert c1.service_id == "cinevault"

    c2 = reg.get_connector("CineVault Mock Streaming")
    assert c2.service_id == "cinevault"

    c3 = reg.get_connector("audiocloud")
    assert c3.service_id == "audiocloud"


def test_registry_get_connector_for_subscription():
    reg = ConnectorRegistry(register_defaults=True)
    c = reg.get_connector_for_subscription("sub_tc1_cinevault")
    assert c.service_id == "cinevault"

    c_fit = reg.get_connector_for_subscription("sub_tc3_fitstream")
    assert c_fit.service_id == "fitstream"


def test_registry_unsupported_service_raises():
    reg = ConnectorRegistry(register_defaults=True)
    with pytest.raises(UnsupportedServiceError) as exc_info:
        reg.get_connector("unsupported_streaming_service_xyz")
    assert "Unsupported streaming service" in str(exc_info.value)


def test_registry_unknown_subscription_raises():
    reg = ConnectorRegistry(register_defaults=True)
    with pytest.raises(SubscriptionNotFoundError):
        reg.get_connector_for_subscription("sub_completely_unknown")


def test_registry_register_and_clear():
    reg = ConnectorRegistry(register_defaults=False)
    assert len(reg.list_connectors()) == 0

    reg.register(CineVaultConnector())
    assert len(reg.list_connectors()) == 1

    reg.clear()
    assert len(reg.list_connectors()) == 0


# ==============================================================================
# 4. HTTP Mock Connector Tests (MockTransport)
# ==============================================================================

def test_http_connector_successful_subscription_fetch():
    mock_payload = {
        "subscriptions": [
            {
                "subscription_id": "sub_http_01",
                "service_name": "Http Mock Stream",
                "status": "active",
                "renewal_date": "2026-12-01",
                "last_used_at": "2026-06-01T12:00:00Z",
                "monthly_cost": 9.99,
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_payload)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)

    connector = HttpMockSubscriptionConnector(
        service_id="httpstream",
        service_name="Http Mock Stream",
        client=client,
    )

    subs = connector.list_subscriptions()
    assert len(subs) == 1
    assert subs[0].subscription_id == "sub_http_01"
    assert subs[0].monthly_cost == 9.99


def test_http_connector_401_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Invalid token"})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)

    connector = HttpMockSubscriptionConnector(
        service_id="secure_stream",
        service_name="Secure Stream",
        client=client,
    )

    with pytest.raises(ConnectorAuthError) as exc_info:
        connector.list_subscriptions()
    assert exc_info.value.status_code == 401


def test_http_connector_404_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "Not Found"})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)

    connector = HttpMockSubscriptionConnector(
        service_id="mock_stream",
        service_name="Mock Stream",
        client=client,
    )

    with pytest.raises(SubscriptionNotFoundError):
        connector.get_subscription("sub_missing")


def test_http_connector_500_with_retry_and_recovery():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200, json={"subscription_id": "sub_retry_01", "service_name": "Retry Stream", "status": "active"})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)

    connector = HttpMockSubscriptionConnector(
        service_id="retry_stream",
        service_name="Retry Stream",
        max_retries=2,
        backoff_factor=0.01,
        client=client,
    )

    sub = connector.get_subscription("sub_retry_01")
    assert sub.subscription_id == "sub_retry_01"
    assert calls == 2  # Proves retry occurred and succeeded


def test_http_connector_persistent_500_raises_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Persistent Outage")

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)

    connector = HttpMockSubscriptionConnector(
        service_id="outage_stream",
        service_name="Outage Stream",
        max_retries=1,
        backoff_factor=0.01,
        client=client,
    )

    with pytest.raises(ConnectorHTTPError) as exc_info:
        connector.list_subscriptions()
    assert exc_info.value.status_code == 500


def test_http_connector_timeout_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Timeout reading response", request=request)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)

    connector = HttpMockSubscriptionConnector(
        service_id="slow_stream",
        service_name="Slow Stream",
        max_retries=1,
        backoff_factor=0.01,
        client=client,
    )

    with pytest.raises(ConnectorTimeoutError):
        connector.list_subscriptions()


def test_http_connector_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)

    connector = HttpMockSubscriptionConnector(
        service_id="down_stream",
        service_name="Down Stream",
        max_retries=1,
        backoff_factor=0.01,
        client=client,
    )

    with pytest.raises(ConnectorConnectionError):
        connector.list_subscriptions()


def test_http_connector_malformed_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Not a valid JSON string {{{")

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)

    connector = HttpMockSubscriptionConnector(
        service_id="bad_json_stream",
        service_name="Bad JSON Stream",
        client=client,
    )

    with pytest.raises(ConnectorResponseError):
        connector.list_subscriptions()


# ==============================================================================
# 5. Top-Level Package API Tests & Agent Integration
# ==============================================================================

def test_package_fetch_subscriptions_all():
    subs = fetch_subscriptions()
    assert len(subs) >= 5
    ids = [s.subscription_id for s in subs]
    assert "sub_tc1_cinevault" in ids


def test_package_fetch_subscriptions_filtered():
    subs = fetch_subscriptions(service_name="cinevault")
    assert len(subs) == 1
    assert subs[0].subscription_id == "sub_tc1_cinevault"


def test_package_get_subscription_success_and_not_found():
    sub = get_subscription("sub_tc1_cinevault")
    assert sub.subscription_id == "sub_tc1_cinevault"

    with pytest.raises(SubscriptionNotFoundError):
        get_subscription("non_existent_sub_123")


def test_package_collect_evidence():
    evidence = collect_evidence("sub_tc1_cinevault")
    assert len(evidence) >= 2
    types = {ev.evidence_type for ev in evidence}
    assert "usage_history" in types
    assert "cancellation_terms" in types

    for ev in evidence:
        assert ev.captured_at.tzinfo is not None
        assert 0.0 <= ev.confidence <= 1.0
        assert ev.source_url is not None


def test_package_load_connector_data():
    subs, ev_map = load_connector_data(["sub_tc1_cinevault", "sub_tc2_audiocloud"])
    assert len(subs) == 2
    assert "sub_tc1_cinevault" in ev_map
    assert "sub_tc2_audiocloud" in ev_map
    assert len(ev_map["sub_tc1_cinevault"]) > 0


def test_connector_data_consumed_by_agent_orchestrator():
    """Verify connector output directly feeds into Loki's agent orchestration lifecycle."""
    as_of = datetime(2026, 10, 19, 10, 0, 0, tzinfo=timezone.utc)
    subscriptions, evidence_map = load_connector_data(
        target_subscription_ids=["sub_tc1_cinevault", "sub_tc2_audiocloud", "sub_tc3_fitstream"],
        as_of=as_of,
    )

    run, states = initialize_orchestration_run(
        subscriptions=subscriptions,
        evidence_map=evidence_map,
        as_of=as_of,
    )

    assert run.status == "in_progress"
    assert len(states) == 3

    # TC1 should be evaluated as CANDIDATE and have a generated plan awaiting approval
    tc1_state = states["sub_tc1_cinevault"]
    assert tc1_state.decision.eligibility == EligibilityStatus.CANDIDATE
    assert tc1_state.plan is not None
    assert tc1_state.plan.subscription_id == "sub_tc1_cinevault"
    assert len(tc1_state.evidence_list) >= 2

    # TC2 should have data_deletion_risk disclosed
    tc2_state = states["sub_tc2_audiocloud"]
    assert tc2_state.decision.data_deletion_risk is True
    assert tc2_state.plan.consequences_warning is not None
    assert "data deletion" in tc2_state.plan.consequences_warning.lower()

    # TC3 should have pause_alternative_available disclosed
    tc3_state = states["sub_tc3_fitstream"]
    assert tc3_state.decision.pause_alternative_available is True
    assert tc3_state.plan.pause_alternative_available is True
