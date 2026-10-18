import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {
  A2A_AGENT, NAMES, TOOL_GUIDANCE, definitionMatches, instructionsWithTools, resolvePromptAgent, toolsApproved,
} from '../scripts/prompt-tools.mjs';
import { validateAuthConfig } from '../scripts/validate-auth-config.mjs';

const agent = JSON.parse(readFileSync(
  new URL('../agents/ai-tutor-prompt-agent/agent.json', import.meta.url), 'utf8',
));
const GATEWAY = 'https://gw.example.azurecontainerapps.io';
const PROJECT = 'https://acct.services.ai.azure.com/api/projects/proj';
const IDENTITY = { principal_id: '78e6eefc-a52a-4eb5-b79f-dca2f4222300', client_id: '78e6eefc-a52a-4eb5-b79f-dca2f4222300' };
const TOOLS = ['list_topics', 'get_topic', 'get_section', 'list_learning_modes'];
const env = {
  PROMPT_TOOLS: 'enabled',
  AZURE_SUBSCRIPTION_ID: 'sub',
  AZURE_RESOURCE_GROUP: 'rg',
  CONSUMER_FOUNDRY_NAME: 'acct',
  CONSUMER_FOUNDRY_PROJECT_ENDPOINT: PROJECT,
  LITELLM_ENDPOINT: GATEWAY,
  ENTRA_GATEWAY_CLIENT_ID: 'c00eeca7-9675-4e1b-96b1-6f499b811527',
  ENTRA_APP_PRINCIPALS: JSON.stringify([{ oid: IDENTITY.principal_id, azp: IDENTITY.client_id }]),
  ENTRA_CAPABILITIES: JSON.stringify({ [IDENTITY.principal_id]: { mcp: { curriculum: TOOLS }, agents: [A2A_AGENT] } }),
};
const json = (body, status = 200) => new Response(JSON.stringify(body), { status });

// Records every call and answers from `routes` ("METHOD path-suffix" -> response factory).
function fakeFetch(routes) {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    const method = options.method ?? 'GET';
    const call = { method, url, body: options.body && JSON.parse(options.body), auth: options.headers?.Authorization };
    calls.push(call);
    const key = Object.keys(routes).find(route => {
      const [m, suffix] = route.split(' ');
      return m === method && url.endsWith(suffix);
    });
    if (!key) throw new Error(`unexpected ${method} ${url}`);
    return routes[key](call);
  };
  return { fetchImpl, calls };
}
const armUrl = name => `/connections/${name}?api-version=2025-04-01-preview`;
const run = routes => {
  const { fetchImpl, calls } = fakeFetch(routes);
  const logs = [];
  return resolvePromptAgent({
    agent, env, foundryToken: 'foundry-token', armToken: 'arm-token', fetchImpl, log: m => logs.push(m),
  }).then(result => ({ result, calls, logs }));
};
const agentRoute = { 'GET /agents/ai-tutor-prompt-agent?api-version=v1': () => json({ instance_identity: IDENTITY }) };
const connectionRoutes = created => Object.fromEntries(Object.values({
  mcp: NAMES.mcpConnection, a2a: NAMES.a2aConnection, toolbox: NAMES.toolboxConnection,
}).flatMap(name => [
  [`GET ${armUrl(name)}`, () => json({}, 404)],
  [`PUT ${armUrl(name)}`, call => {
    created.push(name);
    return json({ id: `/arm/${name}`, properties: call.body.properties });
  }],
]));

test('does nothing unless PROMPT_TOOLS is enabled', async () => {
  const { fetchImpl, calls } = fakeFetch({});
  const result = await resolvePromptAgent({
    agent, env: { ...env, PROMPT_TOOLS: 'disabled' }, foundryToken: 't', armToken: 'a', fetchImpl,
  });
  assert.equal(result, agent);
  assert.equal(calls.length, 0);
});

test('requires the agent identity to be approved twice before any tool is attached', () => {
  assert.equal(toolsApproved(IDENTITY, env), true);
  assert.equal(toolsApproved(IDENTITY, { ...env, ENTRA_APP_PRINCIPALS: '[]' }), false);
  assert.equal(toolsApproved(IDENTITY, { ...env, ENTRA_CAPABILITIES: '{}' }), false);
  assert.equal(toolsApproved(IDENTITY, {
    ...env, ENTRA_CAPABILITIES: JSON.stringify({ [IDENTITY.principal_id]: { mcp: { curriculum: ['list_topics'] }, agents: [A2A_AGENT] } }),
  }), false, 'a partial tool grant is not enough');
  assert.equal(toolsApproved(IDENTITY, {
    ...env, ENTRA_CAPABILITIES: JSON.stringify({ [IDENTITY.principal_id]: { mcp: { curriculum: TOOLS }, agents: ['other'] } }),
  }), false);
  assert.equal(toolsApproved({ principal_id: IDENTITY.principal_id }, env), false);
  assert.equal(toolsApproved(undefined, env), false);
});

test('an unapproved agent is deployed without tools and nothing is written to Azure', async () => {
  const { fetchImpl, calls: unapprovedCalls } = fakeFetch(agentRoute);
  const messages = [];
  const out = await resolvePromptAgent({
    agent, env: { ...env, ENTRA_APP_PRINCIPALS: '[]' }, foundryToken: 't', armToken: 'a', fetchImpl, log: m => messages.push(m),
  });
  assert.equal(out, agent);
  assert.deepEqual(unapprovedCalls.map(c => c.method), ['GET']);
  assert.match(messages[0], /tools NOT attached/);
  assert.match(messages[0], new RegExp(IDENTITY.principal_id));
});

test('a new agent without an identity yet is deployed plain with guidance to approve it afterwards', async () => {
  const { fetchImpl } = fakeFetch({ 'GET /agents/ai-tutor-prompt-agent?api-version=v1': () => json({}, 404) });
  const messages = [];
  const out = await resolvePromptAgent({ agent, env, foundryToken: 't', armToken: 'a', fetchImpl, log: m => messages.push(m) });
  assert.equal(out, agent);
  assert.match(messages[0], /no identity yet/);
});

test('creates connections and a toolbox, then attaches the toolbox MCP tool and the direct A2A tool', async () => {
  const created = [];
  const { result, calls } = await run({
    ...agentRoute,
    ...connectionRoutes(created),
    'GET /toolboxes/tutor-tools?api-version=v1': () => json({}, 404),
    'POST /toolboxes/tutor-tools/versions?api-version=v1': () => json({ version: '1' }),
  });
  assert.deepEqual(created.sort(), Object.values({ a: NAMES.a2aConnection, b: NAMES.mcpConnection, c: NAMES.toolboxConnection }).sort());

  const puts = Object.fromEntries(calls.filter(c => c.method === 'PUT').map(c => [c.url.split('/connections/')[1].split('?')[0], c.body.properties]));
  assert.equal(puts[NAMES.mcpConnection].target, `${GATEWAY}/mcp/curriculum`);
  assert.equal(puts[NAMES.a2aConnection].target, `${GATEWAY}/a2a/quiz_master/`, 'the trailing slash is required');
  for (const name of [NAMES.mcpConnection, NAMES.a2aConnection]) {
    assert.equal(puts[name].authType, 'AgenticIdentityToken');
    assert.equal(puts[name].audience, 'api://c00eeca7-9675-4e1b-96b1-6f499b811527');
  }
  assert.equal(puts[NAMES.toolboxConnection].authType, 'UserEntraToken');
  assert.equal(puts[NAMES.toolboxConnection].audience, 'https://ai.azure.com');
  assert.ok(calls.filter(c => c.url.startsWith('https://management.azure.com')).every(c => c.auth === 'Bearer arm-token'));
  assert.ok(calls.filter(c => c.url.startsWith(PROJECT)).every(c => c.auth === 'Bearer foundry-token'));

  const version = calls.find(c => c.method === 'POST').body;
  assert.equal(version.tools.length, 1);
  assert.equal(version.tools[0].server_url, `${GATEWAY}/mcp/curriculum`);
  assert.equal(version.tools[0].project_connection_id, NAMES.mcpConnection);
  assert.equal(calls.some(c => c.method === 'PATCH'), false, 'the first toolbox version becomes the default by itself');

  const [mcp, a2a] = result.tools;
  assert.equal(mcp.server_url, `${PROJECT}/toolboxes/tutor-tools/mcp?api-version=v1`);
  assert.equal(mcp.project_connection_id, NAMES.toolboxConnection);
  assert.equal(mcp.require_approval, 'never');
  assert.deepEqual(a2a, {
    type: 'a2a', a2a_version: '1.0', base_url: `${GATEWAY}/a2a/quiz_master/`,
    project_connection_id: `/arm/${NAMES.a2aConnection}`, send_credentials_for_agent_card: true,
  });
  assert.ok(!('base_url' in mcp));
  assert.match(result.instructions, /quiz tool \(gateway-a2a\)/);
  assert.doesNotMatch(result.instructions, /no external tools/);
  assert.equal(result.model, agent.model);
});

test('leaves matching connections and toolbox alone and promotes a changed toolbox version', async () => {
  const matching = name => () => json({
    id: `/arm/${name}`,
    properties: {
      authType: name === NAMES.toolboxConnection ? 'UserEntraToken' : 'AgenticIdentityToken',
      category: name === NAMES.a2aConnection ? 'RemoteA2A' : 'RemoteTool',
      target: {
        [NAMES.mcpConnection]: `${GATEWAY}/mcp/curriculum`,
        [NAMES.a2aConnection]: `${GATEWAY}/a2a/quiz_master/`,
        [NAMES.toolboxConnection]: `${PROJECT}/toolboxes/tutor-tools/mcp?api-version=v1`,
      }[name],
      audience: name === NAMES.toolboxConnection ? 'https://ai.azure.com' : 'api://c00eeca7-9675-4e1b-96b1-6f499b811527',
    },
  });
  const routes = {
    ...agentRoute,
    ...Object.fromEntries([NAMES.mcpConnection, NAMES.a2aConnection, NAMES.toolboxConnection].map(n => [`GET ${armUrl(n)}`, matching(n)])),
    'GET /toolboxes/tutor-tools?api-version=v1': () => json({ default_version: '1' }),
    'GET /toolboxes/tutor-tools/versions/1?api-version=v1': () => json({ tools: [{ type: 'mcp', server_url: 'https://old.example/mcp' }] }),
    'POST /toolboxes/tutor-tools/versions?api-version=v1': () => json({ version: '2' }),
    'PATCH /toolboxes/tutor-tools?api-version=v1': () => json({}),
  };
  const { calls } = await run(routes);
  assert.equal(calls.some(c => c.method === 'PUT'), false);
  assert.equal(calls.find(c => c.method === 'PATCH').body.default_version, '2');

  const settled = await run({
    ...routes,
    'GET /toolboxes/tutor-tools/versions/1?api-version=v1': () => json({
      tools: [{
        type: 'mcp', server_label: 'curriculum', server_url: `${GATEWAY}/mcp/curriculum`, require_approval: 'never',
        project_connection_id: NAMES.mcpConnection, extra_default: true,
        description: 'Curriculum catalogue and learning modes behind the LiteLLM gateway',
      }],
    }),
  });
  assert.equal(settled.calls.some(c => ['POST', 'PUT', 'PATCH'].includes(c.method)), false, 'no writes when nothing changed');
});

test('refuses an HTTP gateway and surfaces Azure errors', async () => {
  await assert.rejects(resolvePromptAgent({
    agent, env: { ...env, LITELLM_ENDPOINT: 'http://gw.internal' }, foundryToken: 't', armToken: 'a',
    fetchImpl: fakeFetch(agentRoute).fetchImpl,
  }), /LITELLM_ENDPOINT must be HTTPS/);
  await assert.rejects(run({
    ...agentRoute,
    [`GET ${armUrl(NAMES.mcpConnection)}`]: () => json({ error: 'Forbidden' }, 403),
  }), /failed \(403\).*Forbidden/);
});

test('guidance replaces the "no tools" sentence and spells out the exact A2A messages', () => {
  const text = instructionsWithTools(agent.instructions);
  assert.match(text, /personal or sensitive information/);
  assert.doesNotMatch(text, /You have no external tools/);
  assert.match(text, /"action":"create"/);
  assert.match(text, /"action":"grade"/);
  assert.match(text, /character for character/);
  assert.equal(instructionsWithTools('Be kind.'), `Be kind.\n\n${TOOL_GUIDANCE}`);
});

test('definition comparison tolerates service defaults but notices tool changes', () => {
  const desired = { kind: 'prompt', model: 'a/b', instructions: 'x', tools: [{ type: 'a2a', base_url: 'https://x/' }] };
  assert.equal(definitionMatches({ ...desired, tools: [{ ...desired.tools[0], added: 1 }], metadata: {} }, desired), true);
  assert.equal(definitionMatches({ kind: 'prompt', model: 'a/b', instructions: 'x' }, desired), false);
  assert.equal(definitionMatches(desired, { kind: 'prompt', model: 'a/b', instructions: 'x' }), false, 'removing tools creates a version');
  assert.equal(definitionMatches({ ...desired, instructions: 'y' }, desired), false);
});

test('preflight validates PROMPT_TOOLS', () => {
  const base = {
    GATEWAY_AUTH_MODE: 'entra', HOSTED_AUTH_MODE: 'entra', PROMPT_AUTH_MODE: 'oauth2',
    ENTRA_TENANT_ID: '5d35dd6f-6bd8-469c-8ca3-5972194e432d',
    ENTRA_GATEWAY_CLIENT_ID: 'c00eeca7-9675-4e1b-96b1-6f499b811527',
  };
  assert.throws(() => validateAuthConfig({ ...base, PROMPT_TOOLS: 'yes' }), /PROMPT_TOOLS must be exactly/);
  assert.throws(() => validateAuthConfig({ ...base, PROMPT_TOOLS: 'enabled' }), /requires ENABLE_DEMO_BACKENDS=true/);
  assert.throws(() => validateAuthConfig({ ...base, PROMPT_TOOLS: 'enabled', ENABLE_DEMO_BACKENDS: 'false' }), /requires ENABLE_DEMO_BACKENDS=true/);
});
