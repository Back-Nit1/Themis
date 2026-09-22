# Bank Tools workstream

## STATUS

DYNAMO_DISPATCH_COMPLETE — storage protocol, DynamoDB store, and Gateway dispatcher done on
`agent/bank-tools/dynamo-dispatch` (local commit, not pushed). Lambda handler wiring is integration work.

## DONE

- Core tool surface (17 tools), in-memory store, deterministic policy, demo-fixture loader (35 tests).
- `BankToolsStorage` Protocol (store.py) is what tools/dispatcher depend on; in-memory `BankToolsStore`
  and `DynamoBankToolsStore` (dynamo_store.py, injected low-level client, no boto3 import) implement it.
- `dispatch(store, tool_name, arguments)` (dispatch.py): camelCase -> snake_case via explicit tables,
  required/unknown/type/range validation, mandatory `idempotencyKey` on mutating tools, NOT_OWNED for
  AgentCore/messaging tools, UNKNOWN_TOOL otherwise. A test parses infra/config/tool-schemas.ts and fails on drift.
- Idempotency: `claim_idempotency` (conditional put, 60s lease) -> run -> `remember_result`; replay returns
  the stored result and writes nothing; same key + different args -> IDEMPOTENCY_KEY_REUSED; errors release the key.
- `create_case` now rejects transactions owned by another customer (TRANSACTION_NOT_OWNED) and currency mismatches.
- Policy stays in Python: `confidence`/`claimType`/`hasConfirmedTransactions` from the Gateway are validated but
  ignored; propose_dispute_creation requires supplied transactionIds to be on the case.
- 108 tests; the dispatch tests run against both stores, incl. all current demo fixtures.

## CURRENT INTERFACES

Install: `python3 -m pip install -e services/bank-tools`.
- Tools: `fn(store: BankToolsStorage, **snake_case) -> {"status": "ok", ...} | {"status": "error", "error": {code, message}}`.
- Gateway: `dispatch(store, "create_case", {"customerId": ..., "idempotencyKey": ...})` from `bank_tools.dispatch`.
- Dynamo: `DynamoBankToolsStore(boto3.client("dynamodb"), Tables.from_env())`; tests use `tests/fake_dynamo.py`.
- Seed any store: `load_demo_store(repo_root, store=...)`.

## PERSISTENCE ASSUMPTIONS

- Tables/keys/GSIs from infra/stacks/data-stack.ts. No customer/evidence/policy tables exist, so they share
  Merchants (`C#`,`M#`,`P#`,`A#` prefixes), Cases (`E#`) and Audit (`audit_`,`policy_`,`review_` sort keys).
- No cross-item transactions: a crash mid-tool leaves partial writes; the IN_PROGRESS claim expires after its
  lease and a retry re-runs. `replace_case` is last-writer-wins. GSI reads (cases by customer) are eventually consistent.
- Idempotency rows: pk `<tool>#<key>`, fingerprint, status, result JSON, TTL `expiresAt` (7 days), `leaseOwner` (uuid per claim).
- Lease fencing: `IdempotencyClaim.owner`; dispatch sets `CURRENT_LEASE=(tool,key,owner)`; complete/release are conditional on
  `leaseOwner`, stale worker gets `LeaseLostError` (dispatch -> IDEMPOTENCY_IN_PROGRESS). Rows w/o `leaseOwner` = legacy: live ones block,
  expired ones are taken over, COMPLETE ones replay. No renew API exists (lease 60s, not extended).

## KNOWN ISSUES

- Gateway event envelope unconfirmed; no Lambda handler module yet (handler author calls `dispatch`).
- `get_audit_log` and `include_expired` are not in the Gateway contract, so the dispatcher does not expose them.
- Fake Dynamo evaluates only the condition/key expressions this store emits; verify against real DynamoDB once.

## NEXT 3 TASKS

1. Integration: build Lambda handler + asset per `.handoffs/bank-tools/2026-09-20-dynamo-dispatch-integration.md`.
2. Seed Dynamo from fixtures (qa-demo) and run one live smoke test of the conditional writes.
3. If throughput/consistency matters: version attribute on cases, TransactWriteItems for propose_* writes.

## LAST TEST COMMAND + RESULT

- `python3 -m pytest services/bank-tools/tests -q` -> 108 passed.
- `uv build services/bank-tools` -> wheel + sdist built.

## Workflow hardening (branch agent/agentcore/workflow-hardening)

- `dispatch.OUTCOME_SPECS`: update_case with optional `outcome` (CUSTOMER_RECOGNIZED_MERCHANT). Kept out of `SPECS` so the TS-contract drift test still passes; move it in when Infra adds the field.
- `propose_*` no longer queues a duplicate POLICY_REQUIRES_REVIEW request on a repeat call with a new idempotency key.
- `BankToolsStorage` protocol now lists `human_review_requests_for_case`.

## Investigator review decisions (branch agent/release/investigator-review-workflow)

- New 21st tool `submit_review_decision(case_id, review_id, actor, decision, reason, idempotency_key)`:
  APPROVE/REJECT/REQUEST_MORE_EVIDENCE against a PENDING `HumanReviewRequest` on a
  NEEDS_HUMAN_REVIEW case. Registered in `dispatch.SPECS`, `infra/config/tool-schemas.ts`,
  `infra/lambda/tools-adapter/router.TOOL_OWNERS`, and `scripts/acceptance.py`'s frozen-tool lists
  (all four had to move together or their respective drift tests fail).
- `HumanReviewRequest` now carries `reviewId` (stable key, defaulted so `escalate_case` in
  `services/agent/src/orchestrator/workflow.py` needed no change) and `status`
  (PENDING/RESOLVED). New `ReviewDecision` record, stored like a mutable entity (`replace_case`
  pattern) not an immutable audit-log row, since it has a PENDING -> RESOLVED lifecycle; Dynamo
  keys it by `reviewId` now instead of a fresh id per call.
- Every attempt is audited, not just successes: `record_audit` runs on each rejection path
  (`REVIEW_DECISION_REJECTED:<code>`) instead of relying on `_guarded` to swallow the exception.
- Known gap: case-row and review-row writes are two separate, non-transactional Dynamo puts; a
  crash between them leaves the review PENDING against a case that already moved, and a same-key
  retry then reports INVALID_TRANSITION instead of completing (same limitation as the rest of
  this store -- needs the version-attribute/TransactWriteItems fix already in NEXT TASKS #3).
- 161 tests now; `tests/test_review_decisions.py` covers approve, reject,
  request-more-evidence, replay, conflicting decision, invalid transition, malformed input,
  unknown review, and a simulated backend failure.
