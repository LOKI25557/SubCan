"""SubCan Persistence Layer Placeholder (Owned by Dadu).

Responsible for:
- Database connection lifecycle and session management.
- Persisting Subscription, Evidence, Decision, Plan, Approval, Action, and Verification entities.
- Query interfaces for dashboard and historical runs.

Phase 0: Documented placeholder.
Implementation planned for Phase 1 by Dadu.
"""

from typing import Any, Dict, List, Optional
from backend.app.models import Subscription


def get_db_session() -> Any:
    """Provide a database session or connection.

    Placeholder implementation for Phase 0 (Dadu).
    """
    raise NotImplementedError("Database connectivity will be implemented by Dadu.")


def save_subscription(subscription: Subscription) -> None:
    """Persist a validated subscription entity to storage.

    Placeholder implementation for Phase 0 (Dadu).
    """
    raise NotImplementedError("Persistence logic will be implemented by Dadu.")
