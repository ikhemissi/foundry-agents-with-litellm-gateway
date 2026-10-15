targetScope = 'resourceGroup'

@description('Deployment tags; role assignments do not support Azure resource tags.')
#disable-next-line no-unused-params
param tags object = {}

@description('Foundry hosted agent instance identity, available after agent deployment.')
@minLength(36)
@maxLength(36)
param agentPrincipalId string
param keyVaultName string
param appInsightsName string

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource gatewayKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: vault
  name: 'litellm-master-key'
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource gatewayKeyReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(gatewayKey.id, agentPrincipalId, 'Key Vault Secrets User')
  scope: gatewayKey
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource telemetryPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsights.id, agentPrincipalId, 'Monitoring Metrics Publisher')
  scope: appInsights
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '3913510d-42f4-4e42-8a64-420c390055eb'
    )
    principalId: agentPrincipalId
    principalType: 'ServicePrincipal'
  }
}
