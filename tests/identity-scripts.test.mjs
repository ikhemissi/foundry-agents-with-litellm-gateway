import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { azureClient } from '../scripts/identity-common.mjs';
import { mergedGateway, prepareRegistration } from '../scripts/prepare-entra-registration.mjs';
import { configurePromptOAuth, verifyCredential } from '../scripts/configure-prompt-oauth.mjs';
import { revokeHostedKeyAccess } from '../scripts/revoke-hosted-key-access.mjs';
import { validateAuthConfig } from '../scripts/validate-auth-config.mjs';

const id = n => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const now = Date.parse('2026-10-16T12:00:00Z');
const config = {
  tenantId: id(1), subscriptionId: id(2), resourceGroup: 'rg-test',
  consumerFoundryName: 'consumer', consumerProjectName: 'consumer-project',
  vaultName: 'test-vault', secretName: 'prompt-oauth',
  litellmEndpoint: 'https://gateway.example', modelDeploymentName: 'gpt-5.4-nano',
  registrationKey: 'test',
  gateway: { objectId: id(3), clientId: id(4), principalId: id(5) },
  prompt: { objectId: id(6), clientId: id(7), principalId: id(8) },
  gatewayScopeId: id(9),
};
const password = {
  keyId: id(10), startDateTime: '2026-10-01T00:00:00Z', endDateTime: '2026-12-01T00:00:00Z',
};
const unrelated = { appId: id(20), delegatedPermissionIds: [id(21)] };

function fixture({ hasSecret = true } = {}) {
  const calls = [];
  const applications = new Map([
    [config.gateway.objectId, {
      id: config.gateway.objectId, appId: config.gateway.clientId, uniqueName: 'test-gateway',
      displayName: 'Preserve name', identifierUris: [`api://${config.gateway.clientId}`, 'https://other.example/api'],
      api: { requestedAccessTokenVersion: 2, acceptMappedClaims: false,
        preAuthorizedApplications: [unrelated],
        oauth2PermissionScopes: [{ id: id(9), value: 'gateway.invoke', isEnabled: true },
          { id: id(22), value: 'other.scope', isEnabled: true }] },
      web: { redirectUris: ['https://other.example/signin'] },
      passwordCredentials: [{ keyId: id(23) }], keyCredentials: [{ keyId: id(24) }],
      optionalClaims: {
        idToken: [{ name: 'email', essential: false }],
        accessToken: ['idtyp', 'xms_act_fct', 'xms_sub_fct'].map(name => ({ name, essential: false })),
      },
    }],
    [config.prompt.objectId, {
      id: config.prompt.objectId, appId: config.prompt.clientId, uniqueName: 'test-prompt',
      passwordCredentials: [structuredClone(password)], web: { redirectUris: ['https://prompt.example'] },
    }],
  ]);
  const principals = new Map([config.gateway, config.prompt].map(identity => [
    identity.principalId, { id: identity.principalId, appId: identity.clientId,
      servicePrincipalType: 'Application', accountEnabled: true },
  ]));
  let secret = hasSecret ? {
    value: 'test-only-secret-never-log', attributes: { enabled: true, exp: Date.parse(password.endDateTime) / 1000 },
    tags: { ...config.prompt, tenantId: config.tenantId, keyId: password.keyId },
  } : null;
  let connection = { properties: { authType: 'ApiKey', metadata: {
    unrelated: 'preserve', authConfig: JSON.stringify({ type: 'api_key', name: 'Authorization' }),
  } } };
  const graph = async (method, path, body) => {
    calls.push(['graph', method, path, structuredClone(body)]);
    const [, type, objectId, operation] = path.split('/');
    if (method === 'GET') return structuredClone((type === 'applications' ? applications : principals).get(objectId));
    if (method === 'PATCH') {
      Object.assign(applications.get(objectId), structuredClone(body));
      return null;
    }
    if (operation === 'addPassword') {
      const created = { keyId: id(30), secretText: 'new-test-only-secret', startDateTime: new Date(now).toISOString(),
        endDateTime: body.passwordCredential.endDateTime };
      applications.get(objectId).passwordCredentials.push(created);
      return created;
    }
    throw new Error('Unexpected Graph call');
  };
  const vault = async (method, path, body) => {
    calls.push(['vault', method, path]);
    if (method === 'PUT') secret = structuredClone(body);
    return structuredClone(secret);
  };
  const arm = async (method, path, body) => {
    calls.push(['arm', method, path]);
    if (method === 'PUT') connection = structuredClone(body);
    return structuredClone(connection);
  };
  const verify = async (input, value) => {
    calls.push(['verify']);
    assert.equal(input.prompt.clientId, config.prompt.clientId);
    assert.ok(value.endsWith('secret') || value.endsWith('log'));
  };
  return { calls, applications, principals, graph, vault, arm, verify,
    get secret() { return secret; }, get connection() { return connection; } };
}
const options = f => ({ ...f, approved: true, now });
const mutations = calls => calls.filter(call => ['POST', 'PUT', 'PATCH', 'DELETE'].includes(call[1]));

test('all mutating identity entry points require explicit approval before reads or writes', async () => {
  const f = fixture();
  for (const run of [prepareRegistration, configurePromptOAuth, revokeHostedKeyAccess]) {
    await assert.rejects(run(config, { ...options(f), approved: false }), /approval/);
  }
  assert.deepEqual(f.calls, []);
  const yaml = readFileSync(new URL('../azure.yaml', import.meta.url), 'utf8');
  assert.doesNotMatch(yaml, /configure-prompt-oauth|prepare-entra-registration|revoke-hosted-key-access/);
});

test('adoption merges api arrays and immutable uniqueName without sending any credentials or unrelated fields', async () => {
  const f = fixture();
  f.applications.get(config.gateway.objectId).uniqueName = null;
  const before = structuredClone(f.applications.get(config.gateway.objectId));
  await prepareRegistration({ ...config, preauthorizeAzureCli: true }, options(f));
  const app = f.applications.get(config.gateway.objectId);
  assert.equal(app.uniqueName, 'test-gateway');
  assert.deepEqual(app.passwordCredentials, before.passwordCredentials);
  assert.deepEqual(app.keyCredentials, before.keyCredentials);
  assert.deepEqual(app.web, before.web);
  assert.deepEqual(app.api.oauth2PermissionScopes, before.api.oauth2PermissionScopes);
  assert.ok(app.api.preAuthorizedApplications.some(item => item.appId === unrelated.appId));
  const writes = mutations(f.calls);
  assert.equal(writes.length, 1);
  assert.deepEqual(Object.keys(writes[0][3]).sort(), ['api', 'identifierUris', 'optionalClaims', 'uniqueName']);
  const count = writes.length;
  await prepareRegistration({ ...config, preauthorizeAzureCli: true }, options(f));
  assert.equal(mutations(f.calls).length, count, 'a repeated adoption must be a no-op');
});

test('adoption refuses immutable-name/scope conflicts and validates both identities before mutations', async () => {
  for (const change of [
    f => { f.applications.get(config.prompt.objectId).uniqueName = 'someone-elses-key'; },
    f => { f.principals.get(config.prompt.principalId).appId = id(88); },
    f => { f.applications.get(config.gateway.objectId).api.oauth2PermissionScopes[0].id = id(88); },
  ]) {
    const f = fixture();
    change(f);
    await assert.rejects(prepareRegistration(config, options(f)));
    assert.deepEqual(mutations(f.calls), []);
  }
});

test('scope creation is stable on repeat and optional preauthorization never deletes unrelated clients', () => {
  const app = { appId: id(4), identifierUris: ['https://existing.example'], api: {
    acceptMappedClaims: true, oauth2PermissionScopes: [], preAuthorizedApplications: [unrelated],
  } };
  const first = mergedGateway(app, id(9), true);
  const second = mergedGateway({ ...app, ...first }, id(9), true);
  assert.deepEqual(second, first);
  assert.equal(first.api.requestedAccessTokenVersion, 2);
  assert.equal(first.api.acceptMappedClaims, true);
  assert.equal(first.api.oauth2PermissionScopes[0].id, id(9));
  assert.deepEqual(first.api.preAuthorizedApplications.find(item => item.appId === unrelated.appId), unrelated);
  assert.deepEqual(first.optionalClaims.accessToken.map(claim => claim.name), ['idtyp', 'xms_act_fct', 'xms_sub_fct']);
});

test('registration adds Agent ID/access-token claims without replacing existing optional claim settings', () => {
  const existing = {
    accessToken: [{ name: 'idtyp', essential: true, additionalProperties: ['preserve'] }, { name: 'email' }],
    idToken: [{ name: 'given_name' }], saml2Token: [{ name: 'upn' }],
  };
  const first = mergedGateway({ appId: id(4), optionalClaims: existing }, id(9));
  assert.deepEqual(first.optionalClaims.idToken, existing.idToken);
  assert.deepEqual(first.optionalClaims.saml2Token, existing.saml2Token);
  assert.deepEqual(first.optionalClaims.accessToken.slice(0, 2), existing.accessToken);
  assert.deepEqual(first.optionalClaims.accessToken.map(claim => claim.name),
    ['idtyp', 'email', 'xms_act_fct', 'xms_sub_fct']);
  const second = mergedGateway({ appId: id(4), ...first }, id(9));
  assert.deepEqual(second, first);
});

test('OAuth reuses a valid Key Vault credential without creating/resetting passwords and preserves connection metadata', async () => {
  const f = fixture();
  const result = await configurePromptOAuth(config, options(f));
  assert.deepEqual(result, { rotated: false, created: false });
  assert.equal(f.calls.some(call => call[1] === 'POST' || (call[0] === 'vault' && call[1] === 'PUT')), false);
  const props = f.connection.properties;
  assert.equal(props.authType, 'OAuth2');
  assert.equal(props.credentials.clientId, config.prompt.clientId);
  assert.equal(props.credentials.clientSecret, f.secret.value);
  assert.equal(props.target, 'https://gateway.example/v1');
  assert.equal(props.tokenUrl, `https://login.microsoftonline.com/${config.tenantId}/oauth2/v2.0/token`);
  assert.deepEqual(props.scopes, [`api://${config.gateway.clientId}/.default`]);
  assert.equal(props.metadata.unrelated, 'preserve');
  assert.equal(props.metadata.authConfig, undefined);
  assert.equal(props.useWorkspaceManagedIdentity, undefined);
});

test('OAuth checks application and SP identifiers before any Key Vault or password operations', async () => {
  for (const modify of [
    f => { f.principals.get(config.prompt.principalId).appId = id(88); },
    f => { f.principals.get(config.prompt.principalId).servicePrincipalType = 'ManagedIdentity'; },
    f => { f.principals.get(config.prompt.principalId).accountEnabled = false; },
    f => { f.applications.get(config.gateway.objectId).api.requestedAccessTokenVersion = 1; },
    f => { f.applications.get(config.gateway.objectId).optionalClaims.accessToken = []; },
  ]) {
    const f = fixture();
    modify(f);
    await assert.rejects(configurePromptOAuth(config, options(f)));
    assert.equal(f.calls.some(call => call[0] === 'vault' || call[1] === 'POST'), false);
  }
});

test('OAuth rejects expired, mismatched, missing and disabled stored credentials instead of silently replacing them', async () => {
  for (const modify of [
    f => { f.secret.tags.clientId = id(88); },
    f => { f.secret.tags.keyId = id(88); },
    f => { f.secret.attributes.enabled = false; },
    f => { f.secret.attributes.nbf = now / 1000 + 3600; },
    f => { f.secret.attributes.exp = 0; },
    f => { f.applications.get(config.prompt.objectId).passwordCredentials[0].endDateTime = '2026-01-01T00:00:00Z';
      f.secret.attributes.exp = Date.parse('2026-01-01T00:00:00Z') / 1000; },
  ]) {
    const f = fixture();
    modify(f);
    await assert.rejects(configurePromptOAuth(config, options(f)));
    assert.deepEqual(mutations(f.calls), []);
  }
});

test('first credential creation and explicit rotation append passwords and persist to Key Vault before Foundry', async () => {
  for (const hasSecret of [false, true]) {
    const f = fixture({ hasSecret });
    const result = await configurePromptOAuth(config, { ...options(f), rotate: hasSecret });
    assert.equal(result.rotated, hasSecret);
    assert.equal(result.created, !hasSecret);
    assert.equal(f.applications.get(config.prompt.objectId).passwordCredentials[0].keyId, password.keyId);
    assert.equal(f.secret.tags.keyId, id(30));
    const order = f.calls.map(call => `${call[0]}:${call[1] ?? ''}`);
    assert.ok(order.indexOf('graph:POST') < order.indexOf('vault:PUT'));
    assert.ok(order.indexOf('vault:PUT') < order.indexOf('verify:'));
    assert.ok(order.indexOf('verify:') < order.indexOf('arm:PUT'));
    assert.equal(f.calls.some(call => call[1] === 'DELETE' || call[2]?.includes('removePassword')), false);
  }
});

test('denied prompt credential causes no Foundry update and never falls back to API key', async () => {
  const f = fixture();
  await assert.rejects(configurePromptOAuth(config, {
    ...options(f), verify: async () => { throw new Error('Denied'); },
  }), /Denied/);
  assert.equal(f.calls.some(call => call[0] === 'arm'), false);
});

test('a freshly created credential is retried for bounded Entra propagation; stored credentials are not', async () => {
  const waits = [];
  const sleep = async ms => { waits.push(ms); };
  let attempts = 0;
  const flaky = async (input, value) => {
    attempts += 1;
    if (attempts < 3) throw new Error('Not yet propagated');
  };
  const fresh = fixture({ hasSecret: false });
  await configurePromptOAuth(config, { ...options(fresh), verify: flaky, sleep });
  assert.equal(attempts, 3);
  assert.deepEqual(waits, [5_000, 5_000]);
  assert.equal(fresh.connection.properties.authType, 'OAuth2');

  const never = fixture({ hasSecret: false });
  let count = 0;
  await assert.rejects(configurePromptOAuth(config, {
    ...options(never), sleep: async () => {}, verify: async () => { count += 1; throw new Error('Rejected'); },
  }), /Rejected/);
  assert.equal(count, 8);
  assert.equal(never.calls.some(call => call[0] === 'arm'), false);

  const stored = fixture();
  let storedCount = 0;
  await assert.rejects(configurePromptOAuth(config, {
    ...options(stored), sleep: async () => { throw new Error('must not wait'); },
    verify: async () => { storedCount += 1; throw new Error('Rejected'); },
  }), /Rejected/);
  assert.equal(storedCount, 1);
});

test('explicit rotation can replace an expired matching credential without deleting the old password', async () => {
  const f = fixture();
  const expired = '2026-01-01T00:00:00Z';
  f.applications.get(config.prompt.objectId).passwordCredentials[0].endDateTime = expired;
  f.secret.attributes.exp = Date.parse(expired) / 1000;
  assert.deepEqual(await configurePromptOAuth(config, { ...options(f), rotate: true }),
    { rotated: true, created: false });
  assert.equal(f.applications.get(config.prompt.objectId).passwordCredentials.length, 2);
});

test('failed Key Vault persistence never updates Foundry or destructively cleans up unrelated credentials', async () => {
  const f = fixture({ hasSecret: false });
  await assert.rejects(configurePromptOAuth(config, {
    ...options(f), vault: async method => {
      if (method === 'GET') return null;
      throw new Error('storage unavailable');
    },
  }), /storage unavailable/);
  assert.equal(f.applications.get(config.prompt.objectId).passwordCredentials.length, 2);
  assert.equal(f.calls.some(call => call[0] === 'arm' || call[1] === 'DELETE'), false);
});

test('credential verification uses fixed tenant token endpoint, form body and no redirects', async () => {
  let request;
  await verifyCredential(config, 'test-secret', async (url, init) => {
    request = { url, ...init };
    return new Response(JSON.stringify({ access_token: 'mock-token', token_type: 'Bearer' }));
  });
  assert.equal(request.url, `https://login.microsoftonline.com/${config.tenantId}/oauth2/v2.0/token`);
  assert.equal(request.redirect, 'error');
  assert.equal(request.body.get('scope'), `api://${config.gateway.clientId}/.default`);
  assert.equal(request.body.get('grant_type'), 'client_credentials');
  await assert.rejects(verifyCredential(config, 'secret', async () => new Response('secret leaked?', { status: 401 })),
    /rejected/);
});

test('migration deletes only the exact secret-scoped assignment and verifies persisted deletion', async () => {
  const input = {
    ...config, agentPrincipalId: id(40), assignmentId: id(41),
    hostedAuthMode: 'entra', legacyHostedEnabled: false, entraVerified: true,
  };
  const scope = `/subscriptions/${id(2)}/resourceGroups/rg-test/providers/Microsoft.KeyVault/vaults/test-vault/secrets/litellm-master-key`;
  const expected = { properties: {
    principalId: id(40), scope,
    roleDefinitionId: `/subscriptions/${id(2)}/providers/Microsoft.Authorization/roleDefinitions/4633458b-17de-408a-b874-0445c86b69e6`,
  } };
  for (const mismatch of ['principalId', 'scope', 'roleDefinitionId', null]) {
    let assignment = structuredClone(expected);
    if (mismatch) assignment.properties[mismatch] = 'not-the-approved-assignment';
    const calls = [];
    const arm = async (method, path) => {
      calls.push([method, path]);
      if (method === 'DELETE') assignment = null;
      return assignment;
    };
    if (mismatch) await assert.rejects(revokeHostedKeyAccess(input, { approved: true, arm }), /refusing/);
    else assert.deepEqual(await revokeHostedKeyAccess(input, { approved: true, arm }), { removed: true });
    assert.equal(calls.some(([method]) => method === 'DELETE'), !mismatch);
    assert.ok(calls.every(([, path]) => path.startsWith(`${scope}/providers/Microsoft.Authorization/roleAssignments/`)));
  }
  await assert.rejects(revokeHostedKeyAccess({ ...input, legacyHostedEnabled: true }, { approved: true }), /legacy/);
  await assert.rejects(revokeHostedKeyAccess({ ...input, hostedAuthMode: 'api_key' }, { approved: true }), /legacy/);
});

test('local preflight preserves legacy defaults, requires explicit image/policies, and forbids prompt on delegated-only gateway', () => {
  assert.doesNotThrow(() => validateAuthConfig({}));
  const env = {
    GATEWAY_AUTH_MODE: 'entra', HOSTED_AUTH_MODE: 'entra', PROMPT_AUTH_MODE: 'oauth2',
    ENTRA_TENANT_ID: id(1), ENTRA_GATEWAY_CLIENT_ID: id(4),
    ENTRA_AGENT_CLIENT_ID: id(42), ENTRA_AGENT_PRINCIPAL_ID: id(43), ENTRA_PROMPT_CLIENT_ID: id(7),
    ENTRA_APP_PRINCIPALS: JSON.stringify([{ oid: id(8), azp: id(7) }, { oid: id(43), azp: id(42) }]),
  };
  assert.doesNotThrow(() => validateAuthConfig(env));
  assert.throws(() => validateAuthConfig({ ...env, ENTRA_DELEGATED_ONLY: 'true' }), /separate gateway/);
  assert.throws(() => validateAuthConfig({ ...env, HOSTED_AUTH_MODE: 'api_key' }), /no API-key fallback/);
});

test('Graph template pins GA dynamic extension, keeps applications/passwords outside ordinary ARM and merges scope/URI collections', () => {
  const template = JSON.parse(execFileSync('az', [
    'bicep', 'build', '--file', 'infra/entra/main.bicep', '--stdout',
  ], { encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 }));
  assert.deepEqual(template.imports.MicrosoftGraph, { provider: 'MicrosoftGraph', version: '1.0.0' });
  assert.equal(template.resources.adoptedGateway.existing, true);
  assert.deepEqual(template.resources.adoptedGateway.properties, { uniqueName: "[variables('gatewayKey')]" });
  assert.deepEqual(Object.keys(template.resources.gatewayPrincipal.properties), ['appId']);
  assert.deepEqual(Object.keys(template.resources.promptPrincipal.properties), ['appId']);
  const settings = template.resources.gatewayIdentifier.properties.template.resources;
  assert.equal(settings.current.existing, true);
  assert.match(settings.gateway.properties.identifierUris, /union\(coalesce\(reference\('current'\)\.identifierUris/);
  // Graph read-backs contain nulls that its write path rejects; merges must strip them.
  assert.match(settings.gateway.properties.api, /union\(__bicep\.withoutNulls\(coalesce\(reference\('current'\)\.api/);
  assert.match(settings.gateway.properties.api, /gateway\.invoke/);
  assert.equal(settings.gateway.properties.passwordCredentials, undefined);
  assert.equal(settings.gateway.properties.keyCredentials, undefined);
  assert.match(settings.gateway.properties.optionalClaims, /union\(__bicep\.withoutNulls\(coalesce\(reference\('current'\)\.optionalClaims/);
  assert.ok(settings.current.existing);
  assert.ok(template.resources.adoptedGateway.condition, 'adoption lookups must not run for new registrations');
  const requiredClaims = template.resources.gatewayIdentifier.properties.template.variables.requiredClaims;
  assert.deepEqual(requiredClaims, ['idtyp', 'xms_act_fct', 'xms_sub_fct']);
  const text = readFileSync(new URL('../infra/entra/main.bicep', import.meta.url), 'utf8');
  assert.match(text, /microsoftgraph\/v1\.0:1\.0\.0/);
  assert.doesNotMatch(text, /passwordCredentials|addPassword/);
  const identifier = readFileSync(new URL('../infra/entra/identifier.bicep', import.meta.url), 'utf8');
  assert.match(identifier, /func withoutNulls/);
  assert.match(identifier, /union\(currentApi/);
  assert.match(identifier, /union\(current\.identifierUris/);
  assert.match(identifier, /item\.value == 'gateway.invoke'/);
  const scripts = ['configure-prompt-oauth', 'prepare-entra-registration', 'revoke-hosted-key-access'];
  for (const name of scripts) {
    const source = readFileSync(new URL(`../scripts/${name}.mjs`, import.meta.url), 'utf8');
    assert.doesNotMatch(source, /azd.*env.*set|['"]reset['"]/i);
  }
});

test('Azure client rejects invalid tenants before any command or request', () => {
  assert.throws(() => azureClient('not-a-guid', 'https://graph.microsoft.com', 'https://graph.microsoft.com/v1.0'),
    /GUID/);
});

test('Azure transport keeps Graph v1.0 in the path, disables redirects, caches tokens and suppresses secret-bearing errors', async () => {
  let tokens = 0;
  const requests = [];
  const graph = azureClient(id(1), 'https://graph.microsoft.com', 'https://graph.microsoft.com/v1.0',
    async (url, init) => {
      requests.push({ url: url.href, ...init });
      return new Response(JSON.stringify({ id: id(3) }));
    }, async () => { tokens += 1; return 'mock-token-never-log'; });
  await graph('GET', `/applications/${id(3)}`);
  await graph('GET', `/servicePrincipals/${id(5)}`);
  assert.equal(tokens, 1);
  assert.equal(requests[0].url, `https://graph.microsoft.com/v1.0/applications/${id(3)}`);
  assert.equal(requests[0].redirect, 'error');
  assert.ok(requests[0].signal);
  const denied = azureClient(id(1), 'https://vault.azure.net', 'https://vault.example',
    async () => new Response('secret-in-error-body', { status: 403 }), async () => 'mock-token');
  await assert.rejects(denied('PUT', '/secrets/test', { value: 'secret' }),
    error => /403/.test(error.message) && !/secret/.test(error.message));
});
