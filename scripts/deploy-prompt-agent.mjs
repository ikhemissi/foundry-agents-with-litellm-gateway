import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { isDeepStrictEqual } from 'node:util';
import { pathToFileURL } from 'node:url';

export async function deployPromptAgent({
  endpoint, agent, token, fetchImpl = fetch,
}) {
  for (const [name, value] of Object.entries({
    CONSUMER_FOUNDRY_PROJECT_ENDPOINT: endpoint,
    token,
  })) {
    if (typeof value !== 'string' || !value.trim()) {
      throw new Error(`${name} is required to deploy the prompt agent.`);
    }
  }
  const project = new URL(endpoint);
  if (project.protocol !== 'https:' || !/^\/api\/projects\/[^/]+\/?$/.test(project.pathname)
      || project.search || project.hash || project.username || project.password) {
    throw new Error('CONSUMER_FOUNDRY_PROJECT_ENDPOINT must be an HTTPS Foundry project endpoint.');
  }
  if (typeof agent?.name !== 'string'
      || !/^[a-zA-Z0-9][a-zA-Z0-9-]*$/.test(agent.name)
      || agent.kind !== 'prompt'
      || typeof agent.instructions !== 'string'
      || !agent.instructions.trim()) {
    throw new Error('The agent definition requires a valid name, prompt kind, and nonempty instructions.');
  }
  if (typeof agent.model !== 'string' || !/^[^/\s]+\/[^/\s]+$/.test(agent.model)) {
    throw new Error('The agent model must use <connection-name>/<model-name>.');
  }

  const base = `${project.href.replace(/\/$/, '')}/agents/${encodeURIComponent(agent.name)}`;
  const { name, description, ...definition } = agent;
  const desired = {
    description,
    definition,
  };
  async function request(url, method = 'GET', body) {
    const response = await fetchImpl(url, {
      method,
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      ...(body && { body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(120_000),
      redirect: 'error',
    });
    if (method === 'GET' && response.status === 404) return null;
    if (!response.ok) {
      throw new Error(`Foundry ${method} failed (${response.status}): ${await response.text()}`);
    }
    return response.json();
  }
  const matches = version => version?.description === desired.description
    && isDeepStrictEqual(version.definition, desired.definition);
  const current = await request(`${base}?api-version=v1`);
  if (matches(current?.versions?.latest)) {
    return { ...current.versions.latest, changed: false };
  }

  const created = await request(`${base}/versions?api-version=v1`, 'POST', desired);
  const persisted = await request(`${base}?api-version=v1`);
  const latest = persisted?.versions?.latest;
  if (!created.version || latest?.version !== created.version || !matches(latest)) {
    throw new Error(`Foundry did not persist the expected version of ${agent.name}.`);
  }
  return { ...latest, changed: true };
}

async function main() {
  const subscriptionId = process.env.AZURE_SUBSCRIPTION_ID;
  if (!subscriptionId?.trim()) {
    throw new Error('AZURE_SUBSCRIPTION_ID is required; run this script through azd hooks.');
  }
  const agent = JSON.parse(readFileSync(
    new URL('../agents/ai-tutor-prompt-agent/agent.json', import.meta.url), 'utf8',
  ));
  const token = execFileSync('az', [
    'account', 'get-access-token',
    '--scope', 'https://ai.azure.com/.default',
    '--subscription', subscriptionId,
    '--query', 'accessToken', '--output', 'tsv',
  ], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] }).trim();
  const result = await deployPromptAgent({
    endpoint: process.env.CONSUMER_FOUNDRY_PROJECT_ENDPOINT,
    agent,
    token,
  });
  console.log(`${result.changed ? 'Deployed' : 'Unchanged'}: ${agent.name}, version ${result.version}, model ${result.definition.model}`);
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  main().catch(error => {
    console.error(`Prompt agent deployment failed: ${error.message}`);
    process.exitCode = 1;
  });
}
