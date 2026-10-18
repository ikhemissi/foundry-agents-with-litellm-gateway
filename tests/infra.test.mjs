import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const root = fileURLToPath(new URL('../', import.meta.url));
const template = JSON.parse(execFileSync('az', [
  'bicep', 'build', '--file', 'infra/main.bicep', '--stdout',
], { cwd: root, encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 }));
const allModules = template.resources.filter(
  resource => resource.type === 'Microsoft.Resources/deployments',
);
// Optional demo modules are asserted separately, so the always-on baseline stays comparable.
const demoModuleNames = ['demo-quiz-identity', 'demo-mcp-image', 'demo-a2a-image', 'demo-backends'];
const modules = allModules.filter(module => !demoModuleNames.includes(module.name));
const moduleNamed = name => modules.find(module => module.name === name);
const moduleTemplate = name => moduleNamed(name).properties.template;
const resources = [
  ...template.resources.filter(resource => resource.type !== 'Microsoft.Resources/deployments'),
  ...modules.flatMap(module => module.properties.template.resources),
];
const ofType = type => resources.filter(resource => resource.type === type);

test('orchestrates resource groups with local permissions and stable dependency wiring', () => {
  assert.deepEqual(modules.map(module => module.name), [
    'resource-group-access',
    'gateway-identity',
    'source-foundry',
    'consumer-foundry',
    'gateway-key-vault',
    'gateway-monitoring',
    'gateway-container-apps',
    'consumer-model-connection',
  ]);
  for (const module of modules) {
    assert.equal(module.properties.parameters.tags.value, "[variables('resourceTags')]");
    assert.equal(module.properties.template.parameters.tags.type, 'object');
    assert.equal(module.resourceGroup, "[format('rg-{0}', parameters('environmentName'))]");
    assert.ok(module.dependsOn.some(dependency => /Microsoft.Resources\/resourceGroups/.test(dependency)));
  }
  const dependencies = name => moduleNamed(name).dependsOn.join(' ');
  assert.match(dependencies('source-foundry'), /gateway-identity/);
  assert.match(dependencies('gateway-key-vault'), /gateway-identity/);
  for (const prerequisite of ['gateway-identity', 'source-foundry', 'gateway-key-vault', 'gateway-monitoring']) {
    assert.ok(dependencies('gateway-container-apps').includes(prerequisite));
  }
  for (const prerequisite of ['consumer-foundry', 'gateway-container-apps', 'source-foundry']) {
    assert.ok(dependencies('consumer-model-connection').includes(prerequisite));
  }
  for (const name of ['gateway-key-vault', 'consumer-model-connection']) {
    assert.equal(moduleTemplate(name).parameters.litellmMasterKey.type.toLowerCase(), 'securestring');
    assert.equal(moduleNamed(name).properties.parameters.litellmMasterKey.value,
      "[parameters('litellmMasterKey')]");
  }
  const suffix = "[uniqueString(subscription().id, subscriptionResourceId('Microsoft.Resources/resourceGroups', format('rg-{0}', parameters('environmentName'))))]";
  assert.equal(template.variables.suffix, suffix);
  for (const module of modules.filter(module => module.properties.parameters.suffix)) {
    assert.equal(module.properties.parameters.suffix.value, "[variables('suffix')]");
  }
});

test('allows exactly the requested regions and defaults to East US', () => {
  assert.equal(template.parameters.location.defaultValue, 'eastus');
  assert.deepEqual(template.parameters.location.allowedValues, [
    'eastus', 'uksouth', 'swedencentral',
  ]);
  const parameters = JSON.parse(readFileSync(
    new URL('../infra/main.parameters.json', import.meta.url), 'utf8',
  ));
  assert.equal(parameters.parameters.location.value, '${AZURE_LOCATION=eastus}');
});

test('passes configurable tags through the resource group and module to every taggable resource', () => {
  assert.equal(template.parameters.tags.type, 'object');
  const parameters = JSON.parse(readFileSync(
    new URL('../infra/main.parameters.json', import.meta.url), 'utf8',
  ));
  assert.deepEqual(parameters.parameters.tags.value, {
    SecurityControl: 'Ignore',
    CostControl: 'Ignore',
  });
  const group = template.resources.find(
    resource => resource.type === 'Microsoft.Resources/resourceGroups',
  );
  const module = template.resources.find(
    resource => resource.type === 'Microsoft.Resources/deployments',
  );
  assert.equal(group.tags, "[variables('resourceTags')]");
  assert.equal(module.properties.parameters.tags.value, group.tags);
  assert.match(template.variables.resourceTags, /parameters\('tags'\)/);
  assert.match(template.variables.resourceTags, /azd-env-name/);
  assert.equal(JSON.stringify(template).includes('SecurityControl'), false);
  const untaggableTypes = new Set([
    'Microsoft.Authorization/roleAssignments',
    'Microsoft.CognitiveServices/accounts/deployments',
    'Microsoft.CognitiveServices/accounts/projects/connections',
    'Microsoft.Insights/diagnosticSettings',
  ]);
  for (const resource of resources.filter(resource =>
    resource.type !== 'Microsoft.Resources/resourceGroups' && !untaggableTypes.has(resource.type))) {
    assert.equal(resource.tags, "[parameters('tags')]", resource.type);
  }
});

test('creates two Foundry accounts and projects, but only one source deployment', () => {
  const accounts = ofType('Microsoft.CognitiveServices/accounts');
  assert.equal(accounts.length, 2);
  for (const account of accounts) {
    assert.equal(account.kind, 'AIServices');
    assert.equal(account.properties.allowProjectManagement, true);
    assert.equal(account.properties.disableLocalAuth, true);
  }
  assert.equal(ofType('Microsoft.CognitiveServices/accounts/projects').length, 2);
  const deployments = ofType('Microsoft.CognitiveServices/accounts/deployments');
  assert.equal(deployments.length, 1);
  assert.match(deployments[0].name, /foundry-source-/);
  assert.equal(deployments[0].properties.model.name, 'gpt-5.4-nano');
  assert.equal(deployments[0].properties.model.version, '2026-03-17');
  assert.equal(deployments[0].sku.name, 'GlobalStandard');
  assert.equal(resources.some(resource => /agents/i.test(resource.type)), false);
  assert.equal(ofType('Microsoft.CognitiveServices/accounts/projects/connections').length, 4);
});

test('connects the consumer project to LiteLLM with static discovery and bearer authentication', () => {
  const connection = ofType('Microsoft.CognitiveServices/accounts/projects/connections').find(
    resource => resource.properties.category === 'ModelGateway',
  );
  assert.match(connection.name, /parameters\('consumerFoundryName'\)/);
  assert.match(connection.name, /litellm-gateway/);
  assert.equal(connection.properties.category, 'ModelGateway');
  assert.equal(connection.properties.authType, 'ApiKey');
  assert.equal(connection.properties.credentials.key, "[parameters('litellmMasterKey')]");
  assert.equal(connection.properties.isSharedToAll, false);
  assert.match(connection.properties.target, /parameters\('litellmEndpoint'\)/);
  assert.match(connection.properties.target, /\/v1/);
  assert.equal(connection.properties.metadata.deploymentInPath, 'false');
  assert.equal(connection.properties.metadata.inferenceAPIVersion, '');
  assert.equal(connection.properties.metadata.authConfig,
    "[string(createObject('type', 'api_key', 'name', 'Authorization', 'format', 'Bearer {api_key}'))]");
  assert.equal(connection.properties.metadata.models,
    "[string(createArray(createObject('name', parameters('modelDeploymentName'), 'properties', "
    + "createObject('model', createObject('name', 'gpt-5.4-nano', 'version', '', 'format', 'OpenAI')))))]");
  assert.equal(Object.hasOwn(connection.properties.metadata, 'modelDiscovery'), false);
  assert.match(moduleNamed('consumer-model-connection').properties.parameters.litellmEndpoint.value,
    /gateway-container-apps.*outputs.endpoint.value/);
  assert.match(moduleNamed('consumer-model-connection').properties.parameters.consumerProjectName.value,
    /consumer-foundry.*outputs.projectName.value/);
});

test('uses source-scoped managed identity access and secure gateway secrets', () => {
  assert.equal(template.parameters.litellmMasterKey.type.toLowerCase(), 'securestring');
  const roles = ofType('Microsoft.Authorization/roleAssignments');
  const identityRoles = roles.filter(role => role.properties.principalType === 'ServicePrincipal');
  assert.equal(identityRoles.length, 7);
  assert.equal(identityRoles.filter(role => /foundry-source-/.test(role.scope)).length, 1);
  const [app] = ofType('Microsoft.App/containerApps');
  assert.equal(app.identity.type, 'UserAssigned');
  assert.equal(app.properties.configuration.ingress.allowInsecure, false);
  assert.equal(app.properties.configuration.ingress.targetPort, 4000);
  const container = app.properties.template.containers[0];
  assert.equal(container.env.some(variable => variable.name === 'AZURE_API_KEY'), false);
  assert.equal(container.env.find(variable => variable.name === 'LITELLM_MASTER_KEY').secretRef,
    'litellm-master-key');
  assert.equal(app.properties.template.scale.maxReplicas, 1);
  assert.deepEqual(container.probes.map(probe => probe.type), [
    'Startup', 'Liveness', 'Readiness',
  ]);
  const configReference = app.properties.configuration.secrets.find(
    secret => secret.name === 'litellm-config',
  ).value;
  const variableName = /^\[variables\('([^']+)'\)\]$/.exec(configReference)?.[1];
  assert.ok(variableName, 'configuration must reference the compiled file variable');
  const config = moduleTemplate('gateway-container-apps').variables[variableName];
  assert.equal(config, readFileSync(new URL('../litellm/config.yaml', import.meta.url), 'utf8'));
  assert.match(config, /model: azure\/gpt-5\.4-nano/);
  assert.match(config, /enable_azure_ad_token_refresh: true/);
  assert.match(config, /master_key: os\.environ\/LITELLM_MASTER_KEY/);
  assert.equal(Object.keys(template.outputs).some(key => /master.?key|secret/i.test(key)), false);
});

test('stores the master key in RBAC-protected Key Vault and references it using managed identity', () => {
  const [vault] = ofType('Microsoft.KeyVault/vaults');
  assert.equal(vault.properties.enableRbacAuthorization, true);
  assert.equal(vault.properties.enablePurgeProtection, true);
  assert.equal(vault.properties.enableSoftDelete, true);
  assert.deepEqual(vault.properties.accessPolicies, []);
  const [secret] = ofType('Microsoft.KeyVault/vaults/secrets');
  assert.equal(secret.properties.value, "[parameters('litellmMasterKey')]");
  const access = ofType('Microsoft.Authorization/roleAssignments').find(
    role => role.scope?.includes('Microsoft.KeyVault/vaults')
      && role.properties.principalType === 'ServicePrincipal',
  );
  assert.ok(access);
  assert.match(access.properties.roleDefinitionId, /4633458b-17de-408a-b874-0445c86b69e6/);
  const [app] = ofType('Microsoft.App/containerApps');
  const reference = app.properties.configuration.secrets.find(
    secret => secret.name === 'litellm-master-key',
  );
  assert.equal(Object.hasOwn(reference, 'value'), false);
  assert.equal(reference.keyVaultUrl, "[parameters('masterKeySecretUri')]");
  assert.equal(reference.identity, "[parameters('gatewayIdentityId')]");
  assert.match(moduleNamed('gateway-container-apps').properties.parameters.masterKeySecretUri.value,
    /gateway-key-vault.*outputs.masterKeySecretUri.value/);
});

test('grants the deployer scoped resource management, vault administration, and Foundry data access', () => {
  const roles = ofType('Microsoft.Authorization/roleAssignments').filter(
    role => role.properties.principalType === 'User',
  );
  assert.equal(roles.length, 6);
  for (const role of roles) {
    assert.equal(role.properties.principalId, "[parameters('principalId')]");
  }
  const contributor = roles.find(role => /b24988ac-6180-42a0-ab88-20f7382dd24c/.test(
    role.properties.roleDefinitionId,
  ));
  assert.ok(contributor);
  assert.equal(contributor.scope, undefined);
  const officer = roles.find(role => /b86a8fe4-44ce-4948-aee5-eccb2c155cd7/.test(
    role.properties.roleDefinitionId,
  ));
  assert.match(officer.scope, /Microsoft.KeyVault\/vaults/);
  const aiRoles = roles.filter(role => /53ca6127-db72-4b80-b1b0-d745d6d5456d/.test(
    role.properties.roleDefinitionId,
  ));
  assert.equal(aiRoles.length, 2);
  assert.ok(aiRoles.some(role => /foundry-source-/.test(role.scope)));
  assert.ok(aiRoles.some(role => /foundry-consumer-/.test(role.scope)));
});

test('creates one shared Entra-only Application Insights component backed by the workspace', () => {
  const [insights] = ofType('Microsoft.Insights/components');
  assert.equal(ofType('Microsoft.Insights/components').length, 1);
  assert.equal(ofType('Microsoft.OperationalInsights/workspaces').length, 1);
  assert.equal(insights.properties.DisableLocalAuth, true);
  assert.equal(insights.properties.IngestionMode, 'LogAnalytics');
  assert.match(insights.properties.WorkspaceResourceId, /Microsoft.OperationalInsights\/workspaces/);
  assert.match(moduleNamed('gateway-monitoring').dependsOn.join(' '), /gateway-identity/);
  for (const name of ['source-foundry', 'consumer-foundry', 'gateway-key-vault', 'gateway-container-apps']) {
    assert.match(moduleNamed(name).dependsOn.join(' '), /gateway-monitoring/);
    assert.match(moduleNamed(name).properties.parameters.workspaceId.value, /gateway-monitoring.*outputs.workspaceId.value/);
  }
  assert.match(template.outputs.APPLICATIONINSIGHTS_CONNECTION_STRING.value, /gateway-monitoring.*outputs.connectionString.value/);
  assert.match(template.outputs.APPLICATIONINSIGHTS_RESOURCE_ID.value, /gateway-monitoring.*outputs.appInsightsId.value/);
});

test('grants explicit monitoring read access to the deployer and publish-only access to service identities', () => {
  const monitoring = moduleTemplate('gateway-monitoring');
  assert.deepEqual(monitoring.variables.readerRoles, [
    '73c42c96-874c-492b-b04d-ab87d138a893',
    'dbc9c667-e97f-4491-aee6-90b9cf960190',
  ]);
  const readerAssignments = monitoring.resources.filter(resource =>
    resource.type === 'Microsoft.Authorization/roleAssignments'
      && resource.properties.principalType === 'User');
  assert.equal(readerAssignments.length, 2);
  assert.ok(readerAssignments.some(role => /Microsoft.Insights\/components/.test(role.scope)));
  assert.ok(readerAssignments.some(role => /Microsoft.OperationalInsights\/workspaces/.test(role.scope)));
  for (const role of readerAssignments) {
    assert.equal(role.copy.count, "[length(variables('readerRoles'))]");
    assert.match(role.properties.roleDefinitionId, /variables\('readerRoles'\)/);
    assert.equal(role.properties.principalId, "[parameters('principalId')]");
  }
  const publishers = ofType('Microsoft.Authorization/roleAssignments').filter(role =>
    /3913510d-42f4-4e42-8a64-420c390055eb/.test(role.properties.roleDefinitionId));
  assert.equal(publishers.length, 3);
  for (const publisher of publishers) {
    assert.equal(publisher.properties.principalType, 'ServicePrincipal');
    assert.match(publisher.scope, /Microsoft.Insights\/components/);
  }
  assert.ok(publishers.some(role => role.properties.principalId === "[parameters('gatewayPrincipalId')]"));
  assert.equal(publishers.filter(role => /Microsoft.CognitiveServices\/accounts\/projects.*identity.principalId/.test(
    role.properties.principalId,
  )).length, 2);
});

test('links both Foundry projects to the shared Application Insights through project managed identity', () => {
  const connections = ofType('Microsoft.CognitiveServices/accounts/projects/connections').filter(
    resource => resource.properties.category === 'AppInsights',
  );
  assert.equal(connections.length, 2);
  for (const connection of connections) {
    assert.equal(connection.properties.authType, 'ProjectManagedIdentity');
    assert.equal(connection.properties.credentials, undefined);
    assert.equal(connection.properties.metadata.ApiType, 'Azure');
    assert.equal(connection.properties.metadata.ResourceId, connection.properties.target);
    assert.match(connection.properties.metadata.ApplicationInsightsConnectionString,
      /Microsoft.Insights\/components.*ConnectionString/);
    assert.match(connection.properties.target, /Microsoft.Insights\/components/);
    assert.match(connection.dependsOn.join(' '), /Microsoft.Authorization\/roleAssignments/);
  }
});

test('orders both component-scoped Insights grants before each project connection without renaming Publisher', () => {
  for (const name of ['source-foundry', 'consumer-foundry']) {
    const compiled = moduleTemplate(name);
    const roles = compiled.resources.filter(resource => resource.type === 'Microsoft.Authorization/roleAssignments');
    const reader = roles.find(role => role.properties.roleDefinitionId.includes('43d0d8ad-25c7-4714-9337-8ba259a9fe05'));
    const publisher = roles.find(role => role.properties.roleDefinitionId.includes('3913510d-42f4-4e42-8a64-420c390055eb'));
    assert.ok(reader);
    assert.equal(reader.scope, publisher.scope);
    assert.match(reader.scope, /Microsoft.Insights\/components/);
    assert.equal(reader.properties.principalId, publisher.properties.principalId);
    assert.match(reader.properties.principalId, /Microsoft.CognitiveServices\/accounts\/projects.*identity.principalId/);
    assert.equal(reader.properties.principalType, 'ServicePrincipal');
    assert.match(publisher.name, /Monitoring Metrics Publisher/);
    assert.match(publisher.name, /Microsoft.CognitiveServices\/accounts\/projects/);
    const connection = compiled.resources.find(resource => resource.properties?.category === 'AppInsights');
    assert.ok(connection.dependsOn.some(value => value.includes('Monitoring Reader')));
    assert.ok(connection.dependsOn.some(value => value.includes('Monitoring Metrics Publisher')));
  }
});

test('keeps directory deployment separate and mounts the gateway wrapper and pinned dependency manifest', () => {
  assert.equal(JSON.stringify(template).includes('Microsoft.Graph/'), false);
  assert.equal(template.parameters.gatewayAuthMode.defaultValue, 'api_key');
  assert.equal(template.parameters.hostedAuthMode.defaultValue, 'api_key');
  assert.equal(template.parameters.promptAuthMode.defaultValue, 'api_key');
  assert.match(moduleNamed('consumer-model-connection').condition, /promptAuthMode.*api_key/);
  const [gateway] = ofType('Microsoft.App/containerApps')[0].properties.template.containers;
  assert.equal(gateway.command, "[if(equals(parameters('gatewayAuthMode'), 'entra'), createArray('python'), null())]");
  assert.equal(gateway.args, "[if(equals(parameters('gatewayAuthMode'), 'entra'), createArray('/etc/litellm/serve.py'), createArray('--config', '/etc/litellm/config.yaml', '--host', '0.0.0.0', '--port', '4000'))]");
  assert.equal(moduleTemplate('gateway-container-apps').parameters.gatewayAuthMode.defaultValue, 'api_key');
  const mounts = ofType('Microsoft.App/containerApps')[0].properties.template.volumes[0].secrets;
  for (const path of ['config.yaml', 'config.entra.yaml', 'entra_auth.py', 'gateway_app.py', 'serve.py', 'credential_context.py', 'requirements.txt']) {
    assert.ok(mounts.some(item => item.path === path), path);
  }
  for (const name of ['LITELLM_AUTH_MODE', 'ENTRA_TENANT_ID', 'ENTRA_GATEWAY_CLIENT_ID',
    'ENTRA_APP_PRINCIPALS', 'ENTRA_DELEGATED_USERS', 'ENTRA_DELEGATED_ACTORS', 'ENTRA_DELEGATED_ONLY']) {
    assert.ok(gateway.env.some(item => item.name === name), name);
  }
  for (const [name, parameter] of [
    ['ENTRA_APP_PRINCIPALS', 'entraAppPrincipals'], ['ENTRA_DELEGATED_USERS', 'entraDelegatedUsers'],
    ['ENTRA_DELEGATED_ACTORS', 'entraDelegatedActors'],
  ]) {
    assert.equal(gateway.env.find(item => item.name === name).value, `[string(parameters('${parameter}'))]`);
  }
  // Bicep's string(false) is "False", which the gateway's strict policy parser rejects.
  assert.equal(
    gateway.env.find(item => item.name === 'ENTRA_DELEGATED_ONLY').value,
    "[if(parameters('entraDelegatedOnly'), 'true', 'false')]",
  );
  assert.doesNotMatch(JSON.stringify(gateway), /pip install/);
});

test('supports ModelGateway OAuth2 only with an explicit secure credential, never project managed identity', () => {
  const compiled = moduleTemplate('consumer-model-connection');
  const oauth = compiled.resources.find(resource => resource.properties?.authType === 'OAuth2');
  assert.ok(oauth);
  assert.equal(oauth.properties.credentials.clientSecret, "[parameters('promptClientSecret')]");
  assert.equal(compiled.parameters.promptClientSecret.type.toLowerCase(), 'securestring');
  assert.equal(oauth.properties.scopes[0], "[format('api://{0}/.default', parameters('entraGatewayClientId'))]");
  assert.match(oauth.properties.tokenUrl, /oauth2\/v2\.0\/token.*entraTenantId/);
  assert.equal(oauth.properties.category, 'ModelGateway');
  assert.equal(oauth.properties.credentials.key, undefined);
  assert.equal(oauth.properties.useWorkspaceManagedIdentity, undefined);
  assert.equal(JSON.stringify(template.outputs).includes('promptClientSecret'), false);
});

test('routes resource diagnostics to the shared workspace without collecting model request bodies', () => {
  const diagnostics = ofType('Microsoft.Insights/diagnosticSettings');
  assert.equal(diagnostics.length, 7);
  for (const diagnostic of diagnostics) {
    assert.equal(diagnostic.properties.workspaceId, "[parameters('workspaceId')]");
    assert.deepEqual(diagnostic.properties.metrics, [{ category: 'AllMetrics', enabled: true }]);
    assert.equal(diagnostic.properties.logs?.some(log => log.category === 'RequestResponse') ?? false, false);
  }
  assert.equal(diagnostics.filter(resource => /accounts\/projects/.test(resource.scope)).length, 2);
  assert.equal(diagnostics.filter(resource => /Microsoft.KeyVault\/vaults/.test(resource.scope)).length, 1);
  const [environment] = ofType('Microsoft.App/managedEnvironments');
  assert.deepEqual(environment.properties.appLogsConfiguration, { destination: 'azure-monitor' });
  assert.equal(JSON.stringify(moduleTemplate('gateway-container-apps')).includes('listKeys'), false);
});

test('exports LiteLLM traces through a loopback-only collector with managed identity and no prompt capture', () => {
  const [app] = ofType('Microsoft.App/containerApps');
  const [gateway, collector] = app.properties.template.containers;
  assert.equal(collector.name, 'otel-collector');
  assert.equal(collector.image, 'otel/opentelemetry-collector-contrib:0.162.0');
  assert.equal(collector.env.find(value => value.name === 'AZURE_CLIENT_ID').value, "[parameters('gatewayClientId')]");
  assert.equal(collector.env.find(value => value.name === 'APPLICATIONINSIGHTS_CONNECTION_STRING').value,
    "[parameters('appInsightsConnectionString')]");
  assert.equal(gateway.env.find(value => value.name === 'OTEL_ENDPOINT').value, 'http://127.0.0.1:4318/v1/traces');
  assert.equal(gateway.env.find(value => value.name === 'OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT').value, 'NO_CONTENT');
  const config = readFileSync(new URL('../monitoring/otel-collector.yaml', import.meta.url), 'utf8');
  assert.match(config, /managed_identity:\n\s+client_id: \$\{env:AZURE_CLIENT_ID\}/);
  assert.match(config, /https:\/\/monitor\.azure\.com\/\.default/);
  assert.match(config, /endpoint: 127\.0\.0\.1:4318/);
  assert.match(config, /authenticator: azure_auth/);
  assert.match(config, /connection_string: \$\{env:APPLICATIONINSIGHTS_CONNECTION_STRING\}/);
  const proxyConfig = readFileSync(new URL('../litellm/config.yaml', import.meta.url), 'utf8');
  assert.match(proxyConfig, /callbacks: \["otel"\]/);
  assert.match(proxyConfig, /turn_off_message_logging: true/);
});

test('serializes source project creation after model deployment to avoid account conflicts', () => {
  const sourceProject = ofType('Microsoft.CognitiveServices/accounts/projects').find(
    project => /foundry-source-/.test(project.name),
  );
  assert.ok(sourceProject);
  assert.ok(sourceProject.dependsOn.some(
    dependency => dependency.includes('Microsoft.CognitiveServices/accounts/deployments'),
  ));
  const [deployment] = ofType('Microsoft.CognitiveServices/accounts/deployments');
  assert.equal(deployment.dependsOn.some(
    dependency => dependency.includes('Microsoft.CognitiveServices/accounts/projects'),
  ), false);
});

const demoModule = allModules.find(module => module.name === 'demo-backends');
const demoTemplate = demoModule.properties.template;
const demoOfType = type => demoTemplate.resources.filter(resource => resource.type === type);
const gatewayModule = moduleTemplate('gateway-container-apps');
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');

test('demo backends are off by default and add only conditional modules and guarded outputs', () => {
  for (const name of ['enableDemoBackends', 'demoMcpExists', 'demoA2aExists']) {
    assert.equal(template.parameters[name].type, 'bool', name);
    assert.equal(template.parameters[name].defaultValue, false, name);
  }
  assert.deepEqual(template.parameters.entraCapabilities.defaultValue, {});
  assert.equal(gatewayModule.parameters.enableDemoBackends.defaultValue, false);
  assert.deepEqual(gatewayModule.parameters.entraCapabilities.defaultValue, {});
  assert.equal(demoModule.condition, "[parameters('enableDemoBackends')]");
  assert.equal(allModules.find(module => module.name === 'demo-mcp-image').condition, "[variables('keepMcpImage')]");
  assert.equal(allModules.find(module => module.name === 'demo-a2a-image').condition, "[variables('keepA2aImage')]");
  assert.equal(template.variables.keepMcpImage, "[and(parameters('enableDemoBackends'), parameters('demoMcpExists'))]");
  assert.equal(template.variables.keepA2aImage, "[and(parameters('enableDemoBackends'), parameters('demoA2aExists'))]");
  for (const output of ['AZURE_CONTAINER_REGISTRY_ENDPOINT', 'AZURE_CONTAINER_REGISTRY_NAME']) {
    assert.match(template.outputs[output].value,
      /^\[if\(parameters\('enableDemoBackends'\), reference\(.*'demo-backends'.*\)\.outputs\.registry(Endpoint|Name)\.value, ''\)\]$/, output);
  }
  assert.equal(demoModule.properties.parameters.environmentId.value.includes("outputs.environmentId.value"), true);
  assert.equal(moduleTemplate('gateway-container-apps').outputs.environmentId.type, 'string');
  const gatewayParameters = moduleNamed('gateway-container-apps').properties.parameters;
  assert.equal(gatewayParameters.enableDemoBackends.value, "[parameters('enableDemoBackends')]");
  assert.equal(gatewayParameters.entraCapabilities.value, "[parameters('entraCapabilities')]");
  assert.equal(ofType('Microsoft.ContainerRegistry/registries').length, 0);
});

test('plumbs azd environment values for capabilities, demo switch and image preservation', () => {
  const { parameters } = JSON.parse(read('../infra/main.parameters.json'));
  // Objects take azd's complex-type path (same as the proven arrays); unset omits it so the Bicep default {} applies.
  assert.equal(parameters.entraCapabilities.value, '${ENTRA_CAPABILITIES}');
  assert.equal(parameters.entraAppPrincipals.value, '${ENTRA_APP_PRINCIPALS=[]}');
  assert.equal(parameters.enableDemoBackends.value, '${ENABLE_DEMO_BACKENDS=false}');
  assert.equal(parameters.entraDelegatedOnly.value, '${ENTRA_DELEGATED_ONLY=false}');
  assert.equal(parameters.demoMcpExists.value, '${SERVICE_DEMO_MCP_RESOURCE_EXISTS=false}');
  assert.equal(parameters.demoA2aExists.value, '${SERVICE_DEMO_A2A_RESOURCE_EXISTS=false}');
  assert.equal(template.parameters.entraCapabilities.type, 'object');
  const [gateway] = ofType('Microsoft.App/containerApps')[0].properties.template.containers;
  assert.equal(gateway.env.find(item => item.name === 'ENTRA_CAPABILITIES').value,
    "[string(parameters('entraCapabilities'))]");
});

test('appends the demo overlay to the Entra gateway config only when enabled', () => {
  const base = read('../litellm/config.entra.yaml');
  const overlay = read('../litellm/config.demo.yaml');
  const variables = gatewayModule.variables;
  assert.equal(variables.entraBaseConfig, base);
  assert.equal(variables.demoConfigOverlay, overlay);
  assert.equal(variables.entraConfig,
    "[if(parameters('enableDemoBackends'), format('{0}\n{1}', variables('entraBaseConfig'), variables('demoConfigOverlay')), variables('entraBaseConfig'))]");
  const secret = ofType('Microsoft.App/containerApps')[0].properties.configuration.secrets
    .find(item => item.name === 'litellm-entra-config');
  assert.equal(secret.value, "[variables('entraConfig')]");
  assert.doesNotMatch(base, /^(mcp_servers|agents):/m);
  assert.match(overlay, /^mcp_servers:/m);
  assert.match(overlay, /^agents:/m);
  assert.match(overlay, /http:\/\/demo-mcp\//);
  assert.match(overlay, /http:\/\/demo-a2a\//);
});

test('mounts the gateway policy module like the other gateway Python files', () => {
  const app = ofType('Microsoft.App/containerApps')[0];
  const secret = app.properties.configuration.secrets.find(item => item.name === 'gateway-policy');
  const variable = /^\[variables\('([^']+)'\)\]$/.exec(secret.value)?.[1];
  assert.equal(gatewayModule.variables[variable], read('../litellm/gateway_policy.py'));
  const mount = app.properties.template.volumes[0].secrets.find(item => item.path === 'gateway_policy.py');
  assert.equal(mount.secretRef, 'gateway-policy');
});

test('creates a registry without admin credentials and AcrPull only on that registry for a dedicated identity', () => {
  const [registry] = demoOfType('Microsoft.ContainerRegistry/registries');
  assert.equal(demoOfType('Microsoft.ContainerRegistry/registries').length, 1);
  assert.equal(registry.sku.name, 'Basic');
  assert.equal(registry.properties.adminUserEnabled, false);
  assert.equal(registry.tags, "[parameters('tags')]");
  assert.match(registry.name, /^\[format\('crdemo\{0\}', parameters\('suffix'\)\)\]$/);
  const [identity] = demoOfType('Microsoft.ManagedIdentity/userAssignedIdentities');
  assert.equal(identity.tags, "[parameters('tags')]");
  const [assignment, ...others] = demoOfType('Microsoft.Authorization/roleAssignments');
  assert.equal(others.length, 0);
  assert.equal(assignment.scope, "[resourceId('Microsoft.ContainerRegistry/registries', format('crdemo{0}', parameters('suffix')))]");
  assert.match(assignment.name, /^\[guid\(resourceId\('Microsoft.ContainerRegistry\/registries'.*resourceId\('Microsoft.ManagedIdentity\/userAssignedIdentities'.*variables\('acrPullRoleId'\)\)\]$/);
  assert.equal(demoTemplate.variables.acrPullRoleId, '7f951dda-4ed3-4680-a7ca-43fe172d538d');
  assert.match(assignment.properties.roleDefinitionId, /roleDefinitions.*acrPullRoleId/);
  assert.equal(assignment.properties.principalType, 'ServicePrincipal');
  assert.match(assignment.properties.principalId, /userAssignedIdentities.*principalId/);
  assert.equal(JSON.stringify(demoTemplate).includes('listCredentials'), false);
  assert.equal(JSON.stringify(demoTemplate).includes('passwords'), false);
});

test('runs demo-mcp and demo-a2a as internal-only azd-tagged Container Apps in the shared environment', () => {
  const [app, ...rest] = demoOfType('Microsoft.App/containerApps');
  assert.equal(rest.length, 0);
  assert.equal(app.copy.count, "[length(variables('apps'))]");
  const apps = demoTemplate.variables.apps;
  assert.deepEqual(apps.map(item => item.name), ['demo-mcp', 'demo-a2a']);
  assert.deepEqual(apps[0].env, []);
  assert.deepEqual(apps[1].env.map(item => item.name), ['PUBLIC_URL', 'QUIZ_SIGNING_KEY', 'QUIZ_MODEL_MODE',
    'QUIZ_GATEWAY_BASE_URL', 'QUIZ_GATEWAY_AUDIENCE', 'AZURE_CLIENT_ID', 'QUIZ_MODEL']);
  assert.equal(app.name, "[variables('apps')[copyIndex()].name]");
  assert.equal(app.tags,
    "[union(parameters('tags'), createObject('azd-service-name', variables('apps')[copyIndex()].name))]");
  assert.equal(app.properties.managedEnvironmentId, "[parameters('environmentId')]");
  const { ingress, registries, activeRevisionsMode } = app.properties.configuration;
  assert.deepEqual(ingress, { external: false, allowInsecure: false, targetPort: 8080, transport: 'http' });
  assert.equal(activeRevisionsMode, 'Single');
  assert.equal(registries.length, 1);
  assert.match(registries[0].server, /registries.*loginServer/);
  assert.match(registries[0].identity, /userAssignedIdentities.*id-demo-/);
  // Only demo-a2a also gets the quiz identity; asserted in the quiz tests below.
  assert.match(app.identity.userAssignedIdentities, /^\[if\(variables\('apps'\)\[copyIndex\(\)\]\.quizIdentity, createObject\(/);
  assert.ok(app.dependsOn.some(dependency => /roleAssignments/.test(dependency)));
  const [container, ...extra] = app.properties.template.containers;
  assert.equal(extra.length, 0);
  assert.equal(container.resources.cpu, '[json(\'0.25\')]');
  assert.equal(container.resources.memory, '0.5Gi');
  assert.equal(container.image, "[variables('apps')[copyIndex()].image]");
  assert.equal(container.env, "[variables('apps')[copyIndex()].env]");
  assert.deepEqual(app.properties.template.scale, { minReplicas: 1, maxReplicas: 1 });
  assert.deepEqual(container.probes.map(probe => probe.type), ['Startup', 'Liveness', 'Readiness']);
  for (const probe of container.probes) {
    assert.deepEqual([probe.httpGet.path, probe.httpGet.port, probe.httpGet.scheme], ['/healthz', 8080, 'HTTP']);
  }
  assert.equal(JSON.stringify(app).includes('external":true'), false);
  assert.deepEqual(app.properties.configuration.secrets,
    "[if(variables('apps')[copyIndex()].secrets, createArray(createObject('name', 'quiz-signing-key', 'value', parameters('quizSigningKey'))), createArray())]");
});

test('keeps the azd-deployed image on reprovision and uses a placeholder that passes the probes on first provision', () => {
  assert.equal(demoTemplate.variables.placeholderImage, 'mcr.microsoft.com/dotnet/samples:aspnetapp');
  assert.equal(demoTemplate.variables.apps[0].image,
    "[if(empty(parameters('demoMcpImage')), variables('placeholderImage'), parameters('demoMcpImage'))]");
  assert.equal(demoTemplate.variables.apps[1].image,
    "[if(empty(parameters('demoA2aImage')), variables('placeholderImage'), parameters('demoA2aImage'))]");
  assert.match(demoModule.properties.parameters.demoMcpImage,
    /^\[if\(variables\('keepMcpImage'\), createObject\('value', reference\(.*'demo-mcp-image'.*\.outputs\.image\.value\), createObject\('value', ''\)\)\]$/);
  assert.match(demoModule.properties.parameters.demoA2aImage,
    /^\[if\(variables\('keepA2aImage'\), createObject\('value', reference\(.*'demo-a2a-image'.*\.outputs\.image\.value\), createObject\('value', ''\)\)\]$/);
  const fetched = allModules.find(module => module.name === 'demo-mcp-image');
  assert.equal(fetched.properties.parameters.name.value, 'demo-mcp');
  assert.equal(allModules.find(module => module.name === 'demo-a2a-image').properties.parameters.name.value, 'demo-a2a');
  assert.deepEqual(fetched.properties.template.resources, []);
  assert.equal(fetched.properties.template.outputs.image.value,
    "[reference(resourceId('Microsoft.App/containerApps', parameters('name')), '2025-01-01').template.containers[0].image]");
});

test('declares optional azd services guarded by ENABLE_DEMO_BACKENDS', () => {
  const azure = read('../azure.yaml');
  for (const [name, project] of [['demo-mcp', 'demo/mcp-server'], ['demo-a2a', 'demo/a2a-agent']]) {
    const block = new RegExp(`\\n  ${name}:\\n((?:    .*\\n|\\n)+?)(?=  \\S|\\S)`).exec(azure)?.[1];
    assert.ok(block, name);
    for (const line of ['host: containerapp', `project: ${project}`, 'language: python',
      'condition: ${ENABLE_DEMO_BACKENDS=false}', 'docker:', '      remoteBuild: true']) {
      assert.ok(block.includes(line), `${name} ${line}`);
    }
  }
  assert.match(azure, /GATEWAY_TOOLS: \$\{GATEWAY_TOOLS=disabled\}/);
});

const quizModule = allModules.find(module => module.name === 'demo-quiz-identity');
const mainModule = name => allModules.find(module => module.name === name);
const dependenciesOf = name => mainModule(name).dependsOn.join(' ');

test('quiz signing key is a secure, generated secret used only as a demo-a2a secretRef and never output', () => {
  const key = demoTemplate.parameters.quizSigningKey;
  assert.equal(key.type.toLowerCase(), 'securestring');
  assert.ok(key.minLength >= 32);
  assert.equal(key.defaultValue, "[format('{0}{1}', newGuid(), newGuid())]");
  // The only consumer is the secrets list; the main template never supplies or outputs it.
  assert.equal(JSON.stringify(demoTemplate).split("parameters('quizSigningKey')").length - 1, 1);
  assert.equal('quizSigningKey' in demoModule.properties.parameters, false);
  assert.equal(JSON.stringify(template).includes('quizSigningKey'), true); // only inside the module template
  assert.equal(JSON.stringify(template.outputs).includes('quiz-signing-key'), false);
  assert.equal(JSON.stringify(template.outputs).toLowerCase().includes('signing'), false);
  assert.deepEqual(Object.keys(demoTemplate.outputs), ['registryName', 'registryEndpoint']);
  const [mcp, a2a] = demoTemplate.variables.apps;
  assert.equal(mcp.secrets, false);
  assert.equal(mcp.env.some(item => item.secretRef), false);
  assert.equal(a2a.secrets, true);
  assert.deepEqual(a2a.env.filter(item => item.secretRef), [{ name: 'QUIZ_SIGNING_KEY', secretRef: 'quiz-signing-key' }]);
  assert.equal(JSON.stringify(a2a.env).includes('quizSigningKey'), false);
  assert.equal(ofType('Microsoft.App/containerApps').length, 1); // gateway only; demo apps are in the conditional module
  // The gateway config legitimately names the quiz_master agent; it must never see the signing key.
  for (const name of ['quizSigningKey', 'QUIZ_SIGNING_KEY', 'quiz-signing-key']) {
    assert.equal(JSON.stringify(gatewayModule).includes(name), false, name);
  }
});

test('creates a separate quiz identity before the gateway and attaches it only to demo-a2a', () => {
  assert.equal(quizModule.condition, "[parameters('enableDemoBackends')]");
  assert.equal(quizModule.properties.template.resources.length, 1);
  const [identity] = quizModule.properties.template.resources;
  assert.equal(identity.type, 'Microsoft.ManagedIdentity/userAssignedIdentities');
  assert.equal(identity.name, "[format('id-quiz-{0}', parameters('suffix'))]");
  assert.equal(identity.tags, "[parameters('tags')]");
  assert.equal(quizModule.properties.parameters.suffix.value, "[variables('suffix')]");
  assert.deepEqual(Object.keys(quizModule.properties.template.outputs), ['id', 'principalId', 'clientId']);
  // No role assignments in the identity module: it needs no Azure RBAC.
  assert.equal(quizModule.properties.template.resources.some(item => item.type.includes('roleAssignments')), false);
  // Ordering: the identity precedes both consumers and does not depend on either (no cycle).
  assert.ok(dependenciesOf('gateway-container-apps').includes('demo-quiz-identity'));
  assert.ok(dependenciesOf('demo-backends').includes('demo-quiz-identity'));
  assert.ok(dependenciesOf('demo-backends').includes('gateway-container-apps'));
  assert.equal(dependenciesOf('gateway-container-apps').includes('demo-backends'), false);
  assert.equal(dependenciesOf('demo-quiz-identity').includes('gateway-container-apps'), false);
  assert.equal(dependenciesOf('demo-quiz-identity').includes('demo-backends'), false);
  const [shared] = demoOfType('Microsoft.ManagedIdentity/userAssignedIdentities');
  assert.match(shared.name, /id-demo-/);
  assert.equal(demoOfType('Microsoft.ManagedIdentity/userAssignedIdentities').length, 1);
  const [app] = demoOfType('Microsoft.App/containerApps');
  assert.equal(app.identity.userAssignedIdentities,
    "[if(variables('apps')[copyIndex()].quizIdentity, createObject(format('{0}', resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', format('id-demo-{0}', parameters('suffix')))), createObject(), format('{0}', parameters('quizIdentityId')), createObject()), createObject(format('{0}', resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', format('id-demo-{0}', parameters('suffix')))), createObject()))]");
  assert.deepEqual(demoTemplate.variables.apps.map(item => item.quizIdentity), [false, true]);
  // Registry pull still uses the shared identity only, and AcrPull stays on the registry for that identity.
  assert.match(app.properties.configuration.registries[0].identity, /id-demo-/);
  assert.equal(JSON.stringify(app.properties.configuration.registries).includes('quizIdentityId'), false);
  const assignments = demoOfType('Microsoft.Authorization/roleAssignments');
  assert.equal(assignments.length, 1);
  assert.match(assignments[0].properties.principalId, /id-demo-/);
  assert.equal(JSON.stringify(assignments).includes('quiz'), false);
});

test('approves the quiz identity at the gateway only when demo backends are enabled', () => {
  const principals = mainModule('gateway-container-apps').properties.parameters.entraAppPrincipals;
  const fetch = output => `reference(extensionResourceId(format('/subscriptions/{0}/resourceGroups/{1}', subscription().subscriptionId, format('rg-{0}', parameters('environmentName'))), 'Microsoft.Resources/deployments', 'demo-quiz-identity'), '2025-04-01').outputs.${output}.value`;
  assert.equal(principals,
    `[if(parameters('enableDemoBackends'), createObject('value', union(parameters('entraAppPrincipals'), createArray(createObject('oid', ${fetch('principalId')}, 'azp', ${fetch('clientId')})))), createObject('value', parameters('entraAppPrincipals')))]`);
  // The gateway still serialises exactly the supplied array.
  const [gateway] = ofType('Microsoft.App/containerApps')[0].properties.template.containers;
  assert.equal(gateway.env.find(item => item.name === 'ENTRA_APP_PRINCIPALS').value,
    "[string(parameters('entraAppPrincipals'))]");
  assert.deepEqual(gatewayModule.parameters.entraAppPrincipals.defaultValue, []);
  assert.equal(gatewayModule.parameters.entraAppPrincipals.type, 'array');
});

test('passes the quiz agent its gateway settings, falling back to model-off without an Entra audience', () => {
  const params = demoModule.properties.parameters;
  assert.equal(params.gatewayEndpoint.value.includes("'gateway-container-apps'"), true);
  assert.equal(params.gatewayEndpoint.value.endsWith('.outputs.endpoint.value]'), true);
  assert.equal(params.gatewayAudience,
    "[if(equals(parameters('gatewayAuthMode'), 'entra'), createObject('value', parameters('entraGatewayClientId')), createObject('value', ''))]");
  assert.match(params.quizIdentityId.value, /'demo-quiz-identity'.*outputs\.id\.value\]$/);
  assert.match(params.quizIdentityClientId.value, /'demo-quiz-identity'.*outputs\.clientId\.value\]$/);
  assert.equal(demoTemplate.parameters.gatewayAudience.defaultValue, '');
  const env = Object.fromEntries(demoTemplate.variables.apps[1].env.map(item => [item.name, item]));
  assert.deepEqual(env.PUBLIC_URL, { name: 'PUBLIC_URL', value: 'http://demo-a2a/' });
  assert.equal(env.QUIZ_MODEL_MODE.value, "[if(empty(parameters('gatewayAudience')), 'off', 'gateway')]");
  assert.equal(env.QUIZ_GATEWAY_BASE_URL.value, "[format('{0}/v1', parameters('gatewayEndpoint'))]");
  assert.equal(env.QUIZ_GATEWAY_AUDIENCE.value, "[parameters('gatewayAudience')]");
  assert.equal(env.AZURE_CLIENT_ID.value, "[parameters('quizIdentityClientId')]");
  assert.deepEqual(env.QUIZ_MODEL, { name: 'QUIZ_MODEL', value: 'gpt-5.4-nano' });
  assert.deepEqual(demoTemplate.variables.apps[0].env, []);
});

test('exposes only nonsecret quiz identity ids as azd outputs, empty when disabled', () => {
  for (const [name, output] of [['DEMO_QUIZ_IDENTITY_PRINCIPAL_ID', 'principalId'], ['DEMO_QUIZ_IDENTITY_CLIENT_ID', 'clientId']]) {
    assert.equal(template.outputs[name].type, 'string');
    assert.match(template.outputs[name].value,
      new RegExp(`^\\[if\\(parameters\\('enableDemoBackends'\\), reference\\(.*'demo-quiz-identity'.*\\)\\.outputs\\.${output}\\.value, ''\\)\\]$`), name);
  }
});
