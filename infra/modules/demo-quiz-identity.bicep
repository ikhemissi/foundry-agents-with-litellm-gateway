targetScope = 'resourceGroup'

param location string
param tags object
param suffix string

// Deployed ahead of both the gateway app (which approves it) and demo-backends (which attaches it),
// so neither module has to depend on the other. It holds no Azure RBAC: it only mints gateway tokens.
resource quizIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-quiz-${suffix}'
  location: location
  tags: tags
}

output id string = quizIdentity.id
output principalId string = quizIdentity.properties.principalId
output clientId string = quizIdentity.properties.clientId
