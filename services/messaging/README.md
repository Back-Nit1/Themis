# Messaging boundary and Lambda deployment

Requires Node 22 or newer. Run `npm --prefix services/messaging run build` and
`npm --prefix services/messaging test` from the repository root. Build typechecks
the package and bundles the Lambda plus its runtime dependencies to
`services/messaging/dist/index.mjs`. Deploy that asset with handler
`index.handler`; source TypeScript is not the Lambda asset.

The deployable entry point is `src/aws-entry.ts`. It creates low-level DynamoDB
and Bedrock AgentCore clients, then calls `createMessagingRuntime`. It never
calls Bedrock model APIs. AgentCore SDK retries are disabled because a transport
failure may occur after downstream side effects. Tests use only injected clients
and never construct the entry point or make AWS calls.

Required Lambda environment:

| Variable | Requirement |
| --- | --- |
| `THEMIS_MODE` | Must be `aws`. |
| `IDEMPOTENCY_TABLE` | Dynamo table with string partition key `idempotencyKey`. |
| `AGENT_RUNTIME_ARN` | Full Bedrock AgentCore Runtime ARN. |
| `ENABLE_RCS` | Literal `true` or `false`. |
| `ENABLE_SMS_FALLBACK` | Literal `true` or `false`; at least one channel must be enabled. |
| `ENABLE_SES` | Literal `true` or `false`; defaults to `true` when absent. |
| `THEMIS_RCS_TOPIC_ARN` | Required when RCS is enabled. |
| `THEMIS_SMS_TOPIC_ARN` | Required when SMS is enabled and must differ from the RCS topic. |
| `THEMIS_RCS_POOL_ID` | Required when RCS is enabled; not read otherwise. |
| `THEMIS_SMS_IDENTITY` | Required when SMS fallback is enabled; not read otherwise. |
| `THEMIS_SES_FROM` / `THEMIS_SUPPORT` | Required when SES is enabled; not read otherwise. |

Optional `ACTIVE_MENU_TABLE` selects a separate table with the same
`idempotencyKey` partition key; it defaults to `IDEMPOTENCY_TABLE`.
`AGENT_RUNTIME_QUALIFIER` targets a non-default AgentCore endpoint.

`createMessaging({env, consume, ...})` exposes `inbound`, `channel`, `email`, `sender`, and `support`. `consume` receives only validated shared `InboundMessage` data. Local mode (the default or `THEMIS_MODE=local`) captures outbound payloads in `channel.captured` and `email.captured`, even if AWS clients are supplied. Inject fake messages with `inbound.process(normalizeInbound(payload, channel, timestamp))`.

`createSnsHandler({topics, processor, choicesFor})` returns a Lambda-compatible SNS handler. Map trusted SNS topic ARNs to RCS/SMS explicitly: the provider payload cannot reliably distinguish plain RCS from SMS. `createMessagingRuntime` performs this mapping from the validated environment, uses `DynamoIdempotencyStore`, resolves the customer's durable active menu, and invokes an injected `AgentRuntimeClient`. Only the strict shared `InboundMessage` JSON is placed in the AgentCore payload.

`MemoryActiveMenuStore` supports local tests. `DynamoActiveMenuStore` persists one active menu per customer with its `caseId` and ordered choices. `clear(customerExternalId, caseId)` is conditional, so an old case cannot remove a newer case's menu. Numeric and textual SMS replies are interpreted only against this recovered menu. Every Dynamo `Item` or `Key` uses the `idempotencyKey` partition key; customer lookup keys are SHA-256 hashes.

AWS mode requires injected clients and a durable `IdempotencyStore`. `THEMIS_RCS_POOL_ID` is required only when `ENABLE_RCS=true`; `THEMIS_SMS_IDENTITY` only when `ENABLE_SMS_FALLBACK=true`; `THEMIS_SES_FROM`/`THEMIS_SUPPORT` only when `ENABLE_SES=true` (default). A disabled transport's identity is never required and its client/adapter is never constructed; a call that targets a disabled transport (e.g. `send_case_email` with SES off) fails explicitly instead of silently falling back to another channel. Use an RCS+SMS pool for text fallback. The SMS identity must be a phone number/sender ID (value, ID or ARN), not a pool, because rich RCS per-message fallback disallows pools. Region/credentials are configured on the injected SDK clients. Resource creation is owned by Infra.

The client interfaces accept exact AWS request payloads. Bind AWS SDK v3 operations at the composition root:

```js
const messagingClient = {
  sendTextMessage: input => smsVoice.send(new SendTextMessageCommand(input)),
  sendRcsMessage: input => smsVoice.send(new SendRcsMessageCommand(input)),
};
const sesClient = { sendEmail: input => sesV2.send(new SendEmailCommand(input)) };
const dynamoClient = {
  putItem: input => dynamo.send(new PutItemCommand(input)),
  updateItem: input => dynamo.send(new UpdateItemCommand(input)),
};
const store = new DynamoIdempotencyStore(tableName, dynamoClient);
```

The deployable composition depends on `@aws-sdk/client-dynamodb` and
`@aws-sdk/client-bedrock-agentcore`; the build bundles them. Integration must run
the root `npm install` and commit the root lockfile before merge. The messaging
worker intentionally does not edit that integration-owned file.

Dynamo idempotency uses atomic conditional claims. Claims do not expire or automatically release after failure: partial downstream effects must not execute twice. A runtime failure is surfaced from the Lambda while its `PROCESSING` claim remains; an SNS retry is admitted as a duplicate and cannot invoke AgentCore again. The scheduled reconciler (below) resumes stranded claims from their delivery record and flags REVIEW/QUARANTINED; it never replays AgentCore. This is at-most-once admission, not an exactly-once transaction across systems.

## Claim reconciler (scheduled Lambda `reconciler.handler`, bundle `dist/reconciler.mjs`)

Closes failures between claim acquisition and a durable delivery record (Lambda death/timeout, AgentCore timeout, Dynamo error before `reply:<messageId>` is recorded, send failures outliving SNS redelivery). It **never invokes AgentCore and never deletes a claim**. Claims carry `customerExternalId/messageId/channel` and a sparse `claimState` attribute indexed by `ClaimStateIndex` (`claimState`,`claimedAt`, KEYS_ONLY), so the reconciler Queries instead of Scanning.

| Claim state | Meaning |
|---|---|
| `PROCESSING` | admitted; only this state is reconciled |
| `COMPLETED` | reply settled (by the normal path or by resuming its recorded delivery) |
| `REVIEW` | customer got the deterministic notice, but AgentCore/case effects are unknown — operator verifies the case |
| `QUARANTINED` | reconciler cannot act (`MISSING_IDENTITY` legacy claim, or `RETRIES_EXHAUSTED`); the customer may be unnotified |

Per stale claim, under a lease (`leaseOwner/leaseUntil`, every transition conditional on `PROCESSING` + owner): recorded reply → resume its send (`COMPLETED`, or `REVIEW` if the record is the ambiguity notice); no record → send the notice under the normal `reply:<id>` delivery key (put-if-absent, so a customer never gets two answers) → `REVIEW`; transient error → lease held as backoff, attempt counted; attempts > max → `QUARANTINED`. The normal path's `complete` is conditional, so a slow original attempt cannot overwrite a reconciler decision.

Bounds (env, validated): `RECONCILE_STALE_SECONDS` 300 (60–86400; keep ≫ inbound Lambda timeout), `RECONCILE_LEASE_SECONDS` 240, `RECONCILE_BATCH_SIZE` 25 (≤100), `RECONCILE_MAX_ATTEMPTS` 3, `RECONCILE_BUDGET_SECONDS` 40. Also needs `IDEMPOTENCY_TABLE`, `ENABLE_RCS`, `ENABLE_SMS_FALLBACK`, and each channel's identity only when that channel is enabled. Logs are EMF JSON (namespace `Themis/Messaging`: Scanned, Completed, Review, Quarantined, Retry, Contended, Deferred, DurationMs) with only a 12-char claim-hash prefix, outcome and reason — no phone numbers, message ids or text.

### Operator runbook for `REVIEW` / `QUARANTINED`
```
aws dynamodb query --table-name ThemisIdempotency --index-name ClaimStateIndex \
  --key-condition-expression 'claimState = :s' --expression-attribute-values '{":s":{"S":"QUARANTINED"}}'
```
Inspect the claim (`reason`, `resolvedAt`, identity fields) and the customer's case in the dashboard/audit log. Then close it conditionally so it leaves the index:
```
aws dynamodb update-item --table-name ThemisIdempotency --key '{"idempotencyKey":{"S":"<claimId>"}}' \
  --update-expression 'SET #s = :done REMOVE claimState' --condition-expression '#s = :from' \
  --expression-attribute-names '{"#s":"state"}' --expression-attribute-values '{":done":{"S":"COMPLETED"},":from":{"S":"QUARANTINED"}}'
```
Only if you have verified AgentCore did **not** act may the claim be deleted (which re-admits the message on an SNS redelivery). Ceilings: one index partition per state; legacy claims written before this change have no `claimState` and are never listed (handle by hand); if a slow original Lambda outlives the stale threshold its real reply can lose to the notice.

`caseEmail({case, report, recipient, sender, support, nextSteps})` produces an SES v2 request using structured facts. Case/report identities, transaction membership, currency, totals, and customer ownership are checked. Only `actionsTaken` are presented as completed actions. `nextSteps` must be supplied by deterministic case workflow; the renderer invents none. SES sender identities must be verified; sandbox accounts additionally restrict recipients. Acceptance IDs are not delivery receipts. Infra must configure delivery monitoring/DLQs and SNS subscription permissions. Use the Lambda subscription boundary, not an unauthenticated SNS HTTP endpoint.

Official request references: [inbound SNS](https://docs.aws.amazon.com/sms-voice/latest/userguide/rcs-inbound.html), [suggestion replies](https://docs.aws.amazon.com/sms-voice/latest/userguide/rcs-suggestions.html), [rich RCS fallback](https://docs.aws.amazon.com/sms-voice/latest/userguide/rcs-rich-messaging.html), [conditional Dynamo claims](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_PutItem.html), [SES v2](https://docs.aws.amazon.com/ses/latest/APIReference-V2/API_SendEmail.html).
