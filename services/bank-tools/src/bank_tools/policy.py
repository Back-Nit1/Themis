"""Deterministic policy gate for synthetic protected actions.

Money/account-impacting actions never execute on the LLM's say-so alone: each
propose_* tool calls `evaluate_policy` first and only simulates the action on
ALLOW. This module holds no state and makes no I/O; it is pure so policy
allow/deny/human-review paths stay easy to unit test.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import (
    CUSTOMER_REQUEST_EVIDENCE_MAX_AGE_SECONDS, DEMO_AUTONOMOUS_CREDIT_LIMIT, DEMO_CREDIT_CONFIDENCE_THRESHOLD,
    PROVISIONAL_CREDIT_ELIGIBLE_CLAIM_TYPES,
)
from .models import ActionType, Case, CaseStatus, Evidence, EvidenceCategory, PolicyOutcome

# Evidence type persisted by the caller (save_evidence) for an explicit customer request to block a
# recurring merchant. Must match services/agent/src/orchestrator/engine.py's CUSTOMER_REQUEST_EVIDENCE_TYPE.
CUSTOMER_REQUEST_EVIDENCE_TYPE = "PAYMENT_BLOCK_REQUESTED"


@dataclass(frozen=True, slots=True)
class PolicyResult:
    outcome: PolicyOutcome
    rationale: str


def customer_requested_payment_block(evidence: list[Evidence]) -> bool:
    """Trusted, server-derived replacement for propose_payment_block's customerRequested argument.

    True only if the case has a fresh CUSTOMER_CLAIMS/PAYMENT_BLOCK_REQUESTED evidence record --
    never the caller's own boolean claim, which is not authoritative (see dispatch._adapt_payment_block).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=CUSTOMER_REQUEST_EVIDENCE_MAX_AGE_SECONDS)
    return any(
        item.category is EvidenceCategory.CUSTOMER_CLAIMS and item.type == CUSTOMER_REQUEST_EVIDENCE_TYPE
        and datetime.fromisoformat(item.recordedAt) >= cutoff
        for item in evidence
    )


def evaluate_policy(action: ActionType, case: Case, **params: object) -> PolicyResult:
    if case.status is CaseStatus.CLOSED:
        return PolicyResult(PolicyOutcome.DENY, "Case is closed; no further action is permitted.")

    if action is ActionType.CREATE_DISPUTE:
        if case.transactionIds:
            return PolicyResult(PolicyOutcome.ALLOW, "Case has confirmed transactions.")
        return PolicyResult(PolicyOutcome.REQUIRE_HUMAN_REVIEW, "Case has no confirmed transactions yet.")

    if action is ActionType.PROVISIONAL_CREDIT:
        amount = params.get("amount")
        confidence = case.confidence
        if amount is None or amount > DEMO_AUTONOMOUS_CREDIT_LIMIT:
            return PolicyResult(
                PolicyOutcome.REQUIRE_HUMAN_REVIEW,
                f"Amount exceeds autonomous limit of {DEMO_AUTONOMOUS_CREDIT_LIMIT}.",
            )
        if confidence is None or confidence < DEMO_CREDIT_CONFIDENCE_THRESHOLD:
            return PolicyResult(
                PolicyOutcome.REQUIRE_HUMAN_REVIEW,
                f"Case confidence is below the {DEMO_CREDIT_CONFIDENCE_THRESHOLD} threshold.",
            )
        if case.claimType not in PROVISIONAL_CREDIT_ELIGIBLE_CLAIM_TYPES:
            return PolicyResult(
                PolicyOutcome.REQUIRE_HUMAN_REVIEW, f"Claim type {case.claimType} is not autonomous-eligible.",
            )
        return PolicyResult(PolicyOutcome.ALLOW, "Amount, confidence, and claim type meet the autonomous policy.")

    if action is ActionType.BLOCK_RECURRING_MERCHANT:
        if not params.get("customer_requested"):
            return PolicyResult(PolicyOutcome.DENY, "Merchant block requires an explicit customer request.")
        return PolicyResult(PolicyOutcome.ALLOW, "Customer explicitly requested the merchant block.")

    if action is ActionType.REPLACE_CARD:
        return PolicyResult(PolicyOutcome.REQUIRE_HUMAN_REVIEW, "Card replacement always requires human approval.")

    return PolicyResult(PolicyOutcome.REQUIRE_HUMAN_REVIEW, f"No autonomous policy defined for {action}.")
