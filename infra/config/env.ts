// Deployment-time configuration. Values come from process.env (shell / CI) so the
// same infra/** code works for every environment without editing source.
// Application code reads its own copy of these flags from Lambda/Runtime
// environment variables wired below - this file is the single source infra uses
// to populate those environment variables consistently across stacks.

export type ThemisMode = 'local' | 'aws';

export interface ThemisConfig {
  readonly themisMode: ThemisMode;
  readonly region: string;
  readonly account?: string;
  readonly enableRcs: boolean;
  readonly enableSmsFallback: boolean;
  readonly enableSes: boolean;
  readonly enableBrowserResearch: boolean;
  /** Separate approval control; a feature toggle alone never grants Browser IAM. */
  readonly browserResearchApproved: boolean;
  readonly enableProactiveDetection: boolean;
  readonly enableReasoningEscalation: boolean;
  readonly bedrockModelIdFast: string;
  readonly bedrockModelIdReasoning: string;
  readonly sesSenderDomain: string;
  readonly rcsPoolId: string;
  readonly smsIdentity: string;
  readonly sesFromAddress: string;
  readonly supportContact: string;
  readonly provisionalCreditAutoApproveLimit: number;
  readonly creditConfidenceThreshold: number;
}

function bool(name: string, fallback: boolean): boolean {
  const raw = process.env[name];
  if (raw === undefined) return fallback;
  return raw === '1' || raw.toLowerCase() === 'true';
}

function finiteNumber(name: string, raw: string | undefined, fallback: number): number {
  const value = raw === undefined ? fallback : Number(raw);
  if (!Number.isFinite(value)) throw new Error(`${name} must be a finite number`);
  return value;
}

export function loadConfig(): ThemisConfig {
  const themisMode = process.env.THEMIS_MODE ?? 'local';
  if (themisMode !== 'local' && themisMode !== 'aws') throw new Error('THEMIS_MODE must be local or aws');

  // No hardcoded default: the exact Bedrock model ID/ARN to use is a product
  // decision for the agentcore workstream, and Bedrock model IDs change over
  // time. AWS-mode deploys must set these explicitly; local mode does not call
  // Bedrock, so a placeholder is fine there.
  const requireForAwsMode = (raw: string | undefined, name: string): string => {
    if (raw) return raw;
    if (themisMode === 'aws') {
      throw new Error(`${name} must be set when THEMIS_MODE=aws (see infra/config/env.ts)`);
    }
    return `unset-${name.toLowerCase()}`;
  };

  const provisionalCreditAutoApproveLimit = finiteNumber(
    'DEMO_AUTONOMOUS_CREDIT_LIMIT',
    process.env.DEMO_AUTONOMOUS_CREDIT_LIMIT ?? process.env.PROVISIONAL_CREDIT_AUTO_APPROVE_LIMIT,
    50,
  );
  const creditConfidenceThreshold = finiteNumber(
    'DEMO_CREDIT_CONFIDENCE_THRESHOLD', process.env.DEMO_CREDIT_CONFIDENCE_THRESHOLD, 0.8,
  );
  if (provisionalCreditAutoApproveLimit < 0) throw new Error('DEMO_AUTONOMOUS_CREDIT_LIMIT must be nonnegative');
  if (creditConfidenceThreshold < 0 || creditConfidenceThreshold > 1) {
    throw new Error('DEMO_CREDIT_CONFIDENCE_THRESHOLD must be between 0 and 1');
  }

  const enableRcs = bool('ENABLE_RCS', false);
  const enableSmsFallback = bool('ENABLE_SMS_FALLBACK', true);
  const enableSes = bool('ENABLE_SES', true);

  return {
    themisMode,
    region: process.env.AWS_REGION ?? process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
    account: process.env.CDK_DEFAULT_ACCOUNT,
    enableRcs,
    enableSmsFallback,
    enableSes,
    enableBrowserResearch: bool('ENABLE_BROWSER_RESEARCH', false),
    browserResearchApproved: bool('BROWSER_RESEARCH_APPROVED', false),
    enableProactiveDetection: bool('ENABLE_PROACTIVE_DETECTION', false),
    enableReasoningEscalation: bool('ENABLE_REASONING_ESCALATION', true),
    bedrockModelIdFast: requireForAwsMode(process.env.BEDROCK_MODEL_ID_FAST, 'BEDROCK_MODEL_ID_FAST'),
    bedrockModelIdReasoning: requireForAwsMode(process.env.BEDROCK_MODEL_ID_REASONING, 'BEDROCK_MODEL_ID_REASONING'),
    sesSenderDomain: process.env.SES_SENDER_DOMAIN ?? 'themis-demo.example',
    // Each identity is required only when its transport is enabled: a disabled
    // transport must not force a dummy identifier or construct an unused client.
    rcsPoolId: enableRcs ? requireForAwsMode(process.env.THEMIS_RCS_POOL_ID, 'THEMIS_RCS_POOL_ID') : (process.env.THEMIS_RCS_POOL_ID ?? ''),
    smsIdentity: enableSmsFallback ? requireForAwsMode(process.env.THEMIS_SMS_IDENTITY, 'THEMIS_SMS_IDENTITY') : (process.env.THEMIS_SMS_IDENTITY ?? ''),
    sesFromAddress: enableSes ? requireForAwsMode(process.env.THEMIS_SES_FROM, 'THEMIS_SES_FROM') : (process.env.THEMIS_SES_FROM ?? ''),
    supportContact: enableSes ? requireForAwsMode(process.env.THEMIS_SUPPORT, 'THEMIS_SUPPORT') : (process.env.THEMIS_SUPPORT ?? ''),
    provisionalCreditAutoApproveLimit,
    creditConfidenceThreshold,
  };
}

function isUnset(value: string): boolean {
  return !value || value.startsWith('unset-');
}

/** Fail-fast validation run immediately before a live CDK deployment. */
export function validateDeploymentPreflight(config: ThemisConfig): void {
  const problems: string[] = [];
  if (config.themisMode !== 'aws') problems.push('THEMIS_MODE must be aws');
  if (isUnset(config.bedrockModelIdFast)) problems.push('BEDROCK_MODEL_ID_FAST must be set');
  if (isUnset(config.bedrockModelIdReasoning)) problems.push('BEDROCK_MODEL_ID_REASONING must be set');
  if (!config.enableRcs && !config.enableSmsFallback) {
    problems.push('at least one of ENABLE_RCS or ENABLE_SMS_FALLBACK must be true');
  }
  if (config.enableRcs && isUnset(config.rcsPoolId)) problems.push('THEMIS_RCS_POOL_ID must be set');
  if (config.enableSmsFallback && isUnset(config.smsIdentity)) problems.push('THEMIS_SMS_IDENTITY must be set');
  if (config.enableSes) {
    if (isUnset(config.sesFromAddress)) problems.push('THEMIS_SES_FROM must be set');
    if (!config.sesFromAddress.includes('@')) problems.push('THEMIS_SES_FROM must be an email address');
    if (!config.sesSenderDomain || config.sesSenderDomain.endsWith('.example')) {
      problems.push('SES_SENDER_DOMAIN must name the verified SES identity');
    } else if (!config.sesFromAddress.endsWith(`@${config.sesSenderDomain}`)) {
      problems.push('THEMIS_SES_FROM must belong to SES_SENDER_DOMAIN');
    }
    if (isUnset(config.supportContact)) problems.push('THEMIS_SUPPORT must be set');
  }
  if (problems.length) throw new Error(`AWS deployment preflight failed:\n- ${problems.join('\n- ')}`);
}
