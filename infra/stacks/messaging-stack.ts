import * as cdk from 'aws-cdk-lib';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subs from 'aws-cdk-lib/aws-sns-subscriptions';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as smsvoice from 'aws-cdk-lib/aws-smsvoice';
import { Construct } from 'constructs';
import * as path from 'path';
import type { ThemisConfig } from '../config/env';
import type { DataStack } from './data-stack';
import { MESSAGING_FUNCTION_NAME, type AgentStack } from './agent-stack';

export interface MessagingStackProps extends cdk.StackProps {
  readonly config: ThemisConfig;
  readonly data: DataStack;
  readonly agent: AgentStack;
}

/**
 * Inbound customer messaging (prompt.md #30-#33): trusted channel topic(s)
 * subscribe one normalizer Lambda that forwards into the AgentCore Runtime.
 * Delivery telemetry uses a distinct SNS topic routed to the delivery-only handler boundary. No SQS queue is
 * needed: SNS -> Lambda retries plus the idempotency table cover inbound retry
 * safety for a hackathon-scale demo.
 *
 * What CDK does NOT provision here, and why:
 * - RCS sender registration (AWS End User Messaging Social) has no
 *   CloudFormation resource type as of this aws-cdk-lib version - it's
 *   console/API-only. Manual step, documented below.
 * - SMS origination (a real phone number/pool via AWS::SMSVOICEV2) requires
 *   carrier registration that can take days and has an ongoing per-number
 *   cost; provisioning it via CDK would not be reproducible on a hackathon
 *   timeline. Manual step, documented below. The reusable, immediately
 *   reproducible piece - the ConfigurationSet, the two-way IAM role, and the
 *   SNS topic - is provisioned here so attaching a manually-created number is
 *   a one-field console edit.
 */
export class MessagingStack extends cdk.Stack {
  /** SMS inbound topic (or the only enabled channel's topic). */
  public readonly inboundTopic: sns.Topic;
  /** Distinct RCS topic when both channels are enabled: plain RCS and SMS payloads cannot be told apart. */
  public readonly rcsInboundTopic: sns.Topic;
  /** Delivery telemetry only; never carries customer messages. */
  public readonly deliveryEventTopic: sns.Topic;
  public readonly normalizerFunction: lambda.Function;
  public readonly normalizerLogGroup: logs.LogGroup;
  public readonly reconcilerFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: MessagingStackProps) {
    super(scope, id, props);
    const { config, data, agent } = props;

    this.inboundTopic = new sns.Topic(this, 'ThemisInboundMessaging', {
      topicName: 'ThemisInboundMessaging',
      displayName: 'Themis inbound RCS/SMS messages',
    });

    this.rcsInboundTopic = config.enableRcs && config.enableSmsFallback
      ? new sns.Topic(this, 'ThemisInboundRcs', { topicName: 'ThemisInboundRcs', displayName: 'Themis inbound RCS messages' })
      : this.inboundTopic;

    this.deliveryEventTopic = new sns.Topic(this, 'ThemisMessagingDeliveryEvents', {
      topicName: 'ThemisMessagingDeliveryEvents',
      displayName: 'Themis outbound messaging delivery events',
    });

    const normalizerRole = new iam.Role(this, 'NormalizerRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Execution role for the inbound message normalizer Lambda',
    });
    normalizerRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
    );
    data.idempotencyTable.grantReadWriteData(normalizerRole);
    normalizerRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
      resources: [agent.runtime.attrAgentRuntimeArn],
    }));

    // Outbound: agent replies and the send_* tools. Identities are provisioned manually (see infra README).
    // Plain-text sends (including RCS text without suggestions) always use SendTextMessage; rich RCS
    // suggestions additionally need SendRcsMessage, granted only when RCS is enabled.
    const smsVoiceActions = [
      ...(config.enableRcs || config.enableSmsFallback ? ['sms-voice:SendTextMessage'] : []),
      ...(config.enableRcs ? ['sms-voice:SendRcsMessage'] : []),
    ];
    if (smsVoiceActions.length) {
      normalizerRole.addToPolicy(new iam.PolicyStatement({
        actions: smsVoiceActions,
        resources: [`arn:aws:sms-voice:${this.region}:${this.account}:*`],
      }));
    }
    if (config.enableSes) {
      normalizerRole.addToPolicy(new iam.PolicyStatement({
        actions: ['ses:SendEmail'],
        resources: [`arn:aws:ses:${this.region}:${this.account}:identity/${config.sesSenderDomain}`],
      }));
    }

    this.normalizerLogGroup = new logs.LogGroup(this, 'NormalizerLogGroup', {
      logGroupName: '/aws/lambda/ThemisMessageNormalizer',
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    this.normalizerFunction = new lambda.Function(this, 'NormalizerFunction', {
      functionName: MESSAGING_FUNCTION_NAME,
      runtime: lambda.Runtime.NODEJS_22_X,
      handler: 'index.handler',
      // esbuild bundle of services/messaging/src/aws-entry.ts (npm --prefix services/messaging run build)
      code: lambda.Code.fromAsset(path.join(__dirname, '../../../services/messaging/dist')),
      role: normalizerRole,
      timeout: cdk.Duration.seconds(15),
      memorySize: 256,
      logGroup: this.normalizerLogGroup,
      environment: {
        THEMIS_MODE: 'aws', // the bundled Lambda entry only supports aws; local mode is the in-process test composition
        IDEMPOTENCY_TABLE: data.idempotencyTable.tableName,
        AGENT_RUNTIME_ARN: agent.runtime.attrAgentRuntimeArn,
        ENABLE_RCS: String(config.enableRcs),
        ENABLE_SMS_FALLBACK: String(config.enableSmsFallback),
        ENABLE_SES: String(config.enableSes),
        ...(config.enableRcs ? { THEMIS_RCS_TOPIC_ARN: this.rcsInboundTopic.topicArn, THEMIS_RCS_POOL_ID: config.rcsPoolId } : {}),
        ...(config.enableSmsFallback ? { THEMIS_SMS_TOPIC_ARN: this.inboundTopic.topicArn, THEMIS_SMS_IDENTITY: config.smsIdentity } : {}),
        THEMIS_DELIVERY_EVENT_TOPIC_ARN: this.deliveryEventTopic.topicArn,
        ...(config.enableSes ? { THEMIS_SES_FROM: config.sesFromAddress, THEMIS_SUPPORT: config.supportContact } : {}),
      },
    });
    for (const topic of new Set([this.inboundTopic, this.rcsInboundTopic])) {
      topic.addSubscription(new subs.LambdaSubscription(this.normalizerFunction));
    }
    this.deliveryEventTopic.addSubscription(new subs.LambdaSubscription(this.normalizerFunction));

    // Durable reconciliation of claims stranded after admission (crash/timeout before a reply was delivered). Sparse
    // KEYS_ONLY index (claimState, claimedAt) lets the reconciler Query open claims instead of Scanning the table. Added here
    // so the reconciler and its index ship together; DataStack is untouched.
    const CLAIM_INDEX = 'ClaimStateIndex';
    data.idempotencyTable.addGlobalSecondaryIndex({
      indexName: CLAIM_INDEX,
      partitionKey: { name: 'claimState', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'claimedAt', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });
    // Separate role: no AgentCore (it can never re-run a turn), no SES, no DeleteItem, no Scan, no SNS.
    const reconcilerRole = new iam.Role(this, 'ReconcilerRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Scheduled claim reconciler: resumes recorded replies, sends safe notices, flags claims for review',
    });
    reconcilerRole.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'));
    reconcilerRole.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
      resources: [data.idempotencyTable.tableArn],
    }));
    reconcilerRole.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:Query'],
      resources: [`${data.idempotencyTable.tableArn}/index/${CLAIM_INDEX}`],
    }));
    if (smsVoiceActions.length) {
      reconcilerRole.addToPolicy(new iam.PolicyStatement({
        actions: smsVoiceActions,
        resources: [`arn:aws:sms-voice:${this.region}:${this.account}:*`],
      }));
    }
    const reconcilerLogGroup = new logs.LogGroup(this, 'ReconcilerLogGroup', {
      logGroupName: '/aws/lambda/ThemisClaimReconciler',
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    this.reconcilerFunction = new lambda.Function(this, 'ReconcilerFunction', {
      functionName: 'ThemisClaimReconciler',
      runtime: lambda.Runtime.NODEJS_22_X,
      handler: 'reconciler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../../services/messaging/dist')),
      role: reconcilerRole,
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      retryAttempts: 0, // the next schedule tick is the retry; leases make overlap safe
      logGroup: reconcilerLogGroup,
      environment: {
        IDEMPOTENCY_TABLE: data.idempotencyTable.tableName,
        ENABLE_RCS: String(config.enableRcs),
        ENABLE_SMS_FALLBACK: String(config.enableSmsFallback),
        ...(config.enableRcs ? { THEMIS_RCS_POOL_ID: config.rcsPoolId } : {}),
        ...(config.enableSmsFallback ? { THEMIS_SMS_IDENTITY: config.smsIdentity } : {}),
        // Bounds. Stale threshold must stay well above the normalizer timeout (15s) so the original attempt is dead.
        RECONCILE_STALE_SECONDS: '300',
        RECONCILE_LEASE_SECONDS: '240',
        RECONCILE_BATCH_SIZE: '25',
        RECONCILE_MAX_ATTEMPTS: '3',
        RECONCILE_BUDGET_SECONDS: '40',
      },
    });
    new events.Rule(this, 'ReconcilerSchedule', {
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
      targets: [new targets.LambdaFunction(this.reconcilerFunction, { retryAttempts: 0 })],
    });

    // Role the service assumes to publish inbound customer messages. It can
    // publish only to enabled inbound topics, never the delivery-event topic.
    if (config.enableRcs || config.enableSmsFallback) {
      const smsTwoWayRole = new iam.Role(this, 'SmsTwoWayRole', {
        assumedBy: new iam.ServicePrincipal('sms-voice.amazonaws.com', {
          conditions: { StringEquals: { 'aws:SourceAccount': this.account } },
        }),
        description: 'Assumed by AWS End User Messaging to publish inbound customer messages',
      });
      for (const topic of new Set([this.inboundTopic, this.rcsInboundTopic])) {
        topic.grantPublish(smsTwoWayRole);
      }
      new cdk.CfnOutput(this, 'SmsTwoWayRoleArn', { value: smsTwoWayRole.roleArn });
    }

    if (config.enableSmsFallback) {
      this.deliveryEventTopic.addToResourcePolicy(new iam.PolicyStatement({
        sid: 'AllowSmsVoiceDeliveryEvents',
        principals: [new iam.ServicePrincipal('sms-voice.amazonaws.com')],
        actions: ['sns:Publish'],
        resources: [this.deliveryEventTopic.topicArn],
        conditions: {
          StringEquals: { 'aws:SourceAccount': this.account },
          ArnLike: {
            'aws:SourceArn': `arn:aws:sms-voice:${this.region}:${this.account}:configuration-set/ThemisSmsConfigurationSet`,
          },
        },
      }));
      new smsvoice.CfnConfigurationSet(this, 'SmsConfigurationSet', {
        configurationSetName: 'ThemisSmsConfigurationSet',
        eventDestinations: [{
          eventDestinationName: 'delivery-events-to-sns',
          enabled: true,
          // Verify this matching-event-types value against current
          // AWS::SMSVOICEV2::ConfigurationSet docs before deploy.
          matchingEventTypes: ['ALL'],
          snsDestination: { topicArn: this.deliveryEventTopic.topicArn },
        }],
      });

    }

    new cdk.CfnOutput(this, 'InboundTopicArn', { value: this.inboundTopic.topicArn });
    new cdk.CfnOutput(this, 'RcsInboundTopicArn', { value: this.rcsInboundTopic.topicArn });
    new cdk.CfnOutput(this, 'DeliveryEventTopicArn', { value: this.deliveryEventTopic.topicArn });
    new cdk.CfnOutput(this, 'ReconcilerFunctionName', { value: this.reconcilerFunction.functionName });
    new cdk.CfnOutput(this, 'NormalizerFunctionName', { value: this.normalizerFunction.functionName });
  }
}
