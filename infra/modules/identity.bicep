targetScope = 'resourceGroup'

param location string
param tags object
param suffix string

resource gatewayIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-litellm-${suffix}'
  location: location
  tags: tags
}

output id string = gatewayIdentity.id
output principalId string = gatewayIdentity.properties.principalId
output clientId string = gatewayIdentity.properties.clientId
