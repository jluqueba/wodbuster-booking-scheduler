// postgres.bicep
//
// Azure Database for PostgreSQL Flexible Server for the WodBuster worker.
// Amends ADR-0002 (2026-07-02): the runtime state store moved from SQLite on
// Azure Files to a managed Postgres 16 Flexible Server. Rationale in the ADR
// change history; short version: SQLite on SMB has file-locking hazards under
// concurrent scheduler + heartbeat writes.
//
// AUTH MODEL (three principals):
//   - `wodbadmin` (password auth): break-glass local admin. Password is stored
//     in Key Vault under `postgres-admin-password` and injected into this
//     module by resources.bicep via kv.getSecret() -> @secure() param.
//   - Operator (Entra User): the human running azd + psql from their laptop.
//     Granted server-admin via the `administrators` child resource so they can
//     create schemas, run migrations, GRANT rights.
//   - Runtime UAMI (Entra ServicePrincipal): the Container App's identity.
//     Enrolled at runtime by the operator via SQL (not Bicep) because the
//     `administrators` child resource is single-instance-per-server in current
//     API versions. That step lives in a follow-up task, not here.
//
// FIREWALL MODEL (ADR-0005):
//   No `AllowAzureServices` (0.0.0.0) rule — that would open the server to
//   every Azure subscription in the world. Instead we allow-list the Container
//   Apps environment's static outbound IP (single IP in Consumption mode) and
//   optionally the operator's home CIDR (defaults to empty).
//
// SKU: Burstable B1ms (1 vCPU, 2 GiB RAM), 32 GiB Premium SSD v1 with
// autogrow. Single AZ (zone 1), no HA, 7-day PITR backup. Fits the
// 15-20 EUR/mo target.

@description('Azure region.')
param location string

@description('Short random token for globally-unique resource names.')
@minLength(5)
param resourceToken string

@description('Resource tags.')
param tags object

@description('Break-glass admin password for the `wodbadmin` login. Injected from Key Vault via kv.getSecret() in resources.bicep. Never appears in the repo or in deployment outputs.')
@secure()
param adminPassword string

@description('Operator UPN (Entra user principal name, e.g. `alice@contoso.com` or a guest #EXT# form). Used as `principalName` on the Entra administrators child. Required — leave empty and Bicep will fail validation, which is the intended behaviour.')
param operatorPrincipalUpn string

@description('Operator Entra objectId. Bound to `objectId` on the administrators child. Same value as `operatorPrincipalId` in resources.bicep.')
param operatorPrincipalObjectId string

@description('Static outbound IPs of the Container Apps managed environment (Consumption profile: single IP per env). One firewall rule is emitted per entry. Empty array means no ACA rule (useful for local `bicep build` runs).')
param containerAppsOutboundIps array = []

@description('Optional operator home IP or /32 CIDR for direct psql access. Empty disables the rule. Wider CIDRs are not supported here — split into multiple GH variables if needed.')
param operatorFirewallCidr string = ''

var serverName = 'pg-${resourceToken}'
var databaseName = 'wodbuster'
var adminLogin = 'wodbadmin'

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: adminLogin
    administratorLoginPassword: adminPassword
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Enabled'
      tenantId: subscription().tenantId
    }
    storage: {
      storageSizeGB: 32
      autoGrow: 'Enabled'
      tier: 'P4'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    availabilityZone: '1'
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource wodbusterDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

resource operatorAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: postgres
  // Resource name must be the Entra objectId of the principal.
  name: operatorPrincipalObjectId
  properties: {
    principalName: operatorPrincipalUpn
    principalType: 'User'
    tenantId: subscription().tenantId
  }
}

// One firewall rule per ACA outbound IP. Consumption profile currently exposes
// a single stable IP per env, but we model it as an array so a future move to
// a dedicated workload profile (which advertises multiple IPs) needs no schema
// change here.
resource acaOutboundRules 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = [for (ip, idx) in containerAppsOutboundIps: {
  parent: postgres
  name: 'aca-outbound-${idx}'
  properties: {
    startIpAddress: ip
    endIpAddress: ip
  }
}]

// Special Azure firewall rule: startIp=endIp=0.0.0.0 means "allow traffic
// from any Azure-internal IP", NOT "allow the whole internet". Required for
// the runtime path because Container Apps Consumption envs do not have a
// stable outbound IP (staticIp is inbound only; the actual egress rotates
// within a nearby subnet). See ADR-0005 "Platform limitation" note. The
// security boundary is Entra ID authentication + server-side TLS, not the
// firewall. Revisit if we ever adopt Workload Profiles envs.
resource allowAzureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgres
  name: 'AllowAllAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// Optional operator home-IP rule. Split into a separate deployment because
// `if (!empty(...))` on array items is not supported: we fake it by only
// emitting the resource when the string is non-empty. Wider CIDRs must be
// pre-split by the operator.
resource operatorFirewallRule 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (!empty(operatorFirewallCidr)) {
  parent: postgres
  name: 'operator-home'
  properties: {
    // Treat the input as either a bare IP or a /32 CIDR: strip anything from
    // the first `/` onward and use the address as both start and end.
    startIpAddress: split(operatorFirewallCidr, '/')[0]
    endIpAddress: split(operatorFirewallCidr, '/')[0]
  }
}

output serverName string = postgres.name
output serverFqdn string = postgres.properties.fullyQualifiedDomainName
output databaseName string = wodbusterDb.name
output adminLogin string = adminLogin
output serverId string = postgres.id
