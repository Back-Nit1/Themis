# Messaging workstream

## STATUS

COMPLETE — runtime composition + menu lifecycle + delivery reliability + capability-gated deployment implemented; Infra wiring/deploy pending.

## DONE

- Validated shared RCS/SMS InboundMessage normalization from AWS SNS payloads.
- RCS suggestion JSON, active-menu numeric/text SMS replies share canonical postbacks.
- Atomic memory/Dynamo idempotency prevents repeated downstream admission.
- Deployable SNS Lambda bundle validates environment and invokes AgentCore only.
- Durable memory/Dynamo active menus preserve customer/case choice context.
- RCS text/suggestions, SMS menus and per-message SMS fallback payloads.
- Deterministic Case/CaseReport email renderer and SES v2 adapter.
- Local inbound injection and captured outbound/email payloads make zero AWS calls.
- External configuration and injected AWS clients; all tests remain AWS-free.

## MENUS + DELIVERY (agent/messaging/menus-reliability)

- Runtime reply `{reply,status,caseId|null,suggestions?}`; suggestions validated (`parseSuggestions`), invalid = dropped.
- Menu: one per customer, carries `caseId` + deterministic `menuId=menu:<messageId>`; persisted before send;
  cleared conditionally on answer (caseId+menuId), terminal status (caseId), never by stale id.
- `OutboundService`: delivery record (PENDING/ACCEPTED/FAILED/DELIVERED/UNDELIVERABLE) keyed by customer+messageId;
  duplicate inbound resumes send from the record, never reruns AgentCore. RCS disabled = numbered SMS.
- `createDeliveryEventHandler` on `THEMIS_DELIVERY_EVENT_TOPIC_ARN`; inbound topics reject delivery events and v.v.
- `sendCaseNotification`: SES failure returns `{status:'FAILED'}`, recorded, case untouched.
- `reconcileClaims`/`createReconciler` (src/reconciler.ts): lease-protected, bounded; claim states PROCESSING→COMPLETED|REVIEW|QUARANTINED; wired in messaging-stack.ts (ThemisClaimReconciler, ClaimStateIndex).

## CAPABILITY GATING (agent/messaging/capability-gated-config)

- `parseRuntimeEnvironment` exposes `rcsEnabled`/`smsEnabled`/`sesEnabled`; each identity (`THEMIS_RCS_POOL_ID`,
  `THEMIS_SMS_IDENTITY`, `THEMIS_SES_FROM`/`THEMIS_SUPPORT`) required only when its flag is enabled and only
  when outbound sending is actually composed (a `messagingClient`/`sesClient` is injected).
- `handleTool` fails explicitly (`Messaging transports are disabled`, `SES is disabled`) instead of silently
  no-op'ing or falling back when a call targets a disabled transport.
- `createReconciler` mirrors the same per-flag identity gating and the RCS-or-SMS invariant.
- Infra (`infra/config/env.ts`, `messaging-stack.ts`): identities/env vars/IAM (`sms-voice:Send*`, `ses:SendEmail`)
  wired only for enabled capabilities; `validateDeploymentPreflight` checks each identity only when enabled.
- `.env.example`/READMEs updated to document per-capability requirements; no secrets added.

## CURRENT INTERFACES

- `dist/index.mjs` exports Lambda handler `index.handler`; source is `src/aws-entry.ts`.
- `createMessagingRuntime` composes trusted SNS topics, Dynamo admission/menu stores,
  and an injected `AgentRuntimeClient`.
- `MemoryActiveMenuStore` and `DynamoActiveMenuStore` track ordered menus by
  customer and active case; all Dynamo keys use `idempotencyKey`.
- Shared contracts unchanged; canonical choice intent uses InboundMessage.postback.
- Client/resource binding details: services/messaging/README.md.
- Wiring/dependency requests: .handoffs/messaging/2026-09-20-*.md.

## KNOWN ISSUES

- AWS calls are mocked; live resource provisioning/delivery verification is pending.
- Ambiguous processing keeps claims reserved; scheduled reconciler (see README) resumes/notifies/flags REVIEW|QUARANTINED, never replays AgentCore.
- Concurrent redeliveries of one reply can double-send (no send lock); ponytail ceiling.
- Crash between recording a reply and persisting its menu is not repaired on resume.
- Legacy claims (no identity/claimState) are invisible to the reconciler; no alarm on QUARANTINED yet (metric only).
- At-most-once admission does not guarantee completion across external side effects.
- Root package lock does not yet contain the two declared AWS SDK clients.

## NEXT 3 TASKS

1. Integration runs root npm install and commits the root lockfile.
2. Infra deploys `dist/index.mjs` with capability-gated env/permissions already wired here, plus DLQ.
3. QA smoke-tests AWS incl. reconciler schedule; optional CloudWatch alarm on Quarantined>0.

## LAST TEST COMMAND + RESULT

- `npm --prefix services/messaging run build` — PASS.
- `npm --prefix services/messaging test` — PASS, 68/68 tests.
- `npm test --workspace=infra` (incl. `cdk synth`) — PASS, 53/53 tests.
- `npm run check` (full repo build+test+python+integration+e2e) — PASS.
- `npm run acceptance:json` — 13 PASS, 0 FAIL, 5 MANUAL (live-AWS only).
- `git diff --check` — PASS. All AWS clients mocked; no live AWS calls made.
