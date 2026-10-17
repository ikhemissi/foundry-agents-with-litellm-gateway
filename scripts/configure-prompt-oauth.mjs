import { approve, azureClient, cli, guid, validateApplication } from './identity-common.mjs';

export function connectionPath(config) {
  guid(config.subscriptionId, 'subscriptionId');
  for (const name of ['resourceGroup', 'consumerFoundryName', 'consumerProjectName']) {
    if (!/^[A-Za-z0-9_.()-]+$/.test(config[name] ?? '')) throw new Error(`${name} is invalid.`);
  }
  return `/subscriptions/${config.subscriptionId}/resourceGroups/${config.resourceGroup}`
    + `/providers/Microsoft.CognitiveServices/accounts/${config.consumerFoundryName}`
    + `/projects/${config.consumerProjectName}/connections/litellm-gateway?api-version=2025-04-01-preview`;
}

export async function verifyCredential(config, secret, fetchImpl = fetch) {
  let response;
  try {
    response = await fetchImpl(`https://login.microsoftonline.com/${config.tenantId}/oauth2/v2.0/token`, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(30_000),
      body: new URLSearchParams({
        grant_type: 'client_credentials', client_id: config.prompt.clientId,
        client_secret: secret, scope: `api://${config.gateway.clientId}/.default`,
      }),
    });
  } catch {
    throw new Error('Prompt credential verification failed; details suppressed.');
  }
  if (!response.ok) throw new Error('Prompt credential was rejected; explicit rotation may be required.');
  const token = await response.json();
  if (!token.access_token || token.token_type?.toLowerCase() !== 'bearer') {
    throw new Error('Identity provider did not return a bearer token.');
  }
}

// A new client secret can take several seconds to be accepted by Entra.
async function verifyFresh(verify, config, secret, sleep, attempts = 8) {
  for (let attempt = 1; ; attempt += 1) {
    try {
      return await verify(config, secret);
    } catch (error) {
      if (attempt >= attempts) throw error;
      await sleep(5_000);
    }
  }
}

export async function configurePromptOAuth(config, {
  approved, rotate = false, graph, vault, arm, now = Date.now(),
  verify = verifyCredential, sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
}) {
  approve(approved);
  guid(config.tenantId, 'tenantId');
  const path = connectionPath(config);
  if (!/^[a-zA-Z0-9-]{1,127}$/.test(config.secretName ?? '')) throw new Error('secretName is invalid.');
  const endpoint = new URL(config.litellmEndpoint);
  if (endpoint.protocol !== 'https:' || endpoint.username || endpoint.password || endpoint.search
      || endpoint.hash || !['', '/'].includes(endpoint.pathname)) {
    throw new Error('litellmEndpoint must be an HTTPS origin.');
  }
  if (!/^[A-Za-z0-9_.-]+$/.test(config.modelDeploymentName ?? '')) throw new Error('Invalid model alias.');
  const { app } = await validateApplication(graph, config.prompt, 'prompt');
  const { app: gateway } = await validateApplication(graph, config.gateway, 'gateway');
  if (app.id === gateway.id || gateway.api?.requestedAccessTokenVersion !== 2
      || !gateway.identifierUris?.includes(`api://${config.gateway.clientId}`)
      || !['idtyp', 'xms_act_fct', 'xms_sub_fct'].every(name =>
        gateway.optionalClaims?.accessToken?.some(claim => claim.name === name))
      || !gateway.api?.oauth2PermissionScopes?.some(scope => scope.value === 'gateway.invoke' && scope.isEnabled)) {
    throw new Error('Gateway registration is not ready or prompt identity is not independent.');
  }
  const secretPath = `/secrets/${config.secretName}?api-version=7.4`;
  const existing = await vault('GET', secretPath, undefined, { allow404: true });
  let secret;
  let fresh = false;
  if (existing) {
    const expected = {
      tenantId: config.tenantId, clientId: config.prompt.clientId,
      objectId: config.prompt.objectId, principalId: config.prompt.principalId,
    };
    for (const [key, value] of Object.entries(expected)) {
      if (existing.tags?.[key]?.toLowerCase() !== value.toLowerCase()) {
        throw new Error('Stored prompt credential identity mismatch; refusing replacement.');
      }
    }
    const password = app.passwordCredentials?.find(item => item.keyId === existing.tags.keyId);
    if (!password || !existing.value || existing.attributes?.enabled === false
        || (existing.attributes?.nbf ?? 0) * 1000 > now) {
      throw new Error('Stored credential is disabled, missing, or no longer registered; refusing replacement.');
    }
    const end = Date.parse(password.endDateTime);
    const start = Date.parse(password.startDateTime);
    if (!Number.isFinite(end) || !Number.isFinite(start) || start > now
        || existing.attributes?.exp !== Math.floor(end / 1000)) {
      throw new Error('Stored credential validity metadata does not match Graph.');
    }
    if (!rotate && end <= now) throw new Error('Stored credential expired; explicit --rotate required.');
    secret = existing.value;
  }
  if (!existing || rotate) {
    const days = config.credentialLifetimeDays ?? 90;
    if (!Number.isInteger(days) || days < 1 || days > 180) throw new Error('Credential lifetime must be 1–180 days.');
    const endDateTime = new Date(now + days * 86_400_000).toISOString();
    const created = await graph('POST', `/applications/${app.id}/addPassword`, {
      passwordCredential: { displayName: 'LiteLLM prompt ModelGateway', endDateTime },
    });
    guid(created?.keyId, 'created.keyId');
    if (!created.secretText || Date.parse(created.endDateTime) <= now) throw new Error('Invalid new credential.');
    secret = created.secretText;
    fresh = true;
    await vault('PUT', secretPath, {
      value: secret,
      contentType: 'Entra prompt-client credential; not a human or Agent ID',
      attributes: { enabled: true, exp: Math.floor(Date.parse(created.endDateTime) / 1000) },
      tags: {
        tenantId: config.tenantId, clientId: config.prompt.clientId,
        objectId: config.prompt.objectId, principalId: config.prompt.principalId,
        keyId: created.keyId,
      },
    });
    // Read back before updating Foundry. A KV failure leaves an orphaned Graph
    // password for explicit operator recovery; no destructive rollback/reset.
    const persisted = await vault('GET', secretPath);
    if (persisted?.value !== secret || persisted.tags?.keyId !== created.keyId) {
      throw new Error('Key Vault did not persist the new credential.');
    }
  }
  if (fresh) await verifyFresh(verify, config, secret, sleep);
  else await verify(config, secret);
  const current = await arm('GET', path, undefined, { allow404: true });
  const metadata = {
    ...(current?.properties?.metadata ?? {}),
    deploymentInPath: 'false', inferenceAPIVersion: '',
    models: JSON.stringify([{
      name: config.modelDeploymentName,
      properties: { model: { name: config.modelDeploymentName, version: '', format: 'OpenAI' } },
    }]),
  };
  delete metadata.authConfig; // Remove the previous ApiKey header formatter during OAuth migration.
  await arm('PUT', path, {
    properties: {
      ...(current?.properties ?? {}),
      category: 'ModelGateway', target: `${endpoint.origin}/v1`,
      authType: 'OAuth2', isSharedToAll: false,
      credentials: { clientId: config.prompt.clientId, clientSecret: secret },
      tokenUrl: `https://login.microsoftonline.com/${config.tenantId}/oauth2/v2.0/token`,
      scopes: [`api://${config.gateway.clientId}/.default`], metadata,
    },
  });
  const persisted = await arm('GET', path);
  if (persisted?.properties?.authType !== 'OAuth2'
      || persisted.properties.target !== `${endpoint.origin}/v1`
      || persisted.properties.tokenUrl !== `https://login.microsoftonline.com/${config.tenantId}/oauth2/v2.0/token`
      || JSON.stringify(persisted.properties.scopes) !== JSON.stringify([`api://${config.gateway.clientId}/.default`])) {
    throw new Error('Foundry did not persist the expected OAuth2 connection.');
  }
  return { rotated: Boolean(existing && rotate), created: !existing };
}

cli(import.meta.url, async (config, options) => {
  if (!/^[a-zA-Z][a-zA-Z0-9-]{1,22}[a-zA-Z0-9]$/.test(config.vaultName ?? '')) throw new Error('Invalid vaultName.');
  return configurePromptOAuth(config, {
    ...options,
    graph: azureClient(config.tenantId, 'https://graph.microsoft.com', 'https://graph.microsoft.com/v1.0'),
    vault: azureClient(config.tenantId, 'https://vault.azure.net', `https://${config.vaultName}.vault.azure.net`),
    arm: azureClient(config.tenantId, 'https://management.azure.com', 'https://management.azure.com'),
  });
});
