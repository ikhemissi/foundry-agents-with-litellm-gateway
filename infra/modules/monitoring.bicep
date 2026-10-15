targetScope = 'resourceGroup'

param location string
param tags object
param suffix string
param principalId string
param gatewayPrincipalId string

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-litellm-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-litellm-${suffix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
    DisableLocalAuth: true
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

var readerRoles = [
  '73c42c96-874c-492b-b04d-ab87d138a893' // Log Analytics Reader
  'dbc9c667-e97f-4491-aee6-90b9cf960190' // Privileged Monitoring Data Reader (protected GenAI content)
]

resource deployerAppInsightsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for roleId in readerRoles: {
  name: guid(appInsights.id, principalId, roleId)
  scope: appInsights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
    principalId: principalId
    principalType: 'User'
  }
}]

resource deployerWorkspaceReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for roleId in readerRoles: {
  name: guid(logs.id, principalId, roleId)
  scope: logs
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
    principalId: principalId
    principalType: 'User'
  }
}]

resource gatewayTelemetryPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, gatewayPrincipalId, 'Monitoring Metrics Publisher')
  scope: appInsights
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '3913510d-42f4-4e42-8a64-420c390055eb'
    )
    principalId: gatewayPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output name string = logs.name
output workspaceId string = logs.id
output appInsightsName string = appInsights.name
output appInsightsId string = appInsights.id
output connectionString string = appInsights.properties.ConnectionString
