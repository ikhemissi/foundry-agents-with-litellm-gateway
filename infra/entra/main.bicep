targetScope = 'resourceGroup'

extension 'br:mcr.microsoft.com/bicep/extensions/microsoftgraph/v1.0:1.0.0'

@description('Immutable tenant-wide registration key. Never change after creation/adoption.')
@minLength(1)
param registrationKey string
param gatewayDisplayName string = 'LiteLLM gateway API'
param promptDisplayName string = 'LiteLLM prompt client'
@description('Existing gateway application object ID. Adoption must first be prepared by prepare-entra-registration.mjs.')
param existingGatewayObjectId string = ''
param existingPromptObjectId string = ''
@description('Preserve an existing gateway.invoke scope ID on adoption.')
param gatewayScopeId string = guid(registrationKey, 'gateway.invoke')
param preauthorizeAzureCli bool = false

var gatewayKey = '${registrationKey}-gateway'
var promptKey = '${registrationKey}-prompt'

// Only new applications are managed here. Adopted manifests are read-only; the
// approval-gated preparation script performs narrowly merged updates instead.
resource newGateway 'Microsoft.Graph/applications@v1.0' = if (empty(existingGatewayObjectId)) {
  uniqueName: gatewayKey
  displayName: gatewayDisplayName
  signInAudience: 'AzureADMyOrg'
}
resource adoptedGateway 'Microsoft.Graph/applications@v1.0' existing = if (!empty(existingGatewayObjectId)) {
  uniqueName: gatewayKey
}
var gatewayAppId = empty(existingGatewayObjectId) ? newGateway!.appId : adoptedGateway!.appId

resource newPrompt 'Microsoft.Graph/applications@v1.0' = if (empty(existingPromptObjectId)) {
  uniqueName: promptKey
  displayName: promptDisplayName
  signInAudience: 'AzureADMyOrg'
}
resource adoptedPrompt 'Microsoft.Graph/applications@v1.0' existing = if (!empty(existingPromptObjectId)) {
  uniqueName: promptKey
}
var promptAppId = empty(existingPromptObjectId) ? newPrompt!.appId : adoptedPrompt!.appId

resource gatewayPrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: gatewayAppId
}
resource promptPrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: promptAppId
}

module gatewayIdentifier './identifier.bicep' = if (empty(existingGatewayObjectId)) {
  name: 'gateway-api-identifier'
  params: {
    uniqueName: gatewayKey
    appId: gatewayAppId
    gatewayScopeId: gatewayScopeId
    preauthorizeAzureCli: preauthorizeAzureCli
  }
}

output ENTRA_TENANT_ID string = tenant().tenantId
output ENTRA_GATEWAY_CLIENT_ID string = gatewayAppId
output ENTRA_GATEWAY_OBJECT_ID string = empty(existingGatewayObjectId) ? newGateway!.id : adoptedGateway!.id
output ENTRA_GATEWAY_PRINCIPAL_ID string = gatewayPrincipal.id
output ENTRA_GATEWAY_SCOPE_ID string = empty(existingGatewayObjectId)
  ? gatewayIdentifier!.outputs.scopeId
  : filter(adoptedGateway!.api.oauth2PermissionScopes, item => item.value == 'gateway.invoke')[0].id
output ENTRA_GATEWAY_SCOPE string = 'api://${gatewayAppId}/.default'
output ENTRA_PROMPT_CLIENT_ID string = promptAppId
output ENTRA_PROMPT_OBJECT_ID string = empty(existingPromptObjectId) ? newPrompt!.id : adoptedPrompt!.id
output ENTRA_PROMPT_PRINCIPAL_ID string = promptPrincipal.id
