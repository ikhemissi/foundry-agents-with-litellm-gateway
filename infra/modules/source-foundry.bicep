targetScope = 'resourceGroup'

param location string
param tags object
param suffix string
param principalId string
param gatewayIdentityId string
param gatewayPrincipalId string
param modelCapacity int

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

output name string = sourceFoundry.name
output projectEndpoint string = sourceProject.properties.endpoints['AI Foundry API']
output openAIEndpoint string = 'https://${sourceFoundry.name}.openai.azure.com'
output modelDeploymentName string = modelDeployment.name
