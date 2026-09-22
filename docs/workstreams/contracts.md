# Contracts workstream

## STATUS

CORE_MODELS_AND_FIXTURES_COMPLETE — ready for integration and consuming workers.

## DONE

- All MVP shared types and Zod validators, plus standalone JSON Schema 2020-12 exports.
- Exact 14 specification CaseStatus names and documented transition map.
- Canonical policy action names aligned with specification section 25.
- Case/report outcome `CUSTOMER_RECOGNIZED_MERCHANT` for the no-dispute scenario.
- Guarded recognition transition CLASSIFYING_DISPUTE → RESOLVED → CLOSED.
- Shared `AuditEvent` schema with concise policy-aware operational audit fields.
- Original one-record representative fixtures preserved as a separate dataset.
- Deterministic demo fixtures: 50 customers, 1,200 transactions, 40 merchants/profiles.
- Four alias groups, three ordinary recurring merchants, one expiring emerging-risk profile.
- Twelve historical disputes plus active scenarios A–E, with linked evidence and reports.
- RCS/SMS inbound and RCS/SMS/EMAIL outbound fixture coverage; nothing sent externally.
- Fixture integrity validator and negative tests for corrupted references, totals and snapshots.
- 33 passing contracts tests; no new dependencies or service implementation.
- Investigator review-decision workflow (branch `agent/release/investigator-review-workflow`):
  `HumanReviewRequest` gains `reviewId`/`status` (PENDING/RESOLVED); new `ReviewDecisionSchema`
  and `REVIEW_DECISION_TARGET_STATUS`; `assertReviewDecisionTransition` enforces
  NEEDS_HUMAN_REVIEW + PENDING-review preconditions. No CASE_STATUS_TRANSITIONS edges added —
  REJECT and REQUEST_MORE_EVIDENCE both reuse the existing INVESTIGATING edge. Documented in
  README "Review decision workflow". 36 contracts tests now (was 35).

## CURRENT INTERFACES

- `@themis/contracts`: domain types and `<Type>Schema` runtime validators.
- `packages/contracts/schemas.json`: schema-name → standalone JSON Schema document.
- `CASE_STATUS_TRANSITIONS`, `canTransitionCaseStatus`, `assertCaseStatusTransition`.
- `assertCaseTransition(current, next)`: shape/identity checks and guarded early resolution.
- `Case.outcome`, `CaseReport.outcome`: optional `CUSTOMER_RECOGNIZED_MERCHANT`.
- `AuditEventSchema`: event/action/tool/result plus optional controlled-action policy context.
- Policy actions: CREATE_DISPUTE, PROVISIONAL_CREDIT, BLOCK_RECURRING_MERCHANT,
  REPLACE_CARD, DENY_CASE, REVIEW_FUTURE_RECURRING_PAYMENT, REQUEST_MERCHANT_EVIDENCE.
- `fixtures/**/demo*.json`: independent complete demo dataset; do not combine with examples.
- `fixtures/scenarios.json`: fixture-only scenario inputs and expected outcomes.
- Regenerate: build contracts, then `node fixtures/scripts/generate-demo.mjs`.
- Complete validation: `npm test --workspace @themis/contracts`.

## KNOWN ISSUES / DECISIONS

- Replaced bootstrap action names BLOCK_MERCHANT_PAYMENT, CLOSE_CARD, DENY_DISPUTE.
- Status-only helpers cannot enforce outcome guards; use assertCaseTransition for case documents.
- Transitions do not authorize account actions; service policy/human approval remains mandatory.
- Section 14 names states but no edges; chosen edges are documented in the package README.
- Auth keys follow spec snake_case; absent means unknown. Amounts are major units;
  fixture totals reconcile through integer cents (all demo currencies are USD).
- Evidence/source/review/risk labels remain open nonempty strings; spec has no exhaustive lists.
- Schemas check shapes; fixture validator checks this dataset's relational integrity.
- Cached-profile/no-browser behavior in scenario E is expected metadata, not implemented services.
- Section 31 idempotency is a service persistence responsibility; it has no prescribed shared record
  shape, so contracts preserves message IDs but does not invent an idempotency-store model.
- CodeMunch unavailable; scoped Markdown/source reads used. No external or paid calls.
- Another worker switched the shared checkout to agent/bank-tools/core-tools mid-session.
  Final contracts edits were moved to isolated `/private/tmp/themis-contracts-core-models`;
  the other worker's bank-tools changes were preserved.

## NEXT 3 TASKS

1. Integration merges the contracts worker branch; no push performed in this session.
2. Workers consume canonical actions, outcome and guarded transitions without duplicating models.
3. QA/demo implements load/reset and service-level execution using scenarios A–E.

## LAST TEST COMMAND + RESULT

- `npm test --workspace @themis/contracts` — build passed; 36 tests passed, 0 failures.
- `git diff --check` — passed.

## LAST CODE COMMIT

- `17d8336` — canonical MVP actions and validated deterministic demo fixtures.
- Final audit-contract/tests/handoff commit: `git log -1 --oneline -- docs/workstreams/contracts.md`.
