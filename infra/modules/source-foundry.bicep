targetScope = 'resourceGroup'

param location string
param tags object
param suffix string
param principalId string
param gatewayIdentityId string
param gatewayPrincipalId string
param modelCapacity int
param appInsightsName string
param workspaceId string

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

var modelDeploymentName = 'gpt-5.4-nano'
var openAIUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
)

resource sourceFoundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'foundry-source-${suffix}'
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: 'foundry-source-${suffix}'
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource sourceProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: sourceFoundry
  name: 'source-project'
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Source models'
    description: 'Hosts the model deployment accessed by LiteLLM.'
  }
  // Cognitive Services child writes can conflict when run in parallel.
  dependsOn: [
    modelDeployment
  ]
}

resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: sourceFoundry
  name: modelDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.4-nano'
      version: '2026-03-17'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource gatewayModelAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sourceFoundry.id, gatewayIdentityId, openAIUserRoleId)
  scope: sourceFoundry
  properties: {
    roleDefinitionId: openAIUserRoleId
    principalId: gatewayPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource deployerSourceAIUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sourceFoundry.id, principalId, 'Azure AI User')
  scope: sourceFoundry
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '53ca6127-db72-4b80-b1b0-d745d6d5456d'
    )
    principalId: principalId
    principalType: 'User'
  }
}

resource projectInsightsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, sourceProject.id, 'Monitoring Reader')
  scope: appInsights
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
    )
    principalId: sourceProject.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource projectTelemetryPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, sourceProject.id, 'Monitoring Metrics Publisher')
  scope: appInsights
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '3913510d-42f4-4e42-8a64-420c390055eb'
    )
    principalId: sourceProject.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// This supported API includes ProjectManagedIdentity; Bicep's published types lag the service.
#disable-next-line BCP081
resource projectAppInsights 'Microsoft.CognitiveServices/accounts/projects/connections@2026-09-01' = {
  parent: sourceProject
  name: appInsightsName
  properties: {
    category: 'AppInsights'
    target: appInsights.id
    authType: 'ProjectManagedIdentity'
    isSharedToAll: false
    metadata: {
      ApiType: 'Azure'
      ResourceId: appInsights.id
      ApplicationInsightsConnectionString: appInsights.properties.ConnectionString
    }
  }
  dependsOn: [
    projectInsightsReader
    projectTelemetryPublisher
  ]
}

resource accountDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'shared-monitoring'
  scope: sourceFoundry
  properties: {
    workspaceId: workspaceId
    logs: [
      { category: 'Audit', enabled: true }
      { category: 'AzureOpenAIRequestUsage', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

resource projectDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'shared-monitoring'
  scope: sourceProject
  properties: {
    workspaceId: workspaceId
    logs: [
      { category: 'Audit', enabled: true }
      { category: 'Trace', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

output name string = sourceFoundry.name
output projectEndpoint string = sourceProject.properties.endpoints['AI Foundry API']
output openAIEndpoint string = 'https://${sourceFoundry.name}.openai.azure.com'
output modelDeploymentName string = modelDeployment.name
