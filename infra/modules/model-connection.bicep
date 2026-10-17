targetScope = 'resourceGroup'

@description('Deployment tags; connections do not support Azure resource tags.')
#disable-next-line no-unused-params
param tags object
param consumerFoundryName string
param consumerProjectName string
param litellmEndpoint string
param modelDeploymentName string

@secure()
param litellmMasterKey string

@allowed(['api_key', 'oauth2'])
param promptAuthMode string = 'api_key'
param entraTenantId string = ''
param entraGatewayClientId string = ''
param promptClientId string = ''
@secure()
param promptClientSecret string = ''

resource consumerFoundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: consumerFoundryName
}

resource consumerProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: consumerFoundry
  name: consumerProjectName
}

resource consumerModelConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = if (promptAuthMode == 'api_key') {
  parent: consumerProject
  name: 'litellm-gateway'
  properties: {
    category: 'ModelGateway'
    target: '${litellmEndpoint}/v1'
    authType: 'ApiKey'
    isSharedToAll: false
    credentials: {
      key: litellmMasterKey
    }
    metadata: {
      deploymentInPath: 'false'
      inferenceAPIVersion: ''
      authConfig: string({
        type: 'api_key'
        name: 'Authorization'
        format: 'Bearer {api_key}'
      })
      models: string([
        {
          name: modelDeploymentName
          properties: {
            model: {
              name: 'gpt-5.4-nano'
              version: ''
              format: 'OpenAI'
            }
          }
        }
      ])
    }
  }
}

// tokenUrl/scopes are supported by ModelGateway but absent from the published ARM types.
resource consumerOAuthConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = if (promptAuthMode == 'oauth2') {
  parent: consumerProject
  name: 'litellm-gateway'
  properties: any({
    category: 'ModelGateway'
    target: '${litellmEndpoint}/v1'
    authType: 'OAuth2'
    isSharedToAll: false
    credentials: {
      clientId: promptClientId
      clientSecret: promptClientSecret
    }
    #disable-next-line no-hardcoded-env-urls
    tokenUrl: 'https://login.microsoftonline.com/${entraTenantId}/oauth2/v2.0/token'
    scopes: ['api://${entraGatewayClientId}/.default']
    metadata: {
      deploymentInPath: 'false'
      inferenceAPIVersion: ''
      models: string([
        {
          name: modelDeploymentName
          properties: {
            model: { name: 'gpt-5.4-nano', version: '', format: 'OpenAI' }
          }
        }
      ])
    }
  })
}

output name string = 'litellm-gateway'
output modelReference string = 'litellm-gateway/${modelDeploymentName}'
