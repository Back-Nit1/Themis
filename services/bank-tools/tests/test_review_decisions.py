"""submit_review_decision: approve/reject/request-more-evidence, replay, conflicting decision,
invalid transition, malformed input, and backend failure. Authorization (who may call this tool
at all) is enforced by the caller's boundary, not here -- see apps/dashboard's review-decision
route and packages/contracts README "Review decision workflow".
"""
import pytest

from bank_tools import tools
from bank_tools.models import Case, CaseStatus, ClaimType, HumanReviewRequest


def _add_case(store, case_id, **overrides):
    fields = dict(
        caseId=case_id, customerId="customer_001", status=CaseStatus.NEEDS_HUMAN_REVIEW,
        createdAt="2026-06-01T00:00:00+00:00", updatedAt="2026-06-01T00:00:00+00:00",
        claimType=ClaimType.UNAUTHORIZED_TRANSACTION, merchantId="merchant_asteria",
        transactionIds=[], evidenceIds=[], totalDisputedAmount=0.0, currency="USD",
        confidence=None, recommendedActions=[], requiresHumanReview=True,
    )
    fields.update(overrides)
    store.add_case(Case(**fields))
    return store.get_case(case_id)


def _add_review(store, case_id, **overrides):
    fields = dict(
        caseId=case_id, reason="MISSING_REQUIRED_EVIDENCE", summary="Authorization is unclear.",
        recommendedNextStep="Review evidence.", evidenceRefs=[],
    )
    fields.update(overrides)
    review = HumanReviewRequest(**fields)
    store.add_human_review_request(review)
    return review


def test_approve_moves_case_to_action_approved_and_resolves_review(store):
    _add_case(store, "case_a")
    review = _add_review(store, "case_a")
    result = tools.submit_review_decision(
        store, case_id="case_a", review_id=review.reviewId, actor="investigator_jane",
        decision="APPROVE", reason="Evidence is sufficient.", idempotency_key="k1",
    )
    assert result["status"] == "ok"
    assert result["case"]["status"] == "ACTION_APPROVED"
    assert result["case"]["requiresHumanReview"] is False
    assert result["review"]["status"] == "RESOLVED"
    assert result["decision"]["decision"] == "APPROVE"
    assert result["decision"]["idempotencyKey"] == "k1"
    assert store.get_human_review_request("case_a", review.reviewId).status == "RESOLVED"
    assert len(store.review_decisions_for_case("case_a")) == 1
    audit_actions = [a.action for a in store.audit_for_case("case_a")]
    assert "REVIEW_DECISION:APPROVE" in audit_actions


def test_reject_and_request_more_evidence_both_return_to_investigating(store):
    _add_case(store, "case_r")
    review = _add_review(store, "case_r")
    result = tools.submit_review_decision(
        store, case_id="case_r", review_id=review.reviewId, actor="investigator_jane",
        decision="REJECT", reason="Not persuasive.", idempotency_key="k1",
    )
    assert result["case"]["status"] == "INVESTIGATING"

    _add_case(store, "case_e")
    review2 = _add_review(store, "case_e")
    result2 = tools.submit_review_decision(
        store, case_id="case_e", review_id=review2.reviewId, actor="investigator_jane",
        decision="REQUEST_MORE_EVIDENCE", reason="Need merchant authorization record.", idempotency_key="k1",
    )
    assert result2["case"]["status"] == "INVESTIGATING"


def test_replay_returns_the_same_result_without_reapplying(store):
    _add_case(store, "case_replay")
    review = _add_review(store, "case_replay")
    first = tools.submit_review_decision(
        store, case_id="case_replay", review_id=review.reviewId, actor="investigator_jane",
        decision="APPROVE", reason="ok", idempotency_key="dup-key",
    )
    second = tools.submit_review_decision(
        store, case_id="case_replay", review_id=review.reviewId, actor="investigator_jane",
        decision="APPROVE", reason="ok", idempotency_key="dup-key",
    )
    assert second == first
    assert len(store.review_decisions_for_case("case_replay")) == 1


def test_conflicting_decision_on_an_already_resolved_review_is_rejected(store):
    _add_case(store, "case_conflict")
    review = _add_review(store, "case_conflict")
    tools.submit_review_decision(
        store, case_id="case_conflict", review_id=review.reviewId, actor="investigator_jane",
        decision="APPROVE", reason="ok", idempotency_key="k1",
    )
    second = tools.submit_review_decision(
        store, case_id="case_conflict", review_id=review.reviewId, actor="investigator_bob",
        decision="REJECT", reason="I disagree", idempotency_key="k2",
    )
    assert second["status"] == "error"
    assert second["error"]["code"] == "REVIEW_ALREADY_DECIDED"
    failures = [a for a in store.audit_for_case("case_conflict") if a.result == "FAILURE"]
    assert any(a.action == "REVIEW_DECISION_REJECTED:REVIEW_ALREADY_DECIDED" for a in failures)
    assert failures[-1].actor == "investigator_bob"


def test_invalid_transition_when_case_is_not_awaiting_human_review(store):
    _add_case(store, "case_bad_state", status=CaseStatus.INVESTIGATING, requiresHumanReview=False)
    review = _add_review(store, "case_bad_state")
    result = tools.submit_review_decision(
        store, case_id="case_bad_state", review_id=review.reviewId, actor="investigator_jane",
        decision="APPROVE", reason="ok", idempotency_key="k1",
    )
    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_TRANSITION"


def test_malformed_decision_value_is_rejected(store):
    _add_case(store, "case_malformed")
    review = _add_review(store, "case_malformed")
    result = tools.submit_review_decision(
        store, case_id="case_malformed", review_id=review.reviewId, actor="investigator_jane",
        decision="MAYBE_LATER", reason="ok", idempotency_key="k1",
    )
    assert result["status"] == "error"
    assert result["error"]["code"] == "VALIDATION_ERROR"


def test_unknown_review_id_is_not_found(store):
    _add_case(store, "case_unknown_review")
    result = tools.submit_review_decision(
        store, case_id="case_unknown_review", review_id="review_does_not_exist", actor="investigator_jane",
        decision="APPROVE", reason="ok", idempotency_key="k1",
    )
    assert result["status"] == "error"
    assert result["error"]["code"] == "NOT_FOUND"


class _BackendOutage:
    """Wraps a real store but simulates an infrastructure failure on case lookup."""

    def __init__(self, inner):
        self._inner = inner

    def get_case(self, case_id):
        raise RuntimeError("simulated DynamoDB outage")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_backend_failure_propagates_and_leaves_the_review_pending(store):
    _add_case(store, "case_outage")
    review = _add_review(store, "case_outage")
    broken = _BackendOutage(store)
    with pytest.raises(RuntimeError):
        tools.submit_review_decision(
            broken, case_id="case_outage", review_id=review.reviewId, actor="investigator_jane",
            decision="APPROVE", reason="ok", idempotency_key="k1",
        )
    assert store.get_human_review_request("case_outage", review.reviewId).status == "PENDING"
    assert store.idempotent_result("submit_review_decision", "k1") is None
