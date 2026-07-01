// containerapp.bicep
//
// Container Apps environment + the single Container App revision that runs
// the WodBuster worker. Implements:
//   ADR-0001  min-replicas=1, max-replicas=1, single revision, no scale-to-zero.
//   ADR-0002  Azure Files share mounted at /data for the SQLite database.
//   ADR-0005  User-assigned managed identity for Key Vault reads and ACR pull;
//             no admin credentials on the registry, no client secrets anywhere.
//
// COST: ~10-12 EUR/mo for one always-on 0.25 vCPU / 0.5 GiB replica on the
//       Consumption profile in westeurope.

@description('Azure region.')
param location string

@description('Short random token for globally-unique resource names (uniqueString() output).')
@minLength(5)
param resourceToken string

@description('Resource tags.')
param tags object

@description('Log Analytics workspace ID for the Container Apps environment.')
param logAnalyticsWorkspaceId string

@description('User-assigned managed identity resource ID (bound to the app for KV + ACR).')
param identityId string

@description('Container Registry login server (used for image pull via UAMI + AcrPull).')
param registryLoginServer string

@description('Container image tag to run. Defaults to the hello-world image so the very first azd up succeeds before the real image is pushed. Overridden by azd deploy.')
param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Storage account name backing the Azure Files share for SQLite (ADR-0002).')
param storageAccountName string

@description('Azure Files share name (typically "data"). Mounted at /data in the container.')
param fileShareName string

@description('Storage account primary key. Passed via @secure so it never leaks into deployment logs or outputs. Consumed only by the managed environment SMB bind.')
@secure()
param storageAccountKey string

@description('Application Insights connection string. Injected into the container as a Container Apps secret referenced from env.')
@secure()
param appInsightsConnectionString string

@description('Key Vault URI. Passed to the worker as KEY_VAULT_URL so the app can resolve secrets at startup via DefaultAzureCredential.')
param keyVaultUri string

@description('Target port the container listens on.')
@minValue(1)
@maxValue(65535)
param targetPort int = 8000

// Named alias for the managed environment storage entry that the volume binds to.
var envStorageName = 'data'

resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-10-02-preview' = {
  name: 'cae-${resourceToken}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: reference(logAnalyticsWorkspaceId, '2023-09-01').customerId
        sharedKey: listKeys(logAnalyticsWorkspaceId, '2023-09-01').primarySharedKey
      }
    }
    // NOTE: zone redundancy off (single-region, no zone budget for the
    // 15-20 EUR/mo target per ADR-0001).
    zoneRedundant: false
  }
}

resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-10-02-preview' = {
  parent: managedEnvironment
  name: envStorageName
  properties: {
    azureFile: {
      accountName: storageAccountName
      accountKey: storageAccountKey
      shareName: fileShareName
      accessMode: 'ReadWrite'
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: 'ca-${resourceToken}'
  location: location
  // azd requires this tag on the Container App to locate the deploy target.
  tags: union(tags, {
    'azd-service-name': 'worker'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: managedEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: targetPort
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: registryLoginServer
          identity: identityId
        }
      ]
      secrets: [
        {
          name: 'applicationinsights-connection-string'
          value: appInsightsConnectionString
        }
        // TODO(F3.8+F2.2): add Key-Vault-backed secretRefs for the seven
        // operator secrets once F3.8 seeds them:
        //   wodbuster-cookie-encryption-key, session-encryption-secret,
        //   telegram-bot-token, oauth-microsoft-client-secret,
        //   oauth-github-client-secret, oauth-google-client-secret,
        //   healthchecks-ping-url.
        // Pattern (Container Apps native Key Vault reference):
        //   {
        //     name: '<secret-name>'
        //     keyVaultUrl: '${keyVaultUri}secrets/<secret-name>'
        //     identity: identityId
        //   }
        // Then reference each via secretRef in the env block below.
      ]
    }
    template: {
      revisionSuffix: ''
      containers: [
        {
          name: 'worker'
          image: containerImage
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            {
              name: 'WODBUSTER_ENV'
              value: 'prod'
            }
            {
              name: 'SQLITE_PATH'
              value: '/data/wodbuster.db'
            }
            {
              name: 'LOG_LEVEL'
              value: 'INFO'
            }
            {
              name: 'KEY_VAULT_URL'
              value: keyVaultUri
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              secretRef: 'applicationinsights-connection-string'
            }
            {
              // Predictable Container Apps FQDN pattern: <appName>.<env defaultDomain>.
              // Avoids the self-reference cycle of properties.configuration.ingress.fqdn.
              name: 'APP_BASE_URL'
              value: 'https://ca-${resourceToken}.${managedEnvironment.properties.defaultDomain}'
            }
          ]
          volumeMounts: [
            {
              volumeName: 'data'
              mountPath: '/data'
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      volumes: [
        {
          name: 'data'
          storageType: 'AzureFile'
          storageName: envStorage.name
        }
      ]
    }
  }
}

output containerAppId string = containerApp.id
output containerAppName string = containerApp.name
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output containerAppEndpoint string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output managedEnvironmentId string = managedEnvironment.id
