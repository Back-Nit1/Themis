"""In-memory system of record for bank tools.

No external services are invoked (prompt.md #28 assigns DynamoDB/S3 to
infra; this in-memory store stands in for that until infra provisions it).
Build one with `BankToolsStore()` and seed it with the `add_*` methods, or
use the `store` pytest fixture in tests/conftest.py.

Indices are sized for the modest synthetic dataset (~50 customers, 1-2k
transactions, ~40 merchants): per-customer transaction lists are kept sorted
by occurredAt so date-range lookups are O(log n + k) via bisect instead of a
full scan, and merchant alias resolution is an O(1) dict lookup built once at
seed time.
"""
from __future__ import annotations

import bisect
import contextvars
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .errors import ConflictError, LeaseLostError, NotFoundError
from .models import (
    ActionType, AuditRecord, AuditResult, Case, Evidence, HumanReviewRequest, Merchant,
    MerchantProfile, PolicyDecision, PolicyOutcome, ReviewDecision, Transaction, now_iso,
)

_ALIAS_NORMALIZE_RE = re.compile(r"[^A-Z0-9]")


def normalize_descriptor(descriptor: str) -> str:
    """Uppercase and strip punctuation so 'Asteria.io' / 'ASTERIA*PREMIUM' compare evenly."""
    return _ALIAS_NORMALIZE_RE.sub("", descriptor.upper())


def _parse_ts(value: str) -> datetime:
    """Parse an ISO timestamp, treating an offset-less value as UTC (occurredAt always carries one)."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# Idempotency claim states. CLAIMED: caller owns the key and must run the tool. REPLAY: a prior
# completed result is returned. IN_PROGRESS: another live caller owns it. MISMATCH: key reused
# with different arguments.
CLAIMED, REPLAY, IN_PROGRESS, MISMATCH = "CLAIMED", "REPLAY", "IN_PROGRESS", "MISMATCH"
CLAIM_LEASE_SECONDS = 60
# (tool, key, owner token) of the lease the current worker holds. dispatch sets it after a CLAIMED claim so tool
# signatures stay frozen; stores fence only that slot (a tool may nest another tool under its own key).
CURRENT_LEASE: contextvars.ContextVar[tuple[str, str, str] | None] = contextvars.ContextVar("CURRENT_LEASE", default=None)


def lease_owner(tool: str, key: str) -> str | None:
    """Owner token this worker holds for (tool, key); None = tokenless (direct tool use, nested keys, legacy rows)."""
    held = CURRENT_LEASE.get()
    return held[2] if held and held[:2] == (tool, key) else None


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    state: str
    result: dict[str, Any] | None = None
    owner: str | None = None  # lease owner token, set only when state == CLAIMED


class BankToolsStorage(Protocol):
    """Everything the tool functions and dispatcher need from a system of record.

    Implemented by the in-memory `BankToolsStore` (tests/local) and `DynamoBankToolsStore`.
    Lookups raise NotFoundError; `create_case` raises ConflictError on a duplicate caseId.
    """

    def claim_idempotency(
        self, tool: str, key: str, fingerprint: str, lease_seconds: int = CLAIM_LEASE_SECONDS,
    ) -> IdempotencyClaim: ...
    def release_idempotency(self, tool: str, key: str) -> None: ...
    def idempotent_result(self, tool: str, key: str | None) -> dict[str, Any] | None: ...
    def remember_result(self, tool: str, key: str | None, result: dict[str, Any]) -> None: ...
    def get_transaction(self, transaction_id: str) -> Transaction: ...
    def transactions_for_customer(
        self, customer_id: str, *, since: str | None = None, until: str | None = None,
    ) -> list[Transaction]: ...
    def get_customer(self, customer_id: str) -> dict[str, Any]: ...
    def resolve_merchant_id(self, descriptor: str) -> str | None: ...
    def get_merchant(self, merchant_id: str) -> Merchant: ...
    def get_merchant_profile(self, merchant_id: str) -> MerchantProfile: ...
    def get_case(self, case_id: str) -> Case: ...
    def cases_for_customer(self, customer_id: str) -> list[Case]: ...
    def create_case(self, case: Case) -> Case: ...
    def replace_case(self, case: Case) -> None: ...
    def get_evidence(self, evidence_id: str) -> Evidence: ...
    def add_evidence(self, evidence: Evidence) -> None: ...
    def add_policy_decision(self, decision: PolicyDecision) -> None: ...
    def add_human_review_request(self, request: HumanReviewRequest) -> None: ...
    def replace_human_review_request(self, request: HumanReviewRequest) -> None: ...
    def get_human_review_request(self, case_id: str, review_id: str) -> HumanReviewRequest: ...
    def human_review_requests_for_case(self, case_id: str) -> list[HumanReviewRequest]: ...
    def add_review_decision(self, decision: ReviewDecision) -> None: ...
    def review_decisions_for_case(self, case_id: str) -> list[ReviewDecision]: ...
    def record_audit(
        self, *, case_id: str, action: str, tool: str, result: AuditResult | str = AuditResult.SUCCESS,
        actor: str = "THEMIS_AGENT", policy_name: str | None = None, policy_outcome: PolicyOutcome | None = None,
        proposed_action: ActionType | None = None, input_amount: float | None = None,
        human_approval_required: bool | None = None,
    ) -> AuditRecord: ...
    def audit_for_case(self, case_id: str) -> list[AuditRecord]: ...


def build_audit_record(
    *, case_id: str, action: str, tool: str, result: AuditResult | str = AuditResult.SUCCESS,
    actor: str = "THEMIS_AGENT", policy_name: str | None = None, policy_outcome: PolicyOutcome | None = None,
    proposed_action: ActionType | None = None, input_amount: float | None = None,
    human_approval_required: bool | None = None,
) -> AuditRecord:
    """caseId is required by packages/contracts AuditEventSchema; only call this with a real case."""
    return AuditRecord(
        eventId=new_id("audit"), caseId=case_id, timestamp=now_iso(), action=action, tool=tool,
        result=AuditResult(result), actor=actor, policyName=policy_name, policyOutcome=policy_outcome,
        proposedAction=proposed_action, inputAmount=input_amount,
        humanApprovalRequired=human_approval_required,
    )


class BankToolsStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claims: dict[tuple[str, str], tuple[float, str, str]] = {}  # (tool, key) -> (lease expiry, fingerprint, owner)
        self._transactions: dict[str, Transaction] = {}
        self._by_customer: dict[str, list[Transaction]] = {}
        self._customers: dict[str, dict[str, Any]] = {}
        self._merchants: dict[str, Merchant] = {}
        self._merchant_profiles: dict[str, MerchantProfile] = {}
        self._alias_index: dict[str, str] = {}
        self._cases: dict[str, Case] = {}
        self._cases_by_customer: dict[str, list[str]] = {}
        self._evidence: dict[str, Evidence] = {}
        self._policy_decisions: dict[str, list[PolicyDecision]] = {}
        self._human_review_requests: dict[str, list[HumanReviewRequest]] = {}
        self._review_decisions: dict[str, list[ReviewDecision]] = {}
        self._audit_by_case: dict[str, list[AuditRecord]] = {}
        self._idempotency: dict[tuple[str, str], dict[str, Any]] = {}

    # -- seeding --------------------------------------------------------
    def add_transaction(self, txn: Transaction) -> None:
        self._transactions[txn.transactionId] = txn
        bucket = self._by_customer.setdefault(txn.customerId, [])
        bisect.insort(bucket, txn, key=lambda t: t.occurredAt)

    def add_customer(self, customer: dict[str, Any]) -> None:
        self._customers[customer["customerId"]] = customer

    def add_merchant(self, merchant: Merchant) -> None:
        self._merchants[merchant.merchantId] = merchant
        self._index_alias(merchant.canonicalName, merchant.merchantId)
        for alias in merchant.aliases:
            self._index_alias(alias, merchant.merchantId)

    def add_merchant_profile(self, profile: MerchantProfile) -> None:
        self._merchant_profiles[profile.merchantId] = profile

    def add_case(self, case: Case) -> None:
        self._cases[case.caseId] = case
        self._cases_by_customer.setdefault(case.customerId, []).append(case.caseId)

    def add_evidence(self, evidence: Evidence) -> None:
        self._evidence[evidence.evidenceId] = evidence

    def _index_alias(self, descriptor: str, merchant_id: str) -> None:
        self._alias_index[normalize_descriptor(descriptor)] = merchant_id

    # -- idempotency ------------------------------------------------------
    def idempotent_result(self, tool: str, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        return self._idempotency.get((tool, key))

    def _owns(self, slot: tuple[str, str]) -> bool:
        token = lease_owner(*slot)
        return token is None or (slot in self._claims and self._claims[slot][2] == token)

    def remember_result(self, tool: str, key: str | None, result: dict[str, Any]) -> None:
        if key:
            with self._lock:
                if not self._owns((tool, key)):
                    raise LeaseLostError(f"{tool}#{key}")
                self._idempotency[(tool, key)] = result

    def claim_idempotency(
        self, tool: str, key: str, fingerprint: str, lease_seconds: int = CLAIM_LEASE_SECONDS,
    ) -> IdempotencyClaim:
        with self._lock:
            slot = (tool, key)
            expiry, prior_fp, _ = self._claims.get(slot, (0.0, fingerprint, ""))
            if prior_fp != fingerprint:
                return IdempotencyClaim(MISMATCH)
            if slot in self._idempotency:
                return IdempotencyClaim(REPLAY, self._idempotency[slot])
            if expiry > time.time():
                return IdempotencyClaim(IN_PROGRESS)
            owner = uuid.uuid4().hex
            self._claims[slot] = (time.time() + lease_seconds, fingerprint, owner)
            return IdempotencyClaim(CLAIMED, owner=owner)

    def release_idempotency(self, tool: str, key: str) -> None:
        with self._lock:
            if self._owns((tool, key)):  # stale owner: no-op, successor keeps its lease
                self._claims.pop((tool, key), None)

    # -- transactions -----------------------------------------------------
    def get_transaction(self, transaction_id: str) -> Transaction:
        txn = self._transactions.get(transaction_id)
        if txn is None:
            raise NotFoundError(f"transaction not found: {transaction_id}")
        return txn

    def transactions_for_customer(
        self, customer_id: str, *, since: str | None = None, until: str | None = None,
    ) -> list[Transaction]:
        bucket = self._by_customer.get(customer_id, [])
        if since is None and until is None:
            return list(bucket)
        lo = bisect.bisect_left(bucket, _parse_ts(since), key=lambda t: _parse_ts(t.occurredAt)) if since else 0
        hi = (
            bisect.bisect_right(bucket, _parse_ts(until), key=lambda t: _parse_ts(t.occurredAt))
            if until else len(bucket)
        )
        return bucket[lo:hi]

    # -- customers --------------------------------------------------------
    def get_customer(self, customer_id: str) -> dict[str, Any]:
        customer = self._customers.get(customer_id)
        if customer is None:
            raise NotFoundError(f"customer not found: {customer_id}")
        return customer

    # -- merchants ----------------------------------------------------------
    def resolve_merchant_id(self, descriptor: str) -> str | None:
        return self._alias_index.get(normalize_descriptor(descriptor))

    def get_merchant(self, merchant_id: str) -> Merchant:
        merchant = self._merchants.get(merchant_id)
        if merchant is None:
            raise NotFoundError(f"merchant not found: {merchant_id}")
        return merchant

    def get_merchant_profile(self, merchant_id: str) -> MerchantProfile:
        profile = self._merchant_profiles.get(merchant_id)
        if profile is None:
            raise NotFoundError(f"merchant profile not found: {merchant_id}")
        return profile

    # -- cases --------------------------------------------------------------
    def get_case(self, case_id: str) -> Case:
        case = self._cases.get(case_id)
        if case is None:
            raise NotFoundError(f"case not found: {case_id}")
        return case

    def cases_for_customer(self, customer_id: str) -> list[Case]:
        return [self._cases[cid] for cid in self._cases_by_customer.get(customer_id, [])]

    def replace_case(self, case: Case) -> None:
        if case.caseId not in self._cases:
            raise NotFoundError(f"case not found: {case.caseId}")
        self._cases[case.caseId] = case

    def create_case(self, case: Case) -> Case:
        with self._lock:
            if case.caseId in self._cases:
                raise ConflictError(f"case already exists: {case.caseId}")
            self.add_case(case)
            return case

    # -- evidence -------------------------------------------------------------
    def get_evidence(self, evidence_id: str) -> Evidence:
        evidence = self._evidence.get(evidence_id)
        if evidence is None:
            raise NotFoundError(f"evidence not found: {evidence_id}")
        return evidence

    def evidence_for_case(self, case_id: str) -> list[Evidence]:
        case = self.get_case(case_id)
        return [self._evidence[eid] for eid in case.evidenceIds if eid in self._evidence]

    # -- policy decisions / human review --------------------------------------
    def add_policy_decision(self, decision: PolicyDecision) -> None:
        self._policy_decisions.setdefault(decision.caseId, []).append(decision)

    def policy_decisions_for_case(self, case_id: str) -> list[PolicyDecision]:
        return list(self._policy_decisions.get(case_id, []))

    def add_human_review_request(self, request: HumanReviewRequest) -> None:
        self._human_review_requests.setdefault(request.caseId, []).append(request)

    def replace_human_review_request(self, request: HumanReviewRequest) -> None:
        bucket = self._human_review_requests.get(request.caseId, [])
        for i, existing in enumerate(bucket):
            if existing.reviewId == request.reviewId:
                bucket[i] = request
                return
        raise NotFoundError(f"human review request not found: {request.reviewId}")

    def get_human_review_request(self, case_id: str, review_id: str) -> HumanReviewRequest:
        for request in self._human_review_requests.get(case_id, []):
            if request.reviewId == review_id:
                return request
        raise NotFoundError(f"human review request not found: {review_id}")

    def human_review_requests_for_case(self, case_id: str) -> list[HumanReviewRequest]:
        return list(self._human_review_requests.get(case_id, []))

    def add_review_decision(self, decision: ReviewDecision) -> None:
        self._review_decisions.setdefault(decision.caseId, []).append(decision)

    def review_decisions_for_case(self, case_id: str) -> list[ReviewDecision]:
        return list(self._review_decisions.get(case_id, []))

    # -- audit ------------------------------------------------------------------
    def record_audit(
        self, *, case_id: str, action: str, tool: str, result: AuditResult | str = AuditResult.SUCCESS,
        actor: str = "THEMIS_AGENT", policy_name: str | None = None, policy_outcome: PolicyOutcome | None = None,
        proposed_action: ActionType | None = None, input_amount: float | None = None,
        human_approval_required: bool | None = None,
    ) -> AuditRecord:
        record = build_audit_record(
            case_id=case_id, action=action, tool=tool, result=result, actor=actor, policy_name=policy_name,
            policy_outcome=policy_outcome, proposed_action=proposed_action, input_amount=input_amount,
            human_approval_required=human_approval_required,
        )
        self._audit_by_case.setdefault(case_id, []).append(record)
        return record

    def audit_for_case(self, case_id: str) -> list[AuditRecord]:
        return list(self._audit_by_case.get(case_id, []))
