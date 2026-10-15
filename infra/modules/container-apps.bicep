targetScope = 'resourceGroup'

param location string
param tags object
param suffix string
param litellmImage string
param gatewayIdentityId string
param gatewayClientId string
param sourceOpenAIEndpoint string
param masterKeySecretUri string
param workspaceName string

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: 'cae-litellm-${suffix}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

resource gateway 'Microsoft.App/containerApps@2025-01-01' = {
  name: 'ca-litellm-${suffix}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${gatewayIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 4000
        transport: 'http'
      }
      secrets: [
        {
          name: 'litellm-master-key'
          keyVaultUrl: masterKeySecretUri
          identity: gatewayIdentityId
        }
        {
          name: 'litellm-config'
          // ACA secret volumes also support non-sensitive configuration files.
          #disable-next-line use-secure-value-for-secure-inputs
          value: loadTextContent('../../litellm/config.yaml')
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'litellm'
          image: litellmImage
          args: [
            '--config'
            '/etc/litellm/config.yaml'
            '--host'
            '0.0.0.0'
            '--port'
            '4000'
          ]
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: gatewayClientId
            }
            {
              name: 'AZURE_API_BASE'
              value: sourceOpenAIEndpoint
            }
            {
              name: 'LITELLM_MASTER_KEY'
              secretRef: 'litellm-master-key'
            }
          ]
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          volumeMounts: [
            {
              volumeName: 'litellm-config'
              mountPath: '/etc/litellm'
            }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health/liveliness'
                port: 4000
                scheme: 'HTTP'
              }
              periodSeconds: 5
              failureThreshold: 60
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health/liveliness'
                port: 4000
                scheme: 'HTTP'
              }
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health/readiness'
                port: 4000
                scheme: 'HTTP'
              }
              periodSeconds: 10
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'litellm-config'
          storageType: 'Secret'
          secrets: [
            {
              secretRef: 'litellm-config'
              path: 'config.yaml'
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
}

output endpoint string = 'https://${gateway.properties.configuration.ingress.fqdn}'
output name string = gateway.name
