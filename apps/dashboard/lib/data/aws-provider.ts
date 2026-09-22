import {
  CaseSchema, CaseReportSchema, MerchantProfileSchema, HumanReviewRequestSchema, AuditEventSchema,
} from '@themis/contracts';
import { defaultAwsAccess, type DynamoAccess, type ObjectAccess } from './aws-access';
import type { AwsConfig } from './aws-config';
import {
  DataSourceError, type DataProvider, type DataStatus, type ReviewDecisionInput, type ReviewDecisionResult,
} from './provider';

// Row layout is owned by bank-tools (services/bank-tools/src/bank_tools/dynamo_store.py):
// cases: flat Case row keyed caseId ("E#..." rows are evidence); merchants: "P#<id>" {doc} profile rows;
// audit: {caseId, eventId, doc} with "audit_" events and "review_" human-review requests; reports: S3 reports/<caseId>.json.
const ID = /^[A-Za-z0-9_.-]+$/;
const MAX_CACHED = 500;

type Schema<T> = { safeParse(value: unknown): { success: true; data: T } | { success: false } };

interface Snapshot { value: unknown; fetchedAt: string }

export function createAwsProvider(
  config: AwsConfig,
  access: { dynamo: DynamoAccess; objects: ObjectAccess } = defaultAwsAccess(),
  now: () => Date = () => new Date(),
): DataProvider {
  const lastGood = new Map<string, Snapshot>();
  const stale = new Map<string, string>(); // key -> fetchedAt of the copy being served
  const skipped = new Map<string, number>();

  /** Fresh read, else the last good copy (flagged stale), else a DataSourceError. */
  async function read<T>(key: string, load: () => Promise<T>): Promise<T> {
    try {
      const value = await load();
      lastGood.delete(key); // re-insert so eviction drops the least recently refreshed
      lastGood.set(key, { value, fetchedAt: now().toISOString() });
      if (lastGood.size > MAX_CACHED) lastGood.delete(lastGood.keys().next().value!);
      stale.delete(key);
      return value;
    } catch (cause) {
      const copy = lastGood.get(key);
      if (!copy) throw new DataSourceError(`Backend read failed: ${key}`, { cause });
      stale.set(key, copy.fetchedAt);
      return copy.value as T;
    }
  }

  function parseRows<T>(key: string, schema: Schema<T>, rows: unknown[]): T[] {
    const valid = rows.flatMap((row) => { const r = schema.safeParse(row); return r.success ? [r.data] : []; });
    skipped.set(key, rows.length - valid.length);
    return valid;
  }
  const docs = (rows: Record<string, unknown>[]) => rows.map((r) => r.doc);
  const checkId = (id: string) => ID.test(id);

  return {
    source: 'aws',
    listCases: () => read('cases', async () => parseRows('cases', CaseSchema,
      await access.dynamo.scan(config.casesTable, 'NOT begins_with(caseId, :evidence)', { ':evidence': 'E#' }))),
    getCase: (caseId) => !checkId(caseId) ? Promise.resolve(undefined) : read(`case:${caseId}`, async () => {
      const row = await access.dynamo.get(config.casesTable, { caseId });
      return row ? CaseSchema.parse(row) : undefined;
    }),
    getCaseReport: (caseId) => !checkId(caseId) ? Promise.resolve(undefined) : read(`report:${caseId}`, async () => {
      const text = await access.objects.getText(config.artifactsBucket, `reports/${caseId}.json`);
      if (text === undefined) return undefined;
      const report = CaseReportSchema.parse(JSON.parse(text));
      if (report.caseId !== caseId) throw new Error('Report caseId mismatch');
      return report;
    }),
    listMerchantProfiles: () => read('merchants', async () => parseRows('merchants', MerchantProfileSchema,
      docs(await access.dynamo.scan(config.merchantsTable, 'begins_with(merchantId, :profile)', { ':profile': 'P#' })))),
    getMerchantProfile: (merchantId) => !checkId(merchantId) ? Promise.resolve(undefined) : read(`merchant:${merchantId}`, async () => {
      const row = await access.dynamo.get(config.merchantsTable, { merchantId: `P#${merchantId}` });
      return row ? MerchantProfileSchema.parse(row.doc) : undefined;
    }),
    listHumanReviewRequests: () => read('reviews', async () => parseRows('reviews', HumanReviewRequestSchema,
      docs(await access.dynamo.scan(config.auditTable, 'begins_with(eventId, :review)', { ':review': 'review_' })))),
    listAuditEvents: (caseId) => !checkId(caseId) ? Promise.resolve([]) : read(`audit:${caseId}`, async () => {
      const events = parseRows(`audit:${caseId}`, AuditEventSchema, docs(await access.dynamo.query(
        config.auditTable, 'caseId = :c AND begins_with(eventId, :audit)', { ':c': caseId, ':audit': 'audit_' })));
      return events.sort((a, b) => a.timestamp.localeCompare(b.timestamp));
    }),
    // Blocked, not hand-rolled: writing a review decision means claiming an idempotency lease,
    // updating the case row, resolving the review row and appending a decision row, all against
    // the same conditional-write protocol services/bank-tools/dynamo_store.py owns. Reimplementing
    // that here in TypeScript would be a second, easy-to-drift copy of the state machine. The real
    // path is the Gateway -> bank_tools.dispatch("submit_review_decision", ...) once the Lambda
    // handler exists (services/bank-tools KNOWN ISSUES: "no Lambda handler module yet"). Until
    // then this reports the blocker instead of mutating AWS state through an unreviewed path.
    submitReviewDecision(_input: ReviewDecisionInput): Promise<ReviewDecisionResult> {
      return Promise.resolve({
        status: 'error',
        error: {
          code: 'NOT_IMPLEMENTED',
          message: 'AWS-backed review decisions require the Gateway/Lambda wiring for submit_review_decision, '
            + 'which is not deployed yet. Use THEMIS_DASHBOARD_DATA_SOURCE=fixtures, or decide through bank-tools directly.',
        },
      });
    },
    status(): DataStatus {
      const staleSince = [...stale.values()].sort()[0];
      return { source: 'aws', stale: stale.size > 0, ...(staleSince ? { staleSince } : {}),
        skippedRecords: [...skipped.values()].reduce((a, b) => a + b, 0) };
    },
  };
}
