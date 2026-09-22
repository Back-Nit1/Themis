"""The bank-tools tool surface.

Every function takes the store as its first argument (no hidden globals, so
tests can build a fresh store per case) and returns a compact JSON-able dict
shaped `{"status": "ok", ...}` or `{"status": "error", "error": {...}}` --
never a full account dump. State-changing tools take an `idempotency_key` and
replay their prior result on a repeat call instead of double-applying the
effect (prompt.md #31). Financial actions are policy-gated simulations only
(prompt.md #48); nothing here talks to a real bank.

Audit records require a caseId (packages/contracts AuditEventSchema), so
read-only tools only audit when the caller passes `case_id` (i.e. the lookup
happened in service of an active case); case CRUD and propose_* tools always
audit since they always have one.
"""
from __future__ import annotations

import functools
from typing import Any, Callable

from .errors import InvalidTransitionError, ReviewAlreadyDecidedError, ToolError
from .models import (
    REVIEW_DECISION_TARGET_STATUS, ActionType, Case, CaseOutcome, CaseStatus, ClaimType, Evidence,
    EvidenceCategory, HumanReviewRequest, PolicyDecision, PolicyOutcome, Reliability, ReviewDecision,
    ReviewDecisionType, ReviewStatus, ValidationError, can_transition_case_status, now_iso,
    require_currency, require_id, require_money,
)
from .policy import evaluate_policy
from .store import BankToolsStorage as BankToolsStore, new_id

DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 200


def _ok(**fields: Any) -> dict[str, Any]:
    return {"status": "ok", **fields}


def _err(code: str, message: str) -> dict[str, Any]:
    return {"status": "error", "error": {"code": code, "message": message}}


def _guarded(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Turn ToolError/ValueError (bad input, unknown enum, missing record) into a structured error reply."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            return _err(exc.code, str(exc))
        except ValueError as exc:
            return _err("VALIDATION_ERROR", str(exc))
    return wrapper


def _require_idempotency_key(key: str | None, tool: str) -> str:
    if not key:
        raise ValidationError(f"{tool} requires a nonempty idempotency_key")
    return key


def _compact_transaction(txn) -> dict[str, Any]:
    return {
        "id": txn.transactionId,
        "merchantDescriptor": txn.descriptor,
        "amount": txn.amount,
        "date": txn.occurredAt[:10],
        "recurring": bool(txn.authSignals.get("recurring_indicator", False)),
    }


# -- transactions -----------------------------------------------------------

@_guarded
def search_transactions(
    store: BankToolsStore, *, customer_id: str, merchant_id: str | None = None,
    descriptor_contains: str | None = None, since: str | None = None, until: str | None = None,
    min_amount: float | None = None, max_amount: float | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT, case_id: str | None = None,
) -> dict[str, Any]:
    store.get_customer(customer_id)  # raises NotFoundError if unknown
    matched = store.transactions_for_customer(customer_id, since=since, until=until)
    if merchant_id:
        matched = [t for t in matched if t.merchantId == merchant_id]
    if descriptor_contains:
        needle = descriptor_contains.upper()
        matched = [t for t in matched if needle in t.descriptor.upper()]
    if min_amount is not None:
        matched = [t for t in matched if t.amount >= min_amount]
    if max_amount is not None:
        matched = [t for t in matched if t.amount <= max_amount]

    capped_limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    page = matched[:capped_limit]
    result = _ok(
        transactions=[_compact_transaction(t) for t in page],
        count=len(matched), total=round(sum(t.amount for t in matched), 2),
    )
    if case_id:
        store.record_audit(case_id=case_id, action="SEARCH_TRANSACTIONS", tool="search_transactions")
    return result


@_guarded
def get_transaction_details(store: BankToolsStore, *, transaction_id: str, case_id: str | None = None) -> dict[str, Any]:
    txn = store.get_transaction(transaction_id)
    if case_id:
        store.record_audit(case_id=case_id, action="GET_TRANSACTION_DETAILS", tool="get_transaction_details")
    return _ok(transaction={
        "id": txn.transactionId, "customerId": txn.customerId, "merchantId": txn.merchantId,
        "descriptor": txn.descriptor, "amount": txn.amount, "currency": txn.currency,
        "occurredAt": txn.occurredAt,
    })


@_guarded
def get_transaction_auth_signals(store: BankToolsStore, *, transaction_id: str, case_id: str | None = None) -> dict[str, Any]:
    txn = store.get_transaction(transaction_id)
    if case_id:
        store.record_audit(case_id=case_id, action="GET_TRANSACTION_AUTH_SIGNALS", tool="get_transaction_auth_signals")
    return _ok(transactionId=txn.transactionId, authSignals=dict(txn.authSignals))


@_guarded
def find_related_transactions(
    store: BankToolsStore, *, transaction_id: str, limit: int = 20, case_id: str | None = None,
) -> dict[str, Any]:
    seed = store.get_transaction(transaction_id)
    same_merchant = [
        t for t in store.transactions_for_customer(seed.customerId) if t.merchantId == seed.merchantId
    ]
    capped_limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    if case_id:
        store.record_audit(case_id=case_id, action="FIND_RELATED_TRANSACTIONS", tool="find_related_transactions")
    return _ok(
        seedTransactionId=transaction_id,
        transactions=[_compact_transaction(t) for t in same_merchant[:capped_limit]],
        count=len(same_merchant),
    )


# -- customers ----------------------------------------------------------------

@_guarded
def get_customer_dispute_history(store: BankToolsStore, *, customer_id: str, limit: int = 20) -> dict[str, Any]:
    store.get_customer(customer_id)
    cases = sorted(store.cases_for_customer(customer_id), key=lambda c: c.createdAt, reverse=True)
    capped_limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    return _ok(
        cases=[
            {
                "caseId": c.caseId, "status": str(c.status), "claimType": str(c.claimType),
                "totalDisputedAmount": c.totalDisputedAmount, "currency": c.currency, "createdAt": c.createdAt,
            }
            for c in cases[:capped_limit]
        ],
        count=len(cases),
    )


# -- merchants ------------------------------------------------------------------

@_guarded
def resolve_merchant(store: BankToolsStore, *, descriptor: str, case_id: str | None = None) -> dict[str, Any]:
    merchant_id = store.resolve_merchant_id(descriptor)
    if case_id:
        store.record_audit(case_id=case_id, action="RESOLVE_MERCHANT", tool="resolve_merchant")
    if merchant_id is None:
        return _ok(resolved=False, merchantId=None, canonicalName=None, message="merchant identity unresolved")
    merchant = store.get_merchant(merchant_id)
    return _ok(resolved=True, merchantId=merchant.merchantId, canonicalName=merchant.canonicalName)


@_guarded
def get_merchant_profile(store: BankToolsStore, *, merchant_id: str) -> dict[str, Any]:
    profile = store.get_merchant_profile(merchant_id)
    return _ok(profile=profile.to_dict())


@_guarded
def get_merchant_risk_signals(
    store: BankToolsStore, *, merchant_id: str, include_expired: bool = False,
) -> dict[str, Any]:
    profile = store.get_merchant_profile(merchant_id)
    now = now_iso()
    signals = profile.riskSignals if include_expired else [s for s in profile.riskSignals if s.expiresAt > now]
    severity_rank = {"HIGH": 2, "ELEVATED": 1, "LOW": 0}
    signals = sorted(signals, key=lambda s: (severity_rank.get(str(s.severity), 0), s.observedAt), reverse=True)
    return _ok(merchantId=merchant_id, riskSignals=[s.to_dict() for s in signals], count=len(signals))


# -- case CRUD ------------------------------------------------------------------

@_guarded
def get_case(store: BankToolsStore, *, case_id: str) -> dict[str, Any]:
    return _ok(case=store.get_case(case_id).to_dict())


@_guarded
def create_case(
    store: BankToolsStore, *, customer_id: str, claim_type: str, idempotency_key: str,
    merchant_id: str | None = None, transaction_ids: list[str] | None = None, currency: str = "USD",
) -> dict[str, Any]:
    _require_idempotency_key(idempotency_key, "create_case")
    cached = store.idempotent_result("create_case", idempotency_key)
    if cached is not None:
        return cached

    store.get_customer(customer_id)
    claim = ClaimType(claim_type)
    currency = require_currency(currency)
    txn_ids = list(dict.fromkeys(transaction_ids or []))
    txns = [store.get_transaction(tid) for tid in txn_ids]
    for txn in txns:
        if txn.customerId != customer_id:
            raise ToolError(f"transaction {txn.transactionId} does not belong to customer", "TRANSACTION_NOT_OWNED")
        if txn.currency != currency:
            raise ValidationError(f"transaction {txn.transactionId} currency {txn.currency} != case currency {currency}")
    total = round(sum(t.amount for t in txns), 2)

    case = Case(
        caseId=new_id("case"), customerId=customer_id, status=CaseStatus.NEW,
        createdAt=now_iso(), updatedAt=now_iso(), claimType=claim, merchantId=merchant_id,
        transactionIds=txn_ids, evidenceIds=[], totalDisputedAmount=total, currency=currency,
        confidence=None, recommendedActions=[], requiresHumanReview=False,
    )
    store.create_case(case)
    store.record_audit(case_id=case.caseId, action="CREATE_CASE", tool="create_case")

    result = _ok(case=case.to_dict())
    store.remember_result("create_case", idempotency_key, result)
    return result


_IMMUTABLE_CASE_FIELDS = frozenset({"caseId", "customerId"})


@_guarded
def update_case(
    store: BankToolsStore, *, case_id: str, patch: dict[str, Any], idempotency_key: str,
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key, "update_case")
    cached = store.idempotent_result("update_case", key)
    if cached is not None:
        return cached
    current = store.get_case(case_id)
    bad_keys = _IMMUTABLE_CASE_FIELDS & set(patch)
    if bad_keys:
        raise ValidationError(f"cannot modify immutable case fields: {sorted(bad_keys)}")

    next_data = current.to_dict()
    next_data.update(patch)
    next_data["updatedAt"] = now_iso()

    next_status = CaseStatus(next_data["status"])
    next_outcome = CaseOutcome(next_data["outcome"]) if next_data.get("outcome") else None
    next_actions = [ActionType(a) for a in next_data.get("recommendedActions", [])]
    next_requires_review = bool(next_data.get("requiresHumanReview", False))

    if next_status != current.status and not can_transition_case_status(current.status, next_status):
        raise InvalidTransitionError(f"invalid case transition: {current.status} -> {next_status}")

    if next_outcome is not None and (next_actions or next_requires_review):
        raise ValidationError("recognized-merchant outcome cannot carry recommended actions or human review")

    if current.status == CaseStatus.CLASSIFYING_DISPUTE and next_status == CaseStatus.RESOLVED and next_outcome is None:
        raise InvalidTransitionError("early resolution to RESOLVED requires CUSTOMER_RECOGNIZED_MERCHANT outcome")

    updated = Case(
        caseId=current.caseId, customerId=current.customerId, status=next_status,
        createdAt=current.createdAt, updatedAt=next_data["updatedAt"], claimType=ClaimType(next_data["claimType"]),
        merchantId=next_data.get("merchantId"), transactionIds=list(next_data.get("transactionIds", [])),
        evidenceIds=list(next_data.get("evidenceIds", [])), totalDisputedAmount=next_data.get("totalDisputedAmount", 0.0),
        currency=next_data.get("currency", "USD"), confidence=next_data.get("confidence"),
        recommendedActions=next_actions, requiresHumanReview=next_requires_review, outcome=next_outcome,
    )
    store.replace_case(updated)
    store.record_audit(case_id=case_id, action="UPDATE_CASE", tool="update_case")
    result = _ok(case=updated.to_dict())
    store.remember_result("update_case", key, result)
    return result


@_guarded
def save_evidence(
    store: BankToolsStore, *, case_id: str, category: str, evidence_type: str, claim: str, source: str,
    reliability: str, idempotency_key: str, transaction_ids: list[str] | None = None,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key, "save_evidence")
    cached = store.idempotent_result("save_evidence", key)
    if cached is not None:
        return cached
    case = store.get_case(case_id)
    if evidence_id and evidence_id in case.evidenceIds:
        result = _ok(evidence=store.get_evidence(evidence_id).to_dict())
        store.remember_result("save_evidence", key, result)
        return result

    evidence = Evidence(
        evidenceId=evidence_id or new_id("ev"), caseId=case_id, category=EvidenceCategory(category),
        type=evidence_type, claim=claim, source=source, reliability=Reliability(reliability),
        transactionIds=list(transaction_ids or []),
    )
    store.add_evidence(evidence)
    updated = Case(
        caseId=case.caseId, customerId=case.customerId, status=case.status, createdAt=case.createdAt,
        updatedAt=now_iso(), claimType=case.claimType, merchantId=case.merchantId,
        transactionIds=case.transactionIds, evidenceIds=[*case.evidenceIds, evidence.evidenceId],
        totalDisputedAmount=case.totalDisputedAmount, currency=case.currency, confidence=case.confidence,
        recommendedActions=case.recommendedActions, requiresHumanReview=case.requiresHumanReview,
        outcome=case.outcome,
    )
    store.replace_case(updated)
    store.record_audit(case_id=case_id, action="SAVE_EVIDENCE", tool="save_evidence")
    result = _ok(evidence=evidence.to_dict())
    store.remember_result("save_evidence", key, result)
    return result


# -- synthetic protected action tools --------------------------------------------

def _finalize_proposal(
    store: BankToolsStore, *, tool: str, action: ActionType, case: Case, idempotency_key: str,
    policy_extra: dict[str, Any],
) -> dict[str, Any]:
    """Shared policy-gate -> decision -> (simulate | escalate) flow for the propose_* tools."""
    key = _require_idempotency_key(idempotency_key, tool)
    cached = store.idempotent_result(tool, key)
    if cached is not None:
        return cached

    decision = evaluate_policy(action, case, **policy_extra)
    policy_record = PolicyDecision(
        decisionId=new_id("policy"), caseId=case.caseId, action=action,
        outcome=decision.outcome, rationale=decision.rationale, decidedAt=now_iso(),
    )
    store.add_policy_decision(policy_record)

    reason = f"POLICY_REQUIRES_REVIEW:{action}"
    if decision.outcome is PolicyOutcome.REQUIRE_HUMAN_REVIEW and not any(
        r.reason == reason for r in store.human_review_requests_for_case(case.caseId)
    ):
        store.add_human_review_request(HumanReviewRequest(
            caseId=case.caseId, reason=reason, summary=decision.rationale,
            recommendedNextStep="Review the proposed action and approve or deny it.",
            evidenceRefs=list(case.evidenceIds),
        ))

    amount = policy_extra.get("amount")
    store.record_audit(
        case_id=case.caseId, action=str(action), tool=tool, policy_name=str(action),
        policy_outcome=decision.outcome, proposed_action=action,
        input_amount=float(amount) if isinstance(amount, (int, float)) else None,
        human_approval_required=decision.outcome is not PolicyOutcome.ALLOW,
    )

    result = _ok(
        decision=policy_record.to_dict(),
        simulated=decision.outcome is PolicyOutcome.ALLOW,
        simulatedActionId=new_id("sim") if decision.outcome is PolicyOutcome.ALLOW else None,
    )
    store.remember_result(tool, key, result)
    return result


@_guarded
def propose_provisional_credit(
    store: BankToolsStore, *, case_id: str, amount: float, idempotency_key: str,
) -> dict[str, Any]:
    case = store.get_case(case_id)
    amount = require_money(amount, "amount")
    return _finalize_proposal(
        store, tool="propose_provisional_credit", action=ActionType.PROVISIONAL_CREDIT, case=case,
        idempotency_key=idempotency_key, policy_extra={"amount": amount},
    )


@_guarded
def propose_dispute_creation(store: BankToolsStore, *, case_id: str, idempotency_key: str) -> dict[str, Any]:
    case = store.get_case(case_id)
    return _finalize_proposal(
        store, tool="propose_dispute_creation", action=ActionType.CREATE_DISPUTE, case=case,
        idempotency_key=idempotency_key, policy_extra={},
    )


@_guarded
def propose_payment_block(
    store: BankToolsStore, *, case_id: str, customer_requested: bool, idempotency_key: str,
) -> dict[str, Any]:
    case = store.get_case(case_id)
    return _finalize_proposal(
        store, tool="propose_payment_block", action=ActionType.BLOCK_RECURRING_MERCHANT, case=case,
        idempotency_key=idempotency_key, policy_extra={"customer_requested": customer_requested},
    )


@_guarded
def propose_card_replacement(store: BankToolsStore, *, case_id: str, idempotency_key: str) -> dict[str, Any]:
    case = store.get_case(case_id)
    return _finalize_proposal(
        store, tool="propose_card_replacement", action=ActionType.REPLACE_CARD, case=case,
        idempotency_key=idempotency_key, policy_extra={},
    )


# -- investigator review decisions -------------------------------------------------
#
# submit_review_decision writes the case and the review request as two separate items (no
# cross-item transactions in this store -- see store.py/dynamo_store.py module docs). A crash
# between the two leaves the case moved but the review still PENDING; a retry with the same
# idempotencyKey then sees a case that is no longer NEEDS_HUMAN_REVIEW and reports
# INVALID_TRANSITION rather than completing. Same accepted limitation as the rest of this store;
# a fix needs a version attribute / TransactWriteItems (bank-tools NEXT TASKS).

@_guarded
def submit_review_decision(
    store: BankToolsStore, *, case_id: str, review_id: str, actor: str, decision: str, reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key, "submit_review_decision")
    cached = store.idempotent_result("submit_review_decision", key)
    if cached is not None:
        return cached

    def _fail(exc: ToolError, *, audit_actor: str) -> ToolError:
        # Every attempt -- not just the ones that succeed -- lands in the audit trail.
        store.record_audit(
            case_id=case_id, action=f"REVIEW_DECISION_REJECTED:{exc.code}", tool="submit_review_decision",
            result="FAILURE", actor=audit_actor,
        )
        return exc

    try:
        actor = require_id(actor, "actor")
        reason = require_id(reason, "reason")
        decision_type = ReviewDecisionType(decision)
    except ValueError as exc:
        raise _fail(ToolError(str(exc), "VALIDATION_ERROR"), audit_actor=actor if isinstance(actor, str) and actor else "UNKNOWN") from exc

    try:
        case = store.get_case(case_id)
        review = store.get_human_review_request(case_id, review_id)
    except ToolError as exc:
        raise _fail(exc, audit_actor=actor) from exc

    if review.status is not ReviewStatus.PENDING:
        raise _fail(ReviewAlreadyDecidedError(f"review {review_id} was already decided"), audit_actor=actor)
    if case.status is not CaseStatus.NEEDS_HUMAN_REVIEW:
        raise _fail(
            InvalidTransitionError(f"case {case_id} is not awaiting human review (status={case.status})"),
            audit_actor=actor,
        )
    next_status = REVIEW_DECISION_TARGET_STATUS[decision_type]
    if not can_transition_case_status(case.status, next_status):
        raise _fail(InvalidTransitionError(f"invalid case transition: {case.status} -> {next_status}"), audit_actor=actor)

    now = now_iso()
    updated_case = Case(
        caseId=case.caseId, customerId=case.customerId, status=next_status, createdAt=case.createdAt,
        updatedAt=now, claimType=case.claimType, merchantId=case.merchantId, transactionIds=case.transactionIds,
        evidenceIds=case.evidenceIds, totalDisputedAmount=case.totalDisputedAmount, currency=case.currency,
        confidence=case.confidence, recommendedActions=case.recommendedActions, requiresHumanReview=False,
        outcome=case.outcome,
    )
    store.replace_case(updated_case)

    resolved_review = HumanReviewRequest(
        reviewId=review.reviewId, caseId=review.caseId, reason=review.reason, summary=review.summary,
        recommendedNextStep=review.recommendedNextStep, evidenceRefs=review.evidenceRefs,
        status=ReviewStatus.RESOLVED,
    )
    store.replace_human_review_request(resolved_review)

    record = ReviewDecision(
        decisionId=new_id("decision"), reviewId=review_id, caseId=case_id, actor=actor,
        decision=decision_type, reason=reason, decidedAt=now,
    )
    store.add_review_decision(record)
    store.record_audit(
        case_id=case_id, action=f"REVIEW_DECISION:{decision_type}", tool="submit_review_decision",
        result="SUCCESS", actor=actor, human_approval_required=False,
    )

    result = _ok(case=updated_case.to_dict(), review=resolved_review.to_dict(), decision=record.to_dict())
    store.remember_result("submit_review_decision", key, result)
    return result


# -- audit persistence ------------------------------------------------------------

@_guarded
def get_audit_log(store: BankToolsStore, *, case_id: str) -> dict[str, Any]:
    records = store.audit_for_case(case_id)
    return _ok(caseId=case_id, events=[r.to_dict() for r in records], count=len(records))
