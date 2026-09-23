"""Domain dataclasses aligned with packages/contracts/src/index.ts for Python.

Stdlib only (no pydantic/jsonschema yet -- see .handoffs/dependencies/ for the
pending request). StrEnum gives free membership validation: constructing an
enum with an invalid value raises ValueError.

CASE_STATUS_TRANSITIONS and AuditRecord must stay in sync with
packages/contracts/src/index.ts (CASE_STATUS_TRANSITIONS / AuditEventSchema).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class ValidationError(ValueError):
    """Raised when a domain object or tool input fails validation."""


class CaseStatus(StrEnum):
    NEW = "NEW"
    INTAKE = "INTAKE"
    TRANSACTION_MATCHING = "TRANSACTION_MATCHING"
    AWAITING_TRANSACTION_CONFIRMATION = "AWAITING_TRANSACTION_CONFIRMATION"
    CLASSIFYING_DISPUTE = "CLASSIFYING_DISPUTE"
    INVESTIGATING = "INVESTIGATING"
    AWAITING_CUSTOMER_INFORMATION = "AWAITING_CUSTOMER_INFORMATION"
    AWAITING_MERCHANT_EVIDENCE = "AWAITING_MERCHANT_EVIDENCE"
    RESOLUTION_PROPOSED = "RESOLUTION_PROPOSED"
    POLICY_REVIEW = "POLICY_REVIEW"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    ACTION_APPROVED = "ACTION_APPROVED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


# Ported from packages/contracts/src/index.ts CASE_STATUS_TRANSITIONS.
# Frozenset values give O(1) edge membership checks.
CASE_STATUS_TRANSITIONS: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.NEW: frozenset({CaseStatus.INTAKE}),
    CaseStatus.INTAKE: frozenset({
        CaseStatus.TRANSACTION_MATCHING, CaseStatus.AWAITING_CUSTOMER_INFORMATION,
        CaseStatus.NEEDS_HUMAN_REVIEW,
    }),
    CaseStatus.TRANSACTION_MATCHING: frozenset({
        CaseStatus.AWAITING_TRANSACTION_CONFIRMATION, CaseStatus.AWAITING_CUSTOMER_INFORMATION,
        CaseStatus.NEEDS_HUMAN_REVIEW,
    }),
    CaseStatus.AWAITING_TRANSACTION_CONFIRMATION: frozenset({
        CaseStatus.CLASSIFYING_DISPUTE, CaseStatus.TRANSACTION_MATCHING, CaseStatus.NEEDS_HUMAN_REVIEW,
    }),
    CaseStatus.CLASSIFYING_DISPUTE: frozenset({
        CaseStatus.INVESTIGATING, CaseStatus.AWAITING_CUSTOMER_INFORMATION,
        CaseStatus.NEEDS_HUMAN_REVIEW, CaseStatus.RESOLVED,
    }),
    CaseStatus.INVESTIGATING: frozenset({
        CaseStatus.AWAITING_CUSTOMER_INFORMATION, CaseStatus.AWAITING_MERCHANT_EVIDENCE,
        CaseStatus.RESOLUTION_PROPOSED, CaseStatus.NEEDS_HUMAN_REVIEW,
    }),
    CaseStatus.AWAITING_CUSTOMER_INFORMATION: frozenset({
        CaseStatus.INTAKE, CaseStatus.TRANSACTION_MATCHING, CaseStatus.CLASSIFYING_DISPUTE,
        CaseStatus.INVESTIGATING, CaseStatus.NEEDS_HUMAN_REVIEW,
    }),
    CaseStatus.AWAITING_MERCHANT_EVIDENCE: frozenset({
        CaseStatus.INVESTIGATING, CaseStatus.NEEDS_HUMAN_REVIEW,
    }),
    CaseStatus.RESOLUTION_PROPOSED: frozenset({CaseStatus.POLICY_REVIEW}),
    CaseStatus.POLICY_REVIEW: frozenset({CaseStatus.ACTION_APPROVED, CaseStatus.NEEDS_HUMAN_REVIEW}),
    CaseStatus.NEEDS_HUMAN_REVIEW: frozenset({
        CaseStatus.INVESTIGATING, CaseStatus.RESOLUTION_PROPOSED, CaseStatus.ACTION_APPROVED,
    }),
    CaseStatus.ACTION_APPROVED: frozenset({CaseStatus.RESOLVED}),
    CaseStatus.RESOLVED: frozenset({CaseStatus.CLOSED}),
    CaseStatus.CLOSED: frozenset(),
}


def can_transition_case_status(current: CaseStatus, nxt: CaseStatus) -> bool:
    return nxt in CASE_STATUS_TRANSITIONS[current]


class CaseOutcome(StrEnum):
    CUSTOMER_RECOGNIZED_MERCHANT = "CUSTOMER_RECOGNIZED_MERCHANT"


class ClaimType(StrEnum):
    UNAUTHORIZED_TRANSACTION = "UNAUTHORIZED_TRANSACTION"
    UNRECOGNIZED_MERCHANT = "UNRECOGNIZED_MERCHANT"
    RECURRING_PAYMENT_NOT_AUTHORIZED = "RECURRING_PAYMENT_NOT_AUTHORIZED"
    RECURRING_PAYMENT_AFTER_CANCELLATION = "RECURRING_PAYMENT_AFTER_CANCELLATION"
    DUPLICATE_TRANSACTION = "DUPLICATE_TRANSACTION"
    WRONG_AMOUNT = "WRONG_AMOUNT"
    SERVICE_NOT_RECEIVED = "SERVICE_NOT_RECEIVED"
    REFUND_NOT_RECEIVED = "REFUND_NOT_RECEIVED"
    OTHER_MERCHANT_DISPUTE = "OTHER_MERCHANT_DISPUTE"
    INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"


class ActionType(StrEnum):
    CREATE_DISPUTE = "CREATE_DISPUTE"
    REVIEW_FUTURE_RECURRING_PAYMENT = "REVIEW_FUTURE_RECURRING_PAYMENT"
    REQUEST_MERCHANT_EVIDENCE = "REQUEST_MERCHANT_EVIDENCE"
    PROVISIONAL_CREDIT = "PROVISIONAL_CREDIT"
    BLOCK_RECURRING_MERCHANT = "BLOCK_RECURRING_MERCHANT"
    REPLACE_CARD = "REPLACE_CARD"
    DENY_CASE = "DENY_CASE"


class PolicyOutcome(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_HUMAN_REVIEW = "REQUIRE_HUMAN_REVIEW"


class AuditResult(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PENDING = "PENDING"


class EvidenceCategory(StrEnum):
    CUSTOMER_CLAIMS = "CUSTOMER_CLAIMS"
    TRANSACTION_EVIDENCE = "TRANSACTION_EVIDENCE"
    AUTHENTICATION_EVIDENCE = "AUTHENTICATION_EVIDENCE"
    MERCHANT_IDENTITY = "MERCHANT_IDENTITY"
    MERCHANT_HISTORY = "MERCHANT_HISTORY"
    BANK_HISTORY = "BANK_HISTORY"
    EXTERNAL_MERCHANT_INTELLIGENCE = "EXTERNAL_MERCHANT_INTELLIGENCE"
    CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"


class Reliability(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RiskSeverity(StrEnum):
    LOW = "LOW"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"


_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_AUTH_SIGNAL_KEYS = frozenset({
    "card_present", "cvv_match", "avs_match", "3ds_status", "wallet_token",
    "device_id", "ip_region", "merchant_id", "recurring_indicator",
    "prior_merchant_relationship",
})
_3DS_STATUSES = frozenset({"AUTHENTICATED", "FAILED", "NOT_PERFORMED", "UNKNOWN"})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field_name} must be a nonempty string")
    return value


def require_money(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field_name} must be a number")
    amount = float(value)
    if amount < 0 or amount != amount or amount in (float("inf"), float("-inf")):
        raise ValidationError(f"{field_name} must be a nonnegative finite number")
    return amount


def require_currency(value: Any) -> str:
    if not isinstance(value, str) or not _CURRENCY_RE.match(value):
        raise ValidationError("currency must be a 3-letter uppercase code")
    return value


def validate_auth_signals(signals: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(signals, dict):
        raise ValidationError("authSignals must be an object")
    unknown = set(signals) - _AUTH_SIGNAL_KEYS
    if unknown:
        raise ValidationError(f"unknown authSignals keys: {sorted(unknown)}")
    if "3ds_status" in signals and signals["3ds_status"] not in _3DS_STATUSES:
        raise ValidationError(f"3ds_status must be one of {sorted(_3DS_STATUSES)}")
    return dict(signals)


@dataclass(slots=True)
class Transaction:
    transactionId: str
    customerId: str
    merchantId: str
    descriptor: str
    amount: float
    currency: str
    occurredAt: str
    authSignals: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Transaction":
        return cls(
            transactionId=require_id(d.get("transactionId"), "transactionId"),
            customerId=require_id(d.get("customerId"), "customerId"),
            merchantId=require_id(d.get("merchantId"), "merchantId"),
            descriptor=require_id(d.get("descriptor"), "descriptor"),
            amount=require_money(d.get("amount"), "amount"),
            currency=require_currency(d.get("currency")),
            occurredAt=require_id(d.get("occurredAt"), "occurredAt"),
            authSignals=validate_auth_signals(d.get("authSignals") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "transactionId": self.transactionId, "customerId": self.customerId,
            "merchantId": self.merchantId, "descriptor": self.descriptor,
            "amount": self.amount, "currency": self.currency,
            "occurredAt": self.occurredAt, "authSignals": self.authSignals,
        }


@dataclass(slots=True)
class Merchant:
    merchantId: str
    canonicalName: str
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"merchantId": self.merchantId, "canonicalName": self.canonicalName, "aliases": list(self.aliases)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Merchant":
        return cls(merchantId=d["merchantId"], canonicalName=d["canonicalName"], aliases=list(d["aliases"]))


@dataclass(slots=True)
class MerchantRiskSignal:
    type: str
    severity: RiskSeverity
    observedAt: str
    expiresAt: str
    evidenceRefs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type, "severity": str(self.severity), "observedAt": self.observedAt,
            "expiresAt": self.expiresAt, "evidenceRefs": list(self.evidenceRefs),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MerchantRiskSignal":
        return cls(
            type=d["type"], severity=RiskSeverity(d["severity"]), observedAt=d["observedAt"],
            expiresAt=d["expiresAt"], evidenceRefs=list(d["evidenceRefs"]),
        )


@dataclass(slots=True)
class MerchantProfile:
    merchantId: str
    canonicalName: str
    aliases: list[str]
    billingPatterns: list[dict[str, Any]]
    caseStatistics: dict[str, int]
    riskSignals: list[MerchantRiskSignal]
    updatedAt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "merchantId": self.merchantId, "canonicalName": self.canonicalName,
            "aliases": list(self.aliases), "billingPatterns": list(self.billingPatterns),
            "caseStatistics": dict(self.caseStatistics),
            "riskSignals": [s.to_dict() for s in self.riskSignals],
            "updatedAt": self.updatedAt,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MerchantProfile":
        return cls(
            merchantId=d["merchantId"], canonicalName=d["canonicalName"], aliases=list(d["aliases"]),
            billingPatterns=list(d["billingPatterns"]), caseStatistics=dict(d["caseStatistics"]),
            riskSignals=[MerchantRiskSignal.from_dict(s) for s in d["riskSignals"]], updatedAt=d["updatedAt"],
        )


@dataclass(slots=True)
class Case:
    caseId: str
    customerId: str
    status: CaseStatus
    createdAt: str
    updatedAt: str
    claimType: ClaimType
    merchantId: str | None
    transactionIds: list[str] = field(default_factory=list)
    evidenceIds: list[str] = field(default_factory=list)
    totalDisputedAmount: float = 0.0
    currency: str = "USD"
    confidence: float | None = None
    recommendedActions: list[ActionType] = field(default_factory=list)
    requiresHumanReview: bool = False
    outcome: CaseOutcome | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "caseId": self.caseId, "customerId": self.customerId, "status": str(self.status),
            "createdAt": self.createdAt, "updatedAt": self.updatedAt, "claimType": str(self.claimType),
            "merchantId": self.merchantId, "transactionIds": list(self.transactionIds),
            "evidenceIds": list(self.evidenceIds), "totalDisputedAmount": self.totalDisputedAmount,
            "currency": self.currency, "confidence": self.confidence,
            "recommendedActions": [str(a) for a in self.recommendedActions],
            "requiresHumanReview": self.requiresHumanReview,
        }
        if self.outcome is not None:
            d["outcome"] = str(self.outcome)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Case":
        return cls(
            caseId=d["caseId"], customerId=d["customerId"], status=CaseStatus(d["status"]),
            createdAt=d["createdAt"], updatedAt=d["updatedAt"], claimType=ClaimType(d["claimType"]),
            merchantId=d.get("merchantId"), transactionIds=list(d["transactionIds"]),
            evidenceIds=list(d["evidenceIds"]), totalDisputedAmount=d["totalDisputedAmount"],
            currency=d["currency"], confidence=d.get("confidence"),
            recommendedActions=[ActionType(a) for a in d["recommendedActions"]],
            requiresHumanReview=d["requiresHumanReview"],
            outcome=CaseOutcome(d["outcome"]) if d.get("outcome") else None,
        )


# Legacy/deserialized evidence rows saved before recordedAt existed have no timestamp; treat them as
# maximally stale (fail closed) rather than defaulting to "now" (fail open).
UNKNOWN_RECORDED_AT = "1970-01-01T00:00:00+00:00"


@dataclass(slots=True)
class Evidence:
    evidenceId: str
    caseId: str
    category: EvidenceCategory
    type: str
    claim: str
    source: str
    reliability: Reliability
    transactionIds: list[str] = field(default_factory=list)
    recordedAt: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidenceId": self.evidenceId, "caseId": self.caseId, "category": str(self.category),
            "type": self.type, "claim": self.claim, "source": self.source,
            "reliability": str(self.reliability), "transactionIds": list(self.transactionIds),
            "recordedAt": self.recordedAt,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Evidence":
        return cls(
            evidenceId=d["evidenceId"], caseId=d["caseId"], category=EvidenceCategory(d["category"]),
            type=d["type"], claim=d["claim"], source=d["source"], reliability=Reliability(d["reliability"]),
            transactionIds=list(d["transactionIds"]), recordedAt=d.get("recordedAt") or UNKNOWN_RECORDED_AT,
        )


@dataclass(slots=True)
class PolicyDecision:
    decisionId: str
    caseId: str
    action: ActionType
    outcome: PolicyOutcome
    rationale: str
    decidedAt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "decisionId": self.decisionId, "caseId": self.caseId, "action": str(self.action),
            "outcome": str(self.outcome), "rationale": self.rationale, "decidedAt": self.decidedAt,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PolicyDecision":
        return cls(
            decisionId=d["decisionId"], caseId=d["caseId"], action=ActionType(d["action"]),
            outcome=PolicyOutcome(d["outcome"]), rationale=d["rationale"], decidedAt=d["decidedAt"],
        )


@dataclass(slots=True)
class HumanReviewRequest:
    caseId: str
    reason: str
    summary: str
    recommendedNextStep: str
    evidenceRefs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "caseId": self.caseId, "reason": self.reason, "summary": self.summary,
            "recommendedNextStep": self.recommendedNextStep, "evidenceRefs": list(self.evidenceRefs),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HumanReviewRequest":
        return cls(
            caseId=d["caseId"], reason=d["reason"], summary=d["summary"],
            recommendedNextStep=d["recommendedNextStep"], evidenceRefs=list(d["evidenceRefs"]),
        )


@dataclass(slots=True)
class AuditRecord:
    """Mirrors packages/contracts/src/index.ts AuditEventSchema exactly (strictObject: no extra fields)."""
    eventId: str
    caseId: str
    timestamp: str
    action: str
    tool: str
    result: AuditResult = AuditResult.SUCCESS
    actor: str = "THEMIS_AGENT"
    policyName: str | None = None
    policyOutcome: PolicyOutcome | None = None
    proposedAction: ActionType | None = None
    inputAmount: float | None = None
    humanApprovalRequired: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "eventId": self.eventId, "caseId": self.caseId, "timestamp": self.timestamp,
            "actor": self.actor, "action": self.action, "tool": self.tool, "result": str(self.result),
        }
        if self.policyName is not None:
            d["policyName"] = self.policyName
        if self.policyOutcome is not None:
            d["policyOutcome"] = str(self.policyOutcome)
        if self.proposedAction is not None:
            d["proposedAction"] = str(self.proposedAction)
        if self.inputAmount is not None:
            d["inputAmount"] = self.inputAmount
        if self.humanApprovalRequired is not None:
            d["humanApprovalRequired"] = self.humanApprovalRequired
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AuditRecord":
        return cls(
            eventId=d["eventId"], caseId=d["caseId"], timestamp=d["timestamp"], action=d["action"],
            tool=d["tool"], result=AuditResult(d["result"]), actor=d["actor"], policyName=d.get("policyName"),
            policyOutcome=PolicyOutcome(d["policyOutcome"]) if d.get("policyOutcome") else None,
            proposedAction=ActionType(d["proposedAction"]) if d.get("proposedAction") else None,
            inputAmount=d.get("inputAmount"), humanApprovalRequired=d.get("humanApprovalRequired"),
        )
