targetScope = 'resourceGroup'

param location string
param tags object
param suffix string

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-litellm-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

output name string = logs.name
