targetScope = 'resourceGroup'

@description('Deployment tags; role assignments do not support Azure resource tags.')
#disable-next-line no-unused-params
param tags object
param principalId string

resource deployerContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, principalId, 'Contributor')
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'b24988ac-6180-42a0-ab88-20f7382dd24c'
    )
    principalId: principalId
    principalType: 'User'
  }
}
