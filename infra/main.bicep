targetScope = 'subscription'

@description('Name of the azd environment.')
@minLength(1)
@maxLength(50)
param environmentName string

@description('Object ID of the deploying user, supplied by azd.')
param principalId string

@description('Deployment region: East US (default), UK South, or Sweden Central.')
@allowed([
  'eastus'
  'uksouth'
  'swedencentral'
])
param location string = 'eastus'

@secure()
@minLength(32)
@description('LiteLLM bearer key. Use sk- followed by at least 32 random bytes encoded as hex.')
param litellmMasterKey string

@description('GPT-5.4-nano GlobalStandard capacity in thousands of tokens per minute; subject to subscription quota.')
@minValue(1)
param modelCapacity int = 10

@description('Pinned LiteLLM image. Change deliberately when upgrading.')
param litellmImage string = 'ghcr.io/berriai/litellm:v1.105.1'

@allowed(['api_key', 'entra'])
param gatewayAuthMode string = 'api_key'
@allowed(['api_key', 'entra', 'entra_delegated'])
param hostedAuthMode string = 'api_key'
@allowed(['api_key', 'oauth2'])
param promptAuthMode string = 'api_key'
param entraTenantId string = ''
param entraGatewayClientId string = ''
param entraAppPrincipals array = []
param entraDelegatedUsers array = []
param entraDelegatedActors array = []
param entraDelegatedOnly bool = false
@description('Actual hosted Agent ID application ID, not the ACA/project managed identity.')
param entraAgentClientId string = ''
@description('Actual hosted Agent ID service principal object ID; obtain and approve after first agent deployment.')
param entraAgentPrincipalId string = ''
param promptClientId string = ''
@description('Principal-keyed MCP tool and A2A agent grants as JSON: {"<oid>": {"mcp": {"<server>": ["<tool>"]}, "agents": ["<agent>"]}}.')
param entraCapabilities object = {}
@description('Deploy the optional internal demo MCP and A2A backends (Container Apps plus a registry).')
param enableDemoBackends bool = false
@description('Set by azd (SERVICE_DEMO_MCP_RESOURCE_EXISTS) so reprovisioning keeps the deployed image.')
param demoMcpExists bool = false
@description('Set by azd (SERVICE_DEMO_A2A_RESOURCE_EXISTS) so reprovisioning keeps the deployed image.')
param demoA2aExists bool = false

@description('Additional tags applied to every resource type that supports tags.')
param tags object = {}

var resourceTags = union(tags, {
  'azd-env-name': environmentName
})

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: resourceTags
}

var suffix = uniqueString(subscription().id, resourceGroup.id)

module resourceGroupAccess './modules/resource-group-access.bicep' = {
  name: 'resource-group-access'
  scope: resourceGroup
  params: {
    tags: resourceTags
    principalId: principalId
  }
}

module identity './modules/identity.bicep' = {
  name: 'gateway-identity'
  scope: resourceGroup
  params: {
    location: location
    tags: resourceTags
    suffix: suffix
  }
}

module sourceFoundry './modules/source-foundry.bicep' = {
  name: 'source-foundry'
  scope: resourceGroup
  params: {
    location: location
    tags: resourceTags
    suffix: suffix
    principalId: principalId
    gatewayIdentityId: identity.outputs.id
    gatewayPrincipalId: identity.outputs.principalId
    modelCapacity: modelCapacity
    appInsightsName: monitoring.outputs.appInsightsName
    workspaceId: monitoring.outputs.workspaceId
  }
}

module consumerFoundry './modules/consumer-foundry.bicep' = {
  name: 'consumer-foundry'
  scope: resourceGroup
  params: {
    location: location
    tags: resourceTags
    suffix: suffix
    principalId: principalId
    appInsightsName: monitoring.outputs.appInsightsName
    workspaceId: monitoring.outputs.workspaceId
  }
}

module keyVault './modules/key-vault.bicep' = {
  name: 'gateway-key-vault'
  scope: resourceGroup
  params: {
    location: location
    tags: resourceTags
    suffix: suffix
    principalId: principalId
    gatewayIdentityId: identity.outputs.id
    gatewayPrincipalId: identity.outputs.principalId
    litellmMasterKey: litellmMasterKey
    workspaceId: monitoring.outputs.workspaceId
  }
}

module monitoring './modules/monitoring.bicep' = {
  name: 'gateway-monitoring'
  scope: resourceGroup
  params: {
    location: location
    tags: resourceTags
    suffix: suffix
    principalId: principalId
    gatewayPrincipalId: identity.outputs.principalId
  }
}

// Created before the gateway app so the gateway can approve it without depending on demo-backends,
// which itself depends on the gateway's Container Apps environment.
module demoQuizIdentity './modules/demo-quiz-identity.bicep' = if (enableDemoBackends) {
  name: 'demo-quiz-identity'
  scope: resourceGroup
  params: {
    location: location
    tags: resourceTags
    suffix: suffix
  }
}

module containerApps './modules/container-apps.bicep' = {
  name: 'gateway-container-apps'
  scope: resourceGroup
  params: {
    location: location
    tags: resourceTags
    suffix: suffix
    litellmImage: litellmImage
    gatewayIdentityId: identity.outputs.id
    gatewayClientId: identity.outputs.clientId
    sourceOpenAIEndpoint: sourceFoundry.outputs.openAIEndpoint
    masterKeySecretUri: keyVault.outputs.masterKeySecretUri
    workspaceId: monitoring.outputs.workspaceId
    appInsightsConnectionString: monitoring.outputs.connectionString
    gatewayAuthMode: gatewayAuthMode
    entraTenantId: entraTenantId
    entraGatewayClientId: entraGatewayClientId
    entraAppPrincipals: enableDemoBackends ? union(entraAppPrincipals, [
      {
        oid: demoQuizIdentity!.outputs.principalId
        azp: demoQuizIdentity!.outputs.clientId
      }
    ]) : entraAppPrincipals
    entraDelegatedUsers: entraDelegatedUsers
    entraDelegatedActors: entraDelegatedActors
    entraDelegatedOnly: entraDelegatedOnly
    enableDemoBackends: enableDemoBackends
    entraCapabilities: entraCapabilities
  }
}

// OAuth credentials are installed explicitly by configure-prompt-oauth.mjs, never from azd state.
module modelConnection './modules/model-connection.bicep' = if (promptAuthMode == 'api_key') {
  name: 'consumer-model-connection'
  scope: resourceGroup
  params: {
    tags: resourceTags
    consumerFoundryName: consumerFoundry.outputs.name
    consumerProjectName: consumerFoundry.outputs.projectName
    litellmEndpoint: containerApps.outputs.endpoint
    litellmMasterKey: litellmMasterKey
    modelDeploymentName: sourceFoundry.outputs.modelDeploymentName
  }
}

var keepMcpImage = enableDemoBackends && demoMcpExists
var keepA2aImage = enableDemoBackends && demoA2aExists

module demoMcpImage './modules/fetch-container-image.bicep' = if (keepMcpImage) {
  name: 'demo-mcp-image'
  scope: resourceGroup
  params: {
    name: 'demo-mcp'
  }
}

module demoA2aImage './modules/fetch-container-image.bicep' = if (keepA2aImage) {
  name: 'demo-a2a-image'
  scope: resourceGroup
  params: {
    name: 'demo-a2a'
  }
}

module demoBackends './modules/demo-backends.bicep' = if (enableDemoBackends) {
  name: 'demo-backends'
  scope: resourceGroup
  params: {
    location: location
    tags: resourceTags
    suffix: suffix
    environmentId: containerApps.outputs.environmentId
    demoMcpImage: keepMcpImage ? demoMcpImage!.outputs.image : ''
    demoA2aImage: keepA2aImage ? demoA2aImage!.outputs.image : ''
    quizIdentityId: demoQuizIdentity!.outputs.id
    quizIdentityClientId: demoQuizIdentity!.outputs.clientId
    gatewayEndpoint: containerApps.outputs.endpoint
    gatewayAudience: gatewayAuthMode == 'entra' ? entraGatewayClientId : ''
  }
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = resourceGroup.name
output LITELLM_ENDPOINT string = containerApps.outputs.endpoint
output LITELLM_CONTAINER_APP_NAME string = containerApps.outputs.name
output SOURCE_FOUNDRY_NAME string = sourceFoundry.outputs.name
output SOURCE_FOUNDRY_PROJECT_ENDPOINT string = sourceFoundry.outputs.projectEndpoint
output SOURCE_AZURE_OPENAI_ENDPOINT string = sourceFoundry.outputs.openAIEndpoint
output SOURCE_MODEL_DEPLOYMENT_NAME string = sourceFoundry.outputs.modelDeploymentName
output CONSUMER_FOUNDRY_NAME string = consumerFoundry.outputs.name
output CONSUMER_FOUNDRY_PROJECT_ENDPOINT string = consumerFoundry.outputs.projectEndpoint
output AZURE_AI_PROJECT_ID string = consumerFoundry.outputs.projectId
output AZURE_AI_PROJECT_ENDPOINT string = consumerFoundry.outputs.projectEndpoint
output FOUNDRY_PROJECT_ENDPOINT string = consumerFoundry.outputs.projectEndpoint
output AZURE_KEY_VAULT_NAME string = keyVault.outputs.name
output CONSUMER_MODEL_CONNECTION_NAME string = 'litellm-gateway'
output CONSUMER_MODEL_REFERENCE string = 'litellm-gateway/${sourceFoundry.outputs.modelDeploymentName}'
output GATEWAY_AUTH_MODE string = gatewayAuthMode
output HOSTED_AUTH_MODE string = hostedAuthMode
output PROMPT_AUTH_MODE string = promptAuthMode
output ENTRA_TENANT_ID string = entraTenantId
output ENTRA_GATEWAY_CLIENT_ID string = entraGatewayClientId
output ENTRA_GATEWAY_SCOPE string = empty(entraGatewayClientId) ? '' : 'api://${entraGatewayClientId}/.default'
output ENTRA_AGENT_CLIENT_ID string = entraAgentClientId
output ENTRA_AGENT_PRINCIPAL_ID string = entraAgentPrincipalId
output ENTRA_PROMPT_CLIENT_ID string = promptClientId
output APPLICATIONINSIGHTS_NAME string = monitoring.outputs.appInsightsName
output APPLICATIONINSIGHTS_RESOURCE_ID string = monitoring.outputs.appInsightsId
output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.connectionString
output AZURE_LOG_ANALYTICS_WORKSPACE_ID string = monitoring.outputs.workspaceId
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = enableDemoBackends ? demoBackends!.outputs.registryEndpoint : ''
output AZURE_CONTAINER_REGISTRY_NAME string = enableDemoBackends ? demoBackends!.outputs.registryName : ''
output DEMO_QUIZ_IDENTITY_PRINCIPAL_ID string = enableDemoBackends ? demoQuizIdentity!.outputs.principalId : ''
output DEMO_QUIZ_IDENTITY_CLIENT_ID string = enableDemoBackends ? demoQuizIdentity!.outputs.clientId : ''
