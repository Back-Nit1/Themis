import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { DataStack } from '../dist/stacks/data-stack.js';
import { AgentStack } from '../dist/stacks/agent-stack.js';
import { MessagingStack } from '../dist/stacks/messaging-stack.js';
import { loadConfig } from '../dist/config/env.js';

function synth(overrides = {}) {
  const app = new cdk.App();
  const config = { ...loadConfig(), ...overrides };
  const data = new DataStack(app, 'TestData3', { config });
  const agent = new AgentStack(app, 'TestAgent2', { config, data });
  const stack = new MessagingStack(app, 'TestMessaging', { config, data, agent });
  return Template.fromStack(stack);
}

test('provisions distinct inbound and delivery-event SNS topics (no extra queue invented)', () => {
  const t = synth();
  t.resourceCountIs('AWS::SNS::Topic', 2);
  t.hasResourceProperties('AWS::SNS::Topic', { TopicName: 'ThemisInboundMessaging' });
  t.hasResourceProperties('AWS::SNS::Topic', { TopicName: 'ThemisMessagingDeliveryEvents' });
  t.resourceCountIs('AWS::SQS::Queue', 0);
});

test('subscribes the Lambda to distinct inbound and delivery handler topics', () => {
  const t = synth();
  t.resourceCountIs('AWS::SNS::Subscription', 2);
  t.hasResourceProperties('AWS::SNS::Subscription', { Protocol: 'lambda' });
  t.hasResourceProperties('AWS::Lambda::Function', {
    FunctionName: 'ThemisMessageNormalizer',
    Environment: { Variables: Match.objectLike({ THEMIS_DELIVERY_EVENT_TOPIC_ARN: Match.anyValue() }) },
  });
});

test('routes ConfigurationSet events only to the delivery topic boundary', () => {
  const t = synth({ enableSmsFallback: true });
  const topics = t.findResources('AWS::SNS::Topic');
  const deliveryId = Object.entries(topics).find(([, topic]) =>
    topic.Properties.TopicName === 'ThemisMessagingDeliveryEvents')[0];
  const inboundId = Object.entries(topics).find(([, topic]) =>
    topic.Properties.TopicName === 'ThemisInboundMessaging')[0];
  const configs = t.findResources('AWS::SMSVOICE::ConfigurationSet');
  const [config] = Object.values(configs);
  const destination = config.Properties.EventDestinations[0].SnsDestination.TopicArn;
  assert.deepEqual(destination, { Ref: deliveryId });
  assert.notDeepEqual(destination, { Ref: inboundId });
  const subscriptions = t.findResources('AWS::SNS::Subscription');
  assert.equal(Object.values(subscriptions).filter((sub) =>
    JSON.stringify(sub.Properties.TopicArn).includes(deliveryId)).length, 1);
  t.hasResourceProperties('AWS::SNS::TopicPolicy', {
    PolicyDocument: Match.objectLike({ Statement: Match.arrayWith([Match.objectLike({
      Action: 'sns:Publish', Principal: { Service: 'sms-voice.amazonaws.com' }, Resource: { Ref: deliveryId },
    })]) }),
  });
  assert.ok(t.findOutputs('DeliveryEventTopicArn').DeliveryEventTopicArn);
});

test('normalizer role can invoke the agent runtime and the idempotency table, nothing broader', () => {
  const t = synth();
  t.hasResourceProperties('AWS::IAM::Policy', {
    PolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({ Action: 'bedrock-agentcore:InvokeAgentRuntime' }),
      ]),
    }),
  });
});

test('provisions ConfigurationSet only for SMS and an inbound role for either channel', () => {
  const on = synth({ enableSmsFallback: true });
  on.resourceCountIs('AWS::SMSVOICE::ConfigurationSet', 1);

  const off = synth({ enableSmsFallback: false });
  off.resourceCountIs('AWS::SMSVOICE::ConfigurationSet', 0);
  off.resourceCountIs('AWS::SNS::TopicPolicy', 0);

  const rcsOnly = synth({ enableRcs: true, enableSmsFallback: false });
  assert.ok(rcsOnly.findOutputs('SmsTwoWayRoleArn').SmsTwoWayRoleArn);
});

test('inbound service role can publish to inbound topics but never the delivery-event topic', () => {
  const t = synth({ enableRcs: true, enableSmsFallback: true });
  const topics = t.findResources('AWS::SNS::Topic');
  const deliveryId = Object.entries(topics).find(([, topic]) =>
    topic.Properties.TopicName === 'ThemisMessagingDeliveryEvents')[0];
  const inboundIds = Object.entries(topics).filter(([, topic]) =>
    topic.Properties.TopicName !== 'ThemisMessagingDeliveryEvents').map(([id]) => id);
  const policies = t.findResources('AWS::IAM::Policy');
  const inboundPolicy = Object.values(policies).find((policy) =>
    JSON.stringify(policy.Properties.PolicyDocument).includes('sns:Publish'));
  const policyJson = JSON.stringify(inboundPolicy.Properties.PolicyDocument);
  for (const id of inboundIds) assert.ok(policyJson.includes(id), `missing inbound topic ${id}`);
  assert.equal(policyJson.includes(deliveryId), false);
});

test('normalizer Lambda environment includes only enabled-capability variables', () => {
  const rcsOnly = synth({ enableRcs: true, enableSmsFallback: false, enableSes: false });
  const rcsOnlyFn = Object.values(rcsOnly.findResources('AWS::Lambda::Function'))
    .find(f => f.Properties.FunctionName === 'ThemisMessageNormalizer');
  const rcsOnlyVars = rcsOnlyFn.Properties.Environment.Variables;
  assert.ok('THEMIS_RCS_POOL_ID' in rcsOnlyVars);
  assert.ok(!('THEMIS_SMS_IDENTITY' in rcsOnlyVars));
  assert.ok(!('THEMIS_SES_FROM' in rcsOnlyVars));
  assert.ok(!('THEMIS_SUPPORT' in rcsOnlyVars));

  const smsOnly = synth({ enableRcs: false, enableSmsFallback: true, enableSes: true });
  const smsOnlyFn = Object.values(smsOnly.findResources('AWS::Lambda::Function'))
    .find(f => f.Properties.FunctionName === 'ThemisMessageNormalizer');
  const smsOnlyVars = smsOnlyFn.Properties.Environment.Variables;
  assert.ok(!('THEMIS_RCS_POOL_ID' in smsOnlyVars));
  assert.ok('THEMIS_SMS_IDENTITY' in smsOnlyVars);
  assert.ok('THEMIS_SES_FROM' in smsOnlyVars);
  assert.ok('THEMIS_SUPPORT' in smsOnlyVars);
});

test('SES IAM permission granted only when enabled', () => {
  const on = synth({ enableSes: true });
  assert.match(JSON.stringify(on.findResources('AWS::IAM::Policy')), /ses:SendEmail/);
  const off = synth({ enableSes: false });
  assert.doesNotMatch(JSON.stringify(off.findResources('AWS::IAM::Policy')), /ses:SendEmail/);
});

test('does not provision a real phone number or pool (manual step, not reproducible in CDK)', () => {
  const t = synth();
  t.resourceCountIs('AWS::SMSVOICE::PhoneNumber', 0);
  t.resourceCountIs('AWS::SMSVOICE::Pool', 0);
});

test('claim reconciler: scheduled, bounded, and reads open claims through a sparse KEYS_ONLY index (no Scan)', () => {
  const app = new cdk.App();
  const config = loadConfig();
  const data = new DataStack(app, 'RData', { config });
  const agent = new AgentStack(app, 'RAgent', { config, data });
  const msg = new MessagingStack(app, 'RMessaging', { config, data, agent });
  Template.fromStack(data).hasResourceProperties('AWS::DynamoDB::Table', {
    GlobalSecondaryIndexes: [Match.objectLike({
      IndexName: 'ClaimStateIndex', Projection: { ProjectionType: 'KEYS_ONLY' },
      KeySchema: [{ AttributeName: 'claimState', KeyType: 'HASH' }, { AttributeName: 'claimedAt', KeyType: 'RANGE' }],
    })],
  });
  const t = Template.fromStack(msg);
  t.hasResourceProperties('AWS::Lambda::Function', {
    FunctionName: 'ThemisClaimReconciler', Handler: 'reconciler.handler', Timeout: 60,
    Environment: { Variables: Match.objectLike({ RECONCILE_STALE_SECONDS: '300', RECONCILE_BATCH_SIZE: '25', RECONCILE_MAX_ATTEMPTS: '3', RECONCILE_BUDGET_SECONDS: '40' }) },
  });
  t.hasResourceProperties('AWS::Lambda::EventInvokeConfig', { MaximumRetryAttempts: 0 });
  t.hasResourceProperties('AWS::Events::Rule', { ScheduleExpression: 'rate(5 minutes)', State: 'ENABLED' });
});

function roleActions(t, descriptionPrefix) {
  const roles = t.findResources('AWS::IAM::Role');
  const roleId = Object.entries(roles).find(([, r]) => r.Properties.Description?.startsWith(descriptionPrefix))[0];
  const policy = Object.values(t.findResources('AWS::IAM::Policy'))
    .find(p => p.Properties.Roles?.some(r => r.Ref === roleId));
  return policy.Properties.PolicyDocument.Statement.flatMap(s => [].concat(s.Action));
}

test('claim reconciler role: least privilege (no AgentCore, SES, SNS, DeleteItem, Scan, or wildcard actions)', () => {
  const t = synth({ enableRcs: true, enableSmsFallback: true });
  const roles = t.findResources('AWS::IAM::Role');
  const roleId = Object.entries(roles).find(([, r]) => r.Properties.Description?.startsWith('Scheduled claim reconciler'))[0];
  const policy = Object.values(t.findResources('AWS::IAM::Policy'))
    .find(p => p.Properties.Roles?.some(r => r.Ref === roleId));
  const statements = policy.Properties.PolicyDocument.Statement;
  const actions = statements.flatMap(s => [].concat(s.Action)).sort();
  assert.deepEqual(actions, ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:Query', 'dynamodb:UpdateItem', 'sms-voice:SendRcsMessage', 'sms-voice:SendTextMessage']);
  const query = statements.find(s => s.Action === 'dynamodb:Query');
  assert.match(JSON.stringify(query.Resource), /index\/ClaimStateIndex/);
  assert.doesNotMatch(JSON.stringify(policy.Properties.PolicyDocument), /bedrock-agentcore|ses:|sns:|"\*"/);
});

test('normalizer and reconciler sms-voice grants are scoped to enabled transports only', () => {
  const smsOnly = synth({ enableRcs: false, enableSmsFallback: true });
  assert.deepEqual(
    roleActions(smsOnly, 'Scheduled claim reconciler').filter(a => a.startsWith('sms-voice')).sort(),
    ['sms-voice:SendTextMessage'],
  );
  assert.deepEqual(
    roleActions(smsOnly, 'Execution role for the inbound').filter(a => a.startsWith('sms-voice')).sort(),
    ['sms-voice:SendTextMessage'],
  );

  const rcsOnly = synth({ enableRcs: true, enableSmsFallback: false });
  assert.deepEqual(
    roleActions(rcsOnly, 'Scheduled claim reconciler').filter(a => a.startsWith('sms-voice')).sort(),
    ['sms-voice:SendRcsMessage', 'sms-voice:SendTextMessage'],
  );
  assert.deepEqual(
    roleActions(rcsOnly, 'Execution role for the inbound').filter(a => a.startsWith('sms-voice')).sort(),
    ['sms-voice:SendRcsMessage', 'sms-voice:SendTextMessage'],
  );
});
