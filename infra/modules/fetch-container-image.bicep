targetScope = 'resourceGroup'

@description('Name of an existing Container App whose currently deployed image must be preserved.')
param name string

// Reading the live image in a separate deployment avoids a self-referencing template, so
// re-running provision keeps the image that `azd deploy` pushed instead of the placeholder.
resource existingApp 'Microsoft.App/containerApps@2025-01-01' existing = {
  name: name
}

output image string = existingApp.properties.template.containers[0].image
