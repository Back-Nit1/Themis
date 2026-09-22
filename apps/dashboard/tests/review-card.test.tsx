import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { ReviewCard } from '@/components/ReviewCard';
import { fixturesProvider } from '@/lib/data/fixtures-provider';
import type { ReviewQueueEntry } from '@/lib/data/adapter';

vi.mock('@/app/review/actions', () => ({ decideReview: vi.fn() }));
import { decideReview } from '@/app/review/actions';

afterEach(() => vi.clearAllMocks());

async function entry(): Promise<ReviewQueueEntry> {
  const kase = (await fixturesProvider.getCase('case_demo_d'))!;
  const [request] = await fixturesProvider.listHumanReviewRequests();
  const report = await fixturesProvider.getCaseReport('case_demo_d');
  return { case: kase, request, report, merchantName: report?.merchant?.canonicalName ?? 'Unknown' };
}

describe('ReviewCard', () => {
  it('disables the decision controls when review decisions are disabled', async () => {
    render(<ReviewCard entry={await entry()} decisionsEnabled={false} />);
    const approve = screen.getByRole('button', { name: /approve proposed action/i });
    expect(approve).toBeDisabled();
    expect(screen.getByText(/review decisions are disabled/i)).toBeInTheDocument();
  });

  it('requires actor and reason before a decision can be submitted', async () => {
    render(<ReviewCard entry={await entry()} decisionsEnabled />);
    expect(screen.getByRole('button', { name: /approve proposed action/i })).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/decided by/i), { target: { value: 'investigator_jane' } });
    expect(screen.getByRole('button', { name: /approve proposed action/i })).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/^reason$/i), { target: { value: 'Sufficient evidence.' } });
    expect(screen.getByRole('button', { name: /approve proposed action/i })).not.toBeDisabled();
  });

  it('calls decideReview with the case, review and typed fields on click', async () => {
    vi.mocked(decideReview).mockResolvedValue({
      status: 'ok',
      case: { status: 'ACTION_APPROVED' } as never, review: {} as never, decision: {} as never,
    });
    const e = await entry();
    render(<ReviewCard entry={e} decisionsEnabled />);
    fireEvent.change(screen.getByLabelText(/decided by/i), { target: { value: 'investigator_jane' } });
    fireEvent.change(screen.getByLabelText(/^reason$/i), { target: { value: 'Sufficient evidence.' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /approve proposed action/i }));
    });
    expect(decideReview).toHaveBeenCalledWith(expect.objectContaining({
      caseId: e.case.caseId, reviewId: e.request!.reviewId, actor: 'investigator_jane',
      decision: 'APPROVE', reason: 'Sufficient evidence.',
    }));
  });

  it('shows the error message from a rejected decision', async () => {
    vi.mocked(decideReview).mockResolvedValue({
      status: 'error', error: { code: 'REVIEW_ALREADY_DECIDED', message: 'review already decided' },
    });
    const e = await entry();
    render(<ReviewCard entry={e} decisionsEnabled />);
    fireEvent.change(screen.getByLabelText(/decided by/i), { target: { value: 'investigator_jane' } });
    fireEvent.change(screen.getByLabelText(/^reason$/i), { target: { value: 'ok' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /reject proposed action/i }));
    });
    expect(screen.getByRole('alert')).toHaveTextContent('review already decided');
  });
});
