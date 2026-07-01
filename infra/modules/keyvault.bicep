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
