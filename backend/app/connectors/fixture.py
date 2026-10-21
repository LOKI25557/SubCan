"""SubCan Fixture-Backed Subscription Connector (Phase 1).

Deterministic, in-memory connector backed by JSON fixtures or structured mock records.
Serves as the primary reference implementation and test foundation.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from backend.app.connectors.base import BaseSubscriptionConnector
from backend.app.connectors.exceptions import (
    ConnectorResponseError,
    SubscriptionNotFoundError,
)
from backend.app.connectors.normalization import (
    build_evidence,
    deduplicate_evidence,
    ensure_timezone_aware,
    normalize_subscription_payload,
)
from backend.app.models import Evidence, Subscription


class FixtureSubscriptionConnector(BaseSubscriptionConnector):
    """Deterministic connector backed by fixtures/subscriptions.json or custom dictionary.

    Supports either a specific service (filtering by service_id) or all services in the fixture.
    """

    def __init__(
        self,
        service_id: str = "generic_fixture",
        service_name: str = "Generic Fixture Service",
        fixture_path: Optional[Union[str, Path]] = None,
        fixture_data: Optional[Dict[str, Any]] = None,
        supported_aliases: Optional[List[str]] = None,
    ) -> None:
        self._service_id = service_id
        self._service_name = service_name
        self._supported_aliases = supported_aliases or []
        self._fixture_path = (
            Path(fixture_path)
            if fixture_path is not None
            else Path(__file__).resolve().parents[3] / "fixtures" / "subscriptions.json"
        )
        self._static_data = fixture_data

    @property
    def service_id(self) -> str:
        return self._service_id

    @property
    def service_name(self) -> str:
        return self._service_name

    @property
    def supported_services(self) -> List[str]:
        aliases = [self._service_id, self._service_name]
        aliases.extend(self._supported_aliases)
        return aliases

    def _load_data(self) -> Dict[str, Any]:
        """Load fixture data from in-memory cache or file."""
        if self._static_data is not None:
            return deepcopy(self._static_data)

        if not self._fixture_path.exists():
            raise ConnectorResponseError(f"Fixture file not found: {self._fixture_path}")

        try:
            with self._fixture_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            raise ConnectorResponseError(f"Failed to read fixture: {exc}") from exc

    def list_subscriptions(self) -> List[Subscription]:
        """List all subscriptions matching this connector's service scope."""
        data = self._load_data()
        raw_subs = data.get("subscriptions", [])
        results: List[Subscription] = []

        for item in raw_subs:
            sub = normalize_subscription_payload(item)
            # If connector is service-specific, filter
            if self._service_id != "generic_fixture":
                if not (self.supports_service(sub.service_name) or self.supports_subscription(sub.subscription_id)):
                    continue
            results.append(sub)

        return results

    def get_subscription(self, subscription_id: str) -> Subscription:
        """Fetch a specific subscription by subscription_id."""
        subs = self.list_subscriptions()
        for sub in subs:
            if sub.subscription_id == subscription_id:
                return sub

        raise SubscriptionNotFoundError(
            subscription_id=subscription_id,
            service_name=self.service_name,
        )

    def get_usage_history(
        self,
        subscription_id: str,
        as_of: Optional[datetime] = None,
    ) -> List[Evidence]:
        """Extract usage history evidence from the fixture scenarios."""
        # First verify subscription exists
        self.get_subscription(subscription_id)

        capture_time = ensure_timezone_aware(as_of) or datetime.now(timezone.utc)
        data = self._load_data()
        evidence_list: List[Evidence] = []

        scenarios = data.get("scenarios", {})
        for scenario in scenarios.values():
            for ev_raw in scenario.get("evidence", []):
                if ev_raw.get("subscription_id") == subscription_id:
                    ev_type = ev_raw.get("evidence_type", "")
                    if ev_type in ("usage_history", "playback_logs", "activity_log"):
                        ev = Evidence.model_validate(deepcopy(ev_raw))
                        evidence_list.append(ev)

        return deduplicate_evidence(evidence_list)

    def get_cancellation_terms(
        self,
        subscription_id: str,
        as_of: Optional[datetime] = None,
    ) -> List[Evidence]:
        """Extract cancellation terms, pause policies, and warnings from fixture scenarios."""
        # First verify subscription exists
        self.get_subscription(subscription_id)

        data = self._load_data()
        evidence_list: List[Evidence] = []

        scenarios = data.get("scenarios", {})
        for scenario in scenarios.values():
            for ev_raw in scenario.get("evidence", []):
                if ev_raw.get("subscription_id") == subscription_id:
                    ev_type = ev_raw.get("evidence_type", "")
                    if ev_type in ("cancellation_terms", "pause_policy", "data_deletion_warning", "terms_of_service"):
                        ev = Evidence.model_validate(deepcopy(ev_raw))
                        evidence_list.append(ev)

        return deduplicate_evidence(evidence_list)
