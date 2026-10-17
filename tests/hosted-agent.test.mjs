import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const root = fileURLToPath(new URL('../', import.meta.url));
const manifest = readFileSync(new URL('../azure.yaml', import.meta.url), 'utf8');
const definition = name => JSON.parse(readFileSync(
  new URL(`../agents/${name}/agent.json`, import.meta.url), 'utf8',
));

test('keeps two independent tutors with the same pedagogy and distinct gateway model forms', () => {
  const prompt = definition('ai-tutor-prompt-agent');
  const hosted = definition('ai-tutor-hosted-agent');
  assert.equal(hosted.name, 'ai-tutor-hosted-agent');
  assert.equal(hosted.model, 'gpt-5.4-nano');
  assert.equal(prompt.model, `litellm-gateway/${hosted.model}`);
  assert.equal(hosted.instructions, prompt.instructions);
});

test('uses native Python code deployment, with no model deployment or secret in the hosted environment', () => {
  assert.match(manifest, /codeConfiguration:\n\s+runtime: python_3_13\n\s+entryPoint: main\.py\n\s+dependencyResolution: remote_build/);
  assert.match(manifest, /protocol: responses\n\s+version: 2\.0\.0/);
  assert.match(manifest, /LITELLM_BASE_URL: \$\{LITELLM_ENDPOINT\}\/v1/);
  assert.match(manifest, /APPLICATIONINSIGHTS_CONNECTION_STRING: \$\{APPLICATIONINSIGHTS_CONNECTION_STRING\}/);
  assert.doesNotMatch(manifest, /LITELLM_MASTER_KEY|OPENAI_API_KEY|deployments:/);
  assert.match(manifest, /--query versions\.latest\.instance_identity\.principal_id/);
  assert.match(manifest, /--template-file infra\/modules\/hosted-agent\.bicep/);
  assert.match(manifest, /hostedAuthMode="\$\{HOSTED_AUTH_MODE:-api_key\}"/);
  assert.match(manifest, /LITELLM_AUTH_MODE: \$\{HOSTED_AUTH_MODE=api_key\}/);
  assert.match(manifest, /ENTRA_GATEWAY_SCOPE: \$\{ENTRA_GATEWAY_SCOPE\}/);
  assert.match(manifest, /ENTRA_AGENT_CLIENT_ID: \$\{ENTRA_AGENT_CLIENT_ID\}/);
  assert.match(manifest, /ENTRA_AGENT_PRINCIPAL_ID: \$\{ENTRA_AGENT_PRINCIPAL_ID\}/);
  assert.doesNotMatch(manifest, /deploy-hosted-agent-access/);
});

test('declares only secret-scoped read and component-scoped publishing for the hosted identity in Bicep', () => {
  const template = JSON.parse(execFileSync('az', [
    'bicep', 'build', '--file', 'infra/modules/hosted-agent.bicep', '--stdout',
  ], { cwd: root, encoding: 'utf8' }));
  assert.equal(template.parameters.tags.type, 'object');
  const roles = template.resources;
  assert.equal(roles.length, 2);
  assert.ok(roles.every(role =>
    role.type === 'Microsoft.Authorization/roleAssignments'
      && role.properties.principalId === "[parameters('agentPrincipalId')]"
      && role.properties.principalType === 'ServicePrincipal'));
  const reader = roles.find(role =>
    role.properties.roleDefinitionId.includes('4633458b-17de-408a-b874-0445c86b69e6'));
  assert.match(reader.scope, /Microsoft\.KeyVault\/vaults\/secrets/);
  assert.match(reader.scope, /litellm-master-key/);
  assert.equal(template.parameters.hostedAuthMode.defaultValue, 'api_key');
  assert.deepEqual(template.parameters.hostedAuthMode.allowedValues, ['api_key', 'entra', 'entra_delegated']);
  assert.equal(reader.condition, "[equals(parameters('hostedAuthMode'), 'api_key')]");
  assert.match(reader.name, /Key Vault Secrets User/);
  const publisher = roles.find(role =>
    role.properties.roleDefinitionId.includes('3913510d-42f4-4e42-8a64-420c390055eb'));
  assert.match(publisher.scope, /Microsoft\.Insights\/components/);
  assert.equal(publisher.condition, undefined);
  assert.match(publisher.name, /Monitoring Metrics Publisher/);
});

test('provides workspace-root debug settings using the hosted agent virtual environment', () => {
  const tasks = JSON.parse(readFileSync(new URL('../.vscode/tasks.json', import.meta.url), 'utf8'));
  const launch = JSON.parse(readFileSync(new URL('../.vscode/launch.json', import.meta.url), 'utf8'));
  assert.match(tasks.tasks[0].command, /--no-client/);
  assert.match(tasks.tasks[0].command, /\.venv\/bin\/python -m debugpy/);
  assert.equal(tasks.inputs[0].args.port, 8088);
  assert.equal(launch.configurations[0].connect.port, 5679);
});
