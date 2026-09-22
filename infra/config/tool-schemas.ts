// Gateway tool definitions for the AgentCore Gateway target that fronts the
// Lambda tools adapter. Names and parameter shapes are derived directly from
// the AGENT TOOL SURFACE list (prompt.md #11) and the field names already
// locked in packages/contracts/src/index.ts, so the Gateway's tool contract
// stays in sync with the shared schemas instead of inventing a parallel one.
//
// Schemas are intentionally flat (matches TOOL DESIGN STANDARD, prompt.md
// #12: compact structured JSON, not full resource validation - the Lambda
// adapter still validates with the real Pydantic/Zod models). If bank-tools
// or agentcore change a tool's parameters, update this file in the same PR.

export interface ToolParam {
  readonly name: string;
  readonly type: 'string' | 'number' | 'boolean' | 'array';
  readonly required?: boolean;
  readonly minimum?: number;
  readonly maximum?: number;
  readonly items?: Readonly<{ type: 'string' }>;
}

export interface ToolDefinition {
  readonly name: string;
  readonly description: string;
  readonly params: readonly ToolParam[];
}

const str = (name: string, required = true): ToolParam => ({ name, type: 'string', required });
const num = (name: string, required = true, minimum?: number, maximum?: number): ToolParam => (
  { name, type: 'number', required, minimum, maximum }
);
const arr = (name: string, required = false): ToolParam => ({
  name, type: 'array', required, items: { type: 'string' },
});
const bool = (name: string, required = false): ToolParam => ({ name, type: 'boolean', required });
const idem = (): ToolParam => str('idempotencyKey');

export const TOOL_CONTRACT_VERSION = '2026-09-20';

export const TOOL_DEFINITIONS: readonly ToolDefinition[] = [
  { name: 'search_transactions', description: 'Search a customer\'s transactions with bounded deterministic filters.', params: [str('customerId'), str('merchantId', false), str('descriptorContains', false), str('since', false), str('until', false), num('minAmount', false), num('maxAmount', false), num('limit', false), str('caseId', false)] },
  { name: 'get_transaction_details', description: 'Fetch one transaction by id.', params: [str('transactionId'), str('caseId', false)] },
  { name: 'get_transaction_auth_signals', description: 'Fetch authentication signals (CVV/AVS/3DS/device) for one transaction.', params: [str('transactionId'), str('caseId', false)] },
  { name: 'find_related_transactions', description: 'Find transactions related to a given transaction (same merchant/recurring series).', params: [str('transactionId'), num('limit', false), str('caseId', false)] },
  { name: 'get_customer_dispute_history', description: 'List a customer\'s prior dispute cases.', params: [str('customerId'), num('limit', false)] },
  { name: 'resolve_merchant', description: 'Resolve a raw merchant descriptor to a canonical merchant record.', params: [str('descriptor'), str('caseId', false)] },
  { name: 'get_merchant_profile', description: 'Fetch a merchant\'s profile including billing patterns and case statistics.', params: [str('merchantId')] },
  { name: 'get_merchant_risk_signals', description: 'Fetch a merchant\'s active risk signals.', params: [str('merchantId')] },
  { name: 'get_case', description: 'Fetch one case by id.', params: [str('caseId')] },
  { name: 'create_case', description: 'Create a new dispute case for a customer.', params: [str('customerId'), str('claimType'), arr('transactionIds'), str('merchantId', false), str('currency', false), idem()] },
  { name: 'update_case', description: 'Update mutable fields on an existing case.', params: [str('caseId'), str('status', false), str('claimType', false), str('merchantId', false), num('confidence', false), bool('requiresHumanReview', false), str('outcome', false), idem()] },
  { name: 'save_evidence', description: 'Attach a structured evidence record to a case.', params: [str('caseId'), str('category'), str('type'), str('claim'), str('source'), str('reliability'), arr('transactionIds'), str('evidenceId', false), idem()] },
  { name: 'generate_case_report', description: 'Generate and persist the customer-facing case report.', params: [str('caseId'), idem()] },
  { name: 'propose_payment_block', description: 'Propose blocking future recurring payments to a merchant. Policy-gated.', params: [str('caseId'), bool('customerRequested', true), idem()] },
  { name: 'propose_card_replacement', description: 'Propose replacing the customer\'s card. Always requires human review.', params: [str('caseId'), idem()] },
  { name: 'propose_dispute_creation', description: 'Propose formally disputing confirmed transactions. Policy-gated.', params: [str('caseId'), arr('transactionIds', true), bool('hasConfirmedTransactions', true), idem()] },
  { name: 'propose_provisional_credit', description: 'Propose issuing a provisional credit. Policy-gated on amount, confidence, and classification.', params: [str('caseId'), num('amount', true, 0), num('confidence', true, 0, 1), str('claimType'), idem()] },
  { name: 'submit_review_decision', description: 'Record an investigator decision (approve/reject/request more evidence) on a pending human review.', params: [str('caseId'), str('reviewId'), str('actor'), str('decision'), str('reason'), idem()] },
  { name: 'escalate_case', description: 'Escalate a case to human review.', params: [str('caseId'), str('reason'), str('summary', false), arr('evidenceRefs'), idem()] },
  { name: 'send_customer_message', description: 'Send an RCS/SMS message to the customer.', params: [str('caseId'), str('channel'), str('text'), idem()] },
  { name: 'send_case_email', description: 'Send a transactional case-summary email via SES.', params: [str('caseId'), str('subject', false), idem()] },
];

export const IDEMPOTENT_TOOL_NAMES = Object.freeze([
  'create_case', 'update_case', 'save_evidence', 'generate_case_report',
  'propose_payment_block', 'propose_card_replacement', 'propose_dispute_creation',
  'propose_provisional_credit', 'submit_review_decision', 'escalate_case', 'send_customer_message', 'send_case_email',
] as const);
