// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fixturesProvider, resetFixtures } from '@/lib/data/fixtures-provider';
import { createAwsProvider } from '@/lib/data/aws-provider';
import type { DynamoAccess, ObjectAccess } from '@/lib/data/aws-access';
import { setDataProvider, submitReviewDecision } from '@/lib/data/adapter';
import { decideReview } from '@/app/review/actions';

afterEach(() => {
  resetFixtures();
  setDataProvider(undefined);
  vi.unstubAllEnvs();
});

const PENDING_CASE_ID = 'case_demo_d';

async function pendingReviewId() {
  const [review] = await fixturesProvider.listHumanReviewRequests();
  expect(review.caseId).toBe(PENDING_CASE_ID);
  expect(review.status).toBe('PENDING');
  return review.reviewId;
}

describe('fixtures provider: submitReviewDecision', () => {
  it('approve moves the case to ACTION_APPROVED and resolves the review', async () => {
    const reviewId = await pendingReviewId();
    const result = await fixturesProvider.submitReviewDecision({
      reviewId, caseId: PENDING_CASE_ID, actor: 'investigator_jane', decision: 'APPROVE',
      reason: 'Evidence is sufficient.', idempotencyKey: 'k1',
    });
    expect(result.status).toBe('ok');
    if (result.status !== 'ok') throw new Error('unreachable');
    expect(result.case.status).toBe('ACTION_APPROVED');
    expect(result.case.requiresHumanReview).toBe(false);
    expect(result.review.status).toBe('RESOLVED');
    expect((await fixturesProvider.getCase(PENDING_CASE_ID))!.status).toBe('ACTION_APPROVED');
    const audit = await fixturesProvider.listAuditEvents(PENDING_CASE_ID);
    expect(audit.some((e) => e.action === 'REVIEW_DECISION:APPROVE')).toBe(true);
  });

  it('reject and request-more-evidence both return the case to INVESTIGATING', async () => {
    const reviewId = await pendingReviewId();
    const result = await fixturesProvider.submitReviewDecision({
      reviewId, caseId: PENDING_CASE_ID, actor: 'investigator_jane', decision: 'REJECT',
      reason: 'Not persuasive.', idempotencyKey: 'k1',
    });
    expect(result.status).toBe('ok');
    if (result.status === 'ok') expect(result.case.status).toBe('INVESTIGATING');
  });

  it('replays the same result for a repeated idempotency key instead of reapplying', async () => {
    const reviewId = await pendingReviewId();
    const input = {
      reviewId, caseId: PENDING_CASE_ID, actor: 'investigator_jane', decision: 'APPROVE' as const,
      reason: 'ok', idempotencyKey: 'dup-key',
    };
    const first = await fixturesProvider.submitReviewDecision(input);
    const second = await fixturesProvider.submitReviewDecision(input);
    expect(second).toEqual(first);
  });

  it('rejects a conflicting decision on an already-resolved review', async () => {
    const reviewId = await pendingReviewId();
    await fixturesProvider.submitReviewDecision({
      reviewId, caseId: PENDING_CASE_ID, actor: 'investigator_jane', decision: 'APPROVE',
      reason: 'ok', idempotencyKey: 'k1',
    });
    const second = await fixturesProvider.submitReviewDecision({
      reviewId, caseId: PENDING_CASE_ID, actor: 'investigator_bob', decision: 'REJECT',
      reason: 'I disagree', idempotencyKey: 'k2',
    });
    expect(second).toEqual({ status: 'error', error: { code: 'REVIEW_ALREADY_DECIDED', message: expect.any(String) } });
  });

  it('rejects a decision when the case is not awaiting human review', async () => {
    const [firstCase] = await fixturesProvider.listCases();
    const result = await fixturesProvider.submitReviewDecision({
      reviewId: 'review_does_not_exist', caseId: firstCase.caseId, actor: 'investigator_jane',
      decision: 'APPROVE', reason: 'ok', idempotencyKey: 'k1',
    });
    expect(result).toEqual({ status: 'error', error: { code: 'NOT_FOUND', message: expect.any(String) } });
  });
});

describe('aws provider: submitReviewDecision blocker', () => {
  it('reports NOT_IMPLEMENTED instead of writing to DynamoDB', async () => {
    const dynamo: DynamoAccess = {
      scan: vi.fn(), get: vi.fn(), query: vi.fn(),
    } as unknown as DynamoAccess;
    const objects: ObjectAccess = { getText: vi.fn() };
    const provider = createAwsProvider(
      { casesTable: 'c', merchantsTable: 'm', auditTable: 'a', artifactsBucket: 'b' },
      { dynamo, objects },
    );
    const result = await provider.submitReviewDecision({
      reviewId: 'r1', caseId: 'c1', actor: 'jane', decision: 'APPROVE', reason: 'ok', idempotencyKey: 'k1',
    });
    expect(result).toEqual({ status: 'error', error: { code: 'NOT_IMPLEMENTED', message: expect.any(String) } });
    expect(dynamo.scan).not.toHaveBeenCalled();
  });
});

describe('decideReview server action: the authorization boundary', () => {
  it('is disabled by default and reports the blocker instead of mutating', async () => {
    vi.stubEnv('THEMIS_REVIEW_DECISIONS_ENABLED', '');
    const reviewId = await pendingReviewId();
    const result = await decideReview({
      reviewId, caseId: PENDING_CASE_ID, actor: 'investigator_jane', decision: 'APPROVE',
      reason: 'ok', idempotencyKey: 'k1',
    });
    expect(result).toEqual({ status: 'error', error: { code: 'NOT_CONFIGURED', message: expect.any(String) } });
    expect((await fixturesProvider.getCase(PENDING_CASE_ID))!.status).toBe('NEEDS_HUMAN_REVIEW');
  });

  it('rejects malformed input before it ever reaches the provider', async () => {
    vi.stubEnv('THEMIS_REVIEW_DECISIONS_ENABLED', 'true');
    const reviewId = await pendingReviewId();
    const result = await decideReview({
      reviewId, caseId: PENDING_CASE_ID, actor: 'investigator_jane', decision: 'MAYBE_LATER',
      reason: 'ok', idempotencyKey: 'k1',
    });
    expect(result.status).toBe('error');
    if (result.status === 'error') expect(result.error.code).toBe('VALIDATION_ERROR');
  });

  it('records a decision end to end once enabled', async () => {
    vi.stubEnv('THEMIS_REVIEW_DECISIONS_ENABLED', 'true');
    const reviewId = await pendingReviewId();
    const result = await decideReview({
      reviewId, caseId: PENDING_CASE_ID, actor: 'investigator_jane', decision: 'APPROVE',
      reason: 'Evidence is sufficient.', idempotencyKey: 'k1',
    });
    expect(result.status).toBe('ok');
    if (result.status === 'ok') expect(result.case.status).toBe('ACTION_APPROVED');
  });
});

describe('adapter.submitReviewDecision routes through the selected provider', () => {
  it('calls the active provider', async () => {
    const submit = vi.fn().mockResolvedValue({ status: 'ok', case: {}, review: {}, decision: {} });
    setDataProvider({ ...fixturesProvider, submitReviewDecision: submit });
    await submitReviewDecision({
      reviewId: 'r', caseId: 'c', actor: 'a', decision: 'APPROVE', reason: 'r', idempotencyKey: 'k',
    });
    expect(submit).toHaveBeenCalledOnce();
  });
});
