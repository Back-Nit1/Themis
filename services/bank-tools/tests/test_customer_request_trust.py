"""propose_payment_block's customerRequested must never be trusted on the caller's say-so alone.

Covers the payment-block trust boundary: a claim of true is only honored when backed by fresh,
persisted CUSTOMER_CLAIMS/PAYMENT_BLOCK_REQUESTED evidence on the case (see dispatch._adapt_payment_block
and policy.customer_requested_payment_block).
"""
import pytest

from bank_tools.dispatch import dispatch
from bank_tools.models import Evidence, EvidenceCategory, Reliability
from bank_tools.policy import CUSTOMER_REQUEST_EVIDENCE_TYPE, customer_requested_payment_block
from bank_tools.store import BankToolsStore
from conftest import seed

FRESH_ARGS = {"caseId": "case_seed", "customerRequested": True, "idempotencyKey": "k"}
STALE_RECORDED_AT = "2000-01-01T00:00:00+00:00"


@pytest.fixture
def store() -> BankToolsStore:
    return seed(BankToolsStore())


def ok(result):
    assert result["status"] == "ok", result
    return result


def code(result):
    assert result["status"] == "error", result
    return result["error"]["code"]


def _seed_request_evidence(store: BankToolsStore, *, recorded_at: str | None = None) -> None:
    store.add_evidence(Evidence(
        evidenceId="ev_block_request", caseId="case_seed", category=EvidenceCategory.CUSTOMER_CLAIMS,
        type=CUSTOMER_REQUEST_EVIDENCE_TYPE, claim="please block future payments from this merchant",
        source="CUSTOMER", reliability=Reliability.MEDIUM, transactionIds=[],
        **({"recordedAt": recorded_at} if recorded_at is not None else {}),
    ))
    case = store.get_case("case_seed")
    from dataclasses import replace
    store.replace_case(replace(case, evidenceIds=[*case.evidenceIds, "ev_block_request"]))


def test_confirmed_request_is_allowed(store):
    _seed_request_evidence(store)
    result = ok(dispatch(store, "propose_payment_block", FRESH_ARGS))
    assert result["simulated"] is True and result["decision"]["outcome"] == "ALLOW"


def test_absent_request_is_denied_not_rejected(store):
    result = ok(dispatch(store, "propose_payment_block", {**FRESH_ARGS, "customerRequested": False, "idempotencyKey": "k2"}))
    assert result["simulated"] is False and result["decision"]["outcome"] == "DENY"


def test_fabricated_request_is_rejected(store):
    # No evidence at all: the model/tool argument's claim of true is not backed by anything persisted.
    result = dispatch(store, "propose_payment_block", FRESH_ARGS)
    assert code(result) == "CUSTOMER_REQUEST_UNVERIFIED"
    assert store.policy_decisions_for_case("case_seed") == []


def test_stale_evidence_does_not_authorize(store):
    _seed_request_evidence(store, recorded_at=STALE_RECORDED_AT)
    result = dispatch(store, "propose_payment_block", FRESH_ARGS)
    assert code(result) == "CUSTOMER_REQUEST_UNVERIFIED"


def test_replay_returns_the_same_result_without_rechecking_evidence(store):
    _seed_request_evidence(store)
    first = ok(dispatch(store, "propose_payment_block", FRESH_ARGS))
    assert dispatch(store, "propose_payment_block", FRESH_ARGS) == first
    assert len(store.policy_decisions_for_case("case_seed")) == 1


def test_derivation_helper_agrees_with_dispatch_outcome(store):
    assert customer_requested_payment_block(store.evidence_for_case("case_seed")) is False
    _seed_request_evidence(store)
    assert customer_requested_payment_block(store.evidence_for_case("case_seed")) is True
