targetScope = 'resourceGroup'

param location string
param tags object
param suffix string
param principalId string

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

output name string = consumerFoundry.name
output projectName string = consumerProject.name
output projectEndpoint string = consumerProject.properties.endpoints['AI Foundry API']
