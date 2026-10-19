"""SubCan Human-in-the-Loop Approval Module (Planned - Loki).

Responsible for:
- Validating explicit user approval against exact service and plan version.
- Enforcing safety invariants: no action can run without verified approval.
- Rejecting generic or multi-service approvals.

Phase 0: Documented placeholder.
Implementation planned for Phase 1.
"""

from backend.app.models import Approval, CancellationPlan


def validate_approval(
    plan: CancellationPlan,
    approval: Approval,
) -> bool:
    """Validate that the approval matches the exact plan ID, service name, and version.

    Placeholder implementation for Phase 0.
    """
    raise NotImplementedError("Approval validation logic will be implemented in Phase 1.")
