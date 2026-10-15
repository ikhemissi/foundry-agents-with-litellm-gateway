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
  }
}

module modelConnection './modules/model-connection.bicep' = {
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
output AZURE_KEY_VAULT_NAME string = keyVault.outputs.name
output CONSUMER_MODEL_CONNECTION_NAME string = modelConnection.outputs.name
output CONSUMER_MODEL_REFERENCE string = modelConnection.outputs.modelReference
output APPLICATIONINSIGHTS_NAME string = monitoring.outputs.appInsightsName
output APPLICATIONINSIGHTS_RESOURCE_ID string = monitoring.outputs.appInsightsId
output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.connectionString
output AZURE_LOG_ANALYTICS_WORKSPACE_ID string = monitoring.outputs.workspaceId
