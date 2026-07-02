# IaC Tooling

**Status**: Proposed
**Date**: 2026-06-29

## Context

The worker is hosted on Azure (ADR-0001) with a small set of resources: one resource group, one Container Apps environment, one Container App, one Azure Container Registry (Basic), one Azure Files share, one Key Vault, one user-assigned managed identity, one Application Insights workspace, one Azure Monitor alert rule, and one Log Analytics workspace. Single environment (`prod`). Single region.

The infrastructure footprint is small and stable. The operator is also the developer. The deployment cadence is low (PR merges to `main`, not continuous).

## Priorities and Requirements (ordered)

1. **Reproducible from a clean subscription** by a single command. `azd up` against a checked-in template.
2. **First-party Azure tooling consistency** with the rest of the stack (Container Apps, Key Vault, Azure Files, Application Insights).
3. **Low cognitive load** for a one-person project. No second IaC language to keep current, no state-file management.
4. **GitHub Actions is the provisioning source of record**. Both `azd provision` (infra) and `azd deploy` (app) run from Actions after bootstrap. Laptop `azd provision` is a bootstrap-only escape hatch for a clean subscription (no deploy identity exists yet); after cutover it is forbidden by convention. See ADR-0005 for the deploy identity.
5. **Monthly tooling cost: zero**. IaC tooling itself must not introduce a billing line item.

## Options Considered

### Option 1: Bicep modules orchestrated by an azd template, single environment, single resource group

A `main.bicep` at the repo root defines all resources. An `azure.yaml` ties the Bicep template to one Container App service. `azd up` provisions the resource group and resources on first run, then `azd deploy` builds the container image with `az acr build` and updates the Container App revision. One environment named `prod`. State is held by Azure Resource Manager.

**Evaluation against priorities**:
- **Reproducible from a clean subscription**: Meets. `azd init` plus `azd up` against a clean subscription produces the full stack.
- **First-party Azure tooling consistency**: Meets. Bicep is the Microsoft-native IaC for Azure Resource Manager.
- **Low cognitive load**: Meets. One language (Bicep), one config file (`azure.yaml`), no state files, no provider plugins.
- **GitHub Actions compatible**: Meets. `azd` has a published GitHub Action; `az` CLI is also available.
- **Monthly tooling cost**: Meets. Bicep and `azd` are free. State lives in Azure Resource Manager.

### Option 2: Terraform with a remote backend on Azure Storage, GitHub Actions for plan and apply

`main.tf` plus modules under `infra/`. A storage account holds the Terraform state with a lock blob. GitHub Actions runs `terraform plan` on PRs and `terraform apply` on merge to `main`.

**Evaluation against priorities**:
- **Reproducible from a clean subscription**: Meets, but requires bootstrap of the state backend before the first apply.
- **First-party Azure tooling consistency**: Partially meets. Terraform's AzureRM provider lags Bicep on new resource shapes by weeks to months and occasionally introduces breaking provider releases.
- **Low cognitive load**: Fails. Two languages (Terraform plus shell), provider pinning, state-file management, lock-conflict recovery, and a bootstrap step for the state backend. None of this pays for itself at this footprint.
- **GitHub Actions compatible**: Meets.
- **Monthly tooling cost**: Borderline. Terraform itself is free; the state-backend storage account costs a few cents per month.

### Option 3: Portal click-ops plus `az` CLI scripts in a shell file

Provisioning is documented as a sequence of `az` commands in `scripts/provision.sh`. The operator runs the script once. Drift is caught by visual inspection.

**Evaluation against priorities**:
- **Reproducible from a clean subscription**: Partially meets. Shell scripts work but lack idempotency guarantees; re-runs can produce inconsistent state.
- **First-party Azure tooling consistency**: Meets.
- **Low cognitive load**: Fails at the second provision. Tracking incremental changes via shell-script diffs is fragile.
- **GitHub Actions compatible**: Meets.
- **Monthly tooling cost**: Meets.

## Decision

Bicep modules orchestrated by an `azd` template, with a single environment named `prod` deployed to a single resource group. First-time provisioning uses `azd up` from the operator's laptop as a one-time bootstrap on a clean subscription. After bootstrap, provisioning and application deploys both run from GitHub Actions: `.github/workflows/infra.yml` runs `azd provision` on pushes to `main` that touch `infra/**` or `azure.yaml`; `.github/workflows/deploy.yml` runs `azd deploy` on pushes to `main` that touch `src/**` or the `Dockerfile`. A third workflow runs `azd provision --preview` on pull requests touching infra and posts the what-if diff as a PR comment. Both mutating workflows authenticate to Azure via OIDC federation against a dedicated deploy user-assigned managed identity described in ADR-0005. State is held by Azure Resource Manager.

This option uniquely meets every priority. Terraform adds tooling weight unjustified by the footprint. Portal plus shell scripts cannot keep up with re-provisioning hygiene.

## Implementation Notes

- Repository layout:

  | Path | Purpose |
  |------|---------|
  | `azure.yaml` | `azd` project descriptor. |
  | `infra/main.bicep` | Resource group scope; orchestrates module includes. |
  | `infra/modules/` | One file per resource family: `containerapp.bicep`, `keyvault.bicep`, `acr.bicep`, `storage.bicep`, `monitoring.bicep`, `identity.bicep`. |
  | `infra/main.parameters.json` | Per-environment parameter file for `prod`. |

- Resources are deployed to a single resource group named `rg-wodbuster-booking-worker-prod` (or operator-chosen equivalent passed via `azd env`).
- The Bicep template uses `az identity` and RBAC role assignments to bind the user-assigned managed identity to the Container App (ADR-0005). All role assignments are declared in IaC, never in the portal.
- Container image build path: GitHub Actions step calls `az acr build` against the provisioned ACR Basic. The Container App revision is updated by `azd deploy`.
- Azure Files share for the SQLite database (ADR-0002) is provisioned by the storage module and bound to the Container App as a volume mount.
- Resource naming follows the `{kind}-{project}-{env}` convention (for example `ca-wodbuster-prod`, `kv-wodbuster-prod`, `cr-wodbuster-prod`, `st-wodbuster-prod`).
- Drift detection: `azd` is run only against the template; the portal is read-only for the operator. Any divergence triggers a re-apply.
- Cost ballpark: ACR Basic approximately 4 EUR per month. Container Apps environment approximately 8 to 12 EUR per month for the always-on replica. Azure Files Standard 5 GiB ZRS under 1 EUR per month. Key Vault standard under 1 EUR per month. Application Insights and Log Analytics under 1 EUR per month at single-user log volume. Total provisioned footprint approximately 15 to 20 EUR per month.
- CI/CD topology (GitHub Actions):

  | Workflow | Trigger | Action | Auth |
  |----------|---------|--------|------|
  | `.github/workflows/infra.yml` | Push to `main` touching `infra/**` or `azure.yaml`; `workflow_dispatch`. | `azd provision`. No approval gate for MVP; applies immediately. Operator accepts the risk. | OIDC against deploy UAMI (`main` federated credential). |
  | `.github/workflows/deploy.yml` | Push to `main` touching `src/**` or `Dockerfile`; `workflow_dispatch`. | Build image, push to ACR, `azd deploy`. | OIDC against deploy UAMI (`main` federated credential). |
  | `.github/workflows/infra-preview.yml` | `pull_request` touching `infra/**` or `azure.yaml`. | `azd provision --preview`; post what-if diff as PR comment. Read-only. | OIDC against deploy UAMI (`pull_request` federated credential). |

- No approval gate on the mutating workflows for the MVP. The operator is the sole developer and reviewer; a gate would only slow the loop. Revisit when the surface grows or a second contributor joins.
- Fork PRs are not supported. The `pull_request` federated credential is scoped to the repository owner. Fork contributors would need repo-owner review of the branch before the preview workflow runs.
- Laptop `azd provision` is forbidden by convention after F3.10 (the deploy UAMI creation task) lands. Documented in `README.md`. The only sanctioned laptop provisioning is the initial bootstrap on a clean subscription where no deploy UAMI exists yet.

## References

- `docs/features/wodbuster-booking-worker/spec.md` Invariants (encryption key custody, single-replica writer).
- `docs/envisioning/wodbuster-booking-scheduler.md` section 8 (locked technical choices).
- `docs/architecture/decisions/0001-hosting-service.md` for the Container App definition.
- `docs/architecture/decisions/0002-persistence.md` for the Azure Files share.
- `docs/architecture/decisions/0005-secrets-and-identity-access.md` for the Key Vault and managed identity.
- `docs/architecture/decisions/0006-observability-and-heartbeat.md` for App Insights and the Azure Monitor alert.
