// main.bicep
//
// Subscription-scoped entry point for the WodBuster booking worker infrastructure.
//
// ADRs implemented (or set up for later modules to implement):
//   ADR-0001  Azure Container Apps as the hosting service
//   ADR-0002  SQLite on Azure Files
//   ADR-0005  Key Vault + user-assigned managed identity, no client secrets
//   ADR-0006  Log Analytics + Application Insights + external dead-man
//   ADR-0007  Bicep + azd, single environment `prod`, single resource group
//
// This file only creates the resource group and propagates the azd-standard
// outputs that the resource-group-scoped modules produce (added incrementally
// in F3.3 and F3.4).

targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the azd environment (drives the resource group name). Typical value: prod.')
param environmentName string

@minLength(1)
@description('Azure region for all resources. Default westeurope (operator located in Spain).')
param location string = 'westeurope'

@description('Object ID of the azd caller (used later for local-dev role assignments). Empty in CI.')
param principalId string = ''

var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = {
  'azd-env-name': environmentName
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}-${resourceToken}'
  location: location
  tags: tags
}

// Placeholder reference so the compiler treats the parameter as used until
// F3.3 wires it into role assignments for local developer access.
var _principalIdUnused = principalId

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_PRINCIPAL_ID string = _principalIdUnused
