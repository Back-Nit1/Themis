import { DynamoActiveMenuStore, type ActiveMenuDynamoClient } from './active-menu.ts';
import { DynamoDeliveryStore, type DeliveryDynamoClient, type DeliveryStore } from './delivery-store.ts';
import { createDeliveryEventHandler, OutboundService, sendCaseNotification, TERMINAL_CASE_STATUSES } from './delivery.ts';
import { DynamoIdempotencyStore, type DynamoClient } from './idempotency.ts';
import { createSnsHandler, InboundProcessor, type Channel } from './inbound.ts';
import { AgentRuntimeConsumer, AgentRuntimeInvocationError, type AgentRuntimeClient } from './runtime.ts';
import { ChannelAdapter, type MessagingClient } from './outbound.ts';
import { SesAdapter, type SesClient } from './email.ts';
import { AMBIGUOUS_RUNTIME_FAILURE_REPLY } from './reconciler.ts';
import { OutboundMessageSchema } from '../../../packages/contracts/src/index.ts';

export interface RuntimeEnvironment {
  readonly idempotencyTable: string;
  readonly activeMenuTable: string;
  readonly agentRuntimeArn: string;
  readonly agentRuntimeQualifier?: string;
  readonly topics: Readonly<Record<string, Channel>>;
  readonly rcsEnabled: boolean;
  readonly smsEnabled: boolean;
  readonly sesEnabled: boolean;
  /** Trusted SNS topic for delivery events; absent = delivery events not consumed. Never an inbound topic. */
  readonly deliveryEventTopicArn?: string;
}

function required(env: Record<string, string | undefined>, name: string): string {
  const value = env[name]?.trim();
  if (!value) throw new Error(`Missing ${name}`);
  return value;
}

function enabled(env: Record<string, string | undefined>, name: string): boolean {
  const value = required(env, name);
  if (value !== 'true' && value !== 'false') throw new Error(`${name} must be true or false`);
  return value === 'true';
}

/** Like `enabled`, but defaults when absent instead of requiring the variable. */
function optionalEnabled(env: Record<string, string | undefined>, name: string, fallback: boolean): boolean {
  const raw = env[name];
  if (raw === undefined) return fallback;
  if (raw !== 'true' && raw !== 'false') throw new Error(`${name} must be true or false`);
  return raw === 'true';
}

function topicArn(value: string, name: string): string {
  if (!/^arn:(?:aws|aws-us-gov|aws-cn):sns:[a-z0-9-]+:\d{12}:[A-Za-z0-9_.-]+$/.test(value)) {
    throw new Error(`Invalid ${name}`);
  }
  return value;
}

export function parseRuntimeEnvironment(env: Record<string, string | undefined>): RuntimeEnvironment {
  if (required(env, 'THEMIS_MODE') !== 'aws') throw new Error('THEMIS_MODE must be aws for the Lambda runtime');
  const enableRcs = enabled(env, 'ENABLE_RCS');
  const enableSms = enabled(env, 'ENABLE_SMS_FALLBACK');
  if (!enableRcs && !enableSms) throw new Error('At least one inbound messaging channel must be enabled');
  const enableSes = optionalEnabled(env, 'ENABLE_SES', true);

  const topics: Record<string, Channel> = {};
  if (enableRcs) topics[topicArn(required(env, 'THEMIS_RCS_TOPIC_ARN'), 'THEMIS_RCS_TOPIC_ARN')] = 'RCS';
  if (enableSms) {
    const arn = topicArn(required(env, 'THEMIS_SMS_TOPIC_ARN'), 'THEMIS_SMS_TOPIC_ARN');
    if (topics[arn]) throw new Error('RCS and SMS inbound topic ARNs must be distinct');
    topics[arn] = 'SMS';
  }

  const deliveryEventTopicArn = env.THEMIS_DELIVERY_EVENT_TOPIC_ARN?.trim()
    ? topicArn(env.THEMIS_DELIVERY_EVENT_TOPIC_ARN.trim(), 'THEMIS_DELIVERY_EVENT_TOPIC_ARN') : undefined;
  if (deliveryEventTopicArn && topics[deliveryEventTopicArn]) throw new Error('Delivery event topic must differ from inbound topics');

  const idempotencyTable = required(env, 'IDEMPOTENCY_TABLE');
  const agentRuntimeArn = required(env, 'AGENT_RUNTIME_ARN');
  if (!/^arn:(?:aws|aws-us-gov|aws-cn):bedrock-agentcore:[a-z0-9-]+:\d{12}:runtime\/[A-Za-z0-9_-]+$/.test(agentRuntimeArn)) {
    throw new Error('Invalid AGENT_RUNTIME_ARN');
  }
  const qualifier = env.AGENT_RUNTIME_QUALIFIER?.trim();
  return Object.freeze({
    idempotencyTable,
    activeMenuTable: env.ACTIVE_MENU_TABLE?.trim() || idempotencyTable,
    agentRuntimeArn,
    ...(qualifier ? { agentRuntimeQualifier: qualifier } : {}),
    topics: Object.freeze(topics),
    rcsEnabled: enableRcs,
    smsEnabled: enableSms,
    sesEnabled: enableSes,
    ...(deliveryEventTopicArn ? { deliveryEventTopicArn } : {}),
  });
}

export interface RuntimeDynamoClient extends DynamoClient, ActiveMenuDynamoClient, DeliveryDynamoClient {}

export interface RuntimeDependencies {
  readonly dynamo: RuntimeDynamoClient;
  readonly agentRuntime: AgentRuntimeClient;
  /** Outbound delivery. Without `messagingClient` the agent's reply is not sent (inbound-only composition). */
  readonly messagingClient?: MessagingClient;
  readonly sesClient?: SesClient;
  /** Defaults to a Dynamo store on the idempotency table. */
  readonly deliveryStore?: DeliveryStore;
}

export { AMBIGUOUS_RUNTIME_FAILURE_REPLY } from './reconciler.ts';

type ToolEvent = { readonly themisTool?: unknown; readonly [key: string]: unknown };

export function createMessagingRuntime(env: Record<string, string | undefined>, dependencies: RuntimeDependencies) {
  const config = parseRuntimeEnvironment(env);
  const activeMenus = new DynamoActiveMenuStore(config.activeMenuTable, dependencies.dynamo);
  const runtime = new AgentRuntimeConsumer(
    config.agentRuntimeArn,
    dependencies.agentRuntime,
    config.agentRuntimeQualifier,
  );
  // Each identity is required only when its transport is enabled, and only when outbound sending is
  // actually composed (a messagingClient is injected); a disabled transport gets an empty identity, so
  // sending on it fails explicitly (messagingPayload) instead of using a dummy value.
  const channel = dependencies.messagingClient
    ? new ChannelAdapter({
        mode: 'aws',
        rcsIdentity: config.rcsEnabled ? required(env, 'THEMIS_RCS_POOL_ID') : '',
        smsIdentity: config.smsEnabled ? required(env, 'THEMIS_SMS_IDENTITY') : '',
      }, dependencies.messagingClient)
    : undefined;
  const email = config.sesEnabled && dependencies.sesClient ? new SesAdapter('aws', dependencies.sesClient) : undefined;
  const deliveries = dependencies.deliveryStore ?? new DynamoDeliveryStore(config.idempotencyTable, dependencies.dynamo);
  const outbound = channel ? new OutboundService({ channel, menus: activeMenus, deliveries, rcsEnabled: config.rcsEnabled }) : undefined;
  const processor = new InboundProcessor(
    new DynamoIdempotencyStore(config.idempotencyTable, dependencies.dynamo),
    async (message) => {
      // The customer's answer closes the menu it answered (conditionally: a newer menu survives).
      const menu = message.postback === null ? null : await activeMenus.get(message.customerExternalId);
      const answered = menu?.choices.some(c => c.postback === message.postback) ? menu : null;
      // Replies are recorded and sent after admission is claimed: a failed send is retried from the delivery
      // record on redelivery and never replays AgentCore.
      let answer;
      try {
        answer = await runtime.consume(message);
      } catch (error) {
        if (!(error instanceof AgentRuntimeInvocationError) || !outbound) throw error;
        // A timeout is ambiguous: AgentCore may already have changed case or
        // account state. Never invoke it again automatically. Record and send
        // one deterministic notice under the normal reply delivery key; if
        // this send fails, duplicate SNS delivery resumes only this message.
        await outbound.deliver(OutboundMessageSchema.parse({
          channel: message.channel, customerExternalId: message.customerExternalId,
          messageId: `reply:${message.messageId}`, caseId: 'no-case-yet',
          text: AMBIGUOUS_RUNTIME_FAILURE_REPLY,
        }), true);
        return;
      }
      if (answered) await activeMenus.clear(answered.customerExternalId, answered.caseId, answered.menuId);
      const terminal = TERMINAL_CASE_STATUSES.has(answer?.status ?? '');
      if (terminal && answer?.caseId) await activeMenus.clear(message.customerExternalId, answer.caseId);
      if (answer && outbound && message.channel !== 'EMAIL') {
        await outbound.deliver(OutboundMessageSchema.parse({
          channel: message.channel, customerExternalId: message.customerExternalId,
          messageId: `reply:${message.messageId}`, caseId: answer.caseId ?? 'no-case-yet', text: answer.reply,
          ...(answer.suggestions ? { suggestions: answer.suggestions } : {}),
        }), terminal);
      }
    },
    message => outbound ? outbound.resume(message.customerExternalId, `reply:${message.messageId}`) : Promise.resolve(false),
  );
  const snsHandler = createSnsHandler({
    topics: config.topics,
    processor,
    choicesFor: async customerExternalId => (await activeMenus.get(customerExternalId))?.choices ?? [],
  });
  // Tools-adapter Lambda invokes this function directly ({themisTool, ...}) for send_customer_message / send_case_email.
  const handleTool = async (event: ToolEvent) => {
    if (event.themisTool === 'send_customer_message') {
      if (!channel) throw new Error('Messaging transports are disabled; cannot send customer message');
      return { messageId: (await outbound!.deliver(OutboundMessageSchema.parse(event.message))).providerMessageId };
    }
    if (event.themisTool === 'send_case_email') {
      if (!email) throw new Error('SES is disabled; cannot send case email');
      return await sendCaseNotification(email, deliveries, {
        case: event.case as never, report: event.report as never, recipient: String(event.recipient),
        nextSteps: event.nextSteps as string[], sender: required(env, 'THEMIS_SES_FROM'), support: required(env, 'THEMIS_SUPPORT') });
    }
    throw new Error('Unsupported tool event');
  };
  const deliveryHandler = createDeliveryEventHandler({ topics: new Set(config.deliveryEventTopicArn ? [config.deliveryEventTopicArn] : []), store: deliveries });
  const handler = (event: unknown) => {
    if (event && typeof event === 'object' && 'themisTool' in event) return handleTool(event as ToolEvent);
    // Delivery events and inbound messages are routed by trusted topic and handled by separate boundaries.
    const topic = (event as { Records?: { Sns?: { TopicArn?: unknown } }[] } | null)?.Records?.[0]?.Sns?.TopicArn;
    return topic !== undefined && topic === config.deliveryEventTopicArn ? deliveryHandler(event) : snsHandler(event);
  };
  return Object.freeze({ config, handler, activeMenus, deliveries });
}
