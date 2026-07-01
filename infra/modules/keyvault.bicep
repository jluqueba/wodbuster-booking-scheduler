// keyvault.bicep
//
// Azure Key Vault in RBAC mode. Implements ADR-0005 (Key Vault + UAMI, no client
// secrets). Soft-delete and purge-protection are on so we can survive accidental
// deletion of the vault or the secrets it will hold (populated manually in F3.8).
//
// Public network access is enabled: this is a single-operator MVP; private
// endpoints would blow the 15-20 EUR/mo target for zero real threat-model gain.
//
// COST: ~1 EUR/mo (Standard tier, single-user read cadence).

@description('Azure region.')
param location string

@description('Short random token for globally-unique resource names.')
param resourceToken string

@description('Resource tags.')
param tags object

@description('Optional object ID of an Entra principal (typically the operator running azd) to grant Key Vault Secrets Officer for seeding F3.8 secrets. Empty in CI.')
param operatorPrincipalId string = ''

// Key Vault Secrets Officer role: set and read secret values. Required for F3.8
// (`az keyvault secret set ...`). Scoped to the vault, not the subscription.
var keyVaultSecretsOfficerRoleId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'

resource keyVault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: 'kv-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

output keyVaultId string = keyVault.id
output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri

resource operatorSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorPrincipalId)) {
  name: guid(keyVault.id, operatorPrincipalId, keyVaultSecretsOfficerRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsOfficerRoleId)
    principalId: operatorPrincipalId
    principalType: 'User'
  }
}
