"""SubCan Base Subscription Connector Interface (Phase 1).

Defines the standard abstraction for all subscription data connectors.
Ensures uniform retrieval of subscriptions, usage logs, cancellation policies,
and evidence collection across all mock services.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from backend.app.connectors.exceptions import (
    ConnectorError,
    SubscriptionNotFoundError,
    UnsupportedServiceError,
)
from backend.app.connectors.normalization import deduplicate_evidence
from backend.app.models import Evidence, Subscription


class BaseSubscriptionConnector(ABC):
    """Abstract base class for streaming service subscription connectors.

    Every connector is responsible for:
    1. Identifying supported service(s).
    2. Listing user subscriptions or fetching a specific subscription.
    3. Retrieving usage history as validated Evidence.
    4. Retrieving cancellation terms, pause policies, and warnings as Evidence.
    5. Assembling comprehensive evidence for agent decision rules.
    """

    @property
    @abstractmethod
    def service_id(self) -> str:
        """Machine-readable unique identifier for the connector service (e.g. 'cinevault')."""
        pass

    @property
    @abstractmethod
    def service_name(self) -> str:
        """Human-readable display name of the streaming provider (e.g. 'CineVault Mock Streaming')."""
        pass

    @property
    def supported_services(self) -> List[str]:
        """List of service aliases, IDs, or full names supported by this connector."""
        return [self.service_id, self.service_name]

    def supports_service(self, service_identifier: str) -> bool:
        """Check if this connector supports the given service name or identifier (case-insensitive)."""
        if not service_identifier:
            return False
        clean = service_identifier.strip().lower()
        for item in self.supported_services:
            if clean == item.strip().lower() or clean in item.strip().lower():
                return True
        return False

    def supports_subscription(self, subscription_id: str) -> bool:
        """Check if a subscription ID corresponds to this connector."""
        if not subscription_id:
            return False
        clean = subscription_id.strip().lower()
        return self.service_id.lower() in clean

    @abstractmethod
    def list_subscriptions(self) -> List[Subscription]:
        """Retrieve all subscriptions available for this connector service.

        Returns:
            List of validated Subscription domain entities.

        Raises:
            ConnectorError: On retrieval, network, or normalization failure.
        """
        pass

    @abstractmethod
    def get_subscription(self, subscription_id: str) -> Subscription:
        """Retrieve a specific subscription by its ID.

        Args:
            subscription_id: Unique subscription ID (e.g. 'sub_tc1_cinevault').

        Returns:
            Validated Subscription domain entity.

        Raises:
            SubscriptionNotFoundError: If the subscription does not exist.
            ConnectorError: On retrieval, network, or normalization failure.
        """
        pass

    @abstractmethod
    def get_usage_history(
        self,
        subscription_id: str,
        as_of: Optional[datetime] = None,
    ) -> List[Evidence]:
        """Extract historical streaming/playback activity records as Evidence artifacts.

        Args:
            subscription_id: Subscription ID to query.
            as_of: Reference capture timestamp (defaults to current UTC).

        Returns:
            List of Evidence items with evidence_type='usage_history'.

        Raises:
            SubscriptionNotFoundError: If subscription does not exist.
            ConnectorError: On retrieval or extraction failure.
        """
        pass

    @abstractmethod
    def get_cancellation_terms(
        self,
        subscription_id: str,
        as_of: Optional[datetime] = None,
    ) -> List[Evidence]:
        """Extract cancellation policies, pause terms, and warning notices as Evidence.

        Args:
            subscription_id: Subscription ID to query.
            as_of: Reference capture timestamp (defaults to current UTC).

        Returns:
            List of Evidence items (cancellation_terms, pause_policy, data_deletion_warning).

        Raises:
            SubscriptionNotFoundError: If subscription does not exist.
            ConnectorError: On retrieval or extraction failure.
        """
        pass

    def collect_evidence(
        self,
        subscription_id: str,
        as_of: Optional[datetime] = None,
    ) -> List[Evidence]:
        """Collect all available evidence (usage history, cancellation terms, policies) for a subscription.

        Merges and deduplicates evidence items by evidence_id.

        Args:
            subscription_id: Subscription ID to query.
            as_of: Reference capture timestamp (defaults to current UTC).

        Returns:
            Deduplicated list of Evidence items.
        """
        usage_evidence = self.get_usage_history(subscription_id, as_of=as_of)
        terms_evidence = self.get_cancellation_terms(subscription_id, as_of=as_of)
        combined = list(usage_evidence) + list(terms_evidence)
        return deduplicate_evidence(combined)

    def get_renewal_status(self, subscription_id: str) -> Dict[str, Any]:
        """Query current renewal and billing status directly from the mock provider.

        Useful for independent verification or live status checks.
        """
        sub = self.get_subscription(subscription_id)
        return {
            "subscription_id": sub.subscription_id,
            "service_name": sub.service_name,
            "status": sub.status.value,
            "renewal_date": sub.renewal_date.isoformat() if sub.renewal_date else None,
            "pause_option_available": sub.pause_option_available,
        }
