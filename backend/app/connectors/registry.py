"""SubCan Connector Registry (Phase 1).

Thread-safe registry for managing, resolving, and injecting subscription connectors.
Provides resolution by service ID, service name, alias, or subscription ID.
"""

import threading
from typing import Dict, List, Optional

from backend.app.connectors.base import BaseSubscriptionConnector
from backend.app.connectors.exceptions import (
    SubscriptionNotFoundError,
    UnsupportedServiceError,
)
from backend.app.connectors.services import (
    AudioCloudConnector,
    CineVaultConnector,
    FitStreamConnector,
    GameStreamConnector,
    MovieMaxConnector,
)


class ConnectorRegistry:
    """Thread-safe registry mapping service identifiers to BaseSubscriptionConnector instances."""

    def __init__(self, register_defaults: bool = True) -> None:
        self._lock = threading.RLock()
        self._connectors_by_id: Dict[str, BaseSubscriptionConnector] = {}
        if register_defaults:
            self._register_default_connectors()

    def _register_default_connectors(self) -> None:
        """Register the 5 canonical SubCan mock service connectors."""
        self.register(CineVaultConnector())
        self.register(AudioCloudConnector())
        self.register(FitStreamConnector())
        self.register(MovieMaxConnector())
        self.register(GameStreamConnector())

    def register(
        self,
        connector: BaseSubscriptionConnector,
        overwrite: bool = True,
    ) -> None:
        """Register a connector instance in the registry."""
        with self._lock:
            key = connector.service_id.lower()
            if not overwrite and key in self._connectors_by_id:
                return
            self._connectors_by_id[key] = connector

    def unregister(self, service_id: str) -> Optional[BaseSubscriptionConnector]:
        """Remove a connector by its service ID."""
        with self._lock:
            return self._connectors_by_id.pop(service_id.lower(), None)

    def clear(self) -> None:
        """Clear all registered connectors."""
        with self._lock:
            self._connectors_by_id.clear()

    def reset_defaults(self) -> None:
        """Reset registry to the standard default 5 mock connectors."""
        with self._lock:
            self._connectors_by_id.clear()
            self._register_default_connectors()

    def list_connectors(self) -> List[BaseSubscriptionConnector]:
        """Return a list of all currently registered connector instances."""
        with self._lock:
            return list(self._connectors_by_id.values())

    def list_supported_services(self) -> List[str]:
        """Return a list of all supported service names across registered connectors."""
        with self._lock:
            names = []
            for c in self._connectors_by_id.values():
                names.append(c.service_name)
            return sorted(names)

    def get_connector(self, service_name_or_id: str) -> BaseSubscriptionConnector:
        """Resolve the appropriate connector for a given service ID, display name, or alias.

        Args:
            service_name_or_id: Case-insensitive service identifier.

        Returns:
            The matching BaseSubscriptionConnector instance.

        Raises:
            UnsupportedServiceError: If no registered connector supports the service.
        """
        if not service_name_or_id:
            raise UnsupportedServiceError("Service identifier must not be empty.")

        clean = service_name_or_id.strip().lower()

        with self._lock:
            # 1. Exact match by service_id
            if clean in self._connectors_by_id:
                return self._connectors_by_id[clean]

            # 2. Check supports_service on each connector
            for connector in self._connectors_by_id.values():
                if connector.supports_service(clean):
                    return connector

            # 3. Check supports_subscription if it looks like a subscription ID
            for connector in self._connectors_by_id.values():
                if connector.supports_subscription(clean):
                    return connector

        raise UnsupportedServiceError(
            f"Unsupported streaming service: '{service_name_or_id}'. "
            f"Supported services: {self.list_supported_services()}"
        )

    def get_connector_for_subscription(
        self,
        subscription_id: str,
    ) -> BaseSubscriptionConnector:
        """Find the connector responsible for a given subscription ID.

        Checks supports_subscription first, then probes registered connectors.

        Raises:
            SubscriptionNotFoundError: If the subscription cannot be found in any connector.
        """
        if not subscription_id:
            raise SubscriptionNotFoundError(subscription_id="", message="Subscription ID must not be empty.")

        clean = subscription_id.strip()

        with self._lock:
            connectors = list(self._connectors_by_id.values())

        # First pass: matching by connector prefix / supports_subscription
        for c in connectors:
            if c.supports_subscription(clean):
                return c

        # Second pass: check if any connector actually has this subscription
        for c in connectors:
            try:
                c.get_subscription(clean)
                return c
            except Exception:
                continue

        raise SubscriptionNotFoundError(
            subscription_id=clean,
            message=f"Subscription '{clean}' is not supported or was not found in any registered connector.",
        )


_GLOBAL_REGISTRY = ConnectorRegistry(register_defaults=True)


def get_default_registry() -> ConnectorRegistry:
    """Return the global default connector registry."""
    return _GLOBAL_REGISTRY


def reset_default_registry() -> None:
    """Reset the global connector registry."""
    _GLOBAL_REGISTRY.reset_defaults()
