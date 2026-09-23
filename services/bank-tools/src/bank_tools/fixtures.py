"""Load the canonical committed demo fixtures into the local bank-tools store."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import (
    ActionType,
    Case,
    CaseOutcome,
    CaseStatus,
    ClaimType,
    Evidence,
    EvidenceCategory,
    Merchant,
    MerchantProfile,
    MerchantRiskSignal,
    Reliability,
    RiskSeverity,
    Transaction,
    UNKNOWN_RECORDED_AT,
)
from .store import BankToolsStorage, BankToolsStore


def _read(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, list):
        raise ValueError(f"expected a JSON array in {path}")
    return value


def load_demo_store(repo_root: Path | None = None, store: BankToolsStorage | None = None) -> BankToolsStorage:
    """Seed ``store`` (default: a new in-memory one) from fixtures validated by ``@themis/contracts``."""
    root = repo_root or Path(__file__).resolve().parents[4]
    fixtures = root / "fixtures"
    store = store if store is not None else BankToolsStore()

    for customer in _read(fixtures / "customers" / "demo.json"):
        store.add_customer(customer)
    for raw in _read(fixtures / "transactions" / "demo.json"):
        store.add_transaction(Transaction.from_dict(raw))
    for raw in _read(fixtures / "merchants" / "demo.json"):
        store.add_merchant(Merchant(
            merchantId=raw["merchantId"], canonicalName=raw["canonicalName"], aliases=list(raw["aliases"]),
        ))
    for raw in _read(fixtures / "merchants" / "demo-profiles.json"):
        store.add_merchant_profile(MerchantProfile(
            merchantId=raw["merchantId"], canonicalName=raw["canonicalName"], aliases=list(raw["aliases"]),
            billingPatterns=list(raw["billingPatterns"]), caseStatistics=dict(raw["caseStatistics"]),
            riskSignals=[MerchantRiskSignal(
                type=signal["type"], severity=RiskSeverity(signal["severity"]),
                observedAt=signal["observedAt"], expiresAt=signal["expiresAt"],
                evidenceRefs=list(signal["evidenceRefs"]),
            ) for signal in raw["riskSignals"]], updatedAt=raw["updatedAt"],
        ))
    for raw in _read(fixtures / "cases" / "demo.json"):
        store.add_case(Case(
            caseId=raw["caseId"], customerId=raw["customerId"], status=CaseStatus(raw["status"]),
            outcome=CaseOutcome(raw["outcome"]) if raw.get("outcome") else None,
            createdAt=raw["createdAt"], updatedAt=raw["updatedAt"], claimType=ClaimType(raw["claimType"]),
            merchantId=raw["merchantId"], transactionIds=list(raw["transactionIds"]),
            evidenceIds=list(raw["evidenceIds"]), totalDisputedAmount=raw["totalDisputedAmount"],
            currency=raw["currency"], confidence=raw["confidence"],
            recommendedActions=[ActionType(action) for action in raw["recommendedActions"]],
            requiresHumanReview=raw["requiresHumanReview"],
        ))
    for raw in _read(fixtures / "cases" / "demo-evidence.json"):
        store.add_evidence(Evidence(
            evidenceId=raw["evidenceId"], caseId=raw["caseId"], category=EvidenceCategory(raw["category"]),
            type=raw["type"], claim=raw["claim"], source=raw["source"],
            recordedAt=raw.get("recordedAt", UNKNOWN_RECORDED_AT),
            reliability=Reliability(raw["reliability"]), transactionIds=list(raw["transactionIds"]),
        ))
    return store
