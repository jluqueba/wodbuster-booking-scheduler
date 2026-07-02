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

@description('Object ID of the human operator (empty in CI initial run). Used only to grant the operator Key Vault Secrets Officer on the vault so they can seed F3.8 secrets. Never assigned to a deploy identity: azd cannot be trusted to leave this value alone if it is named `principalId`, so we deliberately use `operatorPrincipalId` here and in main.bicep.')
param operatorPrincipalId string = ''

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
    operatorPrincipalId: operatorPrincipalId
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

module containerApp 'modules/containerapp.bicep' = {
  name: 'containerapp'
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    logAnalyticsWorkspaceId: observability.outputs.logAnalyticsWorkspaceId
    identityId: identity.outputs.identityId
    registryLoginServer: registry.outputs.registryLoginServer
    storageAccountName: storage.outputs.storageAccountName
    fileShareName: storage.outputs.fileShareName
    storageAccountKey: storage.outputs.storageAccountKey
    appInsightsConnectionString: observability.outputs.appInsightsConnectionString
    keyVaultUri: keyVault.outputs.keyVaultUri
  }
}

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
output containerAppName string = containerApp.outputs.containerAppName
output containerAppEndpoint string = containerApp.outputs.containerAppEndpoint
