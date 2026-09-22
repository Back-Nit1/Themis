import type { AuditEvent, Case, CaseReport, HumanReviewRequest, MerchantProfile } from '@themis/contracts';
import type { DataProvider, DataSource, DataStatus, ReviewDecisionInput, ReviewDecisionResult } from './provider';

export type { DataStatus, ReviewDecisionInput, ReviewDecisionResult } from './provider';
export { DataSourceError } from './provider';

let provider: DataProvider | undefined;

function selectedSource(env: Record<string, string | undefined>): DataSource {
  const value = env.THEMIS_DASHBOARD_DATA_SOURCE?.trim() || 'fixtures';
  if (value !== 'fixtures' && value !== 'aws') throw new Error('THEMIS_DASHBOARD_DATA_SOURCE must be fixtures or aws');
  return value;
}

/** Server-side provider chosen by THEMIS_DASHBOARD_DATA_SOURCE (default: fixtures). AWS code is loaded only in aws mode. */
export async function getProvider(env: Record<string, string | undefined> = process.env): Promise<DataProvider> {
  if (provider) return provider;
  if (selectedSource(env) === 'fixtures') return (provider = (await import('./fixtures-provider')).fixturesProvider);
  const { awsConfigFromEnv } = await import('./aws-config');
  const config = awsConfigFromEnv(env);
  const { createAwsProvider } = await import('./aws-provider');
  return (provider = createAwsProvider(config));
}

/** Test seam: pass a provider to force it, or nothing to re-read the environment. */
export function setDataProvider(next?: DataProvider): void {
  provider = next;
}

/** Frontend data boundary: every UI component reads through here, never fixtures or AWS directly. */
export async function listCases(): Promise<Case[]> {
  return (await getProvider()).listCases();
}

export async function getCase(caseId: string): Promise<Case | undefined> {
  return (await getProvider()).getCase(caseId);
}

export async function getCaseReport(caseId: string): Promise<CaseReport | undefined> {
  return (await getProvider()).getCaseReport(caseId);
}

export async function listMerchantProfiles(): Promise<MerchantProfile[]> {
  return (await getProvider()).listMerchantProfiles();
}

export async function getMerchantProfile(merchantId: string): Promise<MerchantProfile | undefined> {
  return (await getProvider()).getMerchantProfile(merchantId);
}

export async function auditEventsForCase(caseId: string): Promise<AuditEvent[]> {
  return (await getProvider()).listAuditEvents(caseId);
}

/** The only dashboard path that may change a case's status; route handlers call this, never components directly. */
export async function submitReviewDecision(input: ReviewDecisionInput): Promise<ReviewDecisionResult> {
  return (await getProvider()).submitReviewDecision(input);
}

/** Read after the page's own data reads so it reflects them (stale copies, skipped records). */
export async function dataStatus(): Promise<DataStatus> {
  return (await getProvider()).status();
}

export async function casesForMerchant(merchantId: string): Promise<Case[]> {
  return (await listCases()).filter((c) => c.merchantId === merchantId);
}

export interface ReviewQueueEntry {
  case: Case;
  request: HumanReviewRequest | undefined;
  report: CaseReport | undefined;
  merchantName: string;
}

export async function listReviewQueue(): Promise<ReviewQueueEntry[]> {
  const p = await getProvider();
  const [cases, requests] = await Promise.all([p.listCases(), p.listHumanReviewRequests()]);
  return Promise.all(cases.filter((c) => c.requiresHumanReview).map(async (c) => {
    const report = await p.getCaseReport(c.caseId);
    return {
      case: c,
      request: requests.find((r) => r.caseId === c.caseId),
      report,
      merchantName: report?.merchant?.canonicalName ?? 'Unmatched merchant',
    };
  }));
}

const NINETY_DAYS_MS = 90 * 24 * 60 * 60 * 1000;

export function commonDisputeType(cases: readonly Case[]): string | undefined {
  const counts = new Map<string, number>();
  for (const c of cases) counts.set(c.claimType, (counts.get(c.claimType) ?? 0) + 1);
  return [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
}

/** Fixtures are frozen in time, so "recent" is relative to their newest case; live data is relative to now. */
export async function referenceTime(): Promise<Date> {
  const p = await getProvider();
  if (p.source === 'aws') return new Date();
  const cases = await p.listCases();
  return new Date(cases.reduce((latest, c) => (c.updatedAt > latest ? c.updatedAt : latest), cases[0]?.updatedAt ?? new Date().toISOString()));
}

export function recentCaseCount(cases: readonly Case[], now: Date): number {
  const cutoff = now.getTime() - NINETY_DAYS_MS;
  return cases.filter((c) => new Date(c.createdAt).getTime() >= cutoff).length;
}
