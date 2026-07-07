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

@description('UPN of the human operator (Entra user principal name). Bound to the Postgres Entra administrators child so the operator can run migrations and GRANT the runtime UAMI. Required at deploy time; empty causes Bicep to fail, which is intentional.')
param operatorPrincipalUpn string

@description('Object ID of the deploy user-assigned managed identity (GitHub Actions OIDC caller). Granted Key Vault Secrets User so main.bicep can resolve `postgres-admin-password` via kv.getSecret(). Empty in local operator-driven runs (the operator already has Secrets Officer).')
param deployPrincipalId string = ''

@description('Break-glass admin password for the Postgres server `wodbadmin` login. Resolved by main.bicep from Key Vault via kv.getSecret() and forwarded as a @secure() param. Never appears in outputs.')
@secure()
param postgresAdminPassword string

@description('Static outbound IPs of the Container Apps managed environment, one per entry. Sourced from a GH Actions variable populated by the operator after the first successful provision (see docs). Empty on the very first provision. The firewall stays closed until the second run wires the ACA env static IP.')
param acaOutboundIps array = []

@description('Optional operator home IP or /32 CIDR for direct psql access. Empty disables the rule.')
param operatorFirewallCidr string = ''

@description('OAuth 2.0 client ID for Microsoft (personal accounts). Non-secret; passed through to the container as env var. Empty on early provisions before the operator publishes the GH Actions variable.')
param oauthMicrosoftClientId string = ''

@description('OAuth 2.0 client ID for GitHub. Non-secret; passed through to the container as env var. Empty until the operator publishes the GH Actions variable.')
param oauthGithubClientId string = ''

@description('OAuth 2.0 client ID for Google. Non-secret; passed through to the container as env var. Empty until the operator publishes the GH Actions variable.')
param oauthGoogleClientId string = ''

@description('WodBuster gym subdomain slug. Non-secret; forwarded to the container as `WODBUSTER_GYM`. Empty until the operator publishes the GH Actions variable; the `/cookie` route returns 503 in that state.')
param wodbusterGym string = ''

@description('WodBuster operator identifier (Phase 0 `idu`). Non-secret; forwarded to the container as `WODBUSTER_IDU`. Empty until the operator publishes the GH Actions variable.')
param wodbusterIdu string = ''

@description('Container image reference for the worker (registry/image:tag). Forwarded from `main.bicep` (which reads `SERVICE_WORKER_IMAGE_NAME`). Empty on first bootstrap; the child module substitutes a public hello-world image so the Container App can be created before anything is pushed to ACR.')
param containerImage string = ''

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
    deployPrincipalId: deployPrincipalId
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

module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    adminPassword: postgresAdminPassword
    operatorPrincipalUpn: operatorPrincipalUpn
    operatorPrincipalObjectId: operatorPrincipalId
    // Firewall allow-list is populated from a GH Actions variable populated
    // after first provision (chicken-and-egg: the ACA env's static IP is only
    // known once the env exists). See containerApp.outputs.managedEnvironmentStaticIp
    // which the operator reads via `azd env get-value` after run 1 and sets as
    // AZURE_ACA_ENVIRONMENT_OUTBOUND_IP before run 2.
    containerAppsOutboundIps: acaOutboundIps
    operatorFirewallCidr: operatorFirewallCidr
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
    identityName: identity.outputs.identityName
    identityClientId: identity.outputs.identityClientId
    registryLoginServer: registry.outputs.registryLoginServer
    postgresServerFqdn: postgres.outputs.serverFqdn
    postgresDatabase: postgres.outputs.databaseName
    appInsightsConnectionString: observability.outputs.appInsightsConnectionString
    keyVaultUri: keyVault.outputs.keyVaultUri
    oauthMicrosoftClientId: oauthMicrosoftClientId
    oauthGithubClientId: oauthGithubClientId
    oauthGoogleClientId: oauthGoogleClientId
    wodbusterGym: wodbusterGym
    wodbusterIdu: wodbusterIdu
    containerImage: containerImage
  }
}

output logAnalyticsWorkspaceId string = observability.outputs.logAnalyticsWorkspaceId
output appInsightsConnectionString string = observability.outputs.appInsightsConnectionString
output keyVaultName string = keyVault.outputs.keyVaultName
output keyVaultUri string = keyVault.outputs.keyVaultUri
output registryName string = registry.outputs.registryName
output registryLoginServer string = registry.outputs.registryLoginServer
output identityId string = identity.outputs.identityId
output identityPrincipalId string = identity.outputs.identityPrincipalId
output identityClientId string = identity.outputs.identityClientId
output identityName string = identity.outputs.identityName
output containerAppName string = containerApp.outputs.containerAppName
output containerAppEndpoint string = containerApp.outputs.containerAppEndpoint
output managedEnvironmentStaticIp string = containerApp.outputs.managedEnvironmentStaticIp
output postgresServerName string = postgres.outputs.serverName
output postgresServerFqdn string = postgres.outputs.serverFqdn
output postgresDatabaseName string = postgres.outputs.databaseName
output postgresAdminLogin string = postgres.outputs.adminLogin
