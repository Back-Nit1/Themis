import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { DataStack } from '../dist/stacks/data-stack.js';
import { AgentStack } from '../dist/stacks/agent-stack.js';
import { loadConfig } from '../dist/config/env.js';
import { IDEMPOTENT_TOOL_NAMES, TOOL_CONTRACT_VERSION, TOOL_DEFINITIONS } from '../dist/config/tool-schemas.js';

function synth(overrides = {}) {
  const app = new cdk.App();
  const config = { ...loadConfig(), ...overrides };
  const data = new DataStack(app, 'TestData2', { config });
  const stack = new AgentStack(app, 'TestAgent', { config, data });
  return Template.fromStack(stack);
}

test('wires an AgentCore Gateway backed by the tools adapter Lambda', () => {
  const t = synth();
  t.resourceCountIs('AWS::BedrockAgentCore::Gateway', 1);
  t.hasResourceProperties('AWS::BedrockAgentCore::GatewayTarget', {
    TargetConfiguration: Match.objectLike({
      Mcp: Match.objectLike({
        Lambda: Match.objectLike({ LambdaArn: Match.anyValue() }),
      }),
    }),
  });
  t.resourceCountIs('AWS::Lambda::Function', 1);
});

test('exposes all 21 tools from AGENT TOOL SURFACE (prompt.md #11) plus submit_review_decision on the Gateway target', () => {
  const t = synth();
  const targets = t.findResources('AWS::BedrockAgentCore::GatewayTarget');
  const [target] = Object.values(targets);
  const tools = target.Properties.TargetConfiguration.Mcp.Lambda.ToolSchema.InlinePayload;
  assert.equal(tools.length, 21);
  const names = tools.map((tool) => tool.Name);
  for (const expected of ['search_transactions', 'propose_provisional_credit', 'send_case_email', 'escalate_case', 'submit_review_decision']) {
    assert.ok(names.includes(expected), `missing tool ${expected}`);
  }
});

test('freezes one versioned invocation contract with idempotency on every mutating tool', () => {
  assert.match(TOOL_CONTRACT_VERSION, /^\d{4}-\d{2}-\d{2}$/);
  assert.equal(new Set(TOOL_DEFINITIONS.map((tool) => tool.name)).size, 21);
  for (const name of IDEMPOTENT_TOOL_NAMES) {
    const tool = TOOL_DEFINITIONS.find((candidate) => candidate.name === name);
    assert.ok(tool, `missing tool ${name}`);
    const idempotency = tool.params.find((param) => param.name === 'idempotencyKey');
    assert.deepEqual(idempotency, { name: 'idempotencyKey', type: 'string', required: true });
  }
});

test('Gateway exposes outcome and structured escalation fields as optional arguments', () => {
  const update = TOOL_DEFINITIONS.find((tool) => tool.name === 'update_case');
  const escalate = TOOL_DEFINITIONS.find((tool) => tool.name === 'escalate_case');
  assert.deepEqual(update.params.find((param) => param.name === 'outcome'), {
    name: 'outcome', type: 'string', required: false,
  });
  assert.deepEqual(escalate.params.filter((param) => ['summary', 'evidenceRefs'].includes(param.name)), [
    { name: 'summary', type: 'string', required: false },
    { name: 'evidenceRefs', type: 'array', required: false, items: { type: 'string' } },
  ]);
});

test('tools adapter uses the shared merchant table and only its required downstream invoke grant', () => {
  const t = synth();
  t.hasResourceProperties('AWS::Lambda::Function', {
    FunctionName: 'ThemisToolsAdapter',
    Environment: { Variables: Match.objectLike({
      THEMIS_MODE: 'aws',
      MERCHANTS_TABLE: Match.anyValue(),
      ARTIFACTS_BUCKET: Match.anyValue(),
    }) },
  });
  const policies = t.findResources('AWS::IAM::Policy');
  assert.equal(JSON.stringify(policies).includes('ses:Send'), false, 'adapter must delegate SES to messaging');
});

test('provisions a policy engine with a financial-actions and a default-deny policy', () => {
  const t = synth();
  t.resourceCountIs('AWS::BedrockAgentCore::PolicyEngine', 1);
  t.resourceCountIs('AWS::BedrockAgentCore::Policy', 2);
  t.hasResourceProperties('AWS::BedrockAgentCore::Policy', { Name: 'FinancialActions' });
  t.hasResourceProperties('AWS::BedrockAgentCore::Policy', { Name: 'DenyArbitraryActions' });
});

test('aligns Cedar financial gates with deterministic banking policy thresholds', () => {
  const t = synth({ provisionalCreditAutoApproveLimit: 123, creditConfidenceThreshold: 0.91 });
  const policies = t.findResources('AWS::BedrockAgentCore::Policy');
  const financial = Object.values(policies).find((p) => p.Properties.Name === 'FinancialActions');
  const cedar = financial.Properties.Definition.Cedar.Statement;
  assert.match(cedar, /context\.amount <= 123/);
  assert.match(cedar, /context\.confidence >= 0\.91/);
  assert.match(cedar, /context\.customerRequested == true/);
  assert.match(cedar, /context\.hasConfirmedTransactions == true/);
  assert.doesNotMatch(cedar, /action == Action::"propose_card_replacement"/);
});

test('provisions Memory for conversational continuity, not as the financial system of record', () => {
  const t = synth();
  t.resourceCountIs('AWS::BedrockAgentCore::Memory', 1);
  t.hasResourceProperties('AWS::BedrockAgentCore::Memory', { Name: 'ThemisConversationMemory' });
});

test('provisions exactly one Runtime hosting the Themis orchestrator', () => {
  const t = synth();
  t.resourceCountIs('AWS::BedrockAgentCore::Runtime', 1);
  t.hasResourceProperties('AWS::BedrockAgentCore::Runtime', { AgentRuntimeName: 'ThemisOrchestrator' });
  t.hasResourceProperties('AWS::BedrockAgentCore::Runtime', {
    EnvironmentVariables: Match.objectLike({
      THEMIS_MODE: 'aws', GATEWAY_IDENTIFIER: Match.anyValue(), GATEWAY_URL: Match.anyValue(), MEMORY_ID: Match.anyValue(),
    }),
  });
});

test('does not provision a custom Browser or WorkloadIdentity resource (nothing uses them)', () => {
  const t = synth({ enableBrowserResearch: true, browserResearchApproved: true });
  t.resourceCountIs('AWS::BedrockAgentCore::BrowserCustom', 0);
  t.resourceCountIs('AWS::BedrockAgentCore::WorkloadIdentity', 0);
});

test('grants browser IAM permission only when explicitly enabled and approved', () => {
  const withBrowser = synth({ enableBrowserResearch: true, browserResearchApproved: true });
  withBrowser.hasResourceProperties('AWS::IAM::Policy', {
    PolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({ Action: Match.arrayWith(['bedrock-agentcore:StartBrowserSession']) }),
      ]),
    }),
  });

  const withoutBrowser = synth({ enableBrowserResearch: true, browserResearchApproved: false });
  const policies = withoutBrowser.findResources('AWS::IAM::Policy');
  const anyBrowserGrant = Object.values(policies).some((p) =>
    JSON.stringify(p.Properties.PolicyDocument).includes('StartBrowserSession'));
  assert.equal(anyBrowserGrant, false);
});

test('no IAM statement uses Action "*" with Resource "*"', () => {
  const t = synth({ enableBrowserResearch: true });
  const policies = t.findResources('AWS::IAM::Policy');
  for (const [id, policy] of Object.entries(policies)) {
    const statements = policy.Properties.PolicyDocument.Statement;
    for (const stmt of statements) {
      const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
      const resources = Array.isArray(stmt.Resource) ? stmt.Resource : [stmt.Resource];
      const wildcardEverything = actions.includes('*') && resources.includes('*');
      assert.equal(wildcardEverything, false, `${id} has Action:* + Resource:*`);
    }
  }
});
