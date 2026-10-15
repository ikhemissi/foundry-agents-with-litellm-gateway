import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const root = fileURLToPath(new URL('../', import.meta.url));
const template = JSON.parse(execFileSync('az', [
  'bicep', 'build', '--file', 'infra/main.bicep', '--stdout',
], { cwd: root, encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 }));
const modules = template.resources.filter(
  resource => resource.type === 'Microsoft.Resources/deployments',
);
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
  assert.equal(ofType('Microsoft.CognitiveServices/accounts/projects/connections').length, 3);
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
  assert.equal(identityRoles.length, 5);
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
