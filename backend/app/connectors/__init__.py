"""SubCan Connectors Package (Owned by Chinmay).

Responsible for:
- Connecting to mock streaming subscription sites.
- Ingestion and retrieval of subscription status and renewal dates.
- Extraction of usage history and streaming activity logs.
- Scraping or querying cancellation terms and pause policies.
- Constructing validated Evidence instances complying with DATA_CONTRACT.md.

Phase 0: Documented starter package.
Connector implementations planned for Phase 1 by Chinmay.
"""

from typing import List
from backend.app.models import Evidence, Subscription


def fetch_subscriptions() -> List[Subscription]:
    """Retrieve subscriptions from mock streaming providers.

    Placeholder implementation for Phase 0 (Chinmay).
    """
    raise NotImplementedError("Connector fetch will be implemented by Chinmay.")


def collect_evidence(subscription_id: str) -> List[Evidence]:
    """Collect usage logs and policy evidence for a subscription.

    Placeholder implementation for Phase 0 (Chinmay).
    """
    raise NotImplementedError("Evidence collection will be implemented by Chinmay.")
