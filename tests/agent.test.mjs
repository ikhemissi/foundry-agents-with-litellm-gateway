import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { deployPromptAgent } from '../scripts/deploy-prompt-agent.mjs';

const manifest = readFileSync(new URL('../azure.yaml', import.meta.url), 'utf8');
const agent = JSON.parse(readFileSync(
  new URL('../agents/ai-tutor-prompt-agent/agent.json', import.meta.url), 'utf8',
));
const settings = {
  endpoint: 'https://consumer.services.ai.azure.com/api/projects/consumer-project',
  agent,
  token: 'test-token',
};
const version = {
  name: agent.name,
  version: '1',
  description: agent.description,
  definition: { kind: agent.kind, model: agent.model, instructions: agent.instructions },
};
const liveAgent = { versions: { latest: version } };
const response = (body, status = 200) => new Response(JSON.stringify(body), { status });

test('keeps prompt deployment hooks alongside the native hosted agent service', () => {
  for (const hook of ['postprovision', 'postdeploy']) {
    assert.match(manifest, new RegExp(`${hook}:\\n\\s+shell: sh\\n\\s+run: node scripts/deploy-prompt-agent\\.mjs`));
  }
  assert.match(manifest, /--template-file infra\/modules\/hosted-agent\.bicep/);
  assert.match(manifest, /ai-tutor-hosted-agent:\n\s+host: azure\.ai\.agent/);
  assert.doesNotMatch(manifest, /ai-tutor-prompt-agent:\n\s+host:/);
  assert.match(manifest, /infra:\n\s+provider: bicep/);
});

test('uses standard top-level prompt agent fields with explicit model selection', () => {
  assert.equal(agent.name, 'ai-tutor-prompt-agent');
  assert.equal(agent.kind, 'prompt');
  assert.equal(agent.model, 'litellm-gateway/gpt-5.4-nano');
  assert.match(agent.instructions, /hint or guiding question/);
  assert.match(agent.instructions, /acknowledge uncertainty/);
  assert.match(agent.instructions, /personal or sensitive information/);
  assert.deepEqual(Object.keys(agent).sort(), ['description', 'instructions', 'kind', 'model', 'name']);
});

test('creates a missing agent with the exact gateway-qualified model and verifies persistence', async () => {
  const calls = [];
  const replies = [response({}, 404), response(version, 201), response(liveAgent)];
  const result = await deployPromptAgent({
    ...settings,
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return replies.shift();
    },
  });
  assert.equal(result.changed, true);
  assert.equal(calls.length, 3);
  assert.equal(calls[1].url, `${settings.endpoint}/agents/${agent.name}/versions?api-version=v1`);
  assert.equal(calls[1].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    description: agent.description, definition: version.definition,
  });
  assert.equal(calls[1].options.headers.Authorization, 'Bearer test-token');
  assert.equal(calls[1].options.redirect, 'error');
});

test('does not create another version when the agent is unchanged', async () => {
  let requests = 0;
  const result = await deployPromptAgent({
    ...settings,
    fetchImpl: async () => {
      requests++;
      return response(liveAgent);
    },
  });
  assert.equal(result.changed, false);
  assert.equal(result.version, '1');
  assert.equal(requests, 1);
});

test('preserves per-agent model selection and additional definition settings', async () => {
  const selected = { ...agent, model: 'another-gateway/tutor-model', temperature: 0.5 };
  const selectedVersion = {
    ...version,
    definition: { ...version.definition, model: selected.model, temperature: selected.temperature },
  };
  const replies = [response({}, 404), response(selectedVersion, 201), response({ versions: { latest: selectedVersion } })];
  const result = await deployPromptAgent({
    ...settings,
    agent: selected,
    fetchImpl: async (_url, options) => {
      if (options.method === 'POST') {
        assert.deepEqual(JSON.parse(options.body), {
          description: selected.description,
          definition: selectedVersion.definition,
        });
      }
      return replies.shift();
    },
  });
  assert.equal(result.definition.model, selected.model);
  assert.equal(result.definition.temperature, 0.5);
});

test('creates a new version after instructions change', async () => {
  const replies = [
    response({ versions: { latest: { ...version, definition: { ...version.definition, instructions: 'Old instructions' } } } }),
    response(version, 201),
    response(liveAgent),
  ];
  const result = await deployPromptAgent({
    ...settings, fetchImpl: async () => replies.shift(),
  });
  assert.equal(result.changed, true);
  assert.equal(replies.length, 0);
});

test('updates a changed gateway model or description instead of silently keeping the old one', async () => {
  for (const previous of [
    { ...version, definition: { ...version.definition, model: 'old-gateway/old-model' } },
    { ...version, description: 'Old description' },
  ]) {
    const replies = [
      response({ versions: { latest: previous } }),
      response(version, 201),
      response(liveAgent),
    ];
    const result = await deployPromptAgent({
      ...settings, fetchImpl: async () => replies.shift(),
    });
    assert.equal(result.changed, true);
    assert.equal(replies.length, 0);
  }
});

test('surfaces authorization and creation errors without success-shaped fallbacks', async () => {
  await assert.rejects(deployPromptAgent({
    ...settings, fetchImpl: async () => response({ error: 'Forbidden' }, 403),
  }), /Foundry GET failed \(403\).*Forbidden/);
  const replies = [response({}, 404), response({ error: 'Invalid model' }, 400)];
  await assert.rejects(deployPromptAgent({
    ...settings, fetchImpl: async () => replies.shift(),
  }), /Foundry POST failed \(400\).*Invalid model/);
});

test('fails if the created configuration was not persisted', async () => {
  const replies = [response({}, 404), response(version, 201), response({}, 404)];
  await assert.rejects(deployPromptAgent({
    ...settings, fetchImpl: async () => replies.shift(),
  }), /did not persist the expected version/);
});

test('rejects missing configuration and invalid endpoints or model references before HTTP calls', async () => {
  for (const invalid of [
    { endpoint: undefined }, { token: '' },
    { endpoint: 'http://consumer.services.ai.azure.com/api/projects/p' },
    { endpoint: 'https://consumer.services.ai.azure.com/' },
    { endpoint: `${settings.endpoint}?api-version=other` },
    { agent: { ...agent, model: '' } },
    { agent: { ...agent, model: undefined } },
    { agent: { ...agent, model: 'gpt-5.4-nano' } },
    { agent: { name: agent.name, definition: version.definition } },
    { agent: { ...agent, name: '../other-agent' } },
  ]) {
    await assert.rejects(deployPromptAgent({
      ...settings, ...invalid,
      fetchImpl: async () => assert.fail('Invalid configuration must not make HTTP requests'),
    }));
  }
});
