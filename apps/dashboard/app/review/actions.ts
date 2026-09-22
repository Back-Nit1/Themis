'use server';

import { ReviewDecisionTypeSchema } from '@themis/contracts';
import { submitReviewDecision, type ReviewDecisionResult } from '@/lib/data/adapter';
import { reviewDecisionsEnabled, REVIEW_DECISIONS_DISABLED_MESSAGE } from '@/lib/data/internal-auth';

export type DecideReviewInput = {
  reviewId: string;
  caseId: string;
  actor: string;
  decision: string;
  reason: string;
  idempotencyKey: string;
};

/**
 * The only place a dashboard click can change a case's status (prompt.md investigator review
 * workflow). A Server Action, not an `app/api/**` route: it runs server-side only and Next.js
 * rejects cross-origin invocation, which is the safe boundary this repository can offer without a
 * real authentication system -- see lib/data/internal-auth.ts. This never itself executes a
 * financial/account action; it only records the decision and its resulting case status.
 */
export async function decideReview(input: DecideReviewInput): Promise<ReviewDecisionResult> {
  if (!reviewDecisionsEnabled()) {
    return { status: 'error', error: { code: 'NOT_CONFIGURED', message: REVIEW_DECISIONS_DISABLED_MESSAGE } };
  }
  for (const [name, value] of Object.entries(input)) {
    if (typeof value !== 'string' || value.trim().length === 0) {
      return { status: 'error', error: { code: 'VALIDATION_ERROR', message: `${name} must be a nonempty string` } };
    }
  }
  const decision = ReviewDecisionTypeSchema.safeParse(input.decision);
  if (!decision.success) {
    return { status: 'error', error: { code: 'VALIDATION_ERROR', message: 'decision must be one of APPROVE, REJECT, REQUEST_MORE_EVIDENCE' } };
  }
  return submitReviewDecision({
    reviewId: input.reviewId, caseId: input.caseId, actor: input.actor,
    decision: decision.data, reason: input.reason, idempotencyKey: input.idempotencyKey,
  });
}
