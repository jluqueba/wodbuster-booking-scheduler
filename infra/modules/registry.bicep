// registry.bicep
//
// Azure Container Registry, Basic SKU. Implements the ACR half of ADR-0007.
// Admin user is off: pull uses the UAMI + AcrPull (bound in identity.bicep),
// push uses OIDC federation from GitHub Actions (F2.2).
//
// COST: ~4 EUR/mo (Basic tier, 10 GiB included).

@description('Azure region.')
param location string

@description('Short random token for globally-unique resource names (uniqueString() output).')
@minLength(5)
param resourceToken string

@description('Resource tags.')
param tags object

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: 'cr${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
    zoneRedundancy: 'Disabled'
  }
}

output registryId string = registry.id
output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
