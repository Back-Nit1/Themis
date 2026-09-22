import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createMessagingRuntime, parseRuntimeEnvironment } from '../src/index.ts';

const agentArn = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/ThemisOrchestrator';
const rcsTopic = 'arn:aws:sns:us-east-1:123456789012:themis-rcs';
const smsTopic = 'arn:aws:sns:us-east-1:123456789012:themis-sms';
const baseEnv = {
  THEMIS_MODE: 'aws',
  IDEMPOTENCY_TABLE: 'themis-messaging',
  AGENT_RUNTIME_ARN: agentArn,
};
const noDynamo = { putItem: async () => ({}), updateItem: async () => ({}), getItem: async () => ({}), deleteItem: async () => ({}) };
const noAgent = { invokeAgentRuntime: async () => undefined };

function messagingClient() {
  const calls: { operation: string }[] = [];
  return {
    calls,
    client: {
      sendTextMessage: async () => { calls.push({ operation: 'sendTextMessage' }); return { MessageId: 'text-1' }; },
      sendRcsMessage: async () => { calls.push({ operation: 'sendRcsMessage' }); return { MessageId: 'rcs-1' }; },
    },
  };
}

test('RCS-only: parses without requiring an SMS topic/identity', () => {
  const env = { ...baseEnv, ENABLE_RCS: 'true', ENABLE_SMS_FALLBACK: 'false', THEMIS_RCS_TOPIC_ARN: rcsTopic };
  const config = parseRuntimeEnvironment(env);
  assert.equal(config.rcsEnabled, true);
  assert.equal(config.smsEnabled, false);
  assert.deepEqual(config.topics, { [rcsTopic]: 'RCS' });
});

test('SMS-only: parses without requiring an RCS topic/identity', () => {
  const env = { ...baseEnv, ENABLE_RCS: 'false', ENABLE_SMS_FALLBACK: 'true', THEMIS_SMS_TOPIC_ARN: smsTopic };
  const config = parseRuntimeEnvironment(env);
  assert.equal(config.rcsEnabled, false);
  assert.equal(config.smsEnabled, true);
  assert.deepEqual(config.topics, { [smsTopic]: 'SMS' });
});

test('both enabled: parses distinct RCS and SMS topics', () => {
  const env = { ...baseEnv, ENABLE_RCS: 'true', ENABLE_SMS_FALLBACK: 'true', THEMIS_RCS_TOPIC_ARN: rcsTopic, THEMIS_SMS_TOPIC_ARN: smsTopic };
  const config = parseRuntimeEnvironment(env);
  assert.equal(config.rcsEnabled, true);
  assert.equal(config.smsEnabled, true);
  assert.deepEqual(config.topics, { [rcsTopic]: 'RCS', [smsTopic]: 'SMS' });
});

test('both inbound channels disabled: fails closed with an explicit error', () => {
  const env = { ...baseEnv, ENABLE_RCS: 'false', ENABLE_SMS_FALLBACK: 'false' };
  assert.throws(() => parseRuntimeEnvironment(env), /At least one inbound messaging channel must be enabled/);
});

test('missing enabled-channel configuration fails closed instead of constructing a client with a dummy identity', () => {
  const env = { ...baseEnv, ENABLE_RCS: 'true', ENABLE_SMS_FALLBACK: 'false', THEMIS_RCS_TOPIC_ARN: rcsTopic };
  const { client } = messagingClient();
  assert.throws(() => createMessagingRuntime(env, { dynamo: noDynamo, agentRuntime: noAgent, messagingClient: client }),
    /Missing THEMIS_RCS_POOL_ID/);
});

test('RCS-only outbound: sends plain RCS text without an SMS identity, but an explicit SMS send fails safely', async () => {
  const env = { ...baseEnv, ENABLE_RCS: 'true', ENABLE_SMS_FALLBACK: 'false', THEMIS_RCS_TOPIC_ARN: rcsTopic, THEMIS_RCS_POOL_ID: 'pool-1' };
  const { client, calls } = messagingClient();
  const rt = createMessagingRuntime(env, { dynamo: noDynamo, agentRuntime: noAgent, messagingClient: client });
  const ok = await rt.handler({ themisTool: 'send_customer_message', message: {
    channel: 'RCS', customerExternalId: '+15555550100', messageId: 'k-rcs', caseId: 'case-1', text: 'Your case is open.' } });
  assert.deepEqual(ok, { messageId: 'text-1' });
  assert.deepEqual(calls, [{ operation: 'sendTextMessage' }]);

  await assert.rejects(rt.handler({ themisTool: 'send_customer_message', message: {
    channel: 'SMS', customerExternalId: '+15555550100', messageId: 'k-sms', caseId: 'case-1', text: 'fallback text' } }),
    /Missing origination identity/);
});

test('SES disabled: send_case_email fails explicitly and never constructs the email adapter', async () => {
  const env = { ...baseEnv, ENABLE_RCS: 'false', ENABLE_SMS_FALLBACK: 'true', THEMIS_SMS_TOPIC_ARN: smsTopic, ENABLE_SES: 'false' };
  let sesCalls = 0;
  const rt = createMessagingRuntime(env, {
    dynamo: noDynamo, agentRuntime: noAgent,
    sesClient: { sendEmail: async () => { sesCalls++; return { MessageId: 'ses-1' }; } },
  });
  await assert.rejects(rt.handler({ themisTool: 'send_case_email', case: {}, report: {}, recipient: 'a@b.test', nextSteps: [] }),
    /SES is disabled/);
  assert.equal(sesCalls, 0);
});

test('SES enabled (default): send_customer_message fails explicitly when no messaging transport is composed', async () => {
  const env = { ...baseEnv, ENABLE_RCS: 'false', ENABLE_SMS_FALLBACK: 'true', THEMIS_SMS_TOPIC_ARN: smsTopic };
  const rt = createMessagingRuntime(env, { dynamo: noDynamo, agentRuntime: noAgent });
  await assert.rejects(rt.handler({ themisTool: 'send_customer_message', message: {
    channel: 'SMS', customerExternalId: '+15555550100', messageId: 'k1', caseId: 'case-1', text: 'hi' } }),
    /Messaging transports are disabled/);
});
