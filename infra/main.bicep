// main.bicep
//
// Resource-group-scoped entry point for the WodBuster booking worker infrastructure.
//
// ADRs implemented (or set up for later modules to implement):
//   ADR-0001  Azure Container Apps as the hosting service
//   ADR-0002  SQLite on Azure Files
//   ADR-0005  Key Vault + user-assigned managed identity, no client secrets
//   ADR-0006  Log Analytics + Application Insights + external dead-man
//   ADR-0007  Bicep + azd, single environment `prod`, single resource group
//
// Resource-group scope (not subscription) because after F3.10 the deploy UAMI
// used by GitHub Actions holds Contributor + User Access Administrator on the
// resource group only. The resource group itself is created once, from the
// operator's laptop, as part of the F3.5 bootstrap. `azd` picks up the target
// resource group from the AZURE_RESOURCE_GROUP env var.

targetScope = 'resourceGroup'

@minLength(1)
@maxLength(64)
@description('Name of the azd environment. Kept as a param so the resourceToken formula stays stable across the subscription-scope bootstrap and the resource-group-scope steady state.')
param environmentName string

@minLength(1)
@description('Azure region for all resources. Defaults to the enclosing resource group location.')
param location string = resourceGroup().location

@description('Object ID of the operator running azd (empty in CI). Wired through to the runtime UAMI Key Vault Secrets Officer grant.')
param principalId string = ''

var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = {
  'azd-env-name': environmentName
}

module resources 'resources.bicep' = {
  name: 'resources'
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    principalId: principalId
  }
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = resourceGroup().name
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
