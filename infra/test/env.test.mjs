import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig, validateDeploymentPreflight } from '../dist/config/env.js';

function deployConfig(overrides = {}) {
  return {
    ...loadConfig(),
    themisMode: 'aws',
    enableRcs: true,
    enableSmsFallback: true,
    enableSes: true,
    bedrockModelIdFast: 'model.fast-v1',
    bedrockModelIdReasoning: 'model.reasoning-v1',
    rcsPoolId: 'pool-123',
    smsIdentity: '+15555550199',
    sesSenderDomain: 'demo.test',
    sesFromAddress: 'themis@demo.test',
    supportContact: 'support@demo.test',
    ...overrides,
  };
}

test('deployment preflight accepts a complete AWS configuration', () => {
  assert.doesNotThrow(() => validateDeploymentPreflight(deployConfig()));
});

test('deployment preflight rejects local mode and missing runtime identities', () => {
  assert.throws(() => validateDeploymentPreflight(deployConfig({
    themisMode: 'local',
    bedrockModelIdFast: 'unset-bedrock_model_id_fast',
    rcsPoolId: 'unset-themis_rcs_pool_id',
    smsIdentity: 'unset-themis_sms_identity',
    sesFromAddress: 'unset-themis_ses_from',
    supportContact: 'unset-themis_support',
  })), /THEMIS_MODE must be aws[\s\S]*BEDROCK_MODEL_ID_FAST[\s\S]*THEMIS_RCS_POOL_ID[\s\S]*THEMIS_SMS_IDENTITY[\s\S]*THEMIS_SES_FROM[\s\S]*THEMIS_SUPPORT/);
});

test('deployment preflight validates SES sender identity alignment', () => {
  assert.throws(() => validateDeploymentPreflight(deployConfig({ sesFromAddress: 'themis@other.test' })),
    /THEMIS_SES_FROM must belong to SES_SENDER_DOMAIN/);
});

test('deployment preflight requires at least one inbound channel', () => {
  assert.throws(() => validateDeploymentPreflight(deployConfig({ enableRcs: false, enableSmsFallback: false })),
    /at least one of ENABLE_RCS or ENABLE_SMS_FALLBACK must be true/);
});

test('RCS-only deployment does not require SMS or SES identities', () => {
  assert.doesNotThrow(() => validateDeploymentPreflight(deployConfig({
    enableRcs: true, enableSmsFallback: false, enableSes: false,
    smsIdentity: '', sesFromAddress: '', supportContact: '',
  })));
});

test('SMS-only deployment does not require RCS or SES identities', () => {
  assert.doesNotThrow(() => validateDeploymentPreflight(deployConfig({
    enableRcs: false, enableSmsFallback: true, enableSes: false,
    rcsPoolId: '', sesFromAddress: '', supportContact: '',
  })));
});

test('deployment preflight requires THEMIS_RCS_POOL_ID only when RCS is enabled', () => {
  assert.throws(() => validateDeploymentPreflight(deployConfig({ enableRcs: true, rcsPoolId: '' })),
    /THEMIS_RCS_POOL_ID must be set/);
  assert.doesNotThrow(() => validateDeploymentPreflight(deployConfig({ enableRcs: false, rcsPoolId: '' })));
});

test('deployment preflight requires THEMIS_SMS_IDENTITY only when SMS fallback is enabled', () => {
  assert.throws(() => validateDeploymentPreflight(deployConfig({ enableSmsFallback: true, smsIdentity: '' })),
    /THEMIS_SMS_IDENTITY must be set/);
  assert.doesNotThrow(() => validateDeploymentPreflight(deployConfig({
    enableRcs: true, enableSmsFallback: false, smsIdentity: '',
  })));
});

test('deployment preflight skips SES identity checks when SES is disabled', () => {
  assert.doesNotThrow(() => validateDeploymentPreflight(deployConfig({
    enableSes: false, sesFromAddress: '', supportContact: '', sesSenderDomain: 'themis-demo.example',
  })));
  assert.throws(() => validateDeploymentPreflight(deployConfig({ enableSes: true, sesFromAddress: '' })),
    /THEMIS_SES_FROM must be set/);
});
