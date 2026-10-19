"""SubCan Safe Action Coordination Module (Planned - Loki).

Responsible for:
- Executing approved cancellation plans against mock sites / APIs.
- Capturing execution logs and structured ActionResult records.
- Enforcing safety invariants: NEVER consider action success as proof of cancellation.

Phase 0: Documented placeholder.
Implementation planned for Phase 2.
"""

from backend.app.models import ActionResult, Approval, CancellationPlan


def execute_plan(
    plan: CancellationPlan,
    approval: Approval,
) -> ActionResult:
    """Execute cancellation steps for an approved plan.

    Placeholder implementation for Phase 0.
    """
    raise NotImplementedError("Action execution coordination will be implemented in Phase 2.")
