'use client';

import { useState, useTransition } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Badge, Card, CardHeader } from '@/components/ui';
import { formatMoney, titleCase } from '@/lib/format';
import type { ReviewQueueEntry } from '@/lib/data/adapter';
import type { ReviewDecisionResult } from '@/lib/data/adapter';
import { decideReview } from '@/app/review/actions';

const DECISIONS = [
  { decision: 'APPROVE', label: 'Approve proposed action' },
  { decision: 'REJECT', label: 'Reject proposed action' },
  { decision: 'REQUEST_MORE_EVIDENCE', label: 'Request more evidence' },
] as const;

export function ReviewCard({ entry, decisionsEnabled }: { entry: ReviewQueueEntry; decisionsEnabled: boolean }) {
  const { case: kase, request, report, merchantName } = entry;
  const proposal = report?.resolution;
  const router = useRouter();

  const [actor, setActor] = useState('');
  const [reason, setReason] = useState('');
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID());
  const [pendingDecision, setPendingDecision] = useState<string | null>(null);
  const [result, setResult] = useState<ReviewDecisionResult | null>(null);
  const [isPending, startTransition] = useTransition();

  const decided = kase.status !== 'NEEDS_HUMAN_REVIEW' || result?.status === 'ok';
  const canSubmit = decisionsEnabled && !decided && actor.trim() && reason.trim();

  function submit(decision: (typeof DECISIONS)[number]['decision']) {
    if (!request || !canSubmit) return;
    setPendingDecision(decision);
    startTransition(async () => {
      const outcome = await decideReview({
        reviewId: request.reviewId, caseId: kase.caseId, actor, decision, reason, idempotencyKey,
      });
      setResult(outcome);
      if (outcome.status === 'ok') {
        setIdempotencyKey(crypto.randomUUID());
        router.refresh();
      }
    });
  }

  return (
    <Card>
      <CardHeader
        title={kase.caseId}
        subtitle={`${merchantName} · ${formatMoney(kase.totalDisputedAmount, kase.currency)}`}
        action={
          <Link href={`/cases/${kase.caseId}`} className="text-xs text-trust hover:underline">
            Open case →
          </Link>
        }
      />
      <div className="grid grid-cols-2 gap-6 px-5 py-4 text-sm">
        <div>
          <p className="font-mono text-[11px] font-semibold uppercase tracking-wide text-muted">Reason for escalation</p>
          <p className="mt-1 text-ink/85">{request ? titleCase(request.reason) : 'Flagged by policy gate'}</p>
          {request && <p className="mt-1 text-xs text-muted">{request.summary}</p>}

          <p className="mt-3 font-mono text-[11px] font-semibold uppercase tracking-wide text-muted">Missing / conflicting evidence</p>
          {report && report.missingEvidence.length > 0 ? (
            <ul className="mt-1 list-inside list-disc text-ink/85">
              {report.missingEvidence.map((m, i) => <li key={i}>{m}</li>)}
            </ul>
          ) : (
            <p className="mt-1 text-muted">
              {(report?.resolution?.contradictoryEvidence.length ?? 0) > 0
                ? `${report!.resolution!.contradictoryEvidence.length} contradictory item(s) — see case evidence`
                : 'None recorded'}
            </p>
          )}
        </div>

        <div>
          <p className="font-mono text-[11px] font-semibold uppercase tracking-wide text-muted">Agent proposal</p>
          {proposal ? (
            <>
              <p className="mt-1 text-ink/85">{titleCase(proposal.classification)}</p>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {proposal.recommendedActions.map((a) => <Badge key={a} tone="progress">{titleCase(a)}</Badge>)}
              </div>
              <p className="mt-1 font-mono text-xs text-muted">Confidence {Math.round(proposal.confidence * 100)}%</p>
            </>
          ) : (
            <p className="mt-1 text-muted">No proposal on file.</p>
          )}
        </div>
      </div>

      <div className="border-t border-line px-5 py-3">
        {decided ? (
          <p className="text-xs text-ink/85">
            Decided — case status is now <Badge tone="progress">{titleCase(kase.status)}</Badge>.
          </p>
        ) : !request ? (
          <p className="text-xs text-muted">No review request on file for this case; decisions are unavailable.</p>
        ) : (
          <div className="flex flex-col gap-2">
            {!decisionsEnabled && (
              <p className="text-xs text-alert">
                Review decisions are disabled on this deployment (THEMIS_REVIEW_DECISIONS_ENABLED is not set).
              </p>
            )}
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <input
                aria-label="Decided by"
                placeholder="Decided by"
                value={actor}
                onChange={(e) => setActor(e.target.value)}
                disabled={!decisionsEnabled || isPending}
                className="col-span-2 rounded-sm border border-line bg-paper px-2 py-1.5 text-xs text-ink placeholder:text-muted"
              />
              <input
                aria-label="Reason"
                placeholder="Reason"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                disabled={!decisionsEnabled || isPending}
                className="col-span-2 rounded-sm border border-line bg-paper px-2 py-1.5 text-xs text-ink placeholder:text-muted"
              />
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {DECISIONS.map(({ decision, label }) => (
                <button
                  key={decision}
                  type="button"
                  disabled={!canSubmit || isPending}
                  onClick={() => submit(decision)}
                  title={decisionsEnabled ? undefined : 'Review decisions are disabled on this deployment'}
                  className="rounded-sm border border-line bg-paper px-3 py-1.5 text-xs font-medium text-ink disabled:cursor-not-allowed disabled:text-muted enabled:hover:border-trust"
                >
                  {isPending && pendingDecision === decision ? 'Recording…' : label}
                </button>
              ))}
            </div>
            {result?.status === 'error' && (
              <p role="alert" className="text-xs text-alert">{result.error.message}</p>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}
