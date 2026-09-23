"""The Themis orchestrator: one agent, explicit state machine, deterministic policy gate.

Per customer turn: <=1 model inference (signal extraction), then code drives the case forward with
Gateway tools until it needs the customer again. The model never touches money or account actions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

from .config import Config
from .interfaces import GatewayClient, MemoryClient, MerchantResearch, ModelClient
from .prefilter import amount_bounds, prefilter
from .prompt import SYSTEM_PROMPT
from .state import CaseState

RETRY_MSG = "I'm having trouble on my end right now. Your case is saved; please send your message again in a moment."
ASK_CHARGE = "Which charge are you asking about? Tell me the merchant name or approximate amount."
ASK_MORE = "I couldn't match those. Can you share the merchant name or the approximate amount and timing of the charge?"
ASK_CLAIM = "Do you recognize this merchant, possibly under another name? And did you ever cancel it?"
CONFIRM_AGAIN = "Please confirm whether these are the charges you mean: confirm all, pick specific ones, or say none."
ESCALATED_MSG = "Thanks for your patience. I've passed this to a specialist who will review your case and follow up."
RESOLVED_MSG = "I've recorded your case and prepared a report. Reference: {case_id}."
CANCELLED_MSG = ("I've opened a dispute for the charges after your cancellation and will ask the merchant to confirm it. "
                 "Reference: {case_id}. I haven't blocked any future payments; tell me if you want that.")
RECOGNIZED_MSG = "Glad we could sort that out. I've noted that you recognize this merchant and I haven't opened a dispute."

DENIAL_CLAIMS = ("UNAUTHORIZED_TRANSACTION", "UNRECOGNIZED_MERCHANT", "RECURRING_PAYMENT_NOT_AUTHORIZED")
CLAIM_KEYS = ("recognizes_merchant", "canceled", "denies_authorization", "requested_block")
# Must match bank_tools.policy.CUSTOMER_REQUEST_EVIDENCE_TYPE: the persisted, auditable signal bank-tools
# trusts instead of the model's requested_block claim (prompt.md trust boundary).
CUSTOMER_REQUEST_EVIDENCE_TYPE = "PAYMENT_BLOCK_REQUESTED"
MAX_CUSTOMER_MESSAGE_CHARS = 4_000
IDENTITY_MISMATCH_MSG = "I can't access that conversation for this customer. Please start a new conversation or contact support."
MESSAGE_TOO_LONG_MSG = "That message is too long for me to process safely. Please resend a shorter description of the charge."


class ToolFailure(Exception):
    def __init__(self, tool: str, message: str):
        super().__init__(f"{tool}: {message}")
        self.tool = tool


@dataclass
class Reply:
    text: str
    status: str
    case_id: str | None = None
    suggestions: list[dict[str, str]] = field(default_factory=list)   # quick replies: {"label", "postback"}


CONFIRM_CHOICES = [
    {"label": "Yes, those are the ones", "postback": "Yes, confirm all"},
    {"label": "Let me choose", "postback": "I want to choose specific charges"},
    {"label": "None of these", "postback": "None of these"},
]
CLAIM_CHOICES = [
    {"label": "I recognize it", "postback": "I recognize this merchant"},
    {"label": "I don't recognize it", "postback": "I don't recognize this merchant"},
    {"label": "I canceled it", "postback": "I canceled this subscription"},
]


def suggestions_for(st: CaseState) -> list[dict[str, str]]:
    if st.status == "AWAITING_TRANSACTION_CONFIRMATION":
        return [dict(c) for c in CONFIRM_CHOICES]
    if st.status == "AWAITING_CUSTOMER_INFORMATION" and st.pending == "claim":
        return [dict(c) for c in CLAIM_CHOICES]
    return []   # terminal and free-text replies carry no choices


def clean_analysis(raw: Any) -> dict[str, Any]:
    """The model output is untrusted: keep only known keys of the right type."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {"hints": {}, "claim": {}, "selection": None}
    h = raw.get("hints") if isinstance(raw.get("hints"), dict) else {}
    if isinstance(h.get("descriptor"), str) and h["descriptor"].strip():
        out["hints"]["descriptor"] = h["descriptor"].strip()[:60]
    if isinstance(h.get("amount"), (int, float)) and not isinstance(h["amount"], bool) and h["amount"] > 0:
        out["hints"]["amount"] = float(h["amount"])
    sel = raw.get("selection")
    if sel in ("all", "none"):
        out["selection"] = sel
    elif isinstance(sel, list) and all(isinstance(x, str) for x in sel):
        out["selection"] = sel[:20]
    for k in CLAIM_KEYS:
        if isinstance(raw.get(k), bool):
            out["claim"][k] = raw[k]
    return out


class Orchestrator:
    def __init__(
        self, *, model: ModelClient, gateway: GatewayClient, memory: MemoryClient,
        research: MerchantResearch, config: Config | None = None,
        today: Callable[[], date] = date.today,
    ):
        self.model, self.gateway, self.memory, self.research = model, gateway, memory, research
        self.cfg = config or Config()
        self.today = today

    # -- entry point ---------------------------------------------------------

    def handle_turn(self, conversation_id: str, customer_id: str, message: str) -> Reply:
        raw = self.memory.load(conversation_id)
        if raw:
            st = CaseState.from_dict(raw)
            # Conversation IDs are channel identities. Never attach an existing
            # conversation to a different bank customer if a directory mapping is
            # changed or a caller supplies inconsistent identity fields.
            if st.customer_id != customer_id:
                return Reply(IDENTITY_MISMATCH_MSG, "UNVERIFIED")
        else:
            st = CaseState(conversation_id, customer_id)
        if len(message) > MAX_CUSTOMER_MESSAGE_CHARS:
            return Reply(MESSAGE_TOO_LONG_MSG, "INPUT_REJECTED", st.case_id)
        try:
            text = self._turn(st, message)
        except ToolFailure as exc:
            text = self._on_tool_failure(st, exc)
        self.memory.save(conversation_id, st.to_dict())
        return Reply(text, st.status, st.case_id, suggestions_for(st))

    def _turn(self, st: CaseState, message: str) -> str:
        if st.status in ("RESOLVED", "CLOSED"):
            return self._resolved_text(st)
        if st.status == "NEEDS_HUMAN_REVIEW":
            if st.escalation and not st.escalation.get("delivered"):
                self._deliver_escalation(st)   # earlier delivery failed; retry (idempotent)
            return ESCALATED_MSG
        if st.model_turns >= self.cfg.max_model_turns:
            return self._escalate(st, "INVESTIGATION_BUDGET_EXCEEDED", "Automated model-turn budget used up.")
        try:
            view = {
                "status": st.status, "pending": st.pending, "claim": st.claim, "hints": st.hints,
                "candidates": st.candidates[: self.cfg.max_candidates],
            }
            low = st.confidence is not None and st.confidence < self.cfg.escalation_confidence
            tier = "reasoning" if low else "fast"   # stronger model only when confidence is low (prompt.md #13.8)
            a = clean_analysis(self.model.analyze(SYSTEM_PROMPT, view, message, tier))
        except Exception:  # noqa: BLE001 - any model failure: preserve state, ask to retry (prompt.md #46)
            st.bump("model_failures")
            return RETRY_MSG
        st.model_turns += 1
        st.bump("model_calls")
        return self._advance(st, a, message)

    def _on_tool_failure(self, st: CaseState, exc: ToolFailure) -> str:
        if st.case_id is None:
            return RETRY_MSG   # no case exists to escalate; state is preserved for the retry
        return self._escalate(st, "TOOL_FAILURE", f"Required tool {exc.tool} failed after retry.")

    # -- state machine driver --------------------------------------------------

    def _advance(self, st: CaseState, a: dict[str, Any], message: str) -> str:
        st.hints.update(a["hints"])
        st.claim.update(a["claim"])
        if a["claim"].get("requested_block") and st.block_request_message is None:
            st.block_request_message = message[:500]
        if st.status == "NEW":
            st.move("INTAKE")
        while True:
            s = st.status
            if s == "INTAKE" or (s == "AWAITING_CUSTOMER_INFORMATION" and st.pending == "matching"):
                st.pending = None
                if not st.hints.get("descriptor") and st.hints.get("amount") is None:
                    return self._ask(st, "matching", ASK_CHARGE)
                self._move(st, "TRANSACTION_MATCHING")
            elif s == "TRANSACTION_MATCHING":
                if (reply := self._match(st)) is not None:
                    return reply
            elif s == "AWAITING_TRANSACTION_CONFIRMATION":
                if (reply := self._confirm(st, a)) is not None:
                    return reply
            elif s == "AWAITING_CUSTOMER_INFORMATION":   # pending == "claim": answer already absorbed
                st.pending = None
                self._move(st, "CLASSIFYING_DISPUTE")
            elif s == "CLASSIFYING_DISPUTE":
                if (reply := self._classify(st)) is not None:
                    return reply
            elif s == "INVESTIGATING":
                return self._investigate(st)
            else:
                return ESCALATED_MSG

    def _move(self, st: CaseState, to: str, **fields: Any) -> None:
        st.move(to)
        if st.case_id:   # keep the server-side case status in step (best effort)
            self._try(st, "update_case", caseId=st.case_id, status=to, **fields,
                      idempotencyKey=self._key(st, f"status:{len(st.history)}:{to}"))

    def _resolved_text(self, st: CaseState) -> str:
        if st.outcome == "CUSTOMER_RECOGNIZED_MERCHANT":
            return RECOGNIZED_MSG
        base = (CANCELLED_MSG if st.classification == "RECURRING_PAYMENT_AFTER_CANCELLATION" else RESOLVED_MSG).format(case_id=st.case_id)
        return " ".join([base, *st.followups])

    def _ask(self, st: CaseState, pending: str, text: str) -> str:
        if st.status != "AWAITING_CUSTOMER_INFORMATION":
            self._move(st, "AWAITING_CUSTOMER_INFORMATION")
        st.pending = pending
        return text

    # -- transaction matching + confirmation -----------------------------------

    def _match(self, st: CaseState) -> str | None:
        today = self.today()
        args: dict[str, Any] = {
            "customerId": st.customer_id, "since": (today - timedelta(days=self.cfg.match_window_days)).isoformat(),
            "limit": 50, "caseId": st.case_id,
        }
        if (d := st.hints.get("descriptor")):
            args["descriptorContains"] = d.split()[0]
        if (amt := st.hints.get("amount")) is not None:
            args["minAmount"], args["maxAmount"] = amount_bounds(amt)
        found = self._call(st, "search_transactions", **args).get("transactions", [])
        st.candidates = prefilter(
            found, st.hints, today=today, window_days=self.cfg.match_window_days,
            max_candidates=self.cfg.max_candidates,
        )
        if not st.candidates:
            st.match_attempts += 1
            if st.match_attempts >= 2:
                return self._escalate(st, "TRANSACTION_MATCHING_UNCERTAIN", "No matching transactions found after two attempts.")
            return self._ask(st, "matching", ASK_MORE)
        self._resolve_merchant(st)
        if st.merchant_id:
            # The first descriptor search is intentionally narrow. Once that
            # descriptor resolves to a trusted directory merchant, repeat the
            # bounded search by merchant ID so alias variants (for example a
            # compact card descriptor) are offered in the same confirmation.
            expanded_args = {
                "customerId": st.customer_id, "merchantId": st.merchant_id,
                "since": (today - timedelta(days=self.cfg.match_window_days)).isoformat(),
                "limit": 50, "caseId": st.case_id,
            }
            if (amt := st.hints.get("amount")) is not None:
                expanded_args["minAmount"], expanded_args["maxAmount"] = amount_bounds(amt)
            expanded = self._try(st, "search_transactions", **expanded_args)
            if expanded:
                st.candidates = prefilter(
                    expanded.get("transactions", []),
                    {"amount": st.hints["amount"]} if st.hints.get("amount") is not None else {"descriptor": st.merchant_name},
                    today=today, window_days=self.cfg.match_window_days, max_candidates=self.cfg.max_candidates,
                ) or st.candidates
        self._move(st, "AWAITING_TRANSACTION_CONFIRMATION")
        return self._proposal_text(st)

    def _resolve_merchant(self, st: CaseState) -> None:
        r = self._try(st, "resolve_merchant", descriptor=st.candidates[0]["merchantDescriptor"], caseId=st.case_id)
        if r and r.get("resolved"):
            st.merchant_id, st.merchant_name = r["merchantId"], r.get("canonicalName")

    def _proposal_text(self, st: CaseState) -> str:
        counts: dict[float, int] = {}
        for t in st.candidates:
            counts[t["amount"]] = counts.get(t["amount"], 0) + 1
        parts = " and ".join(f"{n} × ${amt:.2f}" for amt, n in counts.items())
        total = sum(t["amount"] for t in st.candidates)
        name = st.merchant_name or st.candidates[0]["merchantDescriptor"]
        return (f"I found {parts} from {name} totaling ${total:.2f}. Are these the charges you mean? "
                "You can confirm all, choose specific ones, or say none.")

    def _confirm(self, st: CaseState, a: dict[str, Any]) -> str | None:
        sel = a["selection"]
        ids = [t["id"] for t in st.candidates]
        if sel == "all":
            chosen = ids
        elif isinstance(sel, list):
            chosen = [i for i in ids if i in sel]   # ignore ids the model invented
        elif sel == "none":
            st.candidates = []
            st.match_attempts += 1
            self._move(st, "TRANSACTION_MATCHING")
            if st.match_attempts >= 2:
                return self._escalate(st, "TRANSACTION_MATCHING_UNCERTAIN", "Customer rejected the matched transactions twice.")
            if a["hints"]:
                return None   # new details in the same message: search again immediately
            return self._ask(st, "matching", ASK_MORE)
        else:
            return CONFIRM_AGAIN
        if not chosen:
            return CONFIRM_AGAIN
        st.confirmed_ids = chosen
        self._open_case(st)
        self._move(st, "CLASSIFYING_DISPUTE")
        return None

    def _open_case(self, st: CaseState) -> None:
        r = self._call(
            st, "create_case", customerId=st.customer_id, claimType="INSUFFICIENT_INFORMATION",
            transactionIds=st.confirmed_ids, merchantId=st.merchant_id,
            idempotencyKey=f"{st.conversation_id}:create_case",
        )
        st.case_id = r["case"]["caseId"]
        for i, s in enumerate(st.history[1:], 1):   # bring the server-side case (created NEW) up to our current status
            self._try(st, "update_case", caseId=st.case_id, status=s, idempotencyKey=self._key(st, f"status:{i}:{s}"))

    # -- classification --------------------------------------------------------

    def _classify(self, st: CaseState) -> str | None:
        c = st.claim
        confirmed = [t for t in st.candidates if t["id"] in st.confirmed_ids]
        recurring = bool(confirmed) and all(t["recurring"] for t in confirmed)
        if c.get("canceled"):
            st.classification = "RECURRING_PAYMENT_AFTER_CANCELLATION"
        elif c.get("recognizes_merchant") and not c.get("denies_authorization"):
            return self._recognized(st)
        elif c.get("recognizes_merchant") is False or c.get("denies_authorization"):
            if recurring:
                st.classification = "RECURRING_PAYMENT_NOT_AUTHORIZED"
            elif c.get("recognizes_merchant") is False:
                st.classification = "UNRECOGNIZED_MERCHANT"
            else:
                st.classification = "UNAUTHORIZED_TRANSACTION"
        else:
            return self._ask(st, "claim", ASK_CLAIM)
        self._try(st, "update_case", caseId=st.case_id, claimType=st.classification, idempotencyKey=self._key(st, "claim"))
        self._ev(st, "CUSTOMER_CLAIMS", "CLAIM_STATEMENT", self._claim_text(st), "CUSTOMER", "MEDIUM")
        self._move(st, "INVESTIGATING")
        return None

    def _claim_text(self, st: CaseState) -> str:
        c = st.claim
        bits = [f"classified as {st.classification}"]
        if c.get("canceled"):
            bits.append("customer says the service was cancelled")
        if c.get("recognizes_merchant") is False:
            bits.append("customer does not recognize the merchant")
        if c.get("denies_authorization"):
            bits.append("customer denies authorizing the charges")
        return "Customer statement: " + "; ".join(bits) + "."

    def _recognized(self, st: CaseState) -> str:
        """Scenario C: customer recognizes the merchant, so no dispute is opened."""
        st.classification, st.outcome = None, "CUSTOMER_RECOGNIZED_MERCHANT"
        self._ev(st, "CUSTOMER_CLAIMS", "CUSTOMER_RECOGNIZED_MERCHANT",
                 "Customer recognized the merchant after identification; no dispute opened.", "CUSTOMER", "HIGH")
        # Verify that report generation is available before entering a terminal
        # state. A failure can still transition this case to human review.
        self._report(st, required=True, phase="pre-final")
        # outcome rides on the status update: the bank refuses early RESOLVED without it
        self._move(st, "RESOLVED", **({"outcome": st.outcome} if self.cfg.structured_tools else {}))
        self._report(st, phase="final")
        return RECOGNIZED_MSG

    # -- investigation -> proposal -> policy -----------------------------------

    def _investigate(self, st: CaseState) -> str:
        cfg = self.cfg
        confirmed = [t for t in st.candidates if t["id"] in st.confirmed_ids]
        recurring = len(confirmed) >= 2 and all(t["recurring"] for t in confirmed)
        missing: list[str] = []

        if recurring:
            total = sum(t["amount"] for t in confirmed)
            self._ev(st, "TRANSACTION_EVIDENCE", "RECURRING_PATTERN",
                     f"{len(confirmed)} recurring charges from {confirmed[-1]['date']} to {confirmed[0]['date']} totaling ${total:.2f}.",
                     "BANK_LEDGER", "HIGH", st.confirmed_ids)

        signals: list[tuple[str, dict[str, Any]]] = []
        for tid in st.confirmed_ids[: cfg.max_auth_lookups]:
            r = self._try(st, "get_transaction_auth_signals", transactionId=tid, caseId=st.case_id)
            if r:
                signals.append((tid, r.get("authSignals", {})))
            elif "auth_signals" not in missing:
                missing.append("auth_signals")
        if signals:
            self._ev(st, "AUTHENTICATION_EVIDENCE", "AUTH_SIGNALS", "; ".join(f"{t}: {_auth_text(s)}" for t, s in signals),
                     "PROCESSOR_SIGNALS", "HIGH", [t for t, _ in signals])

        profile, risk_count = None, 0
        if st.merchant_id:
            self._ev(st, "MERCHANT_IDENTITY", "RESOLVED_MERCHANT", f"Descriptor resolves to {st.merchant_name}.", "MERCHANT_DIRECTORY", "HIGH")
            if (p := self._try(st, "get_merchant_profile", merchantId=st.merchant_id)):
                profile = p.get("profile", {})
            else:
                missing.append("merchant_profile")
            if (r := self._try(st, "get_merchant_risk_signals", merchantId=st.merchant_id)):
                risk_count = r.get("count", len(r.get("riskSignals", [])))
        else:
            missing.append("merchant_identity")

        cases = (profile or {}).get("caseStatistics", {}).get("totalCases", 0)
        memory = self._recall(st)
        cached = bool(profile) and (cases > 0 or risk_count > 0 or bool(profile.get("riskSignals")))
        st.bump("cache_hits" if cached else "cache_misses")
        if cases or risk_count:
            kinds = sorted({str(x.get("type")) for x in (profile or {}).get("riskSignals", []) if isinstance(x, dict) and x.get("type")})
            self._ev(st, "MERCHANT_HISTORY", "INSTITUTIONAL_MEMORY",
                     f"Cached merchant profile: {cases} prior bank cases and {risk_count} active risk signals"
                     + (f" ({', '.join(kinds)})" if kinds else "") + "; no new research needed.",
                     "MERCHANT_PROFILE", "HIGH")
        if cached and not self._verify_customer(st):
            missing.append("customer_merchant_history")
        for rec in memory:
            self._ev(st, "MERCHANT_HISTORY", "MEMORY_RECORD", rec[:200], "AGENT_MEMORY", "MEDIUM")

        if (h := self._try(st, "get_customer_dispute_history", customerId=st.customer_id, limit=cfg.max_history_cases)):
            n = len(h.get("cases", []))
            self._ev(st, "BANK_HISTORY", "CUSTOMER_DISPUTE_HISTORY", f"Customer has {n} prior dispute case(s).", "BANK_LEDGER", "MEDIUM")

        if st.merchant_id and not cached:
            if not self._research(st):
                missing.append("external_merchant_research")

        for m in missing:
            self._ev(st, "MISSING_EVIDENCE", m.upper(), f"{m.replace('_', ' ')} unavailable.", "THEMIS_AGENT", "LOW")

        conflicts = _contradictions(st.classification, signals)
        for c in conflicts:
            self._ev(st, "CONTRADICTORY_EVIDENCE", "CLAIM_CONFLICT", c, "PROCESSOR_SIGNALS", "HIGH", st.confirmed_ids)

        conf = 0.6 + 0.15 * recurring + 0.1 * bool(cases or memory) + 0.1 * bool(risk_count) - 0.2 * len(missing)
        st.confidence = round(max(0.0, min(0.95, conf)), 2)

        if conflicts:
            return self._escalate(st, "CONFLICTING_AUTHORIZATION_EVIDENCE",
                                  "Customer claim conflicts with transaction evidence; review needed. " + conflicts[0])
        if "merchant_identity" in missing:
            return self._escalate(st, "MERCHANT_UNRESOLVED", "Merchant could not be resolved from the descriptor.")
        if st.confidence < cfg.escalation_confidence:
            return self._escalate(st, "LOW_CONFIDENCE", f"Confidence {st.confidence} is below {cfg.escalation_confidence}.")
        return self._propose_and_gate(st, confirmed, missing)

    def _verify_customer(self, st: CaseState) -> bool:
        """Scenario E: a known-pattern merchant, so check this customer's own ledger with it before trusting the pattern."""
        r = self._try(st, "find_related_transactions", transactionId=st.confirmed_ids[0], limit=20, caseId=st.case_id)
        if r is None:
            return False
        txns = r.get("transactions", [])
        others = [t for t in txns if t["id"] not in st.confirmed_ids]
        first = min((t["date"] for t in txns), default="unknown")
        self._ev(st, "BANK_HISTORY", "CUSTOMER_MERCHANT_VERIFICATION",
                 f"Customer has {len(txns)} charge(s) from this merchant since {first}; {len(st.confirmed_ids)} in this case, {len(others)} not reported.",
                 "BANK_LEDGER", "HIGH", [t["id"] for t in txns[:20]])
        if others:
            total = sum(t["amount"] for t in others)
            st.followups.append(f"I also see {len(others)} other charge(s) from this merchant totaling ${total:.2f}; tell me if you want those reviewed too.")
        return True

    def _recall(self, st: CaseState) -> list[str]:
        try:
            return list(self.memory.recall(st.merchant_name or st.hints.get("descriptor") or "", self.cfg.max_memory_records))[: self.cfg.max_memory_records]
        except Exception:  # noqa: BLE001 - memory is enrichment only
            st.bump("memory_failures")
            return []

    def _research(self, st: CaseState) -> bool:
        if not self.cfg.browser_research or st.research_calls >= self.cfg.max_research_calls:
            return False
        st.research_calls += 1
        st.bump("browser_calls")
        try:
            r = self.research.research(st.merchant_name or st.hints.get("descriptor", ""), self.cfg.max_research_pages)
        except Exception:  # noqa: BLE001 - browser unavailable: continue on internal evidence
            return False
        if not r:
            return False
        self._ev(st, "EXTERNAL_MERCHANT_INTELLIGENCE", "WEB_RESEARCH", str(r.get("summary", ""))[:300], "MERCHANT_RESEARCH", "MEDIUM")
        return True

    def _propose_and_gate(self, st: CaseState, confirmed: list[dict[str, Any]], missing: list[str]) -> str:
        actions = ["CREATE_DISPUTE"]
        if st.classification == "RECURRING_PAYMENT_NOT_AUTHORIZED":
            actions.append("REVIEW_FUTURE_RECURRING_PAYMENT")
        if st.classification == "RECURRING_PAYMENT_AFTER_CANCELLATION":   # Scenario B: a merchant billing problem, not card fraud
            actions.append("REQUEST_MERCHANT_EVIDENCE")
            missing = [*missing, "merchant_cancellation_confirmation"]
            self._ev(st, "MISSING_EVIDENCE", "MERCHANT_CANCELLATION_CONFIRMATION",
                     "Merchant has not confirmed the cancellation; evidence requested.", "THEMIS_AGENT", "LOW")
        if st.claim.get("requested_block"):
            actions.append("BLOCK_RECURRING_MERCHANT")
            if not st.block_request_recorded:   # persist before propose_payment_block; bank-tools trusts this, not the tool argument
                self._ev(st, "CUSTOMER_CLAIMS", CUSTOMER_REQUEST_EVIDENCE_TYPE,
                         st.block_request_message or self._claim_text(st), "CUSTOMER", "MEDIUM")
                st.block_request_recorded = True
        if st.classification in ("UNAUTHORIZED_TRANSACTION", "UNRECOGNIZED_MERCHANT"):
            actions.append("PROVISIONAL_CREDIT")
        by_cat = lambda cat: [e["evidenceId"] for e in st.evidence if e["category"] == cat]  # noqa: E731
        st.proposal = {
            "caseId": st.case_id, "classification": st.classification, "confidence": st.confidence,
            "supportingEvidence": [e["evidenceId"] for e in st.evidence if e["category"] not in ("CONTRADICTORY_EVIDENCE", "MISSING_EVIDENCE")],
            "contradictoryEvidence": by_cat("CONTRADICTORY_EVIDENCE"), "missingEvidence": missing,
            "recommendedActions": actions, "requiresHumanReview": False,
        }
        self._try(st, "update_case", caseId=st.case_id, confidence=st.confidence, requiresHumanReview=False,
                  idempotencyKey=self._key(st, "proposal"))
        if not self._stored_state_matches(st):   # policy reads the stored case; never trust our own copy of it
            return self._escalate(st, "CASE_STATE_MISMATCH", "Stored case classification, confidence or transactions differ from the agent's; review needed.")
        self._move(st, "RESOLUTION_PROPOSED")
        self._move(st, "POLICY_REVIEW")
        total = round(sum(t["amount"] for t in confirmed), 2)
        for action in actions:
            call = self._policy_call(st, action, total)
            if call is None:
                continue   # informational action, no money/account effect
            tool, args = call
            outcome = self._call(st, tool, caseId=st.case_id, **args, idempotencyKey=self._key(st, tool)).get("decision", {}).get("outcome")
            st.policy_results.append({"action": action, "outcome": outcome or "MISSING"})
            if outcome != "ALLOW":   # DENY, REQUIRE_HUMAN_REVIEW, or anything unexpected: fail closed
                return self._escalate(st, f"POLICY_{outcome or 'UNKNOWN'}", f"Policy did not allow {action}; human review required.")
        # Required artifact generation while POLICY_REVIEW can still fail
        # closed into NEEDS_HUMAN_REVIEW.
        self._report(st, required=True, phase="pre-final")
        self._move(st, "ACTION_APPROVED")
        self._move(st, "RESOLVED")
        self._report(st, phase="final")
        return self._resolved_text(st)

    def _stored_state_matches(self, st: CaseState) -> bool:
        c = self._call(st, "get_case", caseId=st.case_id).get("case", {})
        return (c.get("claimType") == st.classification and c.get("confidence") == st.confidence
                and set(c.get("transactionIds", [])) == set(st.confirmed_ids))

    def _policy_call(self, st: CaseState, action: str, total: float) -> tuple[str, dict[str, Any]] | None:
        return {
            "CREATE_DISPUTE": ("propose_dispute_creation", {"transactionIds": st.confirmed_ids, "hasConfirmedTransactions": bool(st.confirmed_ids)}),
            "BLOCK_RECURRING_MERCHANT": ("propose_payment_block", {"customerRequested": bool(st.claim.get("requested_block"))}),
            "PROVISIONAL_CREDIT": ("propose_provisional_credit", {"amount": total, "confidence": st.confidence, "claimType": st.classification}),
        }.get(action)

    # -- escalation + report ---------------------------------------------------

    def _escalate(self, st: CaseState, reason: str, summary: str) -> str:
        refs = [e["evidenceId"] for e in st.evidence if e["category"] in ("CONTRADICTORY_EVIDENCE", "MISSING_EVIDENCE")]
        st.escalation = {"reason": reason, "summary": summary, "evidenceRefs": refs, "delivered": False}
        st.bump("escalations")
        st.move("NEEDS_HUMAN_REVIEW")
        self._deliver_escalation(st)
        if st.case_id:
            self._report(st)
        return ESCALATED_MSG

    def _deliver_escalation(self, st: CaseState) -> None:
        if not st.case_id:
            return   # no server-side case yet; nothing to queue
        e = st.escalation
        text = f"{e['reason']}: {e['summary']}" + (f" [evidence: {', '.join(e['evidenceRefs'])}]" if e["evidenceRefs"] else "")
        args = ({"reason": e["reason"], "summary": e["summary"][:500], "evidenceRefs": e["evidenceRefs"]}
                if self.cfg.structured_tools else {"reason": text[:500]})
        e["delivered"] = self._try(st, "escalate_case", caseId=st.case_id, **args,
                                   idempotencyKey=self._key(st, f"escalate:{e['reason']}")) is not None

    def _report(self, st: CaseState, *, required: bool = False, phase: str = "current") -> None:
        args = {"caseId": st.case_id, "idempotencyKey": self._key(st, f"report:{phase}")}
        if required:
            self._call(st, "generate_case_report", **args)
        else:
            self._try(st, "generate_case_report", **args)

    # -- tool + evidence plumbing ----------------------------------------------

    def _key(self, st: CaseState, suffix: str) -> str:
        return f"{st.case_id}:{suffix}"

    def _call(self, st: CaseState, tool: str, **args: Any) -> dict[str, Any]:
        """Call a Gateway tool with one retry (all tools are reads or idempotency-keyed). Raises ToolFailure."""
        args = {k: v for k, v in args.items() if v is not None}
        err = "unknown error"
        for _ in range(2):
            st.bump("tool_calls")
            try:
                r = self.gateway.call(tool, args)
            except Exception as exc:  # noqa: BLE001 - transport failure
                r, err = None, f"transport error: {type(exc).__name__}"
            else:
                if r.get("status") == "ok":
                    st.audit.append({"tool": tool, "result": "SUCCESS"})
                    return r
                code = r.get("error", {}).get("code", "ERROR")
                err = str(code)
                if code == "VALIDATION_ERROR":
                    break   # retrying identical bad input cannot help
        st.audit.append({"tool": tool, "result": "FAILURE"})
        raise ToolFailure(tool, err)

    def _try(self, st: CaseState, tool: str, **args: Any) -> dict[str, Any] | None:
        """Best-effort call for enrichment: a failed lookup must not destroy the case (prompt.md #46)."""
        try:
            return self._call(st, tool, **args)
        except ToolFailure:
            return None

    def _ev(self, st: CaseState, category: str, type_: str, claim: str, source: str, reliability: str,
            txn_ids: list[str] | None = None) -> None:
        ev = {"evidenceId": f"ev_{(st.case_id or 'x').removeprefix('case_')}_{len(st.evidence) + 1}", "category": category,
              "type": type_, "claim": claim, "source": source, "reliability": reliability, "transactionIds": list(txn_ids or [])}
        st.evidence.append(ev)
        self._try(st, "save_evidence", caseId=st.case_id, evidenceId=ev["evidenceId"], category=category, type=type_,
                  claim=claim, source=source, reliability=reliability, transactionIds=ev["transactionIds"],
                  idempotencyKey=self._key(st, ev["evidenceId"]))


def _auth_text(s: dict[str, Any]) -> str:
    bits = [f"3DS {s['3ds_status']}"] if s.get("3ds_status") else []
    bits += [f"{k} {'yes' if s[k] else 'no'}" for k in ("cvv_match", "avs_match", "card_present", "prior_merchant_relationship") if k in s]
    return ", ".join(bits) or "no signals"


def _contradictions(classification: str | None, signals: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """Evidence that cuts against a denial claim. Signals are evidence, not proof of anything (prompt.md #18)."""
    if classification not in DENIAL_CLAIMS:
        return []
    out = []
    for tid, s in signals:
        if s.get("3ds_status") == "AUTHENTICATED" and s.get("cvv_match"):
            out.append(f"{tid} passed strong authentication (3DS authenticated, CVV match).")
        elif s.get("prior_merchant_relationship"):
            out.append(f"{tid} shows a prior relationship with the merchant.")
    return out
