import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { z } from 'zod';
import * as c from '../dist/index.js';

const load = async path => JSON.parse(await readFile(new URL(`../../../fixtures/${path}.json`, import.meta.url), 'utf8'));
const fixtureSchemas = {
  'customers/customers': c.CustomerSchema,
  'transactions/transactions': c.TransactionSchema,
  'merchants/merchants': c.MerchantSchema,
  'merchants/profiles': c.MerchantProfileSchema,
  'cases/cases': c.CaseSchema,
  'cases/evidence': c.EvidenceSchema,
  'cases/resolution-proposals': c.ResolutionProposalSchema,
  'cases/policy-decisions': c.PolicyDecisionSchema,
  'cases/audit-events': c.AuditEventSchema,
  'cases/human-review-requests': c.HumanReviewRequestSchema,
  'cases/inbound-messages': c.InboundMessageSchema,
  'cases/outbound-messages': c.OutboundMessageSchema,
  'cases/reports': c.CaseReportSchema,
};
for (const [path, schema] of Object.entries(fixtureSchemas)) {
  test(`synthetic fixtures validate: ${path}`, async () => {
    const records = await load(path);
    assert.ok(records.length > 0);
    for (const record of records) schema.parse(record);
  });
}

test('fixture references and disputed totals are consistent', async () => {
  const data = Object.fromEntries(await Promise.all(Object.keys(fixtureSchemas).map(async p => [p, await load(p)])));
  const [customer] = data['customers/customers'];
  const [merchant] = data['merchants/merchants'];
  const [caseRecord] = data['cases/cases'];
  const transactions = data['transactions/transactions'];
  const evidence = data['cases/evidence'];
  assert.equal(caseRecord.customerId, customer.customerId);
  assert.equal(caseRecord.merchantId, merchant.merchantId);
  for (const transaction of transactions) {
    assert.equal(transaction.customerId, customer.customerId);
    assert.equal(transaction.merchantId, merchant.merchantId);
    assert.equal(transaction.currency, caseRecord.currency);
  }
  assert.deepEqual(caseRecord.transactionIds, transactions.map(t => t.transactionId));
  assert.deepEqual(caseRecord.evidenceIds, evidence.map(e => e.evidenceId));
  assert.equal(Math.round(caseRecord.totalDisputedAmount * 100), transactions.reduce((sum, t) => sum + Math.round(t.amount * 100), 0));
  for (const e of evidence) {
    assert.equal(e.caseId, caseRecord.caseId);
    for (const ref of e.transactionIds) assert.ok(caseRecord.transactionIds.includes(ref));
  }
  for (const path of ['cases/resolution-proposals', 'cases/policy-decisions', 'cases/audit-events', 'cases/human-review-requests', 'cases/reports', 'cases/outbound-messages']) {
    for (const record of data[path]) assert.equal(record.caseId, caseRecord.caseId);
  }
  const [proposal] = data['cases/resolution-proposals'];
  const [review] = data['cases/human-review-requests'];
  const [report] = data['cases/reports'];
  for (const ref of [...proposal.supportingEvidence, ...proposal.contradictoryEvidence, ...review.evidenceRefs]) assert.ok(caseRecord.evidenceIds.includes(ref));
  assert.deepEqual(report.transactions, transactions);
  assert.deepEqual(report.evidence, evidence);
  assert.deepEqual(report.resolution, proposal);
  assert.deepEqual(report.policyDecisions, data['cases/policy-decisions']);
  for (const auditRef of report.auditRefs) assert.equal(data['cases/audit-events'].find(event => event.eventId === auditRef)?.caseId, caseRecord.caseId);
  assert.deepEqual(report.humanReviewEvents, data['cases/human-review-requests']);
  assert.equal(report.totalDisputedAmount, caseRecord.totalDisputedAmount);
  assert.deepEqual(report.merchant, merchant);
  assert.deepEqual(data['merchants/profiles'][0].aliases, merchant.aliases);
  for (const signal of data['merchants/profiles'][0].riskSignals) {
    assert.ok(Date.parse(signal.expiresAt) > Date.parse(signal.observedAt));
    for (const ref of signal.evidenceRefs) assert.ok(caseRecord.evidenceIds.includes(ref));
  }
  for (const path of ['cases/inbound-messages', 'cases/outbound-messages']) assert.equal(data[path][0].customerExternalId, customer.phone);
});

test('reject malformed boundaries and preserve unknown auth signals', async () => {
  const [transaction] = await load('transactions/transactions');
  for (const patch of [{amount: -1}, {amount: Infinity}, {currency: 'usd'}, {occurredAt: 'yesterday'}, {customerId: ''}, {extra: true}]) {
    assert.equal(c.TransactionSchema.safeParse({...transaction, ...patch}).success, false);
  }
  assert.deepEqual(c.TransactionAuthSignalsSchema.parse({}), {});
  assert.equal(c.TransactionAuthSignalsSchema.safeParse({cvv_match: 'false'}).success, false);
  const [caseRecord] = await load('cases/cases');
  for (const patch of [{status: 'DONE'}, {confidence: 1.1}, {recommendedActions: ['TRANSFER_MONEY']}]) {
    assert.equal(c.CaseSchema.safeParse({...caseRecord, ...patch}).success, false);
  }
});

test('normal lifecycle, waiting loops, and human review are explicit', () => {
  const path = ['NEW', 'INTAKE', 'TRANSACTION_MATCHING', 'AWAITING_TRANSACTION_CONFIRMATION', 'CLASSIFYING_DISPUTE', 'INVESTIGATING', 'RESOLUTION_PROPOSED', 'POLICY_REVIEW', 'ACTION_APPROVED', 'RESOLVED', 'CLOSED'];
  for (let i = 1; i < path.length; i++) c.assertCaseStatusTransition(path[i - 1], path[i]);
  for (const [from, to] of [['INVESTIGATING', 'AWAITING_MERCHANT_EVIDENCE'], ['AWAITING_MERCHANT_EVIDENCE', 'INVESTIGATING'], ['AWAITING_TRANSACTION_CONFIRMATION', 'TRANSACTION_MATCHING'], ['POLICY_REVIEW', 'NEEDS_HUMAN_REVIEW'], ['NEEDS_HUMAN_REVIEW', 'ACTION_APPROVED']]) c.assertCaseStatusTransition(from, to);
  assert.equal(c.canTransitionCaseStatus('INVESTIGATING', 'RESOLVED'), false);
  assert.throws(() => c.assertCaseStatusTransition('RESOLUTION_PROPOSED', 'ACTION_APPROVED'));
  assert.equal(c.canTransitionCaseStatus('CLOSED', 'INTAKE'), false);
  assert.equal(c.canTransitionCaseStatus('NEW', 'NEW'), false);
  assert.deepEqual(Object.keys(c.CASE_STATUS_TRANSITIONS), c.CaseStatusSchema.options);
  for (const targets of Object.values(c.CASE_STATUS_TRANSITIONS)) for (const target of targets) c.CaseStatusSchema.parse(target);
});

test('review decisions: shape, target-status map, and the transition guard', async () => {
  const [review] = await load('cases/human-review-requests');
  assert.equal(review.status, 'PENDING');
  assert.ok(review.reviewId);
  assert.equal(c.HumanReviewRequestSchema.safeParse({...review, status: 'ONGOING'}).success, false);
  assert.equal(c.HumanReviewRequestSchema.safeParse((({ reviewId, ...rest }) => rest)(review)).success, false);

  const decision = {
    decisionId: 'decision_001', reviewId: review.reviewId, caseId: review.caseId, actor: 'investigator_jane',
    decision: 'APPROVE', reason: 'Evidence is sufficient.', decidedAt: '2026-09-22T00:00:00Z',
    idempotencyKey: 'idem_review_001',
  };
  assert.equal(c.ReviewDecisionSchema.safeParse(decision).success, true);
  for (const patch of [{decision: 'MAYBE'}, {actor: ''}, {idempotencyKey: ''}, {extra: true}]) {
    assert.equal(c.ReviewDecisionSchema.safeParse({...decision, ...patch}).success, false);
  }
  assert.deepEqual(c.REVIEW_DECISION_TARGET_STATUS, {
    APPROVE: 'ACTION_APPROVED', REJECT: 'INVESTIGATING', REQUEST_MORE_EVIDENCE: 'INVESTIGATING',
  });

  const [caseRecord] = await load('cases/cases');
  const kase = {...caseRecord, status: 'NEEDS_HUMAN_REVIEW'};
  c.assertReviewDecisionTransition(kase, review, decision);
  assert.throws(() => c.assertReviewDecisionTransition({...kase, status: 'INVESTIGATING'}, review, decision), /awaiting human review/);
  assert.throws(() => c.assertReviewDecisionTransition(kase, {...review, status: 'RESOLVED'}, decision), /already decided/);
  assert.throws(() => c.assertReviewDecisionTransition(kase, review, {...decision, reviewId: 'review_other'}), /same case and review/);
  assert.throws(() => c.assertReviewDecisionTransition(kase, {...review, caseId: 'case_other'}, decision), /same case and review/);
});

test('generated JSON Schemas stay aligned with all exported runtime schemas', async () => {
  const generated = JSON.parse(await readFile(new URL('../schemas.json', import.meta.url), 'utf8'));
  const expected = Object.fromEntries(Object.entries(c).filter(([name]) => name.endsWith('Schema')).map(([name, schema]) => [name.slice(0, -6), z.toJSONSchema(schema)]));
  assert.deepEqual(generated, expected);
  assert.equal(generated.Case.$schema, 'https://json-schema.org/draft/2020-12/schema');
});
