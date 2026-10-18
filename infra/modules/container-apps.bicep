targetScope = 'resourceGroup'

param location string
param tags object
param suffix string
param litellmImage string
param gatewayIdentityId string
param gatewayClientId string
param sourceOpenAIEndpoint string
param masterKeySecretUri string
param workspaceId string
param appInsightsConnectionString string
@allowed(['api_key', 'entra'])
param gatewayAuthMode string = 'api_key'
param entraTenantId string = ''
param entraGatewayClientId string = ''
param entraAppPrincipals array = []
param entraDelegatedUsers array = []
param entraDelegatedActors array = []
param entraDelegatedOnly bool = false
@description('Append litellm/config.demo.yaml (MCP server and A2A agent registrations) to the Entra gateway config.')
param enableDemoBackends bool = false
@description('Per-principal MCP tool and A2A agent grants, keyed by principal object ID.')
param entraCapabilities object = {}

var entraBaseConfig = loadTextContent('../../litellm/config.entra.yaml')
var demoConfigOverlay = loadTextContent('../../litellm/config.demo.yaml')
var entraConfig = enableDemoBackends ? '${entraBaseConfig}\n${demoConfigOverlay}' : entraBaseConfig

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: 'cae-litellm-${suffix}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'azure-monitor'
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
        {
          name: 'otel-config'
          // Collector configuration contains only environment references, not credentials.
          #disable-next-line use-secure-value-for-secure-inputs
          value: loadTextContent('../../monitoring/otel-collector.yaml')
        }
        {
          name: 'entra-auth'
          #disable-next-line use-secure-value-for-secure-inputs
          value: loadTextContent('../../litellm/entra_auth.py')
        }
        {
          name: 'gateway-app'
          #disable-next-line use-secure-value-for-secure-inputs
          value: loadTextContent('../../litellm/gateway_app.py')
        }
        {
          name: 'gateway-serve'
          #disable-next-line use-secure-value-for-secure-inputs
          value: loadTextContent('../../litellm/serve.py')
        }
        {
          name: 'gateway-requirements'
          #disable-next-line use-secure-value-for-secure-inputs
          value: loadTextContent('../../litellm/requirements.txt')
        }
        {
          name: 'gateway-policy'
          #disable-next-line use-secure-value-for-secure-inputs
          value: loadTextContent('../../litellm/gateway_policy.py')
        }
        {
          name: 'credential-context'
          #disable-next-line use-secure-value-for-secure-inputs
          value: loadTextContent('../../litellm/credential_context.py')
        }
        {
          name: 'litellm-entra-config'
          #disable-next-line use-secure-value-for-secure-inputs
          value: entraConfig
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'litellm'
          image: litellmImage
          command: gatewayAuthMode == 'entra' ? ['python'] : null
          args: gatewayAuthMode == 'entra' ? ['/etc/litellm/serve.py'] : [
            '--config'
            '/etc/litellm/config.yaml'
            '--host'
            '0.0.0.0'
            '--port'
            '4000'
          ]
          env: [
            { name: 'LITELLM_AUTH_MODE', value: gatewayAuthMode }
            { name: 'ENTRA_TENANT_ID', value: entraTenantId }
            { name: 'ENTRA_GATEWAY_CLIENT_ID', value: entraGatewayClientId }
            { name: 'ENTRA_APP_PRINCIPALS', value: string(entraAppPrincipals) }
            { name: 'ENTRA_DELEGATED_USERS', value: string(entraDelegatedUsers) }
            { name: 'ENTRA_DELEGATED_ACTORS', value: string(entraDelegatedActors) }
            { name: 'ENTRA_DELEGATED_ONLY', value: entraDelegatedOnly ? 'true' : 'false' }
            { name: 'ENTRA_CAPABILITIES', value: string(entraCapabilities) }
            { name: 'PYTHONPATH', value: '/etc/litellm' }
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
            {
              name: 'OTEL_EXPORTER'
              value: 'otlp_http'
            }
            {
              name: 'OTEL_ENDPOINT'
              value: 'http://127.0.0.1:4318/v1/traces'
            }
            {
              name: 'OTEL_SERVICE_NAME'
              value: 'litellm-gateway'
            }
            {
              name: 'OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT'
              value: 'NO_CONTENT'
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
        {
          name: 'otel-collector'
          image: 'otel/opentelemetry-collector-contrib:0.162.0'
          args: [
            '--config=/etc/otelcol/config.yaml'
          ]
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: gatewayClientId
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsightsConnectionString
            }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          volumeMounts: [
            {
              volumeName: 'otel-config'
              mountPath: '/etc/otelcol'
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/'
                port: 13133
                scheme: 'HTTP'
              }
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/'
                port: 13133
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
            { secretRef: 'entra-auth', path: 'entra_auth.py' }
            { secretRef: 'gateway-app', path: 'gateway_app.py' }
            { secretRef: 'gateway-serve', path: 'serve.py' }
            { secretRef: 'gateway-requirements', path: 'requirements.txt' }
            { secretRef: 'gateway-policy', path: 'gateway_policy.py' }
            { secretRef: 'credential-context', path: 'credential_context.py' }
            { secretRef: 'litellm-entra-config', path: 'config.entra.yaml' }
          ]
        }
        {
          name: 'otel-config'
          storageType: 'Secret'
          secrets: [
            {
              secretRef: 'otel-config'
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

resource environmentDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'shared-monitoring'
  scope: environment
  properties: {
    workspaceId: workspaceId
    logs: [
      { category: 'ContainerAppConsoleLogs', enabled: true }
      { category: 'ContainerAppSystemLogs', enabled: true }
      { category: 'ContainerAppHTTPLogs', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

resource gatewayDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'shared-monitoring'
  scope: gateway
  properties: {
    workspaceId: workspaceId
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

output endpoint string = 'https://${gateway.properties.configuration.ingress.fqdn}'
output name string = gateway.name
output environmentId string = environment.id
