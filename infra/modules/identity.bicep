// identity.bicep
//
// User-assigned managed identity + two RBAC role assignments:
//   - Key Vault Secrets User on the Key Vault (ADR-0005: worker reads secrets).
//   - AcrPull on the Container Registry (ADR-0001/0007: managed-identity pull).
//
// The identity has zero credentials of its own: the Container App federates via
// IMDS. No client secrets ever land in the repo, image, or environment.

@description('Azure region.')
param location string

@description('Short random token for globally-unique resource names.')
param resourceToken string

@description('Resource tags.')
param tags object

@description('Key Vault name (same resource group) to scope the Secrets User role.')
param keyVaultName string

@description('Container Registry name (same resource group) to scope the AcrPull role.')
param registryName string

// Well-known Azure built-in role definition IDs (documented, stable GUIDs).
// Note: AcrPull's ID is `7f951dda-4ed3-4680-a7ca-43fe172d538d` in our tenant,
// not the widely-quoted `7f951dda-4ed3-11e8-8f5f-5ba03cf5c85e`. Verify with
// `az role definition list --name AcrPull --query "[0].name" -o tsv` before
// deploying to a new tenant.
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${resourceToken}'
  location: location
  tags: tags
}

resource keyVault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: keyVaultName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: registryName
}

resource kvSecretsUserAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, uami.id, keyVaultSecretsUserRoleId)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
  }
}

resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, uami.id, acrPullRoleId)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

output identityId string = uami.id
output identityName string = uami.name
output identityPrincipalId string = uami.properties.principalId
output identityClientId string = uami.properties.clientId
