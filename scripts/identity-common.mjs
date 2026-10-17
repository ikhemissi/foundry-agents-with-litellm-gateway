import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

export function guid(value, name) {
  if (typeof value !== 'string' || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value)) {
    throw new Error(`${name} must be a GUID.`);
  }
  return value.toLowerCase();
}

export function approve(approved) {
  if (approved !== true) throw new Error('Explicit --approve approval is required; no changes made.');
}

export async function validateApplication(graph, identity, label = 'application') {
  const objectId = guid(identity.objectId, `${label}.objectId`);
  const clientId = guid(identity.clientId, `${label}.clientId`);
  const principalId = guid(identity.principalId, `${label}.principalId`);
  const app = await graph('GET', `/applications/${objectId}`);
  const sp = await graph('GET', `/servicePrincipals/${principalId}`);
  if (app?.id?.toLowerCase() !== objectId || app.appId?.toLowerCase() !== clientId
      || sp?.id?.toLowerCase() !== principalId || sp.appId?.toLowerCase() !== clientId
      || sp.servicePrincipalType !== 'Application' || sp.accountEnabled === false) {
    throw new Error(`${label} application/service-principal identity mismatch or disabled principal.`);
  }
  return { app, sp };
}

// Secrets and Graph error bodies must never reach command-line diagnostics.
export function azureClient(tenantId, resource, base, fetchImpl = fetch, acquireToken) {
  guid(tenantId, 'tenantId');
  let token;
  return async (method, path, body, { allow404 = false, headers = {} } = {}) => {
    if (!token) {
      try {
        token = (acquireToken ? await acquireToken() : execFileSync('az', [
          'account', 'get-access-token', '--tenant', tenantId, '--resource', resource,
          '--query', 'accessToken', '--output', 'tsv',
        ], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })).trim();
      } catch {
        throw new Error('Azure token acquisition failed; check the approved tenant/session.');
      }
    }
    if (!token) throw new Error('Azure returned an empty access token.');
    const url = new URL(`${base.replace(/\/$/, '')}/${path.replace(/^\/+/, '')}`);
    if (url.origin !== new URL(base).origin) throw new Error('Cross-origin request rejected.');
    let response;
    try {
      response = await fetchImpl(url, {
        method, redirect: 'error', signal: AbortSignal.timeout(30_000),
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', ...headers },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    } catch {
      throw new Error('Azure request failed; response details suppressed.');
    }
    if (allow404 && response.status === 404) return null;
    if (!response.ok) throw new Error(`Azure ${method} failed (${response.status}); details suppressed.`);
    if (response.status === 204) return null;
    try {
      const text = await response.text();
      return text ? JSON.parse(text) : null;
    } catch {
      throw new Error('Invalid Azure response; content suppressed.');
    }
  };
}

export function cli(importUrl, run) {
  if (!process.argv[1] || pathToFileURL(process.argv[1]).href !== importUrl) return;
  Promise.resolve().then(async () => {
    const args = process.argv.slice(2);
    const approved = args.includes('--approve');
    approve(approved);
    const index = args.indexOf('--config');
    if (index < 0 || !args[index + 1]) throw new Error('--config <nonsecret-config.json> is required.');
    const config = JSON.parse(readFileSync(args[index + 1], 'utf8'));
    await run(config, { approved, rotate: args.includes('--rotate') });
    console.log('Approved identity operation completed. No credentials emitted.');
  }).catch(() => {
    // A third-party or SDK exception may contain a credential-bearing request.
    console.error('Identity operation failed. Check nonsecret configuration, approvals and permissions; diagnostics suppressed.');
    process.exitCode = 1;
  });
}
