// storage.bicep
//
// UNUSED as of ADR-0002 amendment 2026-07-02. Kept for one PR cycle; scheduled
// for deletion. See docs/architecture/decisions/0002-persistence.md change
// history. The state store moved to Postgres Flexible Server (see
// infra/modules/postgres.bicep); no module in resources.bicep references this
// file any longer, so `az bicep build` will not compile it into main.json.
//
// Storage account + one Azure Files share ("data") for the SQLite database.
// Implements ADR-0002 (SQLite on Azure Files). Container Apps will mount this
// share at /data (wired in containerapp.bicep).
//
// allowSharedKeyAccess is intentionally true: Container Apps SMB volume mounts
// still require the shared key at bind time (managed-identity mount is not yet
// GA for Container Apps as of 2024). The key is passed into the containerapp
// module as an @secure() parameter and never emitted as a top-level output.
//
// COST: <1 EUR/mo (Standard_LRS, 5 GiB share, single-user I/O).

@description('Azure region.')
param location string

@description('Short random token for globally-unique resource names (uniqueString() output).')
@minLength(5)
param resourceToken string

@description('Resource tags.')
param tags object

@description('File share name to mount into the Container App.')
param fileShareName string = 'data'

@description('Quota in GiB for the file share.')
@minValue(1)
@maxValue(102400)
param fileShareQuotaGiB int = 5

resource storage 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: 'st${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowSharedKeyAccess: true
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
    accessTier: 'Hot'
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2024-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    shareDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource fileShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2024-01-01' = {
  parent: fileService
  name: fileShareName
  properties: {
    shareQuota: fileShareQuotaGiB
    accessTier: 'TransactionOptimized'
    enabledProtocols: 'SMB'
  }
}

output storageAccountId string = storage.id
output storageAccountName string = storage.name
output fileShareName string = fileShare.name

@secure()
output storageAccountKey string = storage.listKeys().keys[0].value
