"""Composition of the 21 Gateway tools over their owning services.

`TOOL_OWNERS` is the frozen ownership map (tests/test_tool_mapping.py checks it against
infra/config/tool-schemas.ts). All 21 tools run through bank_tools.dispatch, so camelCase argument
validation, snake_case mapping and idempotencyKey claim/replay are identical no matter who owns the tool.
Owners other than bank-tools are registered as extra Specs.
"""
from __future__ import annotations

from typing import Any, Callable, Protocol

from bank_tools.dispatch import SPECS, Spec, _a, _s, dispatch
from bank_tools.errors import ToolError
from bank_tools.store import BankToolsStorage
from merchant_intel import MerchantIntel, tools as merchant_tools
from orchestrator import workflow

TOOL_OWNERS: dict[str, str] = {
    **dict.fromkeys((
        "search_transactions", "get_transaction_details", "get_transaction_auth_signals", "find_related_transactions",
        "get_customer_dispute_history", "get_case", "create_case", "update_case", "save_evidence",
        "propose_payment_block", "propose_card_replacement", "propose_dispute_creation", "propose_provisional_credit",
        "submit_review_decision",
    ), "bank-tools"),
    **dict.fromkeys(("resolve_merchant", "get_merchant_profile", "get_merchant_risk_signals"), "merchant-intel"),
    **dict.fromkeys(("generate_case_report", "escalate_case"), "agentcore"),
    **dict.fromkeys(("send_customer_message", "send_case_email"), "messaging"),
}

_NEXT_STEPS = {
    "NEEDS_HUMAN_REVIEW": "A specialist will review your case and contact you.",
    "RESOLVED": "No further action is needed from you.",
}
_IDEM = _s("idempotencyKey", "idempotency_key")


class Messenger(Protocol):
    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        """Deliver via the messaging service; return {"messageId": ...}. Raise when delivery outcome is unknown."""


def _merchant_spec(name: str, fn: Callable[..., dict[str, Any]], intel: MerchantIntel) -> Spec:
    params = SPECS[name].params
    keep = {p.snake for p in params} - {"case_id"}  # caseId is accepted for audit context, merchant-intel has no use for it
    return Spec(lambda store, **kw: fn(intel, **{k: v for k, v in kw.items() if k in keep}), params)


def build_specs(intel: MerchantIntel, reports: workflow.ReportStore, messenger: Messenger) -> dict[str, Spec]:
    def route_escalation(
        store: BankToolsStorage, *, case_id: str, reason: str, idempotency_key: str,
        summary: str | None = None, evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        del idempotency_key  # dispatch owns request replay; workflow owns semantic de-duplication across fresh keys
        return workflow.escalate_case(
            store, case_id=case_id, reason=reason, summary=summary, evidence_refs=evidence_refs,
        )

    def send_customer_message(store: BankToolsStorage, *, case_id: str, channel: str, text: str, idempotency_key: str) -> dict[str, Any]:
        if channel not in ("RCS", "SMS"):
            raise ToolError("channel must be RCS or SMS (use send_case_email for email)", "VALIDATION_ERROR")
        case = store.get_case(case_id)
        phone = store.get_customer(case.customerId)["phone"]
        sent = messenger({"themisTool": "send_customer_message", "message": {
            "channel": channel, "customerExternalId": phone, "messageId": idempotency_key, "caseId": case_id, "text": text}})
        return {"status": "ok", "messageId": sent["messageId"]}

    def send_case_email(store: BankToolsStorage, *, case_id: str, idempotency_key: str, subject: str | None = None) -> dict[str, Any]:
        case = store.get_case(case_id)
        report = reports.get(case_id)
        if report is None:
            raise ToolError("no case report yet; call generate_case_report first", "NOT_FOUND")
        steps = _NEXT_STEPS.get(str(case.status), "We will update you as your case progresses.")  # subject is fixed by messaging
        sent = messenger({"themisTool": "send_case_email", "case": case.to_dict(), "report": report,
                          "recipient": store.get_customer(case.customerId)["email"], "nextSteps": [steps]})
        if sent.get("status") == "FAILED":
            return {"status": "error", "error": {"code": "DELIVERY_FAILED", "message": sent.get("error", "email delivery failed")},
                    "caseId": case_id, "caseStatus": str(case.status)}
        return {"status": "ok", "messageId": sent["messageId"]}

    return {
        "resolve_merchant": _merchant_spec("resolve_merchant", merchant_tools.resolve_merchant, intel),
        "get_merchant_profile": _merchant_spec("get_merchant_profile", merchant_tools.get_merchant_profile, intel),
        "get_merchant_risk_signals": _merchant_spec("get_merchant_risk_signals", merchant_tools.get_merchant_risk_signals, intel),
        "generate_case_report": Spec(lambda store, case_id, idempotency_key: workflow.generate_case_report(store, reports, case_id=case_id),
                                     (_s("caseId", "case_id"), _IDEM)),
        "escalate_case": Spec(route_escalation, (
            _s("caseId", "case_id"), _s("reason", "reason"), _s("summary", "summary", False),
            _a("evidenceRefs", "evidence_refs"), _IDEM,
        )),
        "send_customer_message": Spec(send_customer_message, (_s("caseId", "case_id"), _s("channel", "channel"), _s("text", "text"), _IDEM)),
        "send_case_email": Spec(send_case_email, (_s("caseId", "case_id"), _s("subject", "subject", False), _IDEM)),
    }


class ToolAdapter:
    """`call(tool, arguments)` -> {"status": "ok", ...} | {"status": "error", ...}; also the local Gateway stand-in."""
    def __init__(self, store: BankToolsStorage, intel: MerchantIntel, reports: workflow.ReportStore, messenger: Messenger) -> None:
        self.store, self.specs = store, build_specs(intel, reports, messenger)

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return dispatch(self.store, tool, arguments, self.specs)
