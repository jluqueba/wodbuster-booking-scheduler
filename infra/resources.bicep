// resources.bicep
//
// Resource-group-scoped composition. Called from main.bicep with scope: rg.
// This is where all the actual resources live; main.bicep only creates the
// resource group and forwards outputs.
//
// F3.3 wires the five foundational modules. F3.4 will add the Container App.

targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string

@description('Short random token for globally-unique resource names.')
param resourceToken string

@description('Resource tags applied to every resource.')
param tags object

@description('Object ID of the azd caller (used later for local-dev role assignments). Empty in CI.')
param principalId string = ''

module observability 'modules/observability.bicep' = {
  name: 'observability'
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
  }
}

module keyVault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
  }
}

module registry 'modules/registry.bicep' = {
  name: 'registry'
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
  }
}

module identity 'modules/identity.bicep' = {
  name: 'identity'
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    keyVaultName: keyVault.outputs.keyVaultName
    registryName: registry.outputs.registryName
  }
}

// Placeholder reference so the compiler treats the parameter as used until
// local developer role assignments are wired in a later task.
var _principalIdUnused = principalId

output principalIdEcho string = _principalIdUnused
output logAnalyticsWorkspaceId string = observability.outputs.logAnalyticsWorkspaceId
output appInsightsConnectionString string = observability.outputs.appInsightsConnectionString
output keyVaultName string = keyVault.outputs.keyVaultName
output keyVaultUri string = keyVault.outputs.keyVaultUri
output registryName string = registry.outputs.registryName
output registryLoginServer string = registry.outputs.registryLoginServer
output storageAccountName string = storage.outputs.storageAccountName
output fileShareName string = storage.outputs.fileShareName
output identityId string = identity.outputs.identityId
output identityPrincipalId string = identity.outputs.identityPrincipalId
output identityClientId string = identity.outputs.identityClientId
