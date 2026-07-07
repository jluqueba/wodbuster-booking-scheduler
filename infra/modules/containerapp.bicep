// containerapp.bicep
//
// Container Apps environment + the single Container App revision that runs
// the WodBuster worker. Implements:
//   ADR-0001  min-replicas=1, max-replicas=1, single revision, no scale-to-zero.
//   ADR-0002  Postgres 16 Flexible Server for state (amended 2026-07-02, was
//             SQLite on Azure Files). Connection is passwordless: the runtime
//             UAMI authenticates to Postgres via Entra; only host/port/db/user
//             are injected as env vars here.
//   ADR-0005  User-assigned managed identity for Key Vault reads, ACR pull,
//             and Postgres login; no admin credentials on any dependency.
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

@description('Name of the runtime user-assigned managed identity. Used as the `POSTGRES_USER` value so the worker can bind to a matching Postgres role that was pre-provisioned by the operator (see ADR-0002 amendment). This is the UAMI *name*, not its clientId.')
param identityName string

@description('Client ID (application/appId) of the runtime UAMI. Injected as `AZURE_CLIENT_ID` so azure-identity SDK`s ManagedIdentityCredential can disambiguate which identity to request a token for. Container Apps does not set this automatically.')
param identityClientId string

@description('Container Registry login server (used for image pull via UAMI + AcrPull).')
param registryLoginServer string

@description('Container image tag to run. Empty on the very first bootstrap (before `azd deploy` has pushed anything); in that case the module falls back to the public hello-world image so the resource can still be created. On subsequent provisions the caller passes the currently-deployed image (via `SERVICE_WORKER_IMAGE_NAME`) so `azd provision` no longer reverts the running image to hello-world. See F3.15.')
param containerImage string = ''

// Public bootstrap image used only when the caller has nothing to pass in.
// Kept as a local var (not the param default) so the effective value is
// derived deterministically and the `emptiness` check lives close to where
// the value is consumed.
var effectiveContainerImage = empty(containerImage) ? 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest' : containerImage

@description('Fully qualified DNS name of the Postgres Flexible Server (e.g. `pg-<token>.postgres.database.azure.com`). Injected as `POSTGRES_HOST`. TLS is enforced server-side; no client-side sslmode override is needed.')
param postgresServerFqdn string

@description('Postgres database name. Injected as `POSTGRES_DB`.')
param postgresDatabase string

@description('Application Insights connection string. Injected into the container as a Container Apps secret referenced from env.')
@secure()
param appInsightsConnectionString string

@description('Key Vault URI. Passed to the worker as KEY_VAULT_URL so the app can resolve secrets at startup via DefaultAzureCredential.')
param keyVaultUri string

@description('OAuth 2.0 client ID for Microsoft (personal accounts). Non-secret; the paired client secret lives in Key Vault as `oauth-microsoft-client-secret`. Empty string if OAuth is not yet configured — the worker still boots.')
param oauthMicrosoftClientId string = ''

@description('OAuth 2.0 client ID for GitHub. Non-secret; the paired client secret lives in Key Vault as `oauth-github-client-secret`. Empty string if OAuth is not yet configured.')
param oauthGithubClientId string = ''

@description('OAuth 2.0 client ID for Google. Non-secret; the paired client secret lives in Key Vault as `oauth-google-client-secret`. Empty string if OAuth is not yet configured.')
param oauthGoogleClientId string = ''

@description('WodBuster gym subdomain slug (e.g. `antworktrainingcenter`). Non-secret; injected as the `WODBUSTER_GYM` env var so the `WodBusterClient` can build the gym-scoped URL for `LoadClass.ashx`. Empty string leaves the client unwired and the `/cookie` route returns 503.')
param wodbusterGym string = ''

@description('WodBuster operator identifier (Phase 0 `idu`). Non-secret; injected as the `WODBUSTER_IDU` env var. Empty string leaves the client unwired.')
param wodbusterIdu string = ''

@description('Target port the container listens on.')
@minValue(1)
@maxValue(65535)
param targetPort int = 8000

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
        // The seven operator secrets (cookie_encryption_key,
        // session_encryption_secret, telegram_bot_token, three oauth
        // client secrets, healthchecks_ping_url) are intentionally NOT
        // exposed as Container Apps secretRefs. Per ADR-0005 the worker
        // fetches them from Key Vault at startup with DefaultAzureCredential
        // (see security/keyvault.py). Only KEY_VAULT_URL and the UAMI
        // binding are required here.
      ]
    }
    template: {
      revisionSuffix: ''
      containers: [
        {
          name: 'worker'
          image: effectiveContainerImage
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
              name: 'LOG_LEVEL'
              value: 'INFO'
            }
            {
              name: 'KEY_VAULT_URL'
              value: keyVaultUri
            }
            {
              name: 'POSTGRES_HOST'
              value: postgresServerFqdn
            }
            {
              // Literal 5432: Flexible Server does not support port overrides,
              // and the worker's psycopg config expects a string here.
              name: 'POSTGRES_PORT'
              value: '5432'
            }
            {
              name: 'POSTGRES_DB'
              value: postgresDatabase
            }
            {
              // Postgres role name that the runtime UAMI logs in as. Set to
              // the UAMI name (not clientId) — the operator's post-provision
              // SQL bootstrap CREATE ROLE uses the same string.
              name: 'POSTGRES_USER'
              value: identityName
            }
            {
              // Client ID of the runtime UAMI. Required by
              // `DefaultAzureCredential` / `ManagedIdentityCredential` to
              // disambiguate which identity to use for IMDS token
              // acquisition. Container Apps binds the UAMI to the revision
              // but does not expose AZURE_CLIENT_ID automatically, so the
              // Python azure-identity SDK falls back to system-assigned
              // (which we do not have) and fails without this hint.
              name: 'AZURE_CLIENT_ID'
              value: identityClientId
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              secretRef: 'applicationinsights-connection-string'
            }
            {
              // OAuth 2.0 client IDs for the three identity providers wired
              // in US-009. Non-secret (paired secrets live in Key Vault and
              // are loaded at startup by security/keyvault.py). Empty
              // strings are accepted so a partial provider setup still
              // boots the worker; only the login path for the unconfigured
              // provider fails at request time.
              name: 'OAUTH_MICROSOFT_CLIENT_ID'
              value: oauthMicrosoftClientId
            }
            {
              name: 'OAUTH_GITHUB_CLIENT_ID'
              value: oauthGithubClientId
            }
            {
              name: 'OAUTH_GOOGLE_CLIENT_ID'
              value: oauthGoogleClientId
            }
            {
              // WodBuster tenant coordinates for US-003. Non-secret;
              // both are empty until the operator publishes the GH
              // Actions variables. When either is empty the runtime
              // `_build_cookie_stack` leaves `WodBusterClient` /
              // `CookieValidator` as `None`, and `/cookie` returns 503.
              name: 'WODBUSTER_GYM'
              value: wodbusterGym
            }
            {
              name: 'WODBUSTER_IDU'
              value: wodbusterIdu
            }
            {
              // Predictable Container Apps FQDN pattern: <appName>.<env defaultDomain>.
              // Avoids the self-reference cycle of properties.configuration.ingress.fqdn.
              name: 'APP_BASE_URL'
              value: 'https://ca-${resourceToken}.${managedEnvironment.properties.defaultDomain}'
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

output containerAppId string = containerApp.id
output containerAppName string = containerApp.name
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output containerAppEndpoint string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output managedEnvironmentId string = managedEnvironment.id

// Static outbound IP of the managed environment (Consumption profile: single
// stable IP per env). resources.bicep pipes this into the Postgres firewall so
// the worker's egress is allow-listed without opening AllowAzureServices.
output managedEnvironmentStaticIp string = managedEnvironment.properties.staticIp
