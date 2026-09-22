import type {
  AuditEvent, Case, CaseReport, HumanReviewRequest, MerchantProfile, ReviewDecision, ReviewDecisionType,
} from '@themis/contracts';

export type DataSource = 'fixtures' | 'aws';

export interface DataStatus {
  readonly source: DataSource;
  /** True while any served data is a last-known-good copy because the backend read failed. */
  readonly stale: boolean;
  /** When the stalest served copy was fetched. */
  readonly staleSince?: string;
  /** Records dropped for failing contract validation in the latest reads. */
  readonly skippedRecords: number;
}

/** Backend read failed and there is no last-known-good copy to fall back to. */
export class DataSourceError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = 'DataSourceError';
  }
}

export interface ReviewDecisionInput {
  reviewId: string;
  caseId: string;
  actor: string;
  decision: ReviewDecisionType;
  reason: string;
  idempotencyKey: string;
}

export type ReviewDecisionResult =
  | { status: 'ok'; case: Case; review: HumanReviewRequest; decision: ReviewDecision }
  | { status: 'error'; error: { code: string; message: string } };

/** Async, server-side data boundary. Implementations: fixtures (default) and AWS. */
export interface DataProvider {
  readonly source: DataSource;
  listCases(): Promise<Case[]>;
  getCase(caseId: string): Promise<Case | undefined>;
  getCaseReport(caseId: string): Promise<CaseReport | undefined>;
  listMerchantProfiles(): Promise<MerchantProfile[]>;
  getMerchantProfile(merchantId: string): Promise<MerchantProfile | undefined>;
  listHumanReviewRequests(): Promise<HumanReviewRequest[]>;
  listAuditEvents(caseId: string): Promise<AuditEvent[]>;
  /**
   * Record an investigator decision. This is the only path that may change a case's status from
   * the dashboard -- see packages/contracts README "Review decision workflow" for the state
   * machine and this file's callers for the authorization boundary in front of it.
   */
  submitReviewDecision(input: ReviewDecisionInput): Promise<ReviewDecisionResult>;
  status(): DataStatus;
}
