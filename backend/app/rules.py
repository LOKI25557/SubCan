"""SubCan Decision Rules Module (Planned - Loki).

Responsible for:
- Evaluating subscriptions against inactivity rules (>= 90 days / 3 calendar months).
- Identifying consequence warnings (e.g., immediate data loss).
- Flagging alternatives such as pause options.
- Assigning eligibility status and uncertainty scores.

Phase 0: Documented placeholder.
Implementation planned for Phase 1.
"""

from typing import List, Optional
from backend.app.models import Decision, Evidence, Subscription


def evaluate_eligibility(
    subscription: Subscription,
    evidence_list: Optional[List[Evidence]] = None,
) -> Decision:
    """Evaluate whether a subscription is eligible for cancellation consideration.

    Placeholder implementation for Phase 0.
    """
    raise NotImplementedError("Decision rules will be implemented in Phase 1.")
