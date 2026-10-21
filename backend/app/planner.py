"""SubCan Cancellation Planner Module (Phase 2).

Responsible for:
- Constructing structured, versioned cancellation proposals for eligible subscriptions.
- Strict enforcement: generates plans ONLY for subscriptions whose decision is CANDIDATE.
- Binding plans to explicit service names, plan versions, and unique plan IDs.
- Including proposed actions, decision reasons, cancellation terms, renewal dates,
  and supporting evidence references.
- Disclosing data-loss warnings and pause option alternatives.
- Safe handling: missing, uncertain, or contradictory terms produce review-required warnings
  rather than invented terms.
- Deterministic plan fingerprinting to ensure material changes invalidate previous approvals.
- SAFETY INVARIANT: Generating a plan NEVER executes an action.
"""

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import List, Optional, Tuple
from backend.app.models import (
    ApprovalStatus,
    CancellationPlan,
    Decision,
    EligibilityStatus,
    Evidence,
    Subscription,
)


def compute_plan_fingerprint(
    subscription: Subscription,
    decision: Decision,
    terms_summary: str,
    steps: List[str],
    consequences_warning: Optional[str],
) -> str:
    """Generate a deterministic 8-character hex fingerprint of material plan contents.

    Used for versioning and invalidating prior approvals if any plan details change.
    """
    material_data = {
        "subscription_id": subscription.subscription_id,
        "service_name": subscription.service_name,
        "renewal_date": str(subscription.renewal_date),
        "monthly_cost": subscription.monthly_cost,
        "decision_reason": decision.reason,
        "terms_summary": terms_summary,
        "steps": steps,
        "consequences_warning": consequences_warning or "",
        "pause_alternative_available": decision.pause_alternative_available,
        "data_deletion_risk": decision.data_deletion_risk,
    }
    encoded = json.dumps(material_data, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:8]


def _analyze_cancellation_terms(
    subscription: Subscription,
    evidence_list: List[Evidence],
) -> Tuple[str, Optional[str], bool]:
    """Analyze cancellation terms and policies from subscription and evidence.

    Returns:
        (terms_summary, review_warning, is_uncertain_or_missing)
    """
    terms_evidence = [
        ev for ev in evidence_list
        if ev.evidence_type in ("cancellation_terms", "terms_of_service", "billing_policy")
    ]

    # Case 1: Missing terms completely
    if not terms_evidence and not subscription.cancellation_terms_url:
        return (
            "Terms unavailable: no cancellation terms URL or evidence provided.",
            (
                f"REVIEW REQUIRED: Cancellation terms are missing or inaccessible for '{subscription.service_name}'. "
                "Detailed terms must be reviewed manually prior to approval."
            ),
            True,
        )

    # Case 2: Contradictory terms
    if len(terms_evidence) >= 2:
        excerpts_lower = [ev.excerpt.lower() for ev in terms_evidence]
        has_fee = any("fee" in exc or "penalty" in exc for exc in excerpts_lower)
        has_free = any("free" in exc or "no fee" in exc or "zero charge" in exc for exc in excerpts_lower)
        if has_fee and has_free:
            return (
                "Terms conflict: contradictory penalty and fee policies detected in evidence.",
                (
                    f"REVIEW REQUIRED: Contradictory cancellation terms detected for '{subscription.service_name}'. "
                    "Confirm fee and penalty policies prior to approval."
                ),
                True,
            )

    # Case 3: Low confidence or ambiguous terms
    low_confidence_terms = [ev for ev in terms_evidence if ev.confidence < 0.6]
    if low_confidence_terms:
        lowest_conf = min(ev.confidence for ev in low_confidence_terms)
        return (
            f"Terms uncertain: low-confidence evidence (confidence: {lowest_conf}).",
            (
                f"REVIEW REQUIRED: Cancellation terms have low confidence ({lowest_conf}) or ambiguity. "
                "Manual review required before approval."
            ),
            True,
        )

    # Case 4: Verified terms available
    if terms_evidence:
        # Use highest confidence terms excerpt
        best_ev = max(terms_evidence, key=lambda ev: ev.confidence)
        terms_summary = f"Verified terms: {best_ev.excerpt}"
        if subscription.cancellation_terms_url:
            terms_summary += f" (Policy URL: {subscription.cancellation_terms_url})"
        return terms_summary, None, False

    # URL only, no evidence excerpts yet
    return (
        f"Terms available at source: {subscription.cancellation_terms_url}",
        None,
        False,
    )


def is_review_required(plan: CancellationPlan) -> bool:
    """Return True if the cancellation plan requires manual terms or safety review."""
    if plan.consequences_warning and "REVIEW REQUIRED" in plan.consequences_warning:
        return True
    return any("review" in step.lower() for step in plan.steps if "manual" in step.lower())


def has_material_changes(
    plan: CancellationPlan,
    subscription: Subscription,
    decision: Decision,
    evidence_list: Optional[List[Evidence]] = None,
) -> bool:
    """Check if changes to subscription, decision, or evidence invalidate the current plan."""
    evidence_items = evidence_list or []
    terms_summary, _, _ = _analyze_cancellation_terms(subscription, evidence_items)
    current_fingerprint = compute_plan_fingerprint(
        subscription,
        decision,
        terms_summary,
        plan.steps,
        plan.consequences_warning,
    )
    # The version is expected to contain the fingerprint
    return current_fingerprint not in plan.plan_version


def create_cancellation_plan(
    subscription: Subscription,
    decision: Decision,
    evidence_list: Optional[List[Evidence]] = None,
    created_at: Optional[datetime] = None,
    base_version: str = "v1.0",
    explicit_version: Optional[str] = None,
) -> CancellationPlan:
    """Generate a structured, versioned cancellation proposal for an eligible subscription.

    SAFETY INVARIANTS:
    1. Plans are generated ONLY for subscriptions with decision.eligibility == CANDIDATE.
       Ineligible or review-requiring subscriptions raise ValueError.
    2. Generating a plan NEVER executes an action.
    3. Missing, uncertain, or contradictory terms produce review-required warnings
       rather than invented terms.
    4. Pause options are explicitly documented as distinct alternatives, never conflated
       with cancellation.
    5. Each plan is bound to the exact subscription ID, service name, and version.

    Args:
        subscription: Validated Subscription domain entity.
        decision: Validated Decision entity (must have eligibility == CANDIDATE).
        evidence_list: Optional supporting evidence items.
        created_at: Injectable plan creation timestamp (defaults to current UTC time).
        base_version: Base version string (default 'v1.0').
        explicit_version: Optional override version string.

    Returns:
        Validated CancellationPlan in AWAITING_APPROVAL status.

    Raises:
        ValueError: If subscription eligibility does not permit a proposal.
    """
    # --------------------------------------------------------------------------
    # 1. Eligibility Check Gate
    # --------------------------------------------------------------------------
    if decision.eligibility != EligibilityStatus.CANDIDATE:
        raise ValueError(
            f"Cannot generate cancellation plan for subscription '{subscription.subscription_id}' "
            f"with eligibility '{decision.eligibility.value}'. Only 'candidate' subscriptions are eligible. "
            f"Reason: {decision.reason}"
        )

    if created_at is None:
        created_at = datetime.now(timezone.utc)
    elif created_at.tzinfo is None or created_at.tzinfo.utcoffset(created_at) is None:
        raise ValueError("Plan created_at timestamp must be timezone-aware.")

    evidence_items = evidence_list or []

    # --------------------------------------------------------------------------
    # 2. Terms Analysis & Missing/Ambiguous Detection
    # --------------------------------------------------------------------------
    terms_summary, terms_warning, terms_review_needed = _analyze_cancellation_terms(
        subscription, evidence_items
    )

    # --------------------------------------------------------------------------
    # 3. Warning & Consequence Disclosures
    # --------------------------------------------------------------------------
    warning_parts: List[str] = []
    if terms_warning:
        warning_parts.append(terms_warning)

    if decision.data_deletion_risk or subscription.data_deletion_warning:
        warning_parts.append(
            "CRITICAL WARNING: Immediate data deletion risk. Cancellation will permanently purge "
            "all user cloud libraries, playlists, audio stems, and profile metadata."
        )

    consequences_warning = "\n".join(warning_parts) if warning_parts else None

    # --------------------------------------------------------------------------
    # 4. Construct Execution Steps
    # --------------------------------------------------------------------------
    steps: List[str] = [
        f"1. Proposed Action: Cancellation of streaming subscription for '{subscription.service_name}'.",
        f"2. Eligibility Rationale: {decision.reason}",
    ]

    if terms_review_needed:
        steps.append(
            f"3. Terms Verification: MANUAL REVIEW REQUIRED. {terms_summary}"
        )
    else:
        steps.append(
            f"3. Terms & Renewal: Access valid through renewal date {subscription.renewal_date}. {terms_summary}"
        )

    # Evidence references
    ev_ids = [ev.evidence_id for ev in evidence_items]
    if ev_ids:
        steps.append(f"4. Supporting Evidence: Referenced items: {', '.join(ev_ids)}.")
    else:
        steps.append("4. Supporting Evidence: Evaluated based on primary subscription metadata.")

    # Consequence warnings in steps
    if decision.data_deletion_risk or subscription.data_deletion_warning:
        steps.append(
            "5. Consequence Disclosure: User must be explicitly notified of irreversible data loss prior to approval."
        )

    # Pause option disclosure (kept strictly distinct from cancellation)
    if decision.pause_alternative_available or subscription.pause_option_available:
        steps.append(
            "6. Alternative Disclosure: Provider offers a temporary billing pause option. "
            "A paused subscription is NOT cancelled and preserves legacy pricing/history."
        )

    steps.extend([
        "7. Approval Gate: Plan requires explicit human approval matching exact service name and version.",
        "8. Execution Safety: Execute action once approved. An action success does NOT constitute verified cancellation.",
    ])

    # --------------------------------------------------------------------------
    # 5. Versioning & Deterministic Plan ID
    # --------------------------------------------------------------------------
    fingerprint = compute_plan_fingerprint(
        subscription,
        decision,
        terms_summary,
        steps,
        consequences_warning,
    )

    plan_version = explicit_version if explicit_version is not None else f"{base_version}.{fingerprint}"
    plan_id = f"plan_{subscription.subscription_id}_{base_version.replace('.', '_')}"

    return CancellationPlan(
        plan_id=plan_id,
        subscription_id=subscription.subscription_id,
        service_name=subscription.service_name,
        plan_version=plan_version,
        steps=steps,
        consequences_warning=consequences_warning,
        pause_alternative_available=decision.pause_alternative_available or subscription.pause_option_available,
        status=ApprovalStatus.AWAITING_APPROVAL,
        created_at=created_at,
    )
