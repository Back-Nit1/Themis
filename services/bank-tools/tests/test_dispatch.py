import re
from pathlib import Path

import pytest

from bank_tools.dispatch import NOT_OWNED_TOOLS, SPECS, dispatch
from bank_tools.dynamo_store import DynamoBankToolsStore
from bank_tools.fixtures import load_demo_store
from bank_tools.store import BankToolsStore
from conftest import seed
from fake_dynamo import TABLES, FakeDynamo

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(params=["memory", "dynamo"])
def any_store(request):
    store = BankToolsStore() if request.param == "memory" else DynamoBankToolsStore(FakeDynamo(), TABLES)
    return seed(store)


def ok(result):
    assert result["status"] == "ok", result
    return result


def code(result):
    assert result["status"] == "error", result
    return result["error"]["code"]


# -- camelCase dispatch ---------------------------------------------------------------

def test_camel_case_read_tools(any_store):
    found = ok(dispatch(any_store, "search_transactions", {
        "customerId": "customer_001", "descriptorContains": "asteria", "minAmount": 5, "limit": 2,
    }))
    assert [t["id"] for t in found["transactions"]] == ["txn_001", "txn_002"] and found["count"] == 3
    assert ok(dispatch(any_store, "get_merchant_profile", {"merchantId": "merchant_asteria"}))["profile"]["merchantId"]
    assert ok(dispatch(any_store, "resolve_merchant", {"descriptor": "asteria*premium"}))["merchantId"] == "merchant_asteria"


def test_camel_case_mutating_tools(any_store):
    created = ok(dispatch(any_store, "create_case", {
        "customerId": "customer_001", "claimType": "UNRECOGNIZED_MERCHANT",
        "transactionIds": ["txn_001"], "merchantId": "merchant_asteria", "idempotencyKey": "k-create",
    }))["case"]
    assert created["totalDisputedAmount"] == 9.99
    updated = ok(dispatch(any_store, "update_case", {
        "caseId": created["caseId"], "status": "INTAKE", "confidence": 0.5, "requiresHumanReview": True,
        "idempotencyKey": "k-update",
    }))["case"]
    assert (updated["status"], updated["confidence"], updated["requiresHumanReview"]) == ("INTAKE", 0.5, True)
    ev = ok(dispatch(any_store, "save_evidence", {
        "caseId": created["caseId"], "category": "TRANSACTION_EVIDENCE", "type": "ledger", "claim": "3 charges",
        "source": "ledger", "reliability": "HIGH", "transactionIds": ["txn_001"], "idempotencyKey": "k-ev",
    }))["evidence"]
    assert ev["type"] == "ledger"
    assert any_store.get_case(created["caseId"]).evidenceIds == [ev["evidenceId"]]


def test_camel_case_submit_review_decision(any_store):
    case_id = ok(dispatch(any_store, "create_case", {
        "customerId": "customer_001", "claimType": "DUPLICATE_TRANSACTION", "transactionIds": [],
        "idempotencyKey": "k-create-review",
    }))["case"]["caseId"]
    ok(dispatch(any_store, "propose_provisional_credit", {
        "caseId": case_id, "amount": 999.0, "confidence": 0.9, "claimType": "DUPLICATE_TRANSACTION",
        "idempotencyKey": "k-propose-review",
    }))
    review_id = any_store.human_review_requests_for_case(case_id)[0].reviewId
    ok(dispatch(any_store, "update_case", {
        "caseId": case_id, "status": "INTAKE", "idempotencyKey": "k-intake-review",
    }))
    ok(dispatch(any_store, "update_case", {
        "caseId": case_id, "status": "NEEDS_HUMAN_REVIEW", "requiresHumanReview": True,
        "idempotencyKey": "k-escalate-review",
    }))
    decided = ok(dispatch(any_store, "submit_review_decision", {
        "caseId": case_id, "reviewId": review_id, "actor": "investigator_jane",
        "decision": "APPROVE", "reason": "Sufficient evidence.", "idempotencyKey": "k-decide-review",
    }))
    assert decided["case"]["status"] == "ACTION_APPROVED"
    assert decided["review"]["status"] == "RESOLVED"


# -- validation -----------------------------------------------------------------------------

@pytest.mark.parametrize("tool,args,fragment", [
    ("get_case", {}, "missing required arguments: ['caseId']"),
    ("get_case", {"caseId": "c", "case_id": "c"}, "unknown arguments: ['case_id']"),
    ("get_case", {"caseId": ""}, "nonempty"),
    ("get_case", "case_seed", "must be an object"),
    ("create_case", {"customerId": "customer_001", "claimType": "X"}, "idempotencyKey"),
    ("create_case", {"customerId": "c", "claimType": "X", "idempotencyKey": ""}, "idempotencyKey"),
    ("create_case", {"customerId": "c", "claimType": "X", "transactionIds": [1], "idempotencyKey": "k"}, "transactionIds"),
    ("search_transactions", {"customerId": "c", "limit": True}, "limit"),
    ("propose_provisional_credit", {"caseId": "c", "amount": -1, "confidence": 0.5, "claimType": "X", "idempotencyKey": "k"}, "amount"),
    ("propose_provisional_credit", {"caseId": "c", "amount": 1, "confidence": 1.5, "claimType": "X", "idempotencyKey": "k"}, "confidence"),
    ("propose_payment_block", {"caseId": "c", "idempotencyKey": "k"}, "customerRequested"),
])
def test_bad_arguments_rejected_before_running(any_store, tool, args, fragment):
    result = dispatch(any_store, tool, args)
    assert code(result) == "VALIDATION_ERROR" and fragment in result["error"]["message"]


def test_update_case_needs_a_change(any_store):
    assert code(dispatch(any_store, "update_case", {"caseId": "case_seed", "idempotencyKey": "k"})) == "VALIDATION_ERROR"


@pytest.mark.parametrize("tool,owner", NOT_OWNED_TOOLS.items())
def test_other_workstream_tools_are_not_owned(any_store, tool, owner):
    result = dispatch(any_store, tool, {"caseId": "case_seed"})
    assert code(result) == "NOT_OWNED" and result["error"]["owner"] == owner


def test_unknown_tool(any_store):
    assert code(dispatch(any_store, "get_audit_log", {"caseId": "case_seed"})) == "UNKNOWN_TOOL"


@pytest.mark.parametrize("tool,args", [
    ("get_case", {"caseId": "x" * 4097}),
    ("create_case", {"customerId": "c", "claimType": "X", "idempotencyKey": "k" * 257}),
    ("create_case", {"customerId": "c", "claimType": "X", "transactionIds": ["t"] * 101, "idempotencyKey": "k"}),
    ("create_case", {"customerId": "c", "claimType": "X", "transactionIds": ["t" * 257], "idempotencyKey": "k"}),
])
def test_oversized_arguments_are_rejected_before_storage(any_store, tool, args):
    assert code(dispatch(any_store, tool, args)) == "VALIDATION_ERROR"


@pytest.mark.parametrize("name", [None, ["get_case"], "bad\nname", "x" * 65])
def test_invalid_tool_names_are_rejected_without_exception(any_store, name):
    assert code(dispatch(any_store, name, {})) == "UNKNOWN_TOOL"


# -- replay ---------------------------------------------------------------------------------------

def test_create_case_replay_returns_prior_result_without_duplicates(any_store):
    args = {"customerId": "customer_001", "claimType": "UNRECOGNIZED_MERCHANT", "idempotencyKey": "k1"}
    first = ok(dispatch(any_store, "create_case", args))
    assert dispatch(any_store, "create_case", args) == first
    cases = any_store.cases_for_customer("customer_001")
    assert len(cases) == 2
    audits = [e for e in any_store.audit_for_case(first["case"]["caseId"]) if e.action == "CREATE_CASE"]
    assert len(audits) == 1


def test_save_evidence_replay_adds_one_evidence(any_store):
    args = {"caseId": "case_seed", "category": "TRANSACTION_EVIDENCE", "type": "ledger", "claim": "c",
            "source": "s", "reliability": "HIGH", "idempotencyKey": "k-ev"}
    first = ok(dispatch(any_store, "save_evidence", args))
    assert dispatch(any_store, "save_evidence", args) == first
    assert len(any_store.get_case("case_seed").evidenceIds) == 1
    assert [e.action for e in any_store.audit_for_case("case_seed")] == ["SAVE_EVIDENCE"]


def test_propose_replay_keeps_one_decision_action_and_audit(any_store):
    args = {"caseId": "case_seed", "customerRequested": True, "idempotencyKey": "k-block"}
    first = ok(dispatch(any_store, "propose_payment_block", args))
    assert first["simulated"] is True
    assert dispatch(any_store, "propose_payment_block", args) == first
    audits = any_store.audit_for_case("case_seed")
    assert len(audits) == 1 and audits[0].tool == "propose_payment_block"
    assert len(any_store.policy_decisions_for_case("case_seed")) == 1


def test_key_reuse_with_different_arguments_is_rejected(any_store):
    ok(dispatch(any_store, "create_case", {"customerId": "customer_001", "claimType": "UNRECOGNIZED_MERCHANT", "idempotencyKey": "k"}))
    other = dispatch(any_store, "create_case", {"customerId": "customer_001", "claimType": "DUPLICATE_TRANSACTION", "idempotencyKey": "k"})
    assert code(other) == "IDEMPOTENCY_KEY_REUSED"
    assert len(any_store.cases_for_customer("customer_001")) == 2


def test_failed_call_releases_key_for_retry(any_store):
    bad = {"caseId": "case_seed", "status": "RESOLVED", "idempotencyKey": "k"}
    assert code(dispatch(any_store, "update_case", bad)) == "INVALID_TRANSITION"
    assert code(dispatch(any_store, "update_case", bad)) == "INVALID_TRANSITION"  # ran again, not IN_PROGRESS
    assert ok(dispatch(any_store, "update_case", {"caseId": "case_seed", "status": "INTAKE", "idempotencyKey": "k"}))


# -- ownership / policy ---------------------------------------------------------------------------------

def test_create_case_rejects_other_customers_transaction(any_store):
    any_store.add_customer({"customerId": "customer_002", "name": "Other", "phone": "+15555550124", "email": "o@example.test"})
    result = dispatch(any_store, "create_case", {
        "customerId": "customer_002", "claimType": "UNRECOGNIZED_MERCHANT",
        "transactionIds": ["txn_001"], "idempotencyKey": "k",
    })
    assert code(result) == "TRANSACTION_NOT_OWNED"
    assert any_store.cases_for_customer("customer_002") == []


def test_create_case_rejects_currency_mismatch(any_store):
    result = dispatch(any_store, "create_case", {
        "customerId": "customer_001", "claimType": "UNRECOGNIZED_MERCHANT", "currency": "EUR",
        "transactionIds": ["txn_001"], "idempotencyKey": "k",
    })
    assert code(result) == "VALIDATION_ERROR" and "currency" in result["error"]["message"]


def test_python_policy_overrides_gateway_supplied_hints(any_store):
    result = ok(dispatch(any_store, "propose_provisional_credit", {
        "caseId": "case_seed", "amount": 5000, "confidence": 0.99, "claimType": "DUPLICATE_TRANSACTION",
        "idempotencyKey": "k",
    }))
    assert result["simulated"] is False and result["decision"]["outcome"] == "REQUIRE_HUMAN_REVIEW"
    blocked = ok(dispatch(any_store, "propose_payment_block", {
        "caseId": "case_seed", "customerRequested": False, "idempotencyKey": "k2",
    }))
    assert blocked["decision"]["outcome"] == "DENY" and blocked["simulated"] is False


def test_dispute_creation_transactions_must_be_on_case(any_store):
    ok(dispatch(any_store, "update_case", {"caseId": "case_seed", "requiresHumanReview": False, "idempotencyKey": "u"}))
    args = {"caseId": "case_seed", "transactionIds": ["txn_001"], "hasConfirmedTransactions": True, "idempotencyKey": "d"}
    assert code(dispatch(any_store, "propose_dispute_creation", args)) == "VALIDATION_ERROR"
    # hasConfirmedTransactions=True does not override policy: the stored case has no transactions.
    result = ok(dispatch(any_store, "propose_dispute_creation", {**args, "transactionIds": [], "idempotencyKey": "d2"}))
    assert result["decision"]["outcome"] == "REQUIRE_HUMAN_REVIEW"


# -- current demo fixtures ---------------------------------------------------------------------------------

@pytest.fixture(params=["memory", "dynamo"])
def demo_store(request):
    store = BankToolsStore() if request.param == "memory" else DynamoBankToolsStore(FakeDynamo(), TABLES)
    return load_demo_store(ROOT, store)


def test_demo_fixture_dispatch(demo_store):
    found = ok(dispatch(demo_store, "search_transactions", {"customerId": "customer_demo_001", "limit": 200}))
    assert found["count"] == 30
    assert ok(dispatch(demo_store, "get_case", {"caseId": "case_demo_a"}))["case"]["customerId"] == "customer_demo_001"
    assert ok(dispatch(demo_store, "resolve_merchant", {"descriptor": "asteria*premium"}))["merchantId"] == "merchant_demo_001"
    assert ok(dispatch(demo_store, "get_merchant_risk_signals", {"merchantId": "merchant_demo_001"}))["count"] >= 0
    assert ok(dispatch(demo_store, "get_customer_dispute_history", {"customerId": "customer_demo_001"}))["count"] == 1


def test_demo_fixture_case_creation_and_ownership(demo_store):
    good = ok(dispatch(demo_store, "create_case", {
        "customerId": "customer_demo_001", "claimType": "RECURRING_PAYMENT_NOT_AUTHORIZED",
        "transactionIds": ["txn_demo_0001"], "idempotencyKey": "demo-1",
    }))["case"]
    assert good["totalDisputedAmount"] == 9.99
    foreign = dispatch(demo_store, "create_case", {
        "customerId": "customer_demo_001", "claimType": "RECURRING_PAYMENT_NOT_AUTHORIZED",
        "transactionIds": ["txn_demo_0031"], "idempotencyKey": "demo-2",
    })
    assert code(foreign) == "TRANSACTION_NOT_OWNED"


# -- contract drift -----------------------------------------------------------------------------------------

def _ts_contract():
    text = (ROOT / "infra/config/tool-schemas.ts").read_text(encoding="utf-8")
    kinds = {"str": "str", "num": "num", "arr": "list", "bool": "bool"}
    tools = {}
    for name, body in re.findall(r"\{ name: '(\w+)', description: .*? params: \[(.*)\] \},?$", text, re.M):
        params = {"idempotencyKey": ("str", True)} if "idem()" in body else {}
        for kind, args in re.findall(r"(str|num|arr|bool)\(([^)]*)\)", body):
            pname, *rest = (a.strip().strip("'") for a in args.split(","))
            default = kind in ("str", "num")
            required = default if not rest or rest[0] not in ("true", "false") else rest[0] == "true"
            params[pname] = (kinds[kind], required)
        tools[name] = params
    return tools


def test_dispatcher_matches_gateway_contract():
    contract = _ts_contract()
    assert set(contract) == set(SPECS) | set(NOT_OWNED_TOOLS)
    for name, spec in SPECS.items():
        assert {p.camel: (p.kind, p.required) for p in spec.params} == contract[name], name
