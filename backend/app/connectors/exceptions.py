"""SubCan Connector Exceptions (Phase 1).

Standardized exception hierarchy for the data retrieval and connector layer.
Ensures connector errors are informative, typed, and never silently masked.
"""

from typing import Any, Dict, Optional


class ConnectorError(Exception):
    """Base exception for all connector and data retrieval errors."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class UnsupportedServiceError(ConnectorError):
    """Raised when a requested streaming service is not supported by any connector."""
    pass


class SubscriptionNotFoundError(ConnectorError):
    """Raised when a subscription ID cannot be located at the source."""

    def __init__(
        self,
        subscription_id: str,
        service_name: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        msg = message or f"Subscription '{subscription_id}' was not found"
        if service_name:
            msg += f" for service '{service_name}'"
        super().__init__(msg, details={"subscription_id": subscription_id, "service_name": service_name})
        self.subscription_id = subscription_id
        self.service_name = service_name


class ConnectorConnectionError(ConnectorError):
    """Raised when the connector cannot connect to the mock site or upstream host."""
    pass


class ConnectorTimeoutError(ConnectorError):
    """Raised when a connector request exceeds the configured timeout."""
    pass


class ConnectorHTTPError(ConnectorError):
    """Raised when an HTTP request to a mock service returns an error status code."""

    def __init__(
        self,
        status_code: int,
        message: str,
        response_body: Optional[str] = None,
        url: Optional[str] = None,
    ) -> None:
        details = {
            "status_code": status_code,
            "response_body": response_body,
            "url": url,
        }
        super().__init__(f"HTTP {status_code}: {message}", details=details)
        self.status_code = status_code
        self.response_body = response_body
        self.url = url


class ConnectorAuthError(ConnectorHTTPError):
    """Raised when authentication or authorization fails against a mock endpoint (401/403)."""

    def __init__(
        self,
        status_code: int = 401,
        message: str = "Authentication failed",
        response_body: Optional[str] = None,
        url: Optional[str] = None,
    ) -> None:
        super().__init__(status_code=status_code, message=message, response_body=response_body, url=url)


class ConnectorResponseError(ConnectorError):
    """Raised when a source response is malformed, not valid JSON, or missing required fields."""
    pass


class EvidenceExtractionError(ConnectorError):
    """Raised when evidence cannot be parsed, normalized, or validated from raw source data."""
    pass
