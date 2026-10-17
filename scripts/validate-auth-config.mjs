import { pathToFileURL } from 'node:url';
import { guid } from './identity-common.mjs';

export function validateAuthConfig(env) {
  const gateway = env.GATEWAY_AUTH_MODE || 'api_key';
  const hosted = env.HOSTED_AUTH_MODE || 'api_key';
  const prompt = env.PROMPT_AUTH_MODE || 'api_key';
  if (!['api_key', 'entra'].includes(gateway) || !['api_key', 'entra', 'entra_delegated'].includes(hosted)
      || !['api_key', 'oauth2'].includes(prompt)) throw new Error('Invalid explicit authentication mode.');
  if (gateway === 'api_key') {
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
