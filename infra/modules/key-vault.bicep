targetScope = 'resourceGroup'

param location string
param tags object
param suffix string
param principalId string
param gatewayIdentityId string
param gatewayPrincipalId string

@secure()
param litellmMasterKey string

resource keyVault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: 'kv-litellm-${suffix}'
  location: location
  tags: tags
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
    accessPolicies: []
  }
}

resource masterKeySecret 'Microsoft.KeyVault/vaults/secrets@2024-11-01' = {
  parent: keyVault
  name: 'litellm-master-key'
  tags: tags
  properties: {
    value: litellmMasterKey
    contentType: 'LiteLLM gateway bearer credential'
  }
}

resource gatewaySecretAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, gatewayIdentityId, 'Key Vault Secrets User')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
    principalId: gatewayPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource deployerSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, principalId, 'Key Vault Secrets Officer')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
    )
    principalId: principalId
    principalType: 'User'
  }
}

output name string = keyVault.name
output masterKeySecretUri string = '${keyVault.properties.vaultUri}secrets/${masterKeySecret.name}'
