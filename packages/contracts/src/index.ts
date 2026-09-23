import { z } from 'zod';

const id = z.string().min(1);
const text = z.string().min(1);
const timestamp = z.iso.datetime({ offset: true });
const money = z.number().nonnegative().finite();
const currency = z.string().regex(/^[A-Z]{3}$/);
const confidence = z.number().min(0).max(1);
const ids = z.array(id);

export const CaseStatusSchema = z.enum([
  'NEW', 'INTAKE', 'TRANSACTION_MATCHING', 'AWAITING_TRANSACTION_CONFIRMATION',
  'CLASSIFYING_DISPUTE', 'INVESTIGATING', 'AWAITING_CUSTOMER_INFORMATION',
  'AWAITING_MERCHANT_EVIDENCE', 'RESOLUTION_PROPOSED', 'POLICY_REVIEW',
  'NEEDS_HUMAN_REVIEW', 'ACTION_APPROVED', 'RESOLVED', 'CLOSED',
]);
export type CaseStatus = z.infer<typeof CaseStatusSchema>;

// The specification names states; this map defines the bootstrap transition contract.
export const CASE_STATUS_TRANSITIONS: Readonly<Record<CaseStatus, readonly CaseStatus[]>> = Object.freeze({
  NEW: ['INTAKE'],
  INTAKE: ['TRANSACTION_MATCHING', 'AWAITING_CUSTOMER_INFORMATION', 'NEEDS_HUMAN_REVIEW'],
  TRANSACTION_MATCHING: ['AWAITING_TRANSACTION_CONFIRMATION', 'AWAITING_CUSTOMER_INFORMATION', 'NEEDS_HUMAN_REVIEW'],
  AWAITING_TRANSACTION_CONFIRMATION: ['CLASSIFYING_DISPUTE', 'TRANSACTION_MATCHING', 'NEEDS_HUMAN_REVIEW'],
  CLASSIFYING_DISPUTE: ['INVESTIGATING', 'AWAITING_CUSTOMER_INFORMATION', 'NEEDS_HUMAN_REVIEW', 'RESOLVED'],
  INVESTIGATING: ['AWAITING_CUSTOMER_INFORMATION', 'AWAITING_MERCHANT_EVIDENCE', 'RESOLUTION_PROPOSED', 'NEEDS_HUMAN_REVIEW'],
  AWAITING_CUSTOMER_INFORMATION: ['INTAKE', 'TRANSACTION_MATCHING', 'CLASSIFYING_DISPUTE', 'INVESTIGATING', 'NEEDS_HUMAN_REVIEW'],
  AWAITING_MERCHANT_EVIDENCE: ['INVESTIGATING', 'NEEDS_HUMAN_REVIEW'],
  RESOLUTION_PROPOSED: ['POLICY_REVIEW'],
  POLICY_REVIEW: ['ACTION_APPROVED', 'NEEDS_HUMAN_REVIEW'],
  NEEDS_HUMAN_REVIEW: ['INVESTIGATING', 'RESOLUTION_PROPOSED', 'ACTION_APPROVED'],
  ACTION_APPROVED: ['RESOLVED'],
  RESOLVED: ['CLOSED'],
  CLOSED: [],
});
for (const next of Object.values(CASE_STATUS_TRANSITIONS)) Object.freeze(next);
export function canTransitionCaseStatus(from: CaseStatus, to: CaseStatus): boolean {
  return CASE_STATUS_TRANSITIONS[from]?.includes(to) ?? false;
}
export function assertCaseStatusTransition(from: CaseStatus, to: CaseStatus): void {
  if (!canTransitionCaseStatus(from, to)) throw new Error(`Invalid case transition: ${from} -> ${to}`);
}

export const ClaimTypeSchema = z.enum([
  'UNAUTHORIZED_TRANSACTION', 'UNRECOGNIZED_MERCHANT', 'RECURRING_PAYMENT_NOT_AUTHORIZED',
  'RECURRING_PAYMENT_AFTER_CANCELLATION', 'DUPLICATE_TRANSACTION', 'WRONG_AMOUNT',
  'SERVICE_NOT_RECEIVED', 'REFUND_NOT_RECEIVED', 'OTHER_MERCHANT_DISPUTE', 'INSUFFICIENT_INFORMATION',
]);
export const ActionSchema = z.enum([
  'CREATE_DISPUTE', 'REVIEW_FUTURE_RECURRING_PAYMENT', 'REQUEST_MERCHANT_EVIDENCE',
  'PROVISIONAL_CREDIT', 'BLOCK_RECURRING_MERCHANT', 'REPLACE_CARD', 'DENY_CASE',
]);
export const TransactionAuthSignalsSchema = z.strictObject({
  card_present: z.boolean().optional(), cvv_match: z.boolean().optional(),
  avs_match: z.boolean().optional(),
  '3ds_status': z.enum(['AUTHENTICATED', 'FAILED', 'NOT_PERFORMED', 'UNKNOWN']).optional(),
  wallet_token: text.optional(), device_id: id.optional(), ip_region: text.optional(),
  merchant_id: id.optional(), recurring_indicator: z.boolean().optional(),
  prior_merchant_relationship: z.boolean().optional(),
});
export const TransactionSchema = z.strictObject({
  transactionId: id, customerId: id, merchantId: id, descriptor: text,
  amount: money, currency, occurredAt: timestamp, authSignals: TransactionAuthSignalsSchema,
});
export const CustomerSchema = z.strictObject({
  customerId: id, name: text, phone: z.string().regex(/^\+[1-9]\d{7,14}$/), email: z.email(),
});
export const MerchantSchema = z.strictObject({ merchantId: id, canonicalName: text, aliases: z.array(text) });
export const MerchantRiskSignalSchema = z.strictObject({
  type: text, severity: z.enum(['LOW', 'ELEVATED', 'HIGH']),
  observedAt: timestamp, expiresAt: timestamp, evidenceRefs: ids,
});
export const MerchantProfileSchema = MerchantSchema.extend({
  billingPatterns: z.array(z.strictObject({ amount: money, cadenceDays: z.number().int().positive() })),
  caseStatistics: z.strictObject({
    totalCases: z.number().int().nonnegative(), resolvedCustomerDisputes: z.number().int().nonnegative(),
    openCases: z.number().int().nonnegative(),
  }), riskSignals: z.array(MerchantRiskSignalSchema), updatedAt: timestamp,
});
export const CaseOutcomeSchema = z.enum(['CUSTOMER_RECOGNIZED_MERCHANT']);
export const CaseSchema = z.strictObject({
  caseId: id, customerId: id, status: CaseStatusSchema, outcome: CaseOutcomeSchema.optional(), createdAt: timestamp, updatedAt: timestamp,
  claimType: ClaimTypeSchema, merchantId: id.nullable(), transactionIds: ids, evidenceIds: ids,
  totalDisputedAmount: money, currency, confidence: confidence.nullable(),
  recommendedActions: z.array(ActionSchema), requiresHumanReview: z.boolean(),
});
export const EvidenceCategorySchema = z.enum([
  'CUSTOMER_CLAIMS', 'TRANSACTION_EVIDENCE', 'AUTHENTICATION_EVIDENCE', 'MERCHANT_IDENTITY',
  'MERCHANT_HISTORY', 'BANK_HISTORY', 'EXTERNAL_MERCHANT_INTELLIGENCE',
  'CONTRADICTORY_EVIDENCE', 'MISSING_EVIDENCE',
]);
// Types are source-defined labels; the spec only names RECURRING_PATTERN.
export const EvidenceTypeSchema = text;
export const EvidenceSchema = z.strictObject({
  evidenceId: id, caseId: id, category: EvidenceCategorySchema, type: EvidenceTypeSchema,
  claim: text, source: text, reliability: z.enum(['LOW', 'MEDIUM', 'HIGH']), transactionIds: ids,
  // When this evidence was recorded; used to reject stale customer-request evidence at the trust
  // boundary (bank_tools.policy.customer_requested_payment_block). Optional for pre-existing rows.
  recordedAt: timestamp.optional(),
});
export const ResolutionProposalSchema = z.strictObject({
  caseId: id, classification: ClaimTypeSchema, confidence,
  supportingEvidence: ids, contradictoryEvidence: ids, missingEvidence: z.array(text),
  recommendedActions: z.array(ActionSchema), requiresHumanReview: z.boolean(),
});
export const PolicyDecisionSchema = z.strictObject({
  decisionId: id, caseId: id, action: ActionSchema,
  outcome: z.enum(['ALLOW', 'DENY', 'REQUIRE_HUMAN_REVIEW']), rationale: text, decidedAt: timestamp,
});
/** A concise, structured audit event. It must never contain model reasoning. */
export const AuditEventSchema = z.strictObject({
  eventId: id, caseId: id, timestamp, actor: text, action: text, tool: text,
  result: z.enum(['SUCCESS', 'FAILURE', 'PENDING']),
  policyName: text.optional(),
  policyOutcome: z.enum(['ALLOW', 'DENY', 'REQUIRE_HUMAN_REVIEW']).optional(),
  proposedAction: ActionSchema.optional(),
  inputAmount: money.optional(),
  humanApprovalRequired: z.boolean().optional(),
});
export const HumanReviewRequestSchema = z.strictObject({
  caseId: id, reason: text, summary: text, recommendedNextStep: text, evidenceRefs: ids,
});
export const MessageChannelSchema = z.enum(['RCS', 'SMS', 'EMAIL']);
export const InboundMessageSchema = z.strictObject({
  channel: MessageChannelSchema, customerExternalId: id, messageId: id,
  text: z.string(), postback: text.nullable(), receivedAt: timestamp,
});
export const OutboundMessageSchema = z.strictObject({
  channel: MessageChannelSchema, customerExternalId: id, messageId: id, caseId: id,
  text, subject: text.optional(),
  suggestions: z.array(z.strictObject({ label: text, postback: text })).optional(),
});
export const CaseReportSchema = z.strictObject({
  caseId: id, customerComplaintSummary: text, transactions: z.array(TransactionSchema),
  totalDisputedAmount: money, currency, merchant: MerchantSchema.nullable(),
  classification: ClaimTypeSchema, outcome: CaseOutcomeSchema.optional(),
  timeline: z.array(z.strictObject({ timestamp, summary: text })),
  customerStatements: z.array(text), evidence: z.array(EvidenceSchema),
  missingEvidence: z.array(text), resolution: ResolutionProposalSchema.nullable(),
  actionsTaken: z.array(ActionSchema), policyDecisions: z.array(PolicyDecisionSchema),
  humanReviewEvents: z.array(HumanReviewRequestSchema), generatedAt: timestamp, auditRefs: ids,
});

export type Transaction = z.infer<typeof TransactionSchema>;
export type TransactionAuthSignals = z.infer<typeof TransactionAuthSignalsSchema>;
export type Customer = z.infer<typeof CustomerSchema>;
export type Merchant = z.infer<typeof MerchantSchema>;
export type MerchantProfile = z.infer<typeof MerchantProfileSchema>;
export type MerchantRiskSignal = z.infer<typeof MerchantRiskSignalSchema>;
export type Case = z.infer<typeof CaseSchema>;
export type Evidence = z.infer<typeof EvidenceSchema>;
export type EvidenceType = z.infer<typeof EvidenceTypeSchema>;
export type ResolutionProposal = z.infer<typeof ResolutionProposalSchema>;
export type PolicyDecision = z.infer<typeof PolicyDecisionSchema>;
export type AuditEvent = z.infer<typeof AuditEventSchema>;
export type HumanReviewRequest = z.infer<typeof HumanReviewRequestSchema>;
export type InboundMessage = z.infer<typeof InboundMessageSchema>;
export type OutboundMessage = z.infer<typeof OutboundMessageSchema>;
export type CaseReport = z.infer<typeof CaseReportSchema>;
export type ClaimType = z.infer<typeof ClaimTypeSchema>;
export type Action = z.infer<typeof ActionSchema>;

export type CaseOutcome = z.infer<typeof CaseOutcomeSchema>;

/** Validate a case transition, including the no-dispute recognition path. */
export function assertCaseTransition(current: Case, next: Case): void {
  CaseSchema.parse(current);
  CaseSchema.parse(next);
  if (current.caseId !== next.caseId || current.customerId !== next.customerId) {
    throw new Error('Case transition must preserve case and customer identity');
  }
  assertCaseStatusTransition(current.status, next.status);
  if (next.outcome === 'CUSTOMER_RECOGNIZED_MERCHANT' &&
      (next.recommendedActions.length > 0 || next.requiresHumanReview)) {
    throw new Error('Recognized merchant outcome cannot recommend dispute actions or require review');
  }
  if (current.status === 'CLASSIFYING_DISPUTE' && next.status === 'RESOLVED' &&
      next.outcome !== 'CUSTOMER_RECOGNIZED_MERCHANT') {
    throw new Error('Early resolution requires customer recognition of the merchant');
  }
}
