targetScope = 'resourceGroup'
extension 'br:mcr.microsoft.com/bicep/extensions/microsoftgraph/v1.0:1.0.0'

param uniqueName string
param appId string
param gatewayScopeId string
param preauthorizeAzureCli bool

// Graph read-backs include null properties, which the write path rejects.
func withoutNulls(value object) object => toObject(filter(items(value), entry => entry.value != null), entry => entry.key, entry => entry.value)

resource current 'Microsoft.Graph/applications@v1.0' existing = {
  uniqueName: uniqueName
}

var currentApi = withoutNulls(current.api ?? {})
var scopes = map(currentApi.?oauth2PermissionScopes ?? [], scope => withoutNulls(scope))
var existingInvoke = filter(scopes, item => item.value == 'gateway.invoke')
var invokeScope = !empty(existingInvoke) ? existingInvoke[0] : {
  id: gatewayScopeId
  value: 'gateway.invoke'
  isEnabled: true
  type: 'Admin'
  adminConsentDisplayName: 'Invoke the LiteLLM gateway'
  adminConsentDescription: 'Invoke the LiteLLM inference gateway on behalf of an approved user.'
}
var cliAppId = '04b07795-8ddb-461a-bbee-02f9e1bf7b46'
var preauthorized = map(currentApi.?preAuthorizedApplications ?? [], app => withoutNulls(app))
var existingCli = filter(preauthorized, item => item.appId == cliAppId)
var cli = !empty(existingCli) ? existingCli[0] : { appId: cliAppId, delegatedPermissionIds: [] }
var optionalClaims = withoutNulls(current.optionalClaims ?? {})
var accessClaims = map(optionalClaims.?accessToken ?? [], claim => withoutNulls(claim))
var claimNames = map(accessClaims, claim => claim.name)
var requiredClaims = ['idtyp', 'xms_act_fct', 'xms_sub_fct']

// Graph application upsert PATCHes only supplied fields. This second stage uses
// the Entra-assigned appId, not a fabricated GUID or the identifier URI as audience.
resource gateway 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: uniqueName
  displayName: current.displayName
  identifierUris: union(current.identifierUris ?? [], ['api://${appId}'])
  optionalClaims: union(optionalClaims, {
    accessToken: concat(accessClaims, map(
      filter(requiredClaims, claim => !contains(claimNames, claim)),
      claim => { name: claim, essential: false }
    ))
  })
  api: union(currentApi, {
    requestedAccessTokenVersion: 2
    oauth2PermissionScopes: empty(existingInvoke) ? concat(scopes, [invokeScope]) : scopes
    preAuthorizedApplications: preauthorizeAzureCli ? concat(
      filter(preauthorized, item => item.appId != cliAppId),
      [union(cli, { delegatedPermissionIds: union(cli.?delegatedPermissionIds ?? [], [invokeScope.id]) })]
    ) : preauthorized
  })
}

output scopeId string = invokeScope.id
