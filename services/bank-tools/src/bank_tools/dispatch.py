"""Gateway dispatcher for the bank-owned tools.

`dispatch(store, tool_name, arguments)` takes a tool name and the camelCase argument object
declared in infra/config/tool-schemas.ts (TOOL_CONTRACT_VERSION 2026-09-22), validates it, maps it
to the snake_case Python tool signature via the explicit tables below, and runs the tool.

- Unknown tool -> UNKNOWN_TOOL. Tools owned by AgentCore/messaging -> NOT_OWNED (never faked).
- Missing/mistyped/out-of-range/unknown arguments -> VALIDATION_ERROR before anything runs.
- Every mutating tool needs `idempotencyKey`; the key is claimed atomically in the store, so a
  replay returns the prior result and re-runs nothing (no duplicate cases, evidence, policy
  decisions, audit events or simulated actions). Reusing a key with different arguments is rejected.
- Deterministic policy still runs inside the propose_* tools; Gateway-supplied hints that the
  policy owns (`confidence`, `claimType`, `hasConfirmedTransactions`) are validated but ignored.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Callable

from . import tools
from .errors import LeaseLostError, ToolError
from .store import CLAIMED, CURRENT_LEASE, IN_PROGRESS, MISMATCH, BankToolsStorage

# Keep in sync with infra/config/tool-schemas.ts; tests/test_dispatch.py parses that file and fails on drift.
NOT_OWNED_TOOLS = {
    "generate_case_report": "agentcore",
    "escalate_case": "agentcore",
    "send_customer_message": "messaging",
    "send_case_email": "messaging",
}
MAX_ARGUMENT_STRING_LENGTH = 4_096
MAX_IDEMPOTENCY_KEY_LENGTH = 256
MAX_LIST_ITEMS = 100
MAX_LIST_ITEM_LENGTH = 256


@dataclass(frozen=True, slots=True)
class Param:
    camel: str
    snake: str
    kind: str  # str | num | bool | list
    required: bool = True
    minimum: float | None = None
    maximum: float | None = None


def _s(camel: str, snake: str, required: bool = True) -> Param:
    return Param(camel, snake, "str", required)


def _n(camel: str, snake: str, required: bool = True, lo: float | None = None, hi: float | None = None) -> Param:
    return Param(camel, snake, "num", required, lo, hi)


def _b(camel: str, snake: str, required: bool = False) -> Param:
    return Param(camel, snake, "bool", required)


def _a(camel: str, snake: str, required: bool = False) -> Param:
    return Param(camel, snake, "list", required)


_IDEM = _s("idempotencyKey", "idempotency_key")


@dataclass(frozen=True, slots=True)
class Spec:
    fn: Callable[..., dict[str, Any]]
    params: tuple[Param, ...]
    adapt: Callable[[BankToolsStorage, dict[str, Any]], dict[str, Any]] | None = None

    @property
    def mutating(self) -> bool:
        return any(p.camel == "idempotencyKey" for p in self.params)


def _adapt_update_case(store: BankToolsStorage, kw: dict[str, Any]) -> dict[str, Any]:
    patch_fields = {"status": "status", "claim_type": "claimType", "merchant_id": "merchantId",
                    "confidence": "confidence", "requires_human_review": "requiresHumanReview", "outcome": "outcome"}
    patch = {camel: kw[snake] for snake, camel in patch_fields.items() if snake in kw}
    if not patch:
        raise ToolError("update_case requires at least one field to change", "VALIDATION_ERROR")
    return {"case_id": kw["case_id"], "patch": patch, "idempotency_key": kw["idempotency_key"]}


def _adapt_dispute(store: BankToolsStorage, kw: dict[str, Any]) -> dict[str, Any]:
    case = store.get_case(kw["case_id"])
    extra = set(kw["transaction_ids"]) - set(case.transactionIds)
    if extra:
        raise ToolError(f"transactions not on case: {sorted(extra)}", "VALIDATION_ERROR")
    return {"case_id": kw["case_id"], "idempotency_key": kw["idempotency_key"]}


def _adapt_credit(store: BankToolsStorage, kw: dict[str, Any]) -> dict[str, Any]:
    # policy reads the stored case's confidence/claim type, not the caller's claims about them
    return {k: kw[k] for k in ("case_id", "amount", "idempotency_key")}


SPECS: dict[str, Spec] = {
    "search_transactions": Spec(tools.search_transactions, (
        _s("customerId", "customer_id"), _s("merchantId", "merchant_id", False),
        _s("descriptorContains", "descriptor_contains", False), _s("since", "since", False),
        _s("until", "until", False), _n("minAmount", "min_amount", False), _n("maxAmount", "max_amount", False),
        _n("limit", "limit", False), _s("caseId", "case_id", False))),
    "get_transaction_details": Spec(tools.get_transaction_details, (
        _s("transactionId", "transaction_id"), _s("caseId", "case_id", False))),
    "get_transaction_auth_signals": Spec(tools.get_transaction_auth_signals, (
        _s("transactionId", "transaction_id"), _s("caseId", "case_id", False))),
    "find_related_transactions": Spec(tools.find_related_transactions, (
        _s("transactionId", "transaction_id"), _n("limit", "limit", False), _s("caseId", "case_id", False))),
    "get_customer_dispute_history": Spec(tools.get_customer_dispute_history, (
        _s("customerId", "customer_id"), _n("limit", "limit", False))),
    "resolve_merchant": Spec(tools.resolve_merchant, (_s("descriptor", "descriptor"), _s("caseId", "case_id", False))),
    "get_merchant_profile": Spec(tools.get_merchant_profile, (_s("merchantId", "merchant_id"),)),
    "get_merchant_risk_signals": Spec(tools.get_merchant_risk_signals, (_s("merchantId", "merchant_id"),)),
    "get_case": Spec(tools.get_case, (_s("caseId", "case_id"),)),
    "create_case": Spec(tools.create_case, (
        _s("customerId", "customer_id"), _s("claimType", "claim_type"), _a("transactionIds", "transaction_ids"),
        _s("merchantId", "merchant_id", False), _s("currency", "currency", False), _IDEM)),
    "update_case": Spec(tools.update_case, (
        _s("caseId", "case_id"), _s("status", "status", False), _s("claimType", "claim_type", False),
        _s("merchantId", "merchant_id", False), _n("confidence", "confidence", False),
        _b("requiresHumanReview", "requires_human_review"), _s("outcome", "outcome", False), _IDEM), _adapt_update_case),
    "save_evidence": Spec(tools.save_evidence, (
        _s("caseId", "case_id"), _s("category", "category"), _s("type", "evidence_type"), _s("claim", "claim"),
        _s("source", "source"), _s("reliability", "reliability"), _a("transactionIds", "transaction_ids"),
        _s("evidenceId", "evidence_id", False), _IDEM)),
    "propose_payment_block": Spec(tools.propose_payment_block, (
        _s("caseId", "case_id"), _b("customerRequested", "customer_requested", True), _IDEM)),
    "propose_card_replacement": Spec(tools.propose_card_replacement, (_s("caseId", "case_id"), _IDEM)),
    "propose_dispute_creation": Spec(tools.propose_dispute_creation, (
        _s("caseId", "case_id"), _a("transactionIds", "transaction_ids", True),
        _b("hasConfirmedTransactions", "has_confirmed_transactions", True), _IDEM), _adapt_dispute),
    "propose_provisional_credit": Spec(tools.propose_provisional_credit, (
        _s("caseId", "case_id"), _n("amount", "amount", True, 0), _n("confidence", "confidence", True, 0, 1),
        _s("claimType", "claim_type"), _IDEM), _adapt_credit),
    "submit_review_decision": Spec(tools.submit_review_decision, (
        _s("caseId", "case_id"), _s("reviewId", "review_id"), _s("actor", "actor"),
        _s("decision", "decision"), _s("reason", "reason"), _IDEM)),
}

def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "error": {"code": code, "message": message, **extra}}


def _type_problem(p: Param, value: Any) -> str | None:
    if p.kind == "str":
        ok = isinstance(value, str) and (bool(value) or not p.required)
    elif p.kind == "num":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    elif p.kind == "bool":
        ok = isinstance(value, bool)
    else:
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
    if not ok:
        return f"{p.camel} must be a {'nonempty ' if p.required and p.kind == 'str' else ''}{p.kind}"
    if p.kind == "str":
        limit = MAX_IDEMPOTENCY_KEY_LENGTH if p.camel == "idempotencyKey" else MAX_ARGUMENT_STRING_LENGTH
        if len(value) > limit:
            return f"{p.camel} exceeds maximum length {limit}"
    if p.kind == "list" and (len(value) > MAX_LIST_ITEMS or any(len(item) > MAX_LIST_ITEM_LENGTH for item in value)):
        return f"{p.camel} exceeds list limits"
    if p.kind == "num" and ((p.minimum is not None and value < p.minimum) or (p.maximum is not None and value > p.maximum)):
        return f"{p.camel} must be between {p.minimum} and {p.maximum}"
    return None


def _validate(spec: Spec, arguments: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Return (snake_case kwargs, None) or (None, problem)."""
    if not isinstance(arguments, dict):
        return None, "arguments must be an object"
    by_name = {p.camel: p for p in spec.params}
    unknown = sorted(set(arguments) - set(by_name))
    if unknown:
        return None, f"unknown arguments: {unknown}"
    missing = sorted(p.camel for p in spec.params if p.required and p.camel not in arguments)
    if missing:
        return None, f"missing required arguments: {missing}"
    kwargs: dict[str, Any] = {}
    for name, value in arguments.items():
        if value is None and not by_name[name].required:
            continue  # an omitted optional may arrive as null
        problem = _type_problem(by_name[name], value)
        if problem:
            return None, problem
        kwargs[by_name[name].snake] = value
    return kwargs, None


def _fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
    body = {k: v for k, v in arguments.items() if k != "idempotencyKey" and v is not None}
    return hashlib.sha256(json.dumps([tool_name, body], sort_keys=True).encode()).hexdigest()


def dispatch(store: BankToolsStorage, tool_name: str, arguments: Any, extra: dict[str, Spec] | None = None) -> dict[str, Any]:
    """`extra` lets the integration layer add or override Specs for tools owned elsewhere (same validation + idempotency)."""
    specs = {**SPECS, **(extra or {})}
    if not isinstance(tool_name, str) or not tool_name or len(tool_name) > 64 or not tool_name.replace("_", "").isalnum():
        return _error("UNKNOWN_TOOL", "invalid tool name")
    if tool_name in NOT_OWNED_TOOLS and tool_name not in specs:
        owner = NOT_OWNED_TOOLS[tool_name]
        return _error("NOT_OWNED", f"{tool_name} is not implemented by bank-tools; owned by {owner}", owner=owner)
    spec = specs.get(tool_name)
    if spec is None:
        return _error("UNKNOWN_TOOL", f"unknown tool: {tool_name}")
    kwargs, problem = _validate(spec, arguments)
    if problem:
        return _error("VALIDATION_ERROR", problem)
    assert kwargs is not None

    if not spec.mutating:
        return _run(store, spec, kwargs)

    key = kwargs["idempotency_key"]
    claim = store.claim_idempotency(tool_name, key, _fingerprint(tool_name, arguments))
    if claim.state == MISMATCH:
        return _error("IDEMPOTENCY_KEY_REUSED", "idempotencyKey was already used with different arguments")
    if claim.state == IN_PROGRESS:
        return _error("IDEMPOTENCY_IN_PROGRESS", "a request with this idempotencyKey is still running; retry later")
    if claim.state != CLAIMED:  # REPLAY
        return claim.result  # type: ignore[return-value]

    # ponytail: an exception (e.g. store outage) leaves the claim until its lease expires, then a retry re-runs.
    # Every store write below is conditional on this claim's owner token; a stale worker gets LeaseLostError.
    token = CURRENT_LEASE.set((tool_name, key, claim.owner))
    try:
        result = _run(store, spec, kwargs)
        if result["status"] == "ok":
            store.remember_result(tool_name, key, result)
        else:
            store.release_idempotency(tool_name, key)  # nothing committed; let the caller fix and retry
        return result
    except LeaseLostError:  # lease expired mid-run and a successor owns the key; do not clobber it
        return _error("IDEMPOTENCY_IN_PROGRESS", "idempotency lease was taken over by another worker; retry to get its result")
    finally:
        CURRENT_LEASE.reset(token)


def _run(store: BankToolsStorage, spec: Spec, kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        return spec.fn(store, **(spec.adapt(store, kwargs) if spec.adapt else kwargs))
    except ToolError as exc:
        return _error(exc.code, str(exc))
    except ValueError as exc:
        return _error("VALIDATION_ERROR", str(exc))
