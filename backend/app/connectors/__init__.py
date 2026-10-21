"""SubCan Connectors Package (Owned by Chinmay).

Responsible for:
- Connecting to mock streaming subscription sites and fixtures.
- Ingestion and retrieval of subscription status, renewal dates, and costs.
- Extraction of usage history and streaming activity logs.
- Retrieval of cancellation terms, data-deletion warnings, and pause policies.
- Constructing validated Evidence instances complying with DATA_CONTRACT.md.
- Managing connector discovery via thread-safe ConnectorRegistry.

Public API:
- fetch_subscriptions: Ingest subscriptions across all or specific connectors.
- collect_evidence: Collect usage and terms evidence for a specific subscription.
- get_subscription: Retrieve a specific subscription domain model.
- load_connector_data: Drop-in data loader producing (subscriptions, evidence_map) for agent orchestration.
- get_default_registry: Access global connector registry.
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from backend.app.connectors.base import BaseSubscriptionConnector
from backend.app.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorConnectionError,
    ConnectorError,
    ConnectorHTTPError,
    ConnectorResponseError,
    ConnectorTimeoutError,
    EvidenceExtractionError,
    SubscriptionNotFoundError,
    UnsupportedServiceError,
)
from backend.app.connectors.fixture import FixtureSubscriptionConnector
from backend.app.connectors.http import HttpMockSubscriptionConnector
from backend.app.connectors.normalization import (
    build_evidence,
    deduplicate_evidence,
    normalize_status,
    normalize_subscription_payload,
    parse_date,
    parse_timestamp,
)
from backend.app.connectors.registry import (
    ConnectorRegistry,
    get_default_registry,
    reset_default_registry,
)
from backend.app.connectors.services import (
    AudioCloudConnector,
    CineVaultConnector,
    FitStreamConnector,
    GameStreamConnector,
    MovieMaxConnector,
    ServiceConnectorAdapter,
)
from backend.app.models import Evidence, Subscription


def fetch_subscriptions(
    service_name: Optional[str] = None,
    registry: Optional[ConnectorRegistry] = None,
) -> List[Subscription]:
    """Retrieve subscriptions from mock streaming providers.

    Args:
        service_name: Optional service name or ID to filter by.
        registry: Optional custom ConnectorRegistry instance (defaults to global).

    Returns:
        List of validated Subscription domain entities.

    Raises:
        UnsupportedServiceError: If specified service_name is not supported.
        ConnectorError: On retrieval failure.
    """
    reg = registry or get_default_registry()

    if service_name:
        connector = reg.get_connector(service_name)
        return connector.list_subscriptions()

    results: List[Subscription] = []
    seen_ids = set()

    for connector in reg.list_connectors():
        for sub in connector.list_subscriptions():
            if sub.subscription_id not in seen_ids:
                seen_ids.add(sub.subscription_id)
                results.append(sub)

    return results


def get_subscription(
    subscription_id: str,
    registry: Optional[ConnectorRegistry] = None,
) -> Subscription:
    """Retrieve a single subscription by ID across registered connectors.

    Args:
        subscription_id: Target subscription identifier.
        registry: Optional custom ConnectorRegistry.

    Returns:
        Validated Subscription domain entity.

    Raises:
        SubscriptionNotFoundError: If subscription is not found.
    """
    reg = registry or get_default_registry()
    connector = reg.get_connector_for_subscription(subscription_id)
    return connector.get_subscription(subscription_id)


def collect_evidence(
    subscription_id: str,
    as_of: Optional[datetime] = None,
    registry: Optional[ConnectorRegistry] = None,
) -> List[Evidence]:
    """Collect usage logs and policy evidence for a subscription.

    Args:
        subscription_id: Subscription ID to collect evidence for.
        as_of: Optional reference collection timestamp.
        registry: Optional custom ConnectorRegistry.

    Returns:
        Deduplicated list of Evidence artifacts.

    Raises:
        SubscriptionNotFoundError: If subscription is not found.
    """
    reg = registry or get_default_registry()
    connector = reg.get_connector_for_subscription(subscription_id)
    return connector.collect_evidence(subscription_id, as_of=as_of)


def load_connector_data(
    target_subscription_ids: Optional[List[str]] = None,
    registry: Optional[ConnectorRegistry] = None,
    as_of: Optional[datetime] = None,
) -> Tuple[List[Subscription], Dict[str, List[Evidence]]]:
    """Ingest subscriptions and evidence formatted for direct agent orchestration.

    Compatible with agent.initialize_orchestration_run.

    Args:
        target_subscription_ids: Optional list of specific subscription IDs to retrieve.
        registry: Optional custom ConnectorRegistry.
        as_of: Optional reference collection timestamp.

    Returns:
        (subscriptions, evidence_map)
    """
    all_subs = fetch_subscriptions(registry=registry)

    if target_subscription_ids is not None:
        target_set = set(target_subscription_ids)
        filtered_subs = [s for s in all_subs if s.subscription_id in target_set]
    else:
        filtered_subs = all_subs

    evidence_map: Dict[str, List[Evidence]] = {}
    for sub in filtered_subs:
        ev_list = collect_evidence(sub.subscription_id, as_of=as_of, registry=registry)
        evidence_map[sub.subscription_id] = ev_list

    return filtered_subs, evidence_map


__all__ = [
    # Core functions
    "fetch_subscriptions",
    "get_subscription",
    "collect_evidence",
    "load_connector_data",
    # Registry
    "ConnectorRegistry",
    "get_default_registry",
    "reset_default_registry",
    # Base
    "BaseSubscriptionConnector",
    # Implementations
    "FixtureSubscriptionConnector",
    "HttpMockSubscriptionConnector",
    "ServiceConnectorAdapter",
    "CineVaultConnector",
    "AudioCloudConnector",
    "FitStreamConnector",
    "MovieMaxConnector",
    "GameStreamConnector",
    # Exceptions
    "ConnectorError",
    "UnsupportedServiceError",
    "SubscriptionNotFoundError",
    "ConnectorConnectionError",
    "ConnectorTimeoutError",
    "ConnectorHTTPError",
    "ConnectorAuthError",
    "ConnectorResponseError",
    "EvidenceExtractionError",
    # Normalization
    "build_evidence",
    "deduplicate_evidence",
    "parse_timestamp",
    "parse_date",
    "normalize_status",
    "normalize_subscription_payload",
]
