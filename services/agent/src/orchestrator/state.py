"""Structured case state. This, not chat history, is the source of truth (persisted via MemoryClient)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

# Mirrors CASE_STATUS_TRANSITIONS in packages/contracts/src/index.ts (TS-only, so copied here).
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "NEW": ("INTAKE",),
    "INTAKE": ("TRANSACTION_MATCHING", "AWAITING_CUSTOMER_INFORMATION", "NEEDS_HUMAN_REVIEW"),
    "TRANSACTION_MATCHING": ("AWAITING_TRANSACTION_CONFIRMATION", "AWAITING_CUSTOMER_INFORMATION", "NEEDS_HUMAN_REVIEW"),
    "AWAITING_TRANSACTION_CONFIRMATION": ("CLASSIFYING_DISPUTE", "TRANSACTION_MATCHING", "NEEDS_HUMAN_REVIEW"),
    "CLASSIFYING_DISPUTE": ("INVESTIGATING", "AWAITING_CUSTOMER_INFORMATION", "NEEDS_HUMAN_REVIEW", "RESOLVED"),
    "INVESTIGATING": ("AWAITING_CUSTOMER_INFORMATION", "AWAITING_MERCHANT_EVIDENCE", "RESOLUTION_PROPOSED", "NEEDS_HUMAN_REVIEW"),
    "AWAITING_CUSTOMER_INFORMATION": ("INTAKE", "TRANSACTION_MATCHING", "CLASSIFYING_DISPUTE", "INVESTIGATING", "NEEDS_HUMAN_REVIEW"),
    "AWAITING_MERCHANT_EVIDENCE": ("INVESTIGATING", "NEEDS_HUMAN_REVIEW"),
    "RESOLUTION_PROPOSED": ("POLICY_REVIEW",),
    "POLICY_REVIEW": ("ACTION_APPROVED", "NEEDS_HUMAN_REVIEW"),
    "NEEDS_HUMAN_REVIEW": ("INVESTIGATING", "RESOLUTION_PROPOSED", "ACTION_APPROVED"),
    "ACTION_APPROVED": ("RESOLVED",),
    "RESOLVED": ("CLOSED",),
    "CLOSED": (),
}


@dataclass
class CaseState:
    conversation_id: str
    customer_id: str
    status: str = "NEW"
    pending: str | None = None          # what the customer's next answer is for: "matching" | "claim"
    case_id: str | None = None
    merchant_id: str | None = None
    merchant_name: str | None = None
    hints: dict[str, Any] = field(default_factory=dict)      # {"descriptor": str, "amount": float}
    candidates: list[dict[str, Any]] = field(default_factory=list)
    confirmed_ids: list[str] = field(default_factory=list)
    claim: dict[str, Any] = field(default_factory=dict)      # recognizes_merchant/canceled/denies_authorization/requested_block
    evidence: list[dict[str, Any]] = field(default_factory=list)
    block_request_message: str | None = None  # raw customer text that first set claim.requested_block
    block_request_recorded: bool = False      # whether it has been persisted as bank-tools evidence yet
    classification: str | None = None
    confidence: float | None = None
    proposal: dict[str, Any] | None = None
    policy_results: list[dict[str, Any]] = field(default_factory=list)
    escalation: dict[str, Any] | None = None                 # {"reason","summary","evidenceRefs","delivered"}
    outcome: str | None = None
    followups: list[str] = field(default_factory=list)      # proactive notes appended to the final reply
    model_turns: int = 0
    match_attempts: int = 0
    research_calls: int = 0
    metrics: dict[str, int] = field(default_factory=dict)
    audit: list[dict[str, str]] = field(default_factory=list)  # tool + result only, never model reasoning
    history: list[str] = field(default_factory=lambda: ["NEW"])  # statuses visited, replayed to the server on case creation

    def move(self, to: str) -> None:
        if to not in TRANSITIONS.get(self.status, ()):
            raise ValueError(f"Invalid case transition: {self.status} -> {to}")
        self.status = to
        self.history.append(to)

    def bump(self, metric: str) -> None:
        self.metrics[metric] = self.metrics.get(metric, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaseState":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
