// Entirely fictional, deterministic data. No network or random source is used.
export function buildDemoFixtures() {
  const now = '2026-09-17T20:00:00Z';
  const day = 86_400_000;
  const at = daysAgo => new Date(Date.parse(now) - daysAgo * day).toISOString();
  const padded = n => String(n).padStart(3, '0');
  const customers = Array.from({length: 50}, (_, i) => ({
    customerId: `customer_demo_${padded(i + 1)}`, name: `Synthetic Customer ${i + 1}`,
    phone: `+1555555${String(100 + i).padStart(4, '0')}`, email: `customer${i + 1}@example.test`,
  }));
  const names = ['Asteria Digital', 'Fictional Meadow Music', 'Fictional Lantern Fitness', 'Fictional Harbor Cloud'];
  const aliasGroups = [
    ['ASTERIA.IO', 'ASTERIA*PREMIUM', 'ASTDIGITAL', 'ASTERIA SUB'],
    ['MEADOW*MUSIC', 'MEADOW SUB'], ['LANTERN*FIT', 'LANTERN GYM'], ['HARBOR*CLOUD', 'HARBOR STORAGE'],
  ];
  const merchants = Array.from({length: 40}, (_, i) => ({
    merchantId: `merchant_demo_${padded(i + 1)}`,
    canonicalName: names[i] ?? `Fictional Merchant ${i + 1}`,
    aliases: aliasGroups[i] ?? [],
  }));
  const transactions = [];
  const addTransaction = (customerIndex, merchantIndex, amount, daysAgo, recurring = false, strong = false) => {
    const merchant = merchants[merchantIndex];
    const transaction = {
      transactionId: `txn_demo_${String(transactions.length + 1).padStart(4, '0')}`,
      customerId: customers[customerIndex].customerId, merchantId: merchant.merchantId,
      descriptor: merchant.aliases[transactions.length % (merchant.aliases.length || 1)] ?? merchant.canonicalName,
      amount, currency: 'USD', occurredAt: at(daysAgo),
      authSignals: {
        card_present: !recurring, recurring_indicator: recurring, merchant_id: merchant.merchantId,
        prior_merchant_relationship: merchantIndex > 0,
        ...(strong ? {cvv_match: true, avs_match: true, '3ds_status': 'AUTHENTICATED', device_id: 'device_demo_known'} : {}),
      },
    };
    transactions.push(transaction);
    return transaction;
  };
  // A: precisely six weekly $9.99 charges plus an unrelated $19.99 Asteria charge.
  const unknown = Array.from({length: 6}, (_, i) => addTransaction(0, 0, 9.99, 7 * (i + 1), true));
  addTransaction(0, 0, 19.99, 4);
  // B: canceled last month, then billed after the explicit cancellation date.
  const canceled = [addTransaction(1, 0, 9.99, 14, true), addTransaction(1, 0, 9.99, 7, true)];
  const recognized = [addTransaction(2, 1, 12.99, 2, true)];
  const conflicting = [addTransaction(3, 0, 49.99, 1, false, true)];
  const memory = [addTransaction(4, 0, 9.99, 0, true)];
  // Three ordinary legitimate merchants, with distinct recurring cadences.
  for (const [merchantIndex, amount, cadence] of [[1, 12.99, 30], [2, 25, 14], [3, 4.99, 7]]) {
    for (let i = 0; i < 6; i++) addTransaction(5 + merchantIndex, merchantIndex, amount, cadence * (i + 1), true);
  }
  // Reserve twelve historical disputed transactions, before all active scenarios.
  const historicalTransactions = Array.from({length: 12}, (_, i) => addTransaction(10 + i, i < 8 ? 0 : 1 + i % 3, 9.99, 70 + i, true));
  // Background data excludes Asteria and does not contaminate scenario A matching.
  while (transactions.length < 1200) {
    const i = transactions.length;
    addTransaction(i % 50, 4 + i % 36, (100 + (i * 137) % 15000) / 100, 1 + i % 90);
  }
  const cases = [], evidence = [], proposals = [], decisions = [], audits = [], reviews = [], reports = [];
  function addCase(key, txs, {claimType, classification = claimType, status, confidence = 0.92, summary, contradiction = false, outcome, historical = false, intake = false}) {
    const caseId = `case_demo_${key}`;
    const total = txs.reduce((sum, tx) => sum + Math.round(tx.amount * 100), 0) / 100;
    const requiresHumanReview = status === 'NEEDS_HUMAN_REVIEW';
    const createdAt = historical ? at(65) : now;
    const record = {
      caseId, customerId: txs[0].customerId, merchantId: txs[0].merchantId,
      status, createdAt, updatedAt: historical ? at(60) : now, claimType,
      transactionIds: txs.map(tx => tx.transactionId), evidenceIds: [], totalDisputedAmount: total,
      currency: 'USD', confidence: intake ? null : confidence, recommendedActions: outcome || intake ? [] : ['CREATE_DISPUTE'], requiresHumanReview,
      ...(outcome ? {outcome} : {}),
    };
    const claim = {
      evidenceId: `ev_demo_${key}_claim`, caseId, category: 'CUSTOMER_CLAIMS', type: 'CUSTOMER_STATEMENT',
      claim: summary, source: 'CUSTOMER', reliability: 'MEDIUM', transactionIds: record.transactionIds,
    };
    const ledger = {
      evidenceId: `ev_demo_${key}_ledger`, caseId, category: contradiction ? 'CONTRADICTORY_EVIDENCE' : 'TRANSACTION_EVIDENCE',
      type: contradiction ? 'STRONG_AUTHENTICATION' : txs.length > 1 ? 'RECURRING_PATTERN' : 'LEDGER_RECORD',
      claim: contradiction ? 'The synthetic transaction passed CVV, AVS and 3DS; authorization still requires review.' : `${txs.length} synthetic charge(s) total USD ${total.toFixed(2)}.`,
      source: 'BANK_LEDGER', reliability: 'HIGH', transactionIds: record.transactionIds,
    };
    record.evidenceIds.push(claim.evidenceId, ledger.evidenceId);
    evidence.push(claim, ledger);
    const proposal = outcome || intake ? null : {
      caseId, classification, confidence, supportingEvidence: [claim.evidenceId, ...(contradiction ? [] : [ledger.evidenceId])],
      contradictoryEvidence: contradiction ? [ledger.evidenceId] : [],
      missingEvidence: requiresHumanReview ? ['merchant_authorization_record'] : [],
      recommendedActions: record.recommendedActions, requiresHumanReview,
    };
    if (proposal) proposals.push(proposal);
    const policy = outcome || intake || (!historical && !requiresHumanReview) ? [] : [{
      decisionId: `policy_demo_${key}`, caseId, action: 'CREATE_DISPUTE',
      outcome: requiresHumanReview ? 'REQUIRE_HUMAN_REVIEW' : 'ALLOW',
      rationale: requiresHumanReview ? 'Conflicting authentication evidence requires review.' : 'Synthetic transactions confirmed by customer.',
      decidedAt: record.updatedAt,
    }];
    decisions.push(...policy);
    const audit = {
      eventId: `audit_demo_${key}`, caseId, timestamp: record.updatedAt,
      actor: 'THEMIS_AGENT', action: policy[0]?.action ?? 'CASE_INTAKE',
      tool: policy[0] ? 'evaluate_policy' : 'case_intake',
      result: status === 'AWAITING_TRANSACTION_CONFIRMATION' ? 'PENDING' : 'SUCCESS',
      ...(policy[0] ? {
        policyName: 'themis_demo_policy', policyOutcome: policy[0].outcome,
        proposedAction: policy[0].action, inputAmount: total,
        humanApprovalRequired: requiresHumanReview,
      } : {}),
    };
    audits.push(audit);
    const review = requiresHumanReview ? [{
      reviewId: `review_${caseId}_001`, caseId, reason: 'CONFLICTING_AUTHORIZATION_EVIDENCE', summary,
      recommendedNextStep: 'Review merchant authorization evidence without assuming customer dishonesty.',
      evidenceRefs: record.evidenceIds, status: 'PENDING',
    }] : [];
    reviews.push(...review);
    const report = {
      caseId, customerComplaintSummary: summary, transactions: txs, totalDisputedAmount: total, currency: 'USD',
      merchant: merchants.find(m => m.merchantId === record.merchantId), classification,
      ...(outcome ? {outcome} : {}), timeline: [{timestamp: createdAt, summary}], customerStatements: [summary],
      evidence: [claim, ledger], missingEvidence: proposal?.missingEvidence ?? [], resolution: proposal,
      actionsTaken: status === 'CLOSED' && !outcome ? ['CREATE_DISPUTE'] : [],
      policyDecisions: policy, humanReviewEvents: review, generatedAt: record.updatedAt, auditRefs: [audit.eventId],
    };
    cases.push(record); reports.push(report);
    return record;
  }
  historicalTransactions.forEach((tx, i) => addCase(`history_${padded(i + 1)}`, [tx], {
    claimType: 'RECURRING_PAYMENT_NOT_AUTHORIZED', status: 'CLOSED', historical: true,
    summary: 'Historical synthetic customer-confirmed recurring-payment dispute.',
  }));
  const a = addCase('a', unknown, {claimType: 'UNRECOGNIZED_MERCHANT', classification: 'RECURRING_PAYMENT_NOT_AUTHORIZED', status: 'RESOLUTION_PROPOSED', summary: 'I do not recognize these six weekly Asteria charges.'});
  const b = addCase('b', canceled, {claimType: 'RECURRING_PAYMENT_AFTER_CANCELLATION', status: 'RESOLUTION_PROPOSED', summary: `I canceled this subscription on ${at(35)} but was charged again.`});
  const c = addCase('c', recognized, {claimType: 'UNRECOGNIZED_MERCHANT', classification: 'UNRECOGNIZED_MERCHANT', status: 'CLOSED', outcome: 'CUSTOMER_RECOGNIZED_MERCHANT', summary: 'After seeing the canonical merchant name, I remember buying this subscription.'});
  const d = addCase('d', conflicting, {claimType: 'UNAUTHORIZED_TRANSACTION', status: 'NEEDS_HUMAN_REVIEW', confidence: 0.55, contradiction: true, summary: 'I did not authorize this transaction; strong authentication conflicts with the claim.'});
  const e = addCase('e', memory, {claimType: 'UNRECOGNIZED_MERCHANT', status: 'AWAITING_TRANSACTION_CONFIRMATION', intake: true, summary: 'A new Asteria charge prompts customer-specific verification using prior synthetic cases.'});
  const profiles = merchants.map((merchant, i) => {
    const merchantCases = cases.filter(c => c.merchantId === merchant.merchantId);
    const closedDisputes = merchantCases.filter(c => c.status === 'CLOSED' && !c.outcome);
    return {
      ...merchant, billingPatterns: i < 4 ? [{amount: [9.99, 12.99, 25, 4.99][i], cadenceDays: [7, 30, 14, 7][i]}] : [],
      caseStatistics: {totalCases: merchantCases.length, resolvedCustomerDisputes: closedDisputes.length, openCases: merchantCases.filter(c => !['CLOSED', 'RESOLVED'].includes(c.status)).length},
      riskSignals: i === 0 ? [{type: 'UNRECOGNIZED_RECURRING_SPIKE', severity: 'ELEVATED', observedAt: at(60), expiresAt: at(-30), evidenceRefs: evidence.filter(ev => closedDisputes.some(c => c.caseId === ev.caseId)).map(ev => ev.evidenceId)}] : [],
      updatedAt: now,
    };
  });
  const scenarioCases = [a, b, c, d, e];
  const scenarios = scenarioCases.map((record, i) => ({
    scenarioId: 'ABCDE'[i], caseId: record.caseId, customerId: record.customerId, merchantId: record.merchantId,
    transactionIds: record.transactionIds,
    expected: [
      {classification: 'RECURRING_PAYMENT_NOT_AUTHORIZED', totalDisputedAmount: 59.94},
      {classification: 'RECURRING_PAYMENT_AFTER_CANCELLATION', canceledAt: at(35)},
      {outcome: 'CUSTOMER_RECOGNIZED_MERCHANT', opensDispute: false},
      {status: 'NEEDS_HUMAN_REVIEW'},
      {usesCachedProfile: true, performsWebResearch: false, requiresCustomerVerification: true},
    ][i],
  }));
  const inbound = scenarioCases.map((record, i) => ({channel: i % 2 ? 'SMS' : 'RCS', customerExternalId: customers[i].phone, messageId: `msg_demo_in_${i + 1}`, text: reports.find(r => r.caseId === record.caseId).customerComplaintSummary, postback: null, receivedAt: now}));
  const outbound = scenarioCases.map((record, i) => ({channel: i % 2 ? 'SMS' : 'RCS', customerExternalId: customers[i].phone, messageId: `msg_demo_out_${i + 1}`, caseId: record.caseId, text: `Synthetic case ${record.caseId}: ${record.status}.`}));
  outbound.push({channel: 'EMAIL', customerExternalId: customers[3].email, messageId: 'msg_demo_email_1', caseId: d.caseId, subject: 'Synthetic dispute investigation summary', text: `Case ${d.caseId} is awaiting human review. Disputed amount: USD 49.99. No account actions have been taken.`});
  return {
    'customers/demo': customers, 'transactions/demo': transactions, 'merchants/demo': merchants,
    'merchants/demo-profiles': profiles, 'cases/demo': cases, 'cases/demo-evidence': evidence,
    'cases/demo-resolution-proposals': proposals, 'cases/demo-policy-decisions': decisions,
    'cases/demo-audit-events': audits,
    'cases/demo-human-review-requests': reviews, 'cases/demo-reports': reports,
    'cases/demo-inbound-messages': inbound, 'cases/demo-outbound-messages': outbound,
    'scenarios': scenarios,
  };
}
