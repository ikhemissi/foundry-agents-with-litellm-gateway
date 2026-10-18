import { pathToFileURL } from 'node:url';
import { guid } from './identity-common.mjs';

const CAPABILITY_NAME = /^[a-z][a-z0-9_]{0,40}$/;
const isPlainObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);

function nameList(value, label) {
  if (!Array.isArray(value) || !value.length) throw new Error(`${label} must be a non-empty array.`);
  for (const name of value) {
    if (typeof name !== 'string' || !CAPABILITY_NAME.test(name)) {
      throw new Error(`${label} entries must match ${CAPABILITY_NAME}.`);
    }
  }
  if (new Set(value).size !== value.length) throw new Error(`${label} must not contain duplicates.`);
}

// Structure only; principal membership is checked once ENTRA_APP_PRINCIPALS is known.
function readCapabilities(env) {
  if (env.ENTRA_CAPABILITIES === undefined) return {};
  let value;
  try {
    value = JSON.parse(env.ENTRA_CAPABILITIES);
  } catch {
    throw new Error('ENTRA_CAPABILITIES must be a JSON object; unset it instead of leaving it empty.');
  }
  if (!isPlainObject(value)) throw new Error('ENTRA_CAPABILITIES must be a JSON object.');
  for (const [oid, grant] of Object.entries(value)) {
    guid(oid, 'ENTRA_CAPABILITIES key');
    if (!isPlainObject(grant) || !Object.keys(grant).length
        || Object.keys(grant).some(key => !['mcp', 'agents'].includes(key))) {
      throw new Error('ENTRA_CAPABILITIES grants must be objects with only mcp and/or agents.');
    }
    if ('mcp' in grant) {
      if (!isPlainObject(grant.mcp) || !Object.keys(grant.mcp).length) {
        throw new Error('ENTRA_CAPABILITIES mcp must be an object of server to tool arrays.');
      }
      for (const [server, tools] of Object.entries(grant.mcp)) {
        if (!CAPABILITY_NAME.test(server)) throw new Error(`ENTRA_CAPABILITIES server names must match ${CAPABILITY_NAME}.`);
        nameList(tools, `ENTRA_CAPABILITIES mcp.${server}`);
      }
    }
    if ('agents' in grant) nameList(grant.agents, 'ENTRA_CAPABILITIES agents');
  }
  return value;
}

export function validateAuthConfig(env) {
  const gateway = env.GATEWAY_AUTH_MODE || 'api_key';
  const hosted = env.HOSTED_AUTH_MODE || 'api_key';
  const prompt = env.PROMPT_AUTH_MODE || 'api_key';
  if (!['api_key', 'entra'].includes(gateway) || !['api_key', 'entra', 'entra_delegated'].includes(hosted)
      || !['api_key', 'oauth2'].includes(prompt)) throw new Error('Invalid explicit authentication mode.');
  if (env.ENABLE_DEMO_BACKENDS !== undefined && !['true', 'false'].includes(env.ENABLE_DEMO_BACKENDS)) {
    throw new Error('ENABLE_DEMO_BACKENDS must be exactly true or false.');
  }
  if (env.PROMPT_TOOLS !== undefined && !['enabled', 'disabled'].includes(env.PROMPT_TOOLS)) {
    throw new Error('PROMPT_TOOLS must be exactly enabled or disabled.');
  }
  if (env.PROMPT_TOOLS === 'enabled' && env.ENABLE_DEMO_BACKENDS !== 'true') {
    throw new Error('PROMPT_TOOLS=enabled requires ENABLE_DEMO_BACKENDS=true.');
  }
  const capabilities = readCapabilities(env);
  if (env.ENABLE_DEMO_BACKENDS === 'true' && gateway !== 'entra') {
    throw new Error('Demo backends are only registered in the Entra gateway config.');
  }
  // The quiz agent requests tokens for this audience, so it must be a real application ID.
  if (env.ENABLE_DEMO_BACKENDS === 'true') guid(env.ENTRA_GATEWAY_CLIENT_ID, 'ENTRA_GATEWAY_CLIENT_ID (the quiz agent token audience)');
  if (gateway === 'api_key') {
    if (Object.keys(capabilities).length) throw new Error('ENTRA_CAPABILITIES requires the Entra gateway.');
    if (hosted !== 'api_key' || prompt !== 'api_key') throw new Error('Entra clients require an Entra gateway.');
    return;
  }
  if (hosted === 'api_key' || prompt !== 'oauth2') throw new Error('Entra mode has no API-key fallback; migrate both tutors explicitly.');
  for (const key of ['ENTRA_TENANT_ID', 'ENTRA_GATEWAY_CLIENT_ID', 'ENTRA_AGENT_CLIENT_ID',
    'ENTRA_AGENT_PRINCIPAL_ID', 'ENTRA_PROMPT_CLIENT_ID']) guid(env[key], key);
  const readArray = key => {
    const value = JSON.parse(env[key] || '[]');
    if (!Array.isArray(value)) throw new Error(`${key} must be a JSON array.`);
    return value;
  };
  const pairs = readArray('ENTRA_APP_PRINCIPALS');
  for (const pair of pairs) {
    guid(pair.oid, 'ENTRA_APP_PRINCIPALS.oid');
    guid(pair.azp, 'ENTRA_APP_PRINCIPALS.azp');
  }
  const principalOids = new Set(pairs.map(pair => pair.oid.toLowerCase()));
  for (const oid of Object.keys(capabilities)) {
    if (!principalOids.has(oid.toLowerCase())) {
      throw new Error('ENTRA_CAPABILITIES keys must be approved oids in ENTRA_APP_PRINCIPALS.');
    }
  }
  if (env.ENTRA_DELEGATED_ONLY && !['true', 'false'].includes(env.ENTRA_DELEGATED_ONLY)) {
    throw new Error('ENTRA_DELEGATED_ONLY must be true or false.');
  }
  const users = readArray('ENTRA_DELEGATED_USERS');
  const actors = readArray('ENTRA_DELEGATED_ACTORS');
  users.forEach(value => guid(value, 'delegated user'));
  actors.forEach(value => guid(value, 'delegated actor'));
  if (!pairs.some(pair => pair.azp.toLowerCase() === env.ENTRA_PROMPT_CLIENT_ID.toLowerCase())) {
    throw new Error('Approve the prompt-client service principal pair before configuring OAuth2.');
  }
  if (hosted === 'entra' && !pairs.some(pair =>
    pair.oid.toLowerCase() === env.ENTRA_AGENT_PRINCIPAL_ID.toLowerCase()
      && pair.azp.toLowerCase() === env.ENTRA_AGENT_CLIENT_ID.toLowerCase())) {
    throw new Error('Approve the actual hosted Agent ID pair.');
  }
  if (env.ENTRA_DELEGATED_ONLY === 'true') {
    throw new Error('The independent prompt client is app-only; delegated-only policy requires a separate gateway deployment.');
  }
  if (hosted === 'entra_delegated' && (!users.length || !actors.length)) {
    throw new Error('Delegated mode requires explicitly approved humans and actors.');
  }
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  try {
    validateAuthConfig(process.env);
    console.log('Authentication configuration validated locally; no Azure operations performed.');
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
