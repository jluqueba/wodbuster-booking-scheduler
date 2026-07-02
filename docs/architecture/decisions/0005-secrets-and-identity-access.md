# Secrets and Identity Access

**Status**: Proposed
**Date**: 2026-06-29

## Context

This ADR covers two concerns that are easiest to decide together because both ride the same Azure identity surface:

1. **Worker access to secrets**: cookie encryption key (ADR-0003), Telegram bot token, OAuth client secrets for the operator sign-in flow, session encryption secret for Starlette `SessionMiddleware`. None of these can live in the repository, environment variables baked at deploy time, or container image layers.
2. **Operator authentication to the web UI**: FR-028 mandates federated identity via personal Microsoft, GitHub, or Google account. FR-029 forbids local password storage. FR-030 mandates an allow-list of operator identities.

Locked technical choices from envisioning section 8: Azure Key Vault for the cookie encryption key, Telegram bot token, and any internal API tokens. WodBuster credentials are NOT stored (envisioning constraint 6, ADR-0003).

## Priorities and Requirements (ordered)

1. **No secrets in repository, environment files, or container image**. All secret material lives in a managed vault.
2. **No service principal client secrets for the worker**. The worker's identity to Azure is keyless.
3. **Federated identity for the operator with multi-provider choice**. The operator picks Microsoft personal, GitHub, or Google at sign-in (FR-028). No local password handling (FR-029).
4. **Allow-list enforcement** after federated sign-in (FR-030). Identities outside the allow-list are denied without leaking operator data.
5. **Auditable secret access** and **negligible cost** at single-user MVP scale.

## Options Considered

### Option 1: Azure Key Vault with user-assigned managed identity, multi-provider federated operator login

The Container App is assigned a user-assigned managed identity (UAMI). The UAMI holds RBAC role `Key Vault Secrets User` on a single Key Vault. At startup the worker reads four secrets via Azure SDK: `wodbuster-cookie-encryption-key`, `telegram-bot-token`, `session-encryption-secret`, and one OAuth client-secret triple `oauth-{provider}-client-secret` for each of Microsoft, GitHub, Google. The OAuth client IDs are non-secret and live in app configuration. Operator sign-in offers all three providers; the operator picks one at sign-in. After the callback completes, the federated subject identifier is matched against an allow-list configured in the worker's database (one row per allowed `(provider, subject_id, display_name)`).

**Evaluation against priorities**:
- **No secrets in repository or image**: Meets. All secrets read at startup via managed identity. The image is generic.
- **No service principal client secrets for the worker**: Meets. UAMI authenticates to Key Vault via the IMDS endpoint. No client secret material on the worker side.
- **Federated identity with multi-provider choice**: Meets. Three providers, operator chooses, matching FR-028 verbatim.
- **Allow-list enforcement**: Meets. The allow-list lives in the Postgres database (ADR-0002) and is checked in the OAuth callback handler. Mismatched identities receive a generic denial.
- **Auditable secret access and negligible cost**: Meets. Key Vault standard tier secret reads cost approximately 0.03 EUR per 10,000 operations; the worker reads four secrets at startup and caches them for the process lifetime. Diagnostic logs to App Insights record every secret access. Total cost is well under 1 EUR per month.

### Option 2: Environment variables baked as Container App secrets at deploy time

ACA's "secrets" feature stores secret values per environment. `azd deploy` writes them. The application reads them as environment variables.

**Evaluation against priorities**:
- **No secrets in repository or image**: Meets at the image layer but fails at the configuration layer. Secret values pass through the deployment pipeline and end up in environment-variable metadata visible to anyone with Reader on the Container App.
- **No service principal client secrets for the worker**: Partially meets at runtime but the deployment pipeline still needs to hold the values to inject them.
- **Federated identity with multi-provider choice**: Independent of this option.
- **Allow-list enforcement**: Independent of this option.
- **Auditable secret access**: Fails. Environment variables do not produce per-read audit events. Key Vault does.

### Option 3: Single-provider federated identity (Microsoft only) plus Azure Key Vault for the rest

Same Key Vault and UAMI as Option 1, but the operator sign-in flow is wired only to Microsoft personal accounts. GitHub and Google are dropped to reduce OAuth client-secret count by two.

**Evaluation against priorities**:
- **No secrets in repository or image**: Meets, same as Option 1.
- **No service principal client secrets for the worker**: Meets, same as Option 1.
- **Federated identity with multi-provider choice**: Fails. FR-028 explicitly names Microsoft, GitHub, and Google. Restricting to one provider contradicts the spec.
- **Allow-list enforcement**: Meets, same as Option 1.
- **Auditable secret access and negligible cost**: Meets, same as Option 1.

## Decision

Option 1: Azure Key Vault for secrets (cookie encryption key, Telegram bot token, session encryption secret, OAuth client secrets for Microsoft personal plus GitHub plus Google, plus the `postgres-admin-password` break-glass secret introduced by the 2026-07-02 persistence pivot), accessed by the Container App via a user-assigned managed identity holding the `Key Vault Secrets User` RBAC role. Operator authentication via federated OAuth with all three providers offered at sign-in; the operator picks one. The allow-list lives in the Postgres database (ADR-0002), one row per allowed `(provider, subject_id, display_name)`. Mismatched identities receive a generic denial.

This option uniquely meets every priority. Environment-variable baking (Option 2) fails the audit requirement. A single-provider restriction (Option 3) contradicts FR-028.

## Implementation Notes

- The UAMI is provisioned by the Bicep template (ADR-0007) and assigned to the Container App. Role assignment is scoped to the Key Vault.
- Key Vault standard tier with RBAC authorization (not access policies). Soft delete enabled.
- Secret names are stable and committed in configuration: `wodbuster-cookie-encryption-key`, `telegram-bot-token`, `session-encryption-secret`, `oauth-microsoft-client-secret`, `oauth-github-client-secret`, `oauth-google-client-secret`.
- OAuth client IDs are non-secret and live in app configuration; only the client secrets live in Key Vault.
- The OAuth flow uses Authlib or equivalent against:
  - Microsoft personal accounts: tenant `consumers`, scopes `openid profile email`.
  - GitHub: standard OAuth app, scope `read:user`.
  - Google: scopes `openid email profile`.
- The federated subject identifier used for matching is the provider-stable, opaque `sub` claim (Microsoft, Google) or the numeric user ID (GitHub).
- The allow-list is seeded at first-run via a one-time bootstrap command that adds the operator's chosen identity. Subsequent additions are gated by the existing operator's authenticated session.
- The Telegram bot token is one-time set up via BotFather. The token value is pasted into the Key Vault secret `telegram-bot-token` by the operator at provisioning time. This is documented as a manual step in `plan.md`.
- Secret rotation is operator-initiated. Application caches secrets in memory for the process lifetime; a container revision restart picks up rotated values.
- Cost ballpark: under 1 EUR per month for Key Vault standard at this read cadence. No additional cost for the UAMI itself.

### CI/CD identity (deploy UAMI)

Two user-assigned managed identities exist in the subscription, with disjoint responsibilities:

| Identity | Consumer | Roles | Scope | Provisioned by |
|----------|----------|-------|-------|----------------|
| Runtime UAMI (`id-{token}`) | Container App revision at runtime. | `Key Vault Secrets User`, `AcrPull`. | Key Vault, Container Registry. | Bicep template (ADR-0007). Unchanged by this amendment. |
| Deploy UAMI (`id-deploy-wodbuster-prod`) | GitHub Actions workflows (`infra.yml`, `deploy.yml`, `infra-preview.yml`). | `Contributor`, `User Access Administrator`. | Resource group only. No subscription-scope permissions. | Operator manual setup (task F3.10). |

The deploy UAMI carries two federated credentials, both pointing at the repository `jluqueba/wodbuster-booking-scheduler`:

| Subject | Purpose |
|---------|---------|
| `repo:jluqueba/wodbuster-booking-scheduler:ref:refs/heads/main` | Consumed by `infra.yml` and `deploy.yml`. |
| `repo:jluqueba/wodbuster-booking-scheduler:pull_request` | Consumed by `infra-preview.yml` for read-only what-if. |

No environment-scoped federated credential is configured. Fork PRs are unsupported.

The deploy UAMI is deliberately excluded from the Bicep template: it is a prerequisite for the provisioning workflow, so it cannot be provisioned by that workflow (chicken-and-egg). Resource-group creation and deploy-UAMI creation both stay one-time laptop steps during bootstrap. The runtime UAMI stays inside the Bicep template.

The deploy UAMI holds no client secret. All GitHub-to-Azure authentication uses the OIDC token exchange, with `AZURE_CLIENT_ID`, `AZURE_SUBSCRIPTION_ID`, and `AZURE_TENANT_ID` published as GitHub Actions repository variables (non-secret). This preserves the "no service principal client secrets" priority for the CI/CD path as well as the runtime path.

### Postgres connection identity

The persistence pivot in ADR-0002 introduces a Postgres server that carries three principals with disjoint responsibilities. The identity plumbing lives here; the in-database privilege set lives in ADR-0002.

| Principal | Type | Where the identity lives | Who provisions it |
|-----------|------|-------------------------|-------------------|
| `wodbadmin` | Postgres password login (server admin). | Password in Key Vault secret `postgres-admin-password`. Never mounted into the Container App. | Bicep creates the server with this login and password (from the `@secure()` parameter sourced from the KV secret). Operator seeds the KV secret before the first provision (F3.13). |
| Operator's Entra user | Microsoft Entra ID user or group. | Operator's Azure account. No secret material. | Operator, one time, via `az postgres flexible-server ad-admin create` (F3.12). Also declared as the server's Entra admin in Bicep so the same value round-trips through IaC. |
| Runtime UAMI (`id-{token}`) | Microsoft Entra managed identity. | Provisioned by Bicep alongside the Container Registry and Key Vault role assignments (unchanged from the original design). Attached to the Container App revision. | Bicep, plus one manual step: the operator adds the UAMI as an **Entra admin** on the Postgres server via `az postgres flexible-server microsoft-entra-admin create --type ServicePrincipal` (F3.12). See platform-limitation note below. |

**Platform limitation (2026-07-02 amendment)**: the original design assumed a limited (non-admin) Entra role for the runtime UAMI, with schema-scoped `GRANT USAGE, CREATE ON SCHEMA public`. Azure Database for PostgreSQL Flexible Server (verified on API `2024-08-01`, PG 16, Spain Central, Burstable tier) does not expose the `pgaadauth` extension to user control: `azure.extensions` allow-list omits it and `CREATE EXTENSION pgaadauth` is refused with "not allow-listed for `azure_pg_admin` users". The `az postgres flexible-server microsoft-entra-admin create` command is the only documented path to enrol an Entra principal, and it always adds the principal as a member of the `azure_pg_admin` group (server-admin equivalent). Consequences accepted for the MVP:

- Runtime UAMI has effective superuser rights on the `wodbuster` database.
- Blast radius is bounded by the fact that the UAMI is scoped to a single Container App revision, itself scoped to a single subscription and resource group. A compromised UAMI cannot reach other databases, tenants, or Azure services beyond what its role assignments already grant.
- The `wodbadmin` password login remains the break-glass path (KV-stored) but is no longer strictly required for schema recovery, since the operator's own Entra admin covers the same surface.
- The "operator-owns-schema, runtime-owns-data" inversion described below in the "Rationale for the split" subsection is aspirational and not enforced today. Revisit if Azure re-exposes `pgaadauth`.

Runtime connection flow:

1. Container App revision starts. `DefaultAzureCredential` resolves to the runtime UAMI via IMDS.
2. SQLAlchemy engine acquires a Postgres connection. The custom connection callback calls `credential.get_token("https://ossrdbms-aad.database.windows.net/.default")` and passes the resulting access token as the `password` field of the libpq connection string, with the UAMI's principal name as the `user`.
3. Postgres validates the token against Entra ID and authorises the connection under the role name equal to the UAMI's principal name.
4. Alembic runs `upgrade head` under the same connection identity. It succeeds only if the operator has previously executed the F3.12 grant.

Rationale for the split:

- The Container App holds no password. The `postgres-admin-password` secret exists solely for the case where Entra auth is broken (identity provider outage, federation misconfiguration). Cover story, not an operational path.
- The runtime UAMI holds strictly less than the Entra DBA. DDL executed at container start is bounded to schema `public` (Alembic's target). Schema-level catastrophes (`DROP SCHEMA`) require the DBA session, not the runtime session.
- The operator's Entra user, not the runtime UAMI, owns the schema. This inversion means restoring from a bad migration does not require the app to be running: the operator connects with psql via the Entra token and repairs.

Firewall rule policy: the Postgres server keeps public network access enabled with two rule sets:

| Rule | Source | Purpose |
|------|--------|---------|
| Container Apps environment outbound IPs | The static outbound IP ranges published for the ACA environment. | Runtime traffic from the worker. |
| Operator's home IP (or dynamic set) | Operator-managed. Documented in the README as an operator-owned step. | psql access from the DBA laptop. |

`AllowAzureServices` is deliberately not enabled: leaving it on would grant any Azure tenant on the platform the ability to reach the server through the firewall, which defeats the purpose of the rule set at the single-user scale.

## References

- `docs/features/wodbuster-booking-worker/spec.md` FR-028 through FR-031, Invariants on cookie encryption key custody.
- `docs/envisioning/wodbuster-booking-scheduler.md` section 8 (secrets entry).
- `docs/architecture/decisions/0001-hosting-service.md` for the Container App identity attachment.
- `docs/architecture/decisions/0002-persistence.md` for the allow-list storage.
- `docs/architecture/decisions/0003-auth-and-session.md` for the cookie encryption key consumer.
- `docs/architecture/decisions/0004-configuration-interface.md` for the OAuth-callback routes.
