import {
  CaseSchema, CaseReportSchema, MerchantProfileSchema, HumanReviewRequestSchema, AuditEventSchema,
  REVIEW_DECISION_TARGET_STATUS, type Case, type HumanReviewRequest, type ReviewDecision,
} from '@themis/contracts';
import type { DataProvider, ReviewDecisionInput, ReviewDecisionResult } from './provider';

import casesRaw from '../../../../fixtures/cases/demo.json';
import reportsRaw from '../../../../fixtures/cases/demo-reports.json';
import merchantProfilesRaw from '../../../../fixtures/merchants/demo-profiles.json';
import humanReviewRaw from '../../../../fixtures/cases/demo-human-review-requests.json';
import auditEventsRaw from '../../../../fixtures/cases/demo-audit-events.json';

const initialCases = CaseSchema.array().parse(casesRaw);
const initialReviews = HumanReviewRequestSchema.array().parse(humanReviewRaw);
const reports = CaseReportSchema.array().parse(reportsRaw);
const merchantProfiles = MerchantProfileSchema.array().parse(merchantProfilesRaw);
const initialAuditEvents = AuditEventSchema.array().parse(auditEventsRaw);

// Mutable in-process state for the review-decision workflow (prompt.md investigator review).
// `resetFixtures` gives tests a clean slate; nothing here ever reaches a browser bundle -- this
// module is only ever imported from server-side code (route handlers, RSC pages).
let cases: Case[];
let humanReviewRequests: HumanReviewRequest[];
let reviewDecisions: ReviewDecision[];
let auditEvents: typeof initialAuditEvents;
const idempotency = new Map<string, ReviewDecisionResult>();
let nextId = 0;

function reset(): void {
  cases = initialCases.map((c) => ({ ...c }));
  humanReviewRequests = initialReviews.map((r) => ({ ...r }));
  reviewDecisions = [];
  auditEvents = [...initialAuditEvents];
  idempotency.clear();
  nextId = 0;
}
reset();

/** Test-only seam: undo mutations made by submitReviewDecision between tests. */
export function resetFixtures(): void {
  reset();
}

function fail(code: string, message: string): ReviewDecisionResult {
  return { status: 'error', error: { code, message } };
}

async function submitReviewDecision(input: ReviewDecisionInput): Promise<ReviewDecisionResult> {
  const cached = idempotency.get(input.idempotencyKey);
  if (cached) return cached;

  const record = (result: ReviewDecisionResult): ReviewDecisionResult => {
    if (result.status === 'ok') idempotency.set(input.idempotencyKey, result);
    auditEvents.push({
      eventId: `audit_${++nextId}`, caseId: input.caseId, timestamp: new Date().toISOString(),
      actor: input.actor, action: result.status === 'ok' ? `REVIEW_DECISION:${input.decision}` : `REVIEW_DECISION_REJECTED:${result.error.code}`,
      tool: 'submit_review_decision', result: result.status === 'ok' ? 'SUCCESS' : 'FAILURE',
    });
    return result;
  };

  const kase = cases.find((c) => c.caseId === input.caseId);
  if (!kase) return record(fail('NOT_FOUND', `case not found: ${input.caseId}`));
  const review = humanReviewRequests.find((r) => r.reviewId === input.reviewId && r.caseId === input.caseId);
  if (!review) return record(fail('NOT_FOUND', `human review request not found: ${input.reviewId}`));
  if (review.status !== 'PENDING') return record(fail('REVIEW_ALREADY_DECIDED', `review ${input.reviewId} was already decided`));
  if (kase.status !== 'NEEDS_HUMAN_REVIEW') {
    return record(fail('INVALID_TRANSITION', `case ${input.caseId} is not awaiting human review (status=${kase.status})`));
  }

  const nextStatus = REVIEW_DECISION_TARGET_STATUS[input.decision];
  const updatedCase: Case = { ...kase, status: nextStatus, requiresHumanReview: false, updatedAt: new Date().toISOString() };
  const resolvedReview: HumanReviewRequest = { ...review, status: 'RESOLVED' };
  const decision: ReviewDecision = {
    decisionId: `decision_${++nextId}`, reviewId: input.reviewId, caseId: input.caseId, actor: input.actor,
    decision: input.decision, reason: input.reason, decidedAt: new Date().toISOString(),
    idempotencyKey: input.idempotencyKey,
  };
  cases = cases.map((c) => (c.caseId === updatedCase.caseId ? updatedCase : c));
  humanReviewRequests = humanReviewRequests.map((r) => (r.reviewId === resolvedReview.reviewId ? resolvedReview : r));
  reviewDecisions = [...reviewDecisions, decision];

  return record({ status: 'ok', case: updatedCase, review: resolvedReview, decision });
}

export const fixturesProvider: DataProvider = {
  source: 'fixtures',
  listCases: async () => cases,
  getCase: async (caseId) => cases.find((c) => c.caseId === caseId),
  getCaseReport: async (caseId) => reports.find((r) => r.caseId === caseId),
  listMerchantProfiles: async () => merchantProfiles,
  getMerchantProfile: async (merchantId) => merchantProfiles.find((m) => m.merchantId === merchantId),
  listHumanReviewRequests: async () => humanReviewRequests,
  listAuditEvents: async (caseId) => auditEvents.filter((e) => e.caseId === caseId),
  submitReviewDecision,
  status: () => ({ source: 'fixtures', stale: false, skippedRecords: 0 }),
};
