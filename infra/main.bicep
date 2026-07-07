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

@description('Object ID of the human operator (empty in CI initial run). Wired to the runtime UAMI Key Vault Secrets Officer grant so the operator can seed F3.8 secrets. Named `operatorPrincipalId` on purpose: azd auto-populates `AZURE_PRINCIPAL_ID` with the OIDC caller (deploy UAMI when run from GitHub Actions), which would misfire the role assignment. This param binds to `AZURE_OPERATOR_PRINCIPAL_ID` instead, which azd leaves alone.')
param operatorPrincipalId string = ''

@description('UPN of the human operator (Entra user principal name, e.g. `alice@contoso.com` or a guest #EXT# form). Required. Bound to `AZURE_OPERATOR_PRINCIPAL_UPN`. Empty value causes Bicep validation to fail, which is intentional. The Postgres Entra administrators child requires it.')
param operatorPrincipalUpn string

@description('Object ID of the deploy user-assigned managed identity used by GitHub Actions. Bound to `AZURE_DEPLOY_PRINCIPAL_ID` with the `=` fallback so empty local runs still validate. When non-empty, main.bicep resolves `postgres-admin-password` from Key Vault via `kv.getSecret()` at deploy time; the same identity is granted Key Vault Secrets User inside keyvault.bicep so it can read the secret.')
param deployPrincipalId string = ''

@description('Optional operator home IP or /32 CIDR to grant direct psql access to the Postgres server. Empty disables the rule. Bound to `AZURE_OPERATOR_FIREWALL_CIDR` (with `=` fallback).')
param operatorFirewallCidr string = ''

@description('Static outbound IPs of the Container Apps managed environment, one per array entry. Populated after the first provision (chicken-and-egg with `managedEnvironment.properties.staticIp`). Bound to `AZURE_ACA_ENVIRONMENT_OUTBOUND_IPS` with an empty default; the operator reads `managedEnvironmentStaticIp` from run 1 outputs and sets the GH variable before run 2.')
param acaOutboundIps array = []

@description('OAuth 2.0 client ID for Microsoft (personal accounts). Non-secret; bound to `OAUTH_MICROSOFT_CLIENT_ID` GH variable with an empty fallback. Empty is accepted so the container still boots when OAuth is not fully configured.')
param oauthMicrosoftClientId string = ''

@description('OAuth 2.0 client ID for GitHub. Non-secret; bound to `OAUTH_GITHUB_CLIENT_ID` GH variable with an empty fallback.')
param oauthGithubClientId string = ''

@description('OAuth 2.0 client ID for Google. Non-secret; bound to `OAUTH_GOOGLE_CLIENT_ID` GH variable with an empty fallback.')
param oauthGoogleClientId string = ''

@description('WodBuster gym subdomain slug (e.g. `antworktrainingcenter`). Non-secret; bound to `WODBUSTER_GYM` GH variable. Empty on the first bootstrap; the worker still boots and the `/cookie` route returns 503 until this is set.')
param wodbusterGym string = ''

@description('WodBuster operator identifier discovered in every `/athlete/handlers/*` URL during Phase 0. Non-secret; bound to `WODBUSTER_IDU` GH variable. Empty on the first bootstrap; the `/cookie` route returns 503 until both this and `wodbusterGym` are set.')
param wodbusterIdu string = ''

@description('Container image reference for the worker Container App (registry/image:tag). Bound to `SERVICE_WORKER_IMAGE_NAME` (set by `azd deploy` and, in CI, discovered from the running Container App before `azd provision`). Empty on the very first bootstrap; the child module falls back to the public hello-world image so the resource can be created before anything is pushed. Preserving the tag across provisions is why F3.15 exists (previously each `azd provision` reverted the app to hello-world).')
param containerImage string = ''

var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = {
  'azd-env-name': environmentName
}

// Existing Key Vault reference so kv.getSecret() can pull `postgres-admin-password`
// at deploy time. The vault + secret must exist before the very first postgres
// module deployment. F3.8 seeds all operator secrets, including this one, from
// the operator's laptop before CI ever touches the module.
resource kv 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: 'kv-${resourceToken}'
}

module resources 'resources.bicep' = {
  name: 'resources'
  params: {
    location: location
    resourceToken: resourceToken
    tags: tags
    operatorPrincipalId: operatorPrincipalId
    operatorPrincipalUpn: operatorPrincipalUpn
    deployPrincipalId: deployPrincipalId
    postgresAdminPassword: kv.getSecret('postgres-admin-password')
    acaOutboundIps: acaOutboundIps
    operatorFirewallCidr: operatorFirewallCidr
    oauthMicrosoftClientId: oauthMicrosoftClientId
    oauthGithubClientId: oauthGithubClientId
    oauthGoogleClientId: oauthGoogleClientId
    wodbusterGym: wodbusterGym
    wodbusterIdu: wodbusterIdu
    containerImage: containerImage
  }
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = resourceGroup().name
output AZURE_OPERATOR_PRINCIPAL_ID string = operatorPrincipalId

output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.registryName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.registryLoginServer

output AZURE_KEY_VAULT_NAME string = resources.outputs.keyVaultName
output AZURE_KEY_VAULT_ENDPOINT string = resources.outputs.keyVaultUri

output AZURE_APPLICATION_INSIGHTS_CONNECTION_STRING string = resources.outputs.appInsightsConnectionString

output AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID string = resources.outputs.identityClientId
output AZURE_USER_ASSIGNED_IDENTITY_PRINCIPAL_ID string = resources.outputs.identityPrincipalId
output AZURE_USER_ASSIGNED_IDENTITY_NAME string = resources.outputs.identityName

output AZURE_POSTGRES_SERVER_NAME string = resources.outputs.postgresServerName
output AZURE_POSTGRES_SERVER_FQDN string = resources.outputs.postgresServerFqdn
output AZURE_POSTGRES_DATABASE string = resources.outputs.postgresDatabaseName
output AZURE_POSTGRES_ADMIN_LOGIN string = resources.outputs.postgresAdminLogin

// Container Apps managed environment outbound IP. Read this after run 1 with
// `azd env get-value AZURE_ACA_ENVIRONMENT_STATIC_IP` and set it as the GH
// variable AZURE_ACA_ENVIRONMENT_OUTBOUND_IPS (JSON array) so run 2 can
// populate the Postgres firewall allow-list.
output AZURE_ACA_ENVIRONMENT_STATIC_IP string = resources.outputs.managedEnvironmentStaticIp

output SERVICE_WORKER_ENDPOINT string = resources.outputs.containerAppEndpoint
output SERVICE_WORKER_NAME string = resources.outputs.containerAppName
