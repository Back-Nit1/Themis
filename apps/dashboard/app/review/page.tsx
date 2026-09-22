import { dataStatus, listReviewQueue } from '@/lib/data/adapter';
import { ReviewCard } from '@/components/ReviewCard';
import { EmptyState, Card } from '@/components/ui';
import { DataNotice } from '@/components/DataNotice';
import { reviewDecisionsEnabled } from '@/lib/data/internal-auth';

export const dynamic = 'force-dynamic';

export default async function ReviewPage() {
  const queue = await listReviewQueue();
  const status = await dataStatus();
  const decisionsEnabled = reviewDecisionsEnabled();

  return (
    <div className="flex flex-col gap-4">
      <div>
        <p className="font-mono text-[11px] uppercase tracking-[0.14em] text-muted">Escalation queue</p>
        <h1 className="mt-1 text-xl font-semibold text-ink">Human review</h1>
        <p className="mt-0.5 text-sm text-muted">
          {queue.length} case(s) escalated by policy
          {decisionsEnabled ? '' : ' — read-only (review decisions are disabled on this deployment)'}
        </p>
      </div>

      <DataNotice status={status} />

      {queue.length === 0 ? (
        <Card><EmptyState label="No cases currently require human review." /></Card>
      ) : (
        <div className="flex flex-col gap-4">
          {queue.map((entry) => <ReviewCard key={entry.case.caseId} entry={entry} decisionsEnabled={decisionsEnabled} />)}
        </div>
      )}
    </div>
  );
}
