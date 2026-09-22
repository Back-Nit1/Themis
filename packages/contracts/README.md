# Shared contracts

Import types and runtime schemas from `@themis/contracts`. Each domain type has a corresponding `<Type>Schema` and supports `.parse(value)` / `.safeParse(value)`. Objects reject unknown fields. Missing auth signals mean unknown, never false. Amounts use major currency units as in the specification; workers must compute totals in integer minor units. All timestamps are ISO 8601 with an explicit timezone. IDs are opaque nonempty strings.

`npm run build` emits TypeScript declarations and `schemas.json`, a map of standalone JSON Schema 2020-12 documents. This is the serialized boundary for Python workers; generated schemas must not be edited by hand. Build before consuming the package. `EvidenceType`, evidence sources, review reasons, and risk signal types remain nonempty source-defined labels because the specification supplies no exhaustive vocabulary.

## Case state machine

The exact 14 status names come from specification section 14. That section supplies no edge list. `CASE_STATUS_TRANSITIONS` is the initial repository transition contract and the authoritative edge list for workers. `canTransitionCaseStatus` checks an edge; `assertCaseStatusTransition` rejects it. Neither executes actions nor grants policy permission.

Normal path: NEW → INTAKE → TRANSACTION_MATCHING → AWAITING_TRANSACTION_CONFIRMATION → CLASSIFYING_DISPUTE → INVESTIGATING → RESOLUTION_PROPOSED → POLICY_REVIEW → ACTION_APPROVED → RESOLVED → CLOSED.

A rejected match returns to TRANSACTION_MATCHING. Customer-information waits return to the requesting stage (the caller retains that context). Merchant-evidence waits return to INVESTIGATING. Investigation/intake/matching can escalate to NEEDS_HUMAN_REVIEW; policy can do so too. Human review can return to investigation, propose a revised resolution, or record approval before ACTION_APPROVED. CLOSED has no outgoing edges and repeated statuses are not transitions. Persistence/idempotency belongs to the service worker. A human-review approval edge requires recorded human approval; this package validates shape and workflow only.

The schemas validate individual documents, not database references, policy correctness, or monetary reconciliation. Services own those checks. Case reports retain structured evidence and concise rationale, never hidden chain-of-thought.

`AuditEventSchema` is the shared representation of the section 45 audit record. It captures a concise action, tool and result, with optional policy fields for controlled operations. `CaseReport.auditRefs` point to these records. It intentionally has no field for prompts, conversation history or model reasoning.

## MVP completion

`Case.outcome` and `CaseReport.outcome` can record `CUSTOMER_RECOGNIZED_MERCHANT` (scenario C). The state map permits CLASSIFYING_DISPUTE → RESOLVED for that outcome, then RESOLVED → CLOSED. Use `assertCaseTransition(current, next)` to check document shapes, preserve case/customer identity, and guard early resolution. Recognition must have no recommended dispute actions or review request. The status-only helpers cannot enforce document-level guards. All account-impacting actions still require service policy enforcement.

Canonical action names follow section 25: `CREATE_DISPUTE`, `PROVISIONAL_CREDIT`, `BLOCK_RECURRING_MERCHANT`, `REPLACE_CARD`, `DENY_CASE`, with `REVIEW_FUTURE_RECURRING_PAYMENT` from section 23 and `REQUEST_MERCHANT_EVIDENCE` from section 24. The bootstrap-only names `BLOCK_MERCHANT_PAYMENT`, `CLOSE_CARD`, and `DENY_DISPUTE` were replaced before service implementation. Workers should consume `ActionSchema`, not maintain aliases.

The complete modest fixture set lives in `fixtures/**/demo*.json`, separate from the original single-record examples. See `fixtures/README.md` to regenerate it. These are scenario snapshots and expected inputs/results for other workers, not an implemented agent, ledger, policy engine or message transport.

## Review decision workflow

`HumanReviewRequestSchema` now carries `reviewId` (stable identity) and `status` (`PENDING` |
`RESOLVED`), so a request can be referenced and later resolved instead of only ever being appended.
`ReviewDecisionSchema` is the record of an investigator acting on one: `decisionId`, `reviewId`,
`caseId`, `actor`, `decision` (`APPROVE` | `REJECT` | `REQUEST_MORE_EVIDENCE`), `reason`, `decidedAt`,
`idempotencyKey`.

**State transition.** `REVIEW_DECISION_TARGET_STATUS` maps each decision to the case status it
produces:

| decision | target case status |
| --- | --- |
| `APPROVE` | `ACTION_APPROVED` |
| `REJECT` | `INVESTIGATING` |
| `REQUEST_MORE_EVIDENCE` | `INVESTIGATING` |

`NEEDS_HUMAN_REVIEW` only has three outgoing edges (`INVESTIGATING`, `RESOLUTION_PROPOSED`,
`ACTION_APPROVED` — see "Case state machine" above); `REJECT` and `REQUEST_MORE_EVIDENCE` both use
the `INVESTIGATING` edge; the decision record — not the case status — is what distinguishes "this
proposal was wrong" from "this proposal needs more support" for whoever picks the case back up.
`RESOLUTION_PROPOSED` stays reserved for an investigator authoring their own revised proposal, which
this workflow does not do.

**Preconditions**, enforced by `assertReviewDecisionTransition(case, review, decision)` and by every
service that executes a decision: the case must be `NEEDS_HUMAN_REVIEW`; the review must belong to
that case and be `PENDING`; the decision's `caseId`/`reviewId` must match. A decision against a
`RESOLVED` review (already decided, e.g. a duplicate or racing submission) or a case no longer
`NEEDS_HUMAN_REVIEW` (stale) is rejected, not silently applied. `idempotencyKey` makes a retried
submission of the *same* decision replay its prior result instead of double-applying it (services
own the replay store, as with every other mutating tool — see section 31 in `KNOWN ISSUES /
DECISIONS`). Approving a decision never itself executes the underlying financial action; it only
records the case as `ACTION_APPROVED`, which remains a separate, policy-gated execution step.

**Authorization.** This package validates shape and the state machine only — it has no notion of who
`actor` is or what they are allowed to decide, matching every other schema here. Each caller owns
that boundary: `services/bank-tools` trusts `actor` as an already-authenticated audit label supplied
by its caller (the Gateway, for the agent path); a browser-facing caller (the dashboard) must sit
behind its own server-side authentication/authorization boundary before it ever reaches a mutating
tool — see `apps/dashboard`'s review-decision route for the current boundary. Never accept a review
decision from an unauthenticated public endpoint.
