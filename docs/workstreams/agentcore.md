# AgentCore workstream

## STATUS

PAYMENT_BLOCK_TRUST_BOUNDARY_COMPLETE (branch `agent/agentcore/payment-block-trust-boundary`, off `main`; local mocks + real bank-tools path; AWS adapters unverified live)

## DONE

- `services/agent` Python 3.12 package `orchestrator` (`pyproject.toml`, no runtime deps; `boto3` optional extra `aws`).
- `python3 src/orchestrator/main.py`: `THEMIS_MODE=local` reads JSON lines on stdin with mocks; `aws` serves `GET /ping` + `POST /invocations` on :8080.
- One orchestrator, structured `CaseState` persisted through `MemoryClient` (chat history is not state).
- Flow: intake -> matching -> confirmation -> classification -> investigation -> proposal -> policy -> action | human escalation -> report request.
- Deterministic prefilter (`prefilter.py`) before the model; model only extracts signals (<=1 inference/turn, output sanitized by `clean_analysis`).
- Budgets in `Config`: 8 model turns, 20 candidates, 5 history cases, 3 memory records, 5 research pages, 1 research call, 3 auth lookups.
- Policy: only `propose_*` tool results decide; non-`ALLOW` or malformed -> `escalate_case`. Tool failure after case exists -> escalate; before -> keep state, ask to retry. Reads/idempotent writes retried once; enrichment failures become MISSING_EVIDENCE.

## CURRENT INTERFACES (`src/orchestrator/interfaces.py`)

- `ModelClient.analyze(system, view, message, tier["fast"|"reasoning"]) -> dict`
- `GatewayClient.call(tool, arguments: camelCase dict) -> {"status":"ok"|"error", ...}` (tool contract not duplicated)
- `MemoryClient.load(conversation_id) / save(conversation_id, state) / recall(query, limit) -> list[str]`
- `MerchantResearch.research(descriptor, max_pages) -> {"summary","pages"} | None`
- `Orchestrator(model=, gateway=, memory=, research=, config=, today=).handle_turn(conversation_id, customer_id, message) -> Reply(text, status, case_id)`
- Mocks (`local.py`): `LocalGateway` (`demo()`, `fail=`, `outcomes=`), `ScriptedModel`, `HeuristicModel`, `InMemoryMemory`, `StubResearch`. AWS (`aws.py`): `BedrockModel`, `GatewayHTTPClient`, `AgentCoreMemory`, `NoResearch`.
- Config env: THEMIS_MODE, ENABLE_BROWSER_RESEARCH, BEDROCK_MODEL_ID_FAST/REASONING, GATEWAY_URL, MEMORY_ID, optional THEMIS_ESCALATION_CONFIDENCE (0.7).

## KNOWN ISSUES

- AWS adapters never run live (Gateway wire format, tool-name prefix, Memory API calls unverified).
- `update_case.outcome` and `escalate_case.summary/evidenceRefs` are sent only when `THEMIS_STRUCTURED_TOOLS=true` (Config.structured_tools, default off) because the Gateway schema lacks them; see `.handoffs/agentcore/2026-09-20-workflow-hardening-infra.md`. While off, Scenario C persists only locally (bank rejects early RESOLVED without outcome).
- Merchant research adapter not wired (`NoResearch`); uncached merchants escalate on low confidence.
- Scenario B ends RESOLVED with dispute + REQUEST_MERCHANT_EVIDENCE (no merchant reply handling yet). Scenario E verification = customer ledger via find_related_transactions + unreported-charge follow-up in the reply text.
- Status transition table is copied from `packages/contracts` (TS); keep in sync.
- Reasoning-tier routing only triggers when a later turn starts with confidence < threshold.

## HARDENING (this branch)

- `workflow.escalate_case`: one review per case+reason code; POLICY_* reuses the bank's POLICY_REQUIRES_REVIEW request; audit only on real change; evidenceRefs limited to stored evidence.
- Orchestrator calls `get_case` before policy and escalates CASE_STATE_MISMATCH if stored claimType/confidence/transactions differ.
- `Reply.suggestions` [{label, postback}] at transaction confirmation and the claim question; `_reply` adds `suggestions` only when non-empty.
- bank-tools `dispatch.OUTCOME_SPECS` = update_case + optional `outcome`, passed as `extra` until Infra adds it to SPECS.

## PAYMENT-BLOCK TRUST BOUNDARY (this branch)

- Fixed: `propose_payment_block`'s `customerRequested` was accepted as an authoritative model-supplied
  boolean. A claim of `true` is now only honored when the case has fresh (`<=24h`, `CUSTOMER_REQUEST_EVIDENCE_MAX_AGE_SECONDS`)
  `CUSTOMER_CLAIMS`/`PAYMENT_BLOCK_REQUESTED` evidence on file; a claim of `false` still passes through unchanged (never dangerous).
- Orchestrator (`engine.py`) captures the customer's literal message the turn `requested_block` first
  becomes true (`CaseState.block_request_message`) and persists it via `save_evidence` right before
  calling `propose_payment_block` (`CaseState.block_request_recorded` guards against duplicates).
- New trusted derivation: `bank_tools.policy.customer_requested_payment_block(evidence)`, enforced in
  `dispatch._adapt_payment_block` (rejects mismatched `true` claims with `CUSTOMER_REQUEST_UNVERIFIED`);
  Cedar text/comment and `tests/integration/test_policy_compat.py` already assumed this trusted-context
  shape, so no Cedar text change was needed.
- Contracts: added optional `Evidence.recordedAt` (staleness). Kept `services/merchant-intel/.../schemas.json`
  packaged copy in sync -- remember to re-copy it whenever `packages/contracts/schemas.json` changes.
- External tool surface unchanged (still the frozen 20+1 tools; `customerRequested` argument shape unchanged).

## NEXT 3 TASKS

1. Live-verify Gateway/Memory/Bedrock adapters once infra is deployed (one cheap call each).
2. Wire merchant-intel research adapter; add Scenario E proactive verification.
3. Consider an "explicit structured choice" (postback) origin for the block-request evidence, not just free-text messages.

## LAST TEST COMMAND + RESULT

- `python3 -m pytest -q services/agent services/bank-tools` -> 206 passed; `python3 -m pytest -q tests/integration` -> 28 passed;
  `python3 -m pytest -q tests/e2e` -> 42 passed; `npm run check` -> PASS; `python3.12 scripts/acceptance.py --mode full` -> PASS_WITH_MANUAL_GATES (13 PASS, 0 FAIL, 5 MANUAL/live-AWS)

## LAST CODE COMMIT

- See `git log -1` on `agent/agentcore/payment-block-trust-boundary`.
