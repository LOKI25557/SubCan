"""SubCan Mock Service Connector Implementations (Phase 1).

Concrete adapters for the 5 streaming services specified in SubCan:
1. CineVaultConnector (TC1: Standard Inactive Streaming)
2. AudioCloudConnector (TC2: Immediate Data Deletion Risk)
3. FitStreamConnector (TC3: Pause Alternative Available)
4. MovieMaxConnector (TC4: Approval Isolation A)
5. GameStreamConnector (TC4: Approval Isolation B)

Each connector can operate in fixture-backed mode (default, deterministic)
or HTTP-backed mode (when provided with base_url or httpx client).
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import httpx

from backend.app.connectors.base import BaseSubscriptionConnector
from backend.app.connectors.fixture import FixtureSubscriptionConnector
from backend.app.connectors.http import HttpMockSubscriptionConnector
from backend.app.models import Evidence, Subscription


class ServiceConnectorAdapter(BaseSubscriptionConnector):
    """Hybrid connector adapter wrapping either a fixture or HTTP backend."""

    def __init__(
        self,
        service_id: str,
        service_name: str,
        supported_aliases: Optional[List[str]] = None,
        fixture_path: Optional[Union[str, Path]] = None,
        fixture_data: Optional[Dict[str, Any]] = None,
        http_client: Optional[httpx.Client] = None,
        base_url: Optional[str] = None,
        use_http: bool = False,
    ) -> None:
        self._service_id = service_id
        self._service_name = service_name
        self._supported_aliases = supported_aliases or []
        self._use_http = use_http

        self._fixture_backend = FixtureSubscriptionConnector(
            service_id=service_id,
            service_name=service_name,
            fixture_path=fixture_path,
            fixture_data=fixture_data,
            supported_aliases=self._supported_aliases,
        )

        if use_http or http_client or base_url:
            self._http_backend = HttpMockSubscriptionConnector(
                service_id=service_id,
                service_name=service_name,
                base_url=base_url or "https://mock.streaming.test",
                client=http_client,
                supported_aliases=self._supported_aliases,
            )
        else:
            self._http_backend = None

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

    @property
    def active_backend(self) -> BaseSubscriptionConnector:
        if self._use_http and self._http_backend is not None:
            return self._http_backend
        return self._fixture_backend

    def list_subscriptions(self) -> List[Subscription]:
        return self.active_backend.list_subscriptions()

    def get_subscription(self, subscription_id: str) -> Subscription:
        return self.active_backend.get_subscription(subscription_id)

    def get_usage_history(
        self,
        subscription_id: str,
        as_of: Optional[datetime] = None,
    ) -> List[Evidence]:
        return self.active_backend.get_usage_history(subscription_id, as_of=as_of)

    def get_cancellation_terms(
        self,
        subscription_id: str,
        as_of: Optional[datetime] = None,
    ) -> List[Evidence]:
        return self.active_backend.get_cancellation_terms(subscription_id, as_of=as_of)


class CineVaultConnector(ServiceConnectorAdapter):
    """Connector for CineVault Mock Streaming (TC1)."""

    def __init__(
        self,
        fixture_path: Optional[Union[str, Path]] = None,
        fixture_data: Optional[Dict[str, Any]] = None,
        http_client: Optional[httpx.Client] = None,
        base_url: Optional[str] = None,
        use_http: bool = False,
    ) -> None:
        super().__init__(
            service_id="cinevault",
            service_name="CineVault Mock Streaming",
            supported_aliases=["cinevault", "CineVault", "sub_tc1_cinevault"],
            fixture_path=fixture_path,
            fixture_data=fixture_data,
            http_client=http_client,
            base_url=base_url,
            use_http=use_http,
        )


class AudioCloudConnector(ServiceConnectorAdapter):
    """Connector for AudioCloud Pro Mock (TC2: Data Deletion Warning)."""

    def __init__(
        self,
        fixture_path: Optional[Union[str, Path]] = None,
        fixture_data: Optional[Dict[str, Any]] = None,
        http_client: Optional[httpx.Client] = None,
        base_url: Optional[str] = None,
        use_http: bool = False,
    ) -> None:
        super().__init__(
            service_id="audiocloud",
            service_name="AudioCloud Pro Mock",
            supported_aliases=["audiocloud", "AudioCloud", "sub_tc2_audiocloud"],
            fixture_path=fixture_path,
            fixture_data=fixture_data,
            http_client=http_client,
            base_url=base_url,
            use_http=use_http,
        )


class FitStreamConnector(ServiceConnectorAdapter):
    """Connector for FitStream Pass Mock (TC3: Pause Alternative Available)."""

    def __init__(
        self,
        fixture_path: Optional[Union[str, Path]] = None,
        fixture_data: Optional[Dict[str, Any]] = None,
        http_client: Optional[httpx.Client] = None,
        base_url: Optional[str] = None,
        use_http: bool = False,
    ) -> None:
        super().__init__(
            service_id="fitstream",
            service_name="FitStream Pass Mock",
            supported_aliases=["fitstream", "FitStream", "sub_tc3_fitstream"],
            fixture_path=fixture_path,
            fixture_data=fixture_data,
            http_client=http_client,
            base_url=base_url,
            use_http=use_http,
        )


class MovieMaxConnector(ServiceConnectorAdapter):
    """Connector for MovieMax Mock (TC4)."""

    def __init__(
        self,
        fixture_path: Optional[Union[str, Path]] = None,
        fixture_data: Optional[Dict[str, Any]] = None,
        http_client: Optional[httpx.Client] = None,
        base_url: Optional[str] = None,
        use_http: bool = False,
    ) -> None:
        super().__init__(
            service_id="moviemax",
            service_name="MovieMax Mock",
            supported_aliases=["moviemax", "MovieMax", "sub_tc4_moviemax"],
            fixture_path=fixture_path,
            fixture_data=fixture_data,
            http_client=http_client,
            base_url=base_url,
            use_http=use_http,
        )


class GameStreamConnector(ServiceConnectorAdapter):
    """Connector for GameStream Mock (TC4)."""

    def __init__(
        self,
        fixture_path: Optional[Union[str, Path]] = None,
        fixture_data: Optional[Dict[str, Any]] = None,
        http_client: Optional[httpx.Client] = None,
        base_url: Optional[str] = None,
        use_http: bool = False,
    ) -> None:
        super().__init__(
            service_id="gamestream",
            service_name="GameStream Mock",
            supported_aliases=["gamestream", "GameStream", "sub_tc4_gamestream"],
            fixture_path=fixture_path,
            fixture_data=fixture_data,
            http_client=http_client,
            base_url=base_url,
            use_http=use_http,
        )
