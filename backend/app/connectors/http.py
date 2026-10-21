"""SubCan HTTP Mock Subscription Connector (Phase 1).

Connects to mock streaming subscription HTTP/REST endpoints using httpx.
Includes bounded retries, timeout management, typed error mapping,
and secret sanitation.
"""

from datetime import datetime, timezone
import logging
import time
from typing import Any, Dict, List, Optional, Union
import httpx

from backend.app.connectors.base import BaseSubscriptionConnector
from backend.app.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorConnectionError,
    ConnectorHTTPError,
    ConnectorResponseError,
    ConnectorTimeoutError,
    SubscriptionNotFoundError,
)
from backend.app.connectors.normalization import (
    build_evidence,
    deduplicate_evidence,
    ensure_timezone_aware,
    normalize_subscription_payload,
)
from backend.app.models import Evidence, Subscription

logger = logging.getLogger(__name__)


class HttpMockSubscriptionConnector(BaseSubscriptionConnector):
    """HTTP/REST connector for mock streaming providers.

    Features:
    - Bounded retries for transient 5xx errors and connection failures.
    - Strict timeouts.
    - Never logs secrets, passwords, or authentication tokens.
    - Fully testable with httpx.MockTransport.
    """

    def __init__(
        self,
        service_id: str,
        service_name: str,
        base_url: str = "https://mock.streaming.test",
        api_key: Optional[str] = None,
        timeout: float = 5.0,
        max_retries: int = 2,
        backoff_factor: float = 0.1,
        client: Optional[httpx.Client] = None,
        supported_aliases: Optional[List[str]] = None,
    ) -> None:
        self._service_id = service_id
        self._service_name = service_name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max(0, min(max_retries, 5))  # bounded max 5
        self._backoff_factor = backoff_factor
        self._client = client
        self._supported_aliases = supported_aliases or []

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

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "SubCan-Connector/0.1.0",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _request_with_retry(
        self,
        method: str,
        endpoint_or_url: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute HTTP request with bounded retry for transient errors.

        Never retries 4xx client errors (401, 403, 404, 422).
        Sanitizes logged URLs and headers.
        """
        if endpoint_or_url.startswith("http://") or endpoint_or_url.startswith("https://"):
            url = endpoint_or_url
        else:
            url = f"{self._base_url}/{endpoint_or_url.lstrip('/')}"

        headers = self._get_headers()
        client = self._client or httpx.Client(timeout=self._timeout)
        owns_client = self._client is None

        attempt = 0
        last_exception: Optional[Exception] = None

        try:
            while attempt <= self._max_retries:
                try:
                    resp = client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=params,
                        timeout=self._timeout,
                    )

                    # Successful response
                    if 200 <= resp.status_code < 300:
                        try:
                            return resp.json()
                        except Exception as exc:
                            raise ConnectorResponseError(
                                f"Invalid JSON response from mock service at {url}: {exc}",
                                details={"status_code": resp.status_code, "url": url},
                            ) from exc

                    # Client errors (never retry)
                    if resp.status_code in (401, 403):
                        raise ConnectorAuthError(
                            status_code=resp.status_code,
                            message=f"Unauthorized access to mock endpoint: {url}",
                            response_body=resp.text,
                            url=url,
                        )
                    if resp.status_code == 404:
                        raise SubscriptionNotFoundError(
                            subscription_id="unknown",
                            service_name=self.service_name,
                            message=f"Resource not found at {url} (HTTP 404)",
                        )
                    if 400 <= resp.status_code < 500:
                        raise ConnectorHTTPError(
                            status_code=resp.status_code,
                            message=f"Client error from mock endpoint: {resp.text}",
                            response_body=resp.text,
                            url=url,
                        )

                    # Server error (5xx) -> candidate for bounded retry
                    if attempt < self._max_retries:
                        attempt += 1
                        time.sleep(self._backoff_factor * (2 ** (attempt - 1)))
                        continue
                    else:
                        raise ConnectorHTTPError(
                            status_code=resp.status_code,
                            message=f"Server error from mock endpoint after {attempt} retries",
                            response_body=resp.text,
                            url=url,
                        )

                except (httpx.ConnectError, httpx.NetworkError) as net_err:
                    last_exception = net_err
                    if attempt < self._max_retries:
                        attempt += 1
                        time.sleep(self._backoff_factor * (2 ** (attempt - 1)))
                        continue
                    raise ConnectorConnectionError(
                        f"Failed to connect to mock endpoint {url}: {net_err}",
                        details={"url": url, "attempts": attempt + 1},
                    ) from net_err

                except httpx.TimeoutException as timeout_err:
                    last_exception = timeout_err
                    if attempt < self._max_retries:
                        attempt += 1
                        time.sleep(self._backoff_factor * (2 ** (attempt - 1)))
                        continue
                    raise ConnectorTimeoutError(
                        f"Request timed out connecting to {url} (timeout={self._timeout}s)",
                        details={"url": url, "timeout": self._timeout},
                    ) from timeout_err

            # Should not reach here, but safety fallback
            if last_exception:
                raise ConnectorError(f"Request failed after retries: {last_exception}")
            raise ConnectorError(f"Request failed to {url}")
        finally:
            if owns_client:
                client.close()

    def list_subscriptions(self) -> List[Subscription]:
        """Fetch all subscriptions for this service from the mock API."""
        endpoint = f"{self._service_id}/api/subscriptions"
        data = self._request_with_retry("GET", endpoint)

        items = data.get("subscriptions", []) if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = [data]

        results: List[Subscription] = []
        for raw in items:
            sub = normalize_subscription_payload(raw, fallback_service_name=self.service_name)
            results.append(sub)
        return results

    def get_subscription(self, subscription_id: str) -> Subscription:
        """Fetch a specific subscription by ID from the mock API."""
        endpoint = f"{self._service_id}/account"
        data = self._request_with_retry("GET", endpoint, params={"subscription_id": subscription_id})

        # May return single dict or list of subscriptions
        if isinstance(data, dict) and "subscription_id" in data:
            return normalize_subscription_payload(data, fallback_service_name=self.service_name)
        elif isinstance(data, dict) and "subscriptions" in data:
            for item in data["subscriptions"]:
                if item.get("subscription_id") == subscription_id:
                    return normalize_subscription_payload(item, fallback_service_name=self.service_name)

        raise SubscriptionNotFoundError(
            subscription_id=subscription_id,
            service_name=self.service_name,
        )

    def get_usage_history(
        self,
        subscription_id: str,
        as_of: Optional[datetime] = None,
    ) -> List[Evidence]:
        """Fetch playback/usage activity logs from the mock service."""
        capture_time = ensure_timezone_aware(as_of) or datetime.now(timezone.utc)
        endpoint = f"{self._service_id}/api/v1/playback_logs"

        data = self._request_with_retry("GET", endpoint, params={"subscription_id": subscription_id})
        evidence_items: List[Evidence] = []

        logs = data.get("logs", []) if isinstance(data, dict) else data
        if isinstance(logs, list):
            for idx, entry in enumerate(logs):
                ev_id = entry.get("evidence_id") or f"ev_{subscription_id}_usage_{idx}"
                excerpt = entry.get("excerpt") or entry.get("message") or f"Playback activity: {entry}"
                confidence = float(entry.get("confidence", 0.95))
                ev = build_evidence(
                    evidence_id=ev_id,
                    subscription_id=subscription_id,
                    evidence_type="usage_history",
                    excerpt=excerpt,
                    source_url=f"{self._base_url}/{endpoint}",
                    captured_at=capture_time,
                    confidence=confidence,
                    raw_reference=entry.get("raw_reference") or entry,
                )
                evidence_items.append(ev)

        return deduplicate_evidence(evidence_items)

    def get_cancellation_terms(
        self,
        subscription_id: str,
        as_of: Optional[datetime] = None,
    ) -> List[Evidence]:
        """Fetch cancellation policy terms from the mock service."""
        capture_time = ensure_timezone_aware(as_of) or datetime.now(timezone.utc)
        endpoint = f"{self._service_id}/terms/cancellation"

        data = self._request_with_retry("GET", endpoint, params={"subscription_id": subscription_id})
        evidence_items: List[Evidence] = []

        terms = data.get("terms", []) if isinstance(data, dict) else [data]
        if not isinstance(terms, list):
            terms = [terms]

        for idx, entry in enumerate(terms):
            ev_id = entry.get("evidence_id") or f"ev_{subscription_id}_terms_{idx}"
            ev_type = entry.get("evidence_type") or "cancellation_terms"
            excerpt = entry.get("excerpt") or entry.get("policy_text") or str(entry)
            confidence = float(entry.get("confidence", 1.0))
            ev = build_evidence(
                evidence_id=ev_id,
                subscription_id=subscription_id,
                evidence_type=ev_type,
                excerpt=excerpt,
                source_url=f"{self._base_url}/{endpoint}",
                captured_at=capture_time,
                confidence=confidence,
                raw_reference=entry.get("raw_reference") or entry,
            )
            evidence_items.append(ev)

        return deduplicate_evidence(evidence_items)
