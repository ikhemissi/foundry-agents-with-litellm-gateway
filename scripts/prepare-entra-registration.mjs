import { randomUUID } from 'node:crypto';
import { approve, azureClient, cli, guid, validateApplication } from './identity-common.mjs';

const CLI_APP_ID = '04b07795-8ddb-461a-bbee-02f9e1bf7b46';

export function mergedGateway(app, scopeId, preauthorizeAzureCli = false) {
  const api = structuredClone(app.api ?? {});
  const scopes = api.oauth2PermissionScopes ?? [];
  const invoke = scopes.filter(scope => scope.value === 'gateway.invoke');
  if (invoke.length > 1) throw new Error('Ambiguous gateway.invoke scopes.');
  if (invoke.length && scopeId && invoke[0].id.toLowerCase() !== guid(scopeId, 'gatewayScopeId')) {
    throw new Error('Existing gateway.invoke scope ID does not match; refusing replacement.');
  }
  if (invoke.length && invoke[0].isEnabled !== true) throw new Error('Existing gateway.invoke scope is disabled.');
  const id = invoke[0]?.id ?? guid(scopeId ?? randomUUID(), 'gatewayScopeId');
  api.requestedAccessTokenVersion = 2;
  api.oauth2PermissionScopes = invoke.length ? scopes : [...scopes, {
    id, value: 'gateway.invoke', isEnabled: true, type: 'Admin',
    adminConsentDisplayName: 'Invoke the LiteLLM gateway',
    adminConsentDescription: 'Invoke the LiteLLM inference gateway on behalf of an approved user.',
  }];
  if (preauthorizeAzureCli) {
    const clients = api.preAuthorizedApplications ?? [];
    const existing = clients.find(client => client.appId.toLowerCase() === CLI_APP_ID);
    api.preAuthorizedApplications = [
      ...clients.filter(client => client.appId.toLowerCase() !== CLI_APP_ID),
      { ...existing, appId: CLI_APP_ID,
        delegatedPermissionIds: [...new Set([...(existing?.delegatedPermissionIds ?? []), id])] },
    ];
  }
  const optionalClaims = structuredClone(app.optionalClaims ?? {});
  const accessClaims = optionalClaims.accessToken ?? [];
  optionalClaims.accessToken = [
    ...accessClaims,
    ...['idtyp', 'xms_act_fct', 'xms_sub_fct']
      .filter(name => !accessClaims.some(claim => claim.name === name))
      .map(name => ({ name, essential: false })),
  ];
  return {
    api,
    optionalClaims,
    identifierUris: [...new Set([...(app.identifierUris ?? []), `api://${app.appId}`])],
  };
}

export async function prepareRegistration(config, { approved, graph }) {
  approve(approved);
  guid(config.tenantId, 'tenantId');
  if (!/^[A-Za-z0-9_-]{1,100}$/.test(config.registrationKey ?? '')) {
    throw new Error('registrationKey must be a stable simple identifier.');
  }
  // Validate BOTH identities before any mutation, including immutable onboarding.
  const gateway = await validateApplication(graph, config.gateway, 'gateway');
  const prompt = await validateApplication(graph, config.prompt, 'prompt');
  if (gateway.app.id === prompt.app.id) throw new Error('Gateway and prompt applications must be independent.');
  for (const [name, { app }] of [['gateway', gateway], ['prompt', prompt]]) {
    const key = `${config.registrationKey}-${name}`;
    if (app.uniqueName && app.uniqueName !== key) throw new Error('Immutable uniqueName conflicts with registrationKey.');
  }
  const merged = mergedGateway(gateway.app, config.gatewayScopeId, config.preauthorizeAzureCli);
  for (const [name, { app }] of [['gateway', gateway], ['prompt', prompt]]) {
    const desired = {
      ...(app.uniqueName ? {} : { uniqueName: `${config.registrationKey}-${name}` }),
      ...(name === 'gateway' ? merged : {}),
    };
    if (Object.entries(desired).every(([key, value]) => JSON.stringify(app[key]) === JSON.stringify(value))) continue;
    // Graph PATCH preserves omitted manifest fields and credentials. Collection
    // properties are replace-on-write, so api collections are read/merged above.
    await graph('PATCH', `/applications/${app.id}`, desired);
    const persisted = await graph('GET', `/applications/${app.id}`);
    for (const [key, value] of Object.entries(desired)) {
      if (JSON.stringify(persisted[key]) !== JSON.stringify(value)) {
        throw new Error('Graph did not persist the approved registration update.');
      }
    }
  }
  return { gatewayScopeId: merged.api.oauth2PermissionScopes.find(scope => scope.value === 'gateway.invoke').id };
}

cli(import.meta.url, async (config, options) => prepareRegistration(config, {
  ...options,
  graph: azureClient(config.tenantId, 'https://graph.microsoft.com', 'https://graph.microsoft.com/v1.0/'),
}));
