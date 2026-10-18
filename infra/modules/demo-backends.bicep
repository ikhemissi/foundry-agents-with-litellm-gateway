targetScope = 'resourceGroup'

param location string
param tags object
param suffix string
@description('Existing managed environment shared with the gateway.')
param environmentId string
@description('Image currently deployed to demo-mcp; empty on first provision.')
param demoMcpImage string = ''
@description('Image currently deployed to demo-a2a; empty on first provision.')
param demoA2aImage string = ''
@description('Resource ID of the quiz agent identity that calls the gateway; attached to demo-a2a only.')
param quizIdentityId string
@description('Client ID of the quiz agent identity (AZURE_CLIENT_ID for DefaultAzureCredential).')
param quizIdentityClientId string
@description('LiteLLM gateway endpoint, e.g. https://<fqdn>.')
param gatewayEndpoint string
@description('Entra audience (application ID) of the gateway; empty turns the quiz model off.')
param gatewayAudience string = ''
@secure()
@minLength(32)
@description('HMAC key for quiz ids. Generated per provision; only invalidates open quizzes when it rotates.')
param quizSigningKey string = '${newGuid()}${newGuid()}'

// Serves 200 on /healthz at 8080, so the required probes pass until `azd deploy` replaces it.
var placeholderImage = 'mcr.microsoft.com/dotnet/samples:aspnetapp'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'crdemo${suffix}'
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource demoIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-demo-${suffix}'
  location: location
  tags: tags
}

resource demoAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, demoIdentity.id, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: demoIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

var apps = [
  {
    name: 'demo-mcp'
    image: empty(demoMcpImage) ? placeholderImage : demoMcpImage
    env: []
    secrets: false
    quizIdentity: false
  }
  {
    name: 'demo-a2a'
    image: empty(demoA2aImage) ? placeholderImage : demoA2aImage
    env: [
      { name: 'PUBLIC_URL', value: 'http://demo-a2a/' }
      { name: 'QUIZ_SIGNING_KEY', secretRef: 'quiz-signing-key' }
      { name: 'QUIZ_MODEL_MODE', value: empty(gatewayAudience) ? 'off' : 'gateway' }
      { name: 'QUIZ_GATEWAY_BASE_URL', value: '${gatewayEndpoint}/v1' }
      { name: 'QUIZ_GATEWAY_AUDIENCE', value: gatewayAudience }
      { name: 'AZURE_CLIENT_ID', value: quizIdentityClientId }
      { name: 'QUIZ_MODEL', value: 'gpt-5.4-nano' }
    ]
    secrets: true
    quizIdentity: true
  }
]

resource demoApps 'Microsoft.App/containerApps@2025-01-01' = [for app in apps: {
  name: app.name
  location: location
  tags: union(tags, { 'azd-service-name': app.name })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: app.quizIdentity ? {
      '${demoIdentity.id}': {}
      '${quizIdentityId}': {}
    } : {
      '${demoIdentity.id}': {}
    }
  }
  dependsOn: [
    demoAcrPull
  ]
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        allowInsecure: false
        targetPort: 8080
        transport: 'http'
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: demoIdentity.id
        }
      ]
      secrets: app.secrets ? [
        {
          name: 'quiz-signing-key'
          value: quizSigningKey
        }
      ] : []
    }
    template: {
      containers: [
        {
          name: app.name
          image: app.image
          env: app.env
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/healthz'
                port: 8080
                scheme: 'HTTP'
              }
              periodSeconds: 5
              failureThreshold: 30
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8080
                scheme: 'HTTP'
              }
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: 8080
                scheme: 'HTTP'
              }
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}]

output registryName string = registry.name
output registryEndpoint string = registry.properties.loginServer
