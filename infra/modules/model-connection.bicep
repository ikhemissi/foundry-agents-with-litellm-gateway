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

resource consumerFoundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: consumerFoundryName
}

resource consumerProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: consumerFoundry
  name: consumerProjectName
}

resource consumerModelConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
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

output name string = consumerModelConnection.name
output modelReference string = '${consumerModelConnection.name}/${modelDeploymentName}'
