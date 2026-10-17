targetScope = 'resourceGroup'

param location string
param tags object
param suffix string
param principalId string
param appInsightsName string
param workspaceId string

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource consumerFoundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'foundry-consumer-${suffix}'
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
    customSubDomainName: 'foundry-consumer-${suffix}'
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource consumerProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: consumerFoundry
  name: 'consumer-project'
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Gateway model consumer'
    description: 'Hosts an AI tutor using the source model through LiteLLM; no local model capacity.'
  }
}

resource deployerConsumerAIUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(consumerFoundry.id, principalId, 'Azure AI User')
  scope: consumerFoundry
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
  name: guid(appInsights.id, consumerProject.id, 'Monitoring Reader')
  scope: appInsights
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
    )
    principalId: consumerProject.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource projectTelemetryPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, consumerProject.id, 'Monitoring Metrics Publisher')
  scope: appInsights
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '3913510d-42f4-4e42-8a64-420c390055eb'
    )
    principalId: consumerProject.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// This supported API includes ProjectManagedIdentity; Bicep's published types lag the service.
#disable-next-line BCP081
resource projectAppInsights 'Microsoft.CognitiveServices/accounts/projects/connections@2026-09-01' = {
  parent: consumerProject
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
  scope: consumerFoundry
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
  scope: consumerProject
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

output name string = consumerFoundry.name
output projectName string = consumerProject.name
output projectId string = consumerProject.id
output projectEndpoint string = consumerProject.properties.endpoints['AI Foundry API']
