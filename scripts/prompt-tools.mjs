// Optional gateway tools for the Foundry prompt agent (PROMPT_TOOLS=enabled).
//
// The curriculum MCP server is reached through a Foundry toolbox and the quiz A2A agent through a
// direct A2A tool; both go to the LiteLLM gateway as the prompt agent's own agent identity. A toolbox
// cannot carry the quiz agent: it fetches the card anonymously and the gateway protects it.
import { isDeepStrictEqual } from 'node:util';

export const NAMES = Object.freeze({
  toolbox: 'tutor-tools',
  mcpConnection: 'gateway-mcp',
  a2aConnection: 'gateway-a2a',
  toolboxConnection: 'tutor-tools-conn',
});
export const MCP_SERVER = 'curriculum';
export const A2A_AGENT = 'quiz_master';
export const REQUIRED_MCP_TOOLS = ['list_learning_modes', 'list_topics', 'get_topic', 'get_section'];
const ARM_API = '2025-04-01-preview';
const ARM_HOST = 'https://management.azure.com';
const FOUNDRY_AUDIENCE = 'https://ai.azure.com';

export const TOOL_GUIDANCE = [
  'You can use these tools whenever they help. If one is unavailable or returns an error, carry on from your own knowledge and say so briefly.',
  '- The curriculum tools (list_learning_modes, list_topics, get_topic, get_section): the course catalogue. Ground lessons in it and do not invent topics.',
  '- The quiz tool (gateway-a2a): a quiz service. Its message text must be exactly one JSON object and nothing else.',
  '  Create: {"action":"create","kind":"diagnostic|section|mastery","topic_id":"<topic id>","section_id":"<section id, only for section and mastery>","level":"intro|working|deep","count":3}',
  '  Grade: {"action":"grade","quiz_id":"<quiz_id from the create reply>","answers":{"q1":"<learner answer>","q2":"<learner answer>"}} with every question id and the learner\'s exact answers.',
  'Never state, guess or hint at a quiz answer yourself. Show the learner the questions, collect their answers, then grade. Copy the quiz_id character for character from the create reply (never retype or shorten it) and keep it in the conversation until the quiz is graded.',
  'Learning flow: at the start ask what the learner wants to learn, how deep to go (intro, working or deep) and how they like to learn; offer the modes from list_learning_modes and follow the chosen mode\'s tutor behaviour. Teach one section at a time using get_section at the chosen depth. At the end of each section offer a short section quiz. After grading, acknowledge what was right, explain mistakes using the feedback, and revisit weak objectives before moving on. Never mention tool credentials, identifiers or internal error details.',
].join('\n');

const NO_TOOLS = /\n*You have no external tools[^\n]*$/;

export function instructionsWithTools(instructions) {
  const base = NO_TOOLS.test(instructions) ? instructions.replace(NO_TOOLS, '') : instructions;
  return `${base.trimEnd()}\n\n${TOOL_GUIDANCE}`;
}

function parseJson(text, name) {
  try {
    return text?.trim() ? JSON.parse(text) : undefined;
  } catch {
    throw new Error(`${name} is not valid JSON.`);
  }
}

// The gateway only serves tools to identities it has approved twice: for the application and for
// the specific MCP tools and agent. Attaching tools to an unapproved agent would make Foundry fail
// every request of the tutor, so approval is checked before anything is attached.
export function toolsApproved(identity, env) {
  const id = String(identity?.principal_id ?? '').toLowerCase();
  const client = String(identity?.client_id ?? '').toLowerCase();
  if (!id || !client) return false;
  const principals = parseJson(env.ENTRA_APP_PRINCIPALS, 'ENTRA_APP_PRINCIPALS') ?? [];
  const capabilities = parseJson(env.ENTRA_CAPABILITIES, 'ENTRA_CAPABILITIES') ?? {};
  if (!Array.isArray(principals)
      || !principals.some(p => String(p?.oid).toLowerCase() === id && String(p?.azp).toLowerCase() === client)) {
    return false;
  }
  const key = Object.keys(capabilities).find(k => k.toLowerCase() === id);
  const grant = key && capabilities[key];
  return Boolean(grant?.agents?.includes(A2A_AGENT)
    && REQUIRED_MCP_TOOLS.every(tool => grant?.mcp?.[MCP_SERVER]?.includes(tool)));
}

// True when every field of `desired` appears in `actual`; the service may add defaults.
function covers(actual, desired) {
  if (Array.isArray(desired)) {
    return Array.isArray(actual) && actual.length === desired.length
      && desired.every((item, index) => covers(actual[index], item));
  }
  if (desired && typeof desired === 'object') {
    return actual && typeof actual === 'object'
      && Object.entries(desired).every(([key, value]) => covers(actual[key], value));
  }
  return isDeepStrictEqual(actual, desired);
}

export function definitionMatches(actual, desired) {
  const count = value => (Array.isArray(value?.tools) ? value.tools.length : 0);
  return count(actual) === count(desired) && covers(actual, desired);
}

export function settingsFrom(env) {
  const need = name => {
    const value = env[name];
    if (typeof value !== 'string' || !value.trim()) throw new Error(`${name} is required for PROMPT_TOOLS.`);
    return value.trim();
  };
  const project = new URL(need('CONSUMER_FOUNDRY_PROJECT_ENDPOINT'));
  const match = /^\/api\/projects\/([^/]+)\/?$/.exec(project.pathname);
  const gateway = new URL(need('LITELLM_ENDPOINT'));
  if (project.protocol !== 'https:' || !match) throw new Error('CONSUMER_FOUNDRY_PROJECT_ENDPOINT must be an HTTPS Foundry project endpoint.');
  if (gateway.protocol !== 'https:') throw new Error('LITELLM_ENDPOINT must be HTTPS: Foundry sends no credentials over HTTP.');
  const clientId = need('ENTRA_GATEWAY_CLIENT_ID');
  return {
    projectEndpoint: project.href.replace(/\/$/, ''),
    gateway: gateway.origin,
    audience: `api://${clientId}`,
    connectionBase: `/subscriptions/${need('AZURE_SUBSCRIPTION_ID')}/resourceGroups/${need('AZURE_RESOURCE_GROUP')}`
      + `/providers/Microsoft.CognitiveServices/accounts/${need('CONSUMER_FOUNDRY_NAME')}/projects/${match[1]}/connections`,
  };
}

export function connectionSpecs(settings) {
  const { gateway, audience, projectEndpoint } = settings;
  return [
    {
      name: NAMES.mcpConnection, authType: 'AgenticIdentityToken', category: 'RemoteTool',
      target: `${gateway}/mcp/${MCP_SERVER}`, audience, metadata: { type: 'generic_mcp', audience },
    },
    {
      // The trailing slash matters: without it Foundry looks for the card at /a2a/.well-known/...
      name: NAMES.a2aConnection, authType: 'AgenticIdentityToken', category: 'RemoteA2A',
      target: `${gateway}/a2a/${A2A_AGENT}/`, audience, metadata: { ApiType: 'Azure' },
    },
    {
      name: NAMES.toolboxConnection, authType: 'UserEntraToken', category: 'RemoteTool',
      target: `${projectEndpoint}/toolboxes/${NAMES.toolbox}/mcp?api-version=v1`, audience: FOUNDRY_AUDIENCE,
      metadata: { type: 'generic_mcp', audience: FOUNDRY_AUDIENCE },
    },
  ];
}

export async function configurePromptTools({ env, foundryToken, armToken, fetchImpl = fetch }) {
  const settings = settingsFrom(env);
  async function call(url, token, method = 'GET', body) {
    const response = await fetchImpl(url, {
      method,
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      ...(body && { body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(120_000),
      redirect: 'error',
    });
    if (method === 'GET' && response.status === 404) return null;
    if (!response.ok) throw new Error(`${method} ${new URL(url).pathname} failed (${response.status}): ${await response.text()}`);
    return response.status === 204 ? {} : response.json();
  }

  const ids = {};
  for (const spec of connectionSpecs(settings)) {
    const url = `${ARM_HOST}${settings.connectionBase}/${spec.name}?api-version=${ARM_API}`;
    const current = await call(url, armToken);
    const live = current?.properties;
    const same = live && live.authType === spec.authType && live.category === spec.category
      && live.target === spec.target && live.audience === spec.audience;
    let resource = current;
    if (!same) {
      resource = await call(url, armToken, 'PUT', {
        properties: {
          authType: spec.authType, category: spec.category, group: 'ServicesAndApps', target: spec.target,
          audience: spec.audience, isSharedToAll: false, credentials: {}, metadata: spec.metadata,
        },
      });
      if (resource?.properties?.target !== spec.target) {
        throw new Error(`Connection ${spec.name} did not take the expected target; delete it and retry.`);
      }
    }
    ids[spec.name] = resource.id ?? `${settings.connectionBase}/${spec.name}`;
  }

  const base = `${settings.projectEndpoint}/toolboxes/${NAMES.toolbox}`;
  const desiredTools = [{
    type: 'mcp',
    description: 'Curriculum catalogue and learning modes behind the LiteLLM gateway',
    server_label: MCP_SERVER,
    server_url: `${settings.gateway}/mcp/${MCP_SERVER}`,
    require_approval: 'never',
    project_connection_id: NAMES.mcpConnection,
  }];
  const toolbox = await call(`${base}?api-version=v1`, foundryToken);
  const live = toolbox?.default_version
    ? await call(`${base}/versions/${toolbox.default_version}?api-version=v1`, foundryToken)
    : null;
  if (!live || !covers(live.tools, desiredTools)) {
    const created = await call(`${base}/versions?api-version=v1`, foundryToken, 'POST', {
      description: 'Tutor tools behind the LiteLLM gateway', tools: desiredTools,
    });
    if (toolbox) {
      await call(`${base}?api-version=v1`, foundryToken, 'PATCH', { default_version: created.version });
    }
  }

  return [
    {
      type: 'mcp', server_label: 'toolbox', server_url: `${base}/mcp?api-version=v1`,
      require_approval: 'never', project_connection_id: NAMES.toolboxConnection,
    },
    {
      type: 'a2a', a2a_version: '1.0', base_url: `${settings.gateway}/a2a/${A2A_AGENT}/`,
      project_connection_id: ids[NAMES.a2aConnection], send_credentials_for_agent_card: true,
    },
  ];
}

// Returns the agent definition to deploy. Tools are attached only when the agent already has an
// identity and the gateway approves it; otherwise the plain tutor is deployed and the operator is told why.
export async function resolvePromptAgent({ agent, env, foundryToken, armToken, fetchImpl = fetch, log = console.log }) {
  if (env.PROMPT_TOOLS !== 'enabled') return agent;
  const project = settingsFrom(env).projectEndpoint;
  const response = await fetchImpl(`${project}/agents/${encodeURIComponent(agent.name)}?api-version=v1`, {
    headers: { Authorization: `Bearer ${foundryToken}` }, signal: AbortSignal.timeout(120_000), redirect: 'error',
  });
  const identity = response.ok ? (await response.json()).instance_identity : undefined;
  if (!response.ok && response.status !== 404) {
    throw new Error(`Foundry GET agent failed (${response.status}).`);
  }
  if (!toolsApproved(identity, env)) {
    const id = identity?.principal_id;
    log(`PROMPT_TOOLS: tools NOT attached to ${agent.name}. ${id
      ? `Approve its identity ${id} (application ID ${identity.client_id}) in ENTRA_APP_PRINCIPALS and give it the curriculum tools and ${A2A_AGENT} in ENTRA_CAPABILITIES, run azd provision, then azd deploy again.`
      : 'It has no identity yet; this deployment creates it. Approve it afterwards, then deploy again.'}`);
    return agent;
  }
  const tools = await configurePromptTools({ env, foundryToken, armToken, fetchImpl });
  return { ...agent, instructions: instructionsWithTools(agent.instructions), tools };
}
