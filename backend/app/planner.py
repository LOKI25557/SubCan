"""SubCan Cancellation Planner Module (Planned - Loki).

Responsible for:
- Constructing stepwise cancellation plans for eligible subscriptions.
- Binding plans to explicit service names and plan versions.
- Attaching data loss warnings and pause option disclosures.

Phase 0: Documented placeholder.
Implementation planned for Phase 1.
"""

from backend.app.models import CancellationPlan, Decision, Subscription


def create_cancellation_plan(
    subscription: Subscription,
    decision: Decision,
) -> CancellationPlan:
    """Generate a structured cancellation plan requiring explicit user approval.

    Placeholder implementation for Phase 0.
    """
    raise NotImplementedError("Planner logic will be implemented in Phase 1.")
