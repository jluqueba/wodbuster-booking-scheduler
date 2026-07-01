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
// This file creates the resource group and delegates all resource-group-scoped
// work to resources.bicep. The azd-standard outputs are forwarded up.

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

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    principalId: principalId
  }
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_PRINCIPAL_ID string = principalId

output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.registryName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.registryLoginServer

output AZURE_KEY_VAULT_NAME string = resources.outputs.keyVaultName
output AZURE_KEY_VAULT_ENDPOINT string = resources.outputs.keyVaultUri

output AZURE_APPLICATION_INSIGHTS_CONNECTION_STRING string = resources.outputs.appInsightsConnectionString

output AZURE_STORAGE_ACCOUNT_NAME string = resources.outputs.storageAccountName
output AZURE_STORAGE_FILE_SHARE_NAME string = resources.outputs.fileShareName

output AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID string = resources.outputs.identityClientId
output AZURE_USER_ASSIGNED_IDENTITY_PRINCIPAL_ID string = resources.outputs.identityPrincipalId

output SERVICE_WORKER_ENDPOINT string = resources.outputs.containerAppEndpoint
output SERVICE_WORKER_NAME string = resources.outputs.containerAppName
