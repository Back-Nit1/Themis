"""Duplicate mutating-tool calls (same idempotencyKey) must replay, never re-write. Different arguments under one key are rejected."""
import pytest
from world import Config, World

C = "customer_demo_001"


@pytest.fixture
def w():
    return World(config=Config(structured_tools=True))


def rows(w, case_id):
    s = w.store
    return (len(s.evidence_for_case(case_id)), len(s.audit_for_case(case_id)), len(s.policy_decisions_for_case(case_id)),
            len(s.human_review_requests_for_case(case_id)), len(s.cases_for_customer(C)), len(w.messenger.sent))


def new_case(w):
    r = w.adapter.call("create_case", {"customerId": C, "claimType": "INSUFFICIENT_INFORMATION", "transactionIds": ["txn_demo_0001"],
                                       "merchantId": "merchant_demo_001", "idempotencyKey": "k-create"})
    return r["case"]["caseId"]


def test_create_case_twice_yields_one_case(w):
    before = len(w.store.cases_for_customer(C))
    a = new_case(w)
    b = new_case(w)
    assert a == b and len(w.store.cases_for_customer(C)) == before + 1


def test_every_mutating_tool_replays_without_new_rows(w):
    case = new_case(w)
    calls = [
        ("update_case", {"caseId": case, "status": "INTAKE"}),
        ("save_evidence", {"caseId": case, "category": "CUSTOMER_CLAIMS", "type": "PAYMENT_BLOCK_REQUESTED", "claim": "please block future payments",
                           "source": "CUSTOMER", "reliability": "MEDIUM", "transactionIds": ["txn_demo_0001"], "evidenceId": "ev_dup_1"}),
        ("propose_dispute_creation", {"caseId": case, "transactionIds": ["txn_demo_0001"], "hasConfirmedTransactions": True}),
        ("propose_payment_block", {"caseId": case, "customerRequested": True}),
        ("generate_case_report", {"caseId": case}),
        ("escalate_case", {"caseId": case, "reason": "QA_DUPLICATE_CHECK", "summary": "dup", "evidenceRefs": ["ev_dup_1"]}),
        ("send_customer_message", {"caseId": case, "channel": "SMS", "text": "hello"}),
        ("send_case_email", {"caseId": case}),
    ]
    for tool, args in calls:
        first = w.adapter.call(tool, {**args, "idempotencyKey": f"k-{tool}"})
        assert first["status"] == "ok", (tool, first)
        snapshot = rows(w, case)
        again = w.adapter.call(tool, {**args, "idempotencyKey": f"k-{tool}"})
        assert again == first, tool
        assert rows(w, case) == snapshot, f"{tool} wrote again on replay"
    assert len(w.messenger.sent) == 2  # one SMS, one email


def test_same_key_different_arguments_is_rejected_and_writes_nothing(w):
    case = new_case(w)
    args = {"caseId": case, "category": "CUSTOMER_CLAIMS", "type": "CLAIM_STATEMENT", "source": "CUSTOMER",
            "reliability": "MEDIUM", "transactionIds": [], "idempotencyKey": "k-ev"}
    assert w.adapter.call("save_evidence", {**args, "claim": "one"})["status"] == "ok"
    n = rows(w, case)
    r = w.adapter.call("save_evidence", {**args, "claim": "two"})
    assert r["error"]["code"] == "IDEMPOTENCY_KEY_REUSED" and rows(w, case) == n


def test_escalating_twice_with_new_keys_queues_one_review(w):
    case = new_case(w)
    assert w.adapter.call("update_case", {"caseId": case, "status": "INTAKE", "idempotencyKey": "k-intake"})["status"] == "ok"
    for k in ("e1", "e2"):
        assert w.adapter.call("escalate_case", {"caseId": case, "reason": "QA_TWICE", "idempotencyKey": k})["status"] == "ok"
    assert len(w.store.human_review_requests_for_case(case)) == 1


def test_agent_rerun_of_finished_conversation_makes_no_further_writes():
    w = World(config=Config(structured_tools=True))
    r = w.chat(C, "9.99 from ASTERIA", "yes I don't recognize it")
    calls, snapshot = len(w.gateway.calls), rows(w, r.case_id)
    again = w.chat(C, "yes I don't recognize it")  # e.g. the same confirmation delivered twice past the messaging guard
    assert again.text == r.text and len(w.gateway.calls) == calls and rows(w, r.case_id) == snapshot
