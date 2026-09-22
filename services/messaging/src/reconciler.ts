import { OutboundMessageSchema } from '../../../packages/contracts/src/index.ts';
import { DynamoActiveMenuStore, type ActiveMenuDynamoClient } from './active-menu.ts';
import { DynamoDeliveryStore, type DeliveryDynamoClient } from './delivery-store.ts';
import { OutboundService } from './delivery.ts';
import { DynamoClaimReconciler, type ClaimDynamoClient } from './idempotency.ts';
import type { ClaimReconciler, ClaimResolution } from './inbound.ts';
import { ChannelAdapter, type MessagingClient } from './outbound.ts';
import type { DeliveryStore } from './delivery-store.ts';

export const AMBIGUOUS_RUNTIME_FAILURE_REPLY =
  "I'm having trouble completing that request. To avoid duplicate account actions, I haven't retried it. Please contact support if you need immediate help.";

export interface ReconcileOptions {
  readonly claims: ClaimReconciler;
  readonly outbound: OutboundService;
  readonly deliveries: DeliveryStore;
  readonly owner: string;
  /** Claims younger than this are left alone: it must exceed the inbound Lambda timeout so the original attempt is dead. */
  readonly staleMs: number;
  readonly leaseMs: number;
  readonly batchSize: number;
  readonly maxAttempts: number;
  /** Stop starting new claims after this much runtime. */
  readonly budgetMs: number;
  readonly now?: () => Date;
  /** Receives one PII-free object per claim plus one summary; defaults to a JSON line on stdout. */
  readonly log?: (entry: Record<string, unknown>) => void;
}

export interface ReconcileSummary {
  scanned: number; completed: number; review: number; quarantined: number; retry: number; contended: number; deferred: number; durationMs: number;
}

const replyId = (messageId: string) => `reply:${messageId}`;

/**
 * One bounded pass over stale PROCESSING claims. It never invokes AgentCore and never deletes a claim, so it cannot replay
 * financial work. Per claim (under a lease, all transitions conditional on that lease):
 *  - reply record exists       -> resume its send from the record; COMPLETED, or REVIEW if it is only the ambiguity notice
 *  - no reply record           -> AgentCore outcome unknown: send the deterministic notice; REVIEW
 *  - no identity / attempts out -> QUARANTINED (customer possibly unnotified; operator decides)
 *  - transient failure          -> lease is kept as backoff; counted attempt
 */
export async function reconcileClaims(o: ReconcileOptions): Promise<ReconcileSummary> {
  const now = o.now ?? (() => new Date());
  const log = o.log ?? (entry => console.log(JSON.stringify(entry)));
  const started = now().getTime();
  const summary: ReconcileSummary = { scanned: 0, completed: 0, review: 0, quarantined: 0, retry: 0, contended: 0, deferred: 0, durationMs: 0 };
  const refs = await o.claims.listStale(new Date(started - o.staleMs), o.batchSize);
  for (const [i, ref] of refs.entries()) {
    if (now().getTime() - started >= o.budgetMs) { summary.deferred = refs.length - i; break; }
    summary.scanned++;
    const claim = await o.claims.lease(ref.claimId, o.owner, now(), o.leaseMs);
    if (!claim) { summary.contended++; continue; }
    // Hash prefix only: enough to correlate with the operator index listing, useless for identifying a customer.
    const entry: Record<string, unknown> = { event: 'claim_reconciled', claim: ref.claimId.slice(0, 12), attempts: claim.attempts, ageMs: started - Date.parse(claim.claimedAt) };
    try {
      let to: ClaimResolution; let reason: string;
      if (!claim.meta) { to = 'QUARANTINED'; reason = 'MISSING_IDENTITY'; }
      else if (claim.attempts > o.maxAttempts) { to = 'QUARANTINED'; reason = 'RETRIES_EXHAUSTED'; }
      else {
        const { customerExternalId, messageId, channel } = claim.meta;
        const record = await o.deliveries.get(customerExternalId, replyId(messageId));
        if (record?.message) {
          await o.outbound.resume(customerExternalId, replyId(messageId));
          [to, reason] = record.message.text === AMBIGUOUS_RUNTIME_FAILURE_REPLY ? ['REVIEW', 'AMBIGUOUS_AGENT_FAILURE'] : ['COMPLETED', 'REPLY_SETTLED'];
        } else {
          // Same delivery key as a real reply: whichever record is created first wins, so the customer never gets two answers.
          await o.outbound.deliver(OutboundMessageSchema.parse({ channel, customerExternalId, messageId: replyId(messageId),
            caseId: 'no-case-yet', text: AMBIGUOUS_RUNTIME_FAILURE_REPLY }), true);
          to = 'REVIEW'; reason = 'NO_REPLY_RECORD';
        }
      }
      const moved = await o.claims.finish(ref.claimId, o.owner, to, reason);
      Object.assign(entry, { outcome: moved ? to : 'LEASE_LOST', reason });
      if (!moved) summary.contended++;
      else if (to === 'COMPLETED') summary.completed++; else if (to === 'REVIEW') summary.review++; else summary.quarantined++;
    } catch (error) {
      summary.retry++;
      Object.assign(entry, { outcome: 'RETRY', errorName: error instanceof Error ? error.name : 'Unknown' });
    }
    log(entry);
  }
  summary.durationMs = now().getTime() - started;
  // CloudWatch Embedded Metric Format: the same line is both the log record and the metrics.
  log({ _aws: { Timestamp: now().getTime(), CloudWatchMetrics: [{ Namespace: 'Themis/Messaging', Dimensions: [[]],
    Metrics: (Object.keys(summary) as (keyof ReconcileSummary)[]).map(name => ({ Name: name[0].toUpperCase() + name.slice(1), Unit: name === 'durationMs' ? 'Milliseconds' : 'Count' })) }] },
    event: 'reconcile_summary', ...Object.fromEntries(Object.entries(summary).map(([k, v]) => [k[0].toUpperCase() + k.slice(1), v])) });
  return summary;
}

export interface ReconcilerDependencies {
  readonly dynamo: ClaimDynamoClient & DeliveryDynamoClient & ActiveMenuDynamoClient;
  readonly messagingClient: MessagingClient;
  readonly owner: string;
  readonly now?: () => Date;
  readonly log?: ReconcileOptions['log'];
}

function bounded(env: Record<string, string | undefined>, name: string, fallback: number, min: number, max: number): number {
  const raw = env[name]?.trim();
  const value = raw ? Number(raw) : fallback;
  if (!Number.isInteger(value) || value < min || value > max) throw new Error(`${name} must be an integer in [${min}, ${max}]`);
  return value;
}

/** Composes the reconciler Lambda: no AgentCore client, no SES client — it cannot reach either by construction. */
export function createReconciler(env: Record<string, string | undefined>, d: ReconcilerDependencies) {
  const table = env.IDEMPOTENCY_TABLE?.trim();
  if (!table) throw new Error('Missing IDEMPOTENCY_TABLE');
  const enableRcs = env.ENABLE_RCS?.trim() === 'true';
  const enableSms = env.ENABLE_SMS_FALLBACK?.trim() === 'true';
  if (!enableRcs && !enableSms) throw new Error('At least one inbound messaging channel must be enabled');
  // Each identity is required only when its transport is enabled: a disabled transport must not force a
  // dummy identifier.
  if (enableRcs && !env.THEMIS_RCS_POOL_ID?.trim()) throw new Error('Missing THEMIS_RCS_POOL_ID');
  if (enableSms && !env.THEMIS_SMS_IDENTITY?.trim()) throw new Error('Missing THEMIS_SMS_IDENTITY');
  const rcsIdentity = enableRcs ? env.THEMIS_RCS_POOL_ID!.trim() : '';
  const smsIdentity = enableSms ? env.THEMIS_SMS_IDENTITY!.trim() : '';
  const deliveries = new DynamoDeliveryStore(table, d.dynamo);
  const outbound = new OutboundService({ channel: new ChannelAdapter({ mode: 'aws', rcsIdentity, smsIdentity }, d.messagingClient),
    menus: new DynamoActiveMenuStore(table, d.dynamo), deliveries, rcsEnabled: enableRcs });
  const options: ReconcileOptions = {
    claims: new DynamoClaimReconciler(table, d.dynamo), outbound, deliveries, owner: d.owner,
    staleMs: bounded(env, 'RECONCILE_STALE_SECONDS', 300, 60, 86_400) * 1000,
    leaseMs: bounded(env, 'RECONCILE_LEASE_SECONDS', 240, 30, 900) * 1000,
    batchSize: bounded(env, 'RECONCILE_BATCH_SIZE', 25, 1, 100),
    maxAttempts: bounded(env, 'RECONCILE_MAX_ATTEMPTS', 3, 1, 10),
    budgetMs: bounded(env, 'RECONCILE_BUDGET_SECONDS', 40, 5, 600) * 1000,
    ...(d.now ? { now: d.now } : {}), ...(d.log ? { log: d.log } : {}),
  };
  return { options, run: () => reconcileClaims(options) };
}
