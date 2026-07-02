# WodBuster Booking Worker

Unattended booking worker for the WodBuster platform. Single FastAPI ASGI process running on Azure Container Apps with APScheduler, SQLite on Azure Files, and dual-channel notifications (Telegram + web banner).

See `docs/features/wodbuster-booking-worker/` for the feature spec, plan, and tasks, and `docs/architecture/decisions/` for the architectural decisions (ADRs 0001-0007).

## Local development

Requires Python 3.12 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
.\check.ps1
```

On Linux or macOS:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
make check
```

`check` runs `ruff check`, `mypy src`, and `pytest` (excluding the `live_contract` marker).

## Run the app locally

```powershell
uvicorn wodbuster_worker.app:app --reload --port 8000
```

Then `GET http://localhost:8000/health` returns `200 OK`.

## Container build

```powershell
docker build -t wodbuster-worker:dev .
docker run --rm -p 8000:8000 wodbuster-worker:dev
```

## Deployments

Provisioning and application deploys both run from **GitHub Actions** using OIDC federation against a dedicated **deploy user-assigned managed identity** (`id-deploy-wodbuster-prod`), separate from the runtime UAMI. Details in ADR-0005 and ADR-0007.

| Workflow | Trigger | Action |
|----------|---------|--------|
| `.github/workflows/infra.yml` | Push to `main` touching `infra/**` or `azure.yaml`; `workflow_dispatch`. | `azd provision` (applies immediately, no approval gate for MVP). |
| `.github/workflows/deploy.yml` | Push to `main` touching `src/**` or `Dockerfile`; `workflow_dispatch`. | Build image, push to ACR, `azd deploy`. |
| `.github/workflows/infra-preview.yml` | `pull_request` touching `infra/**` or `azure.yaml`. | `azd provision --preview`, posts what-if diff as a PR comment. Read-only. |

### Bootstrap (one-time per subscription)

The deploy UAMI is a prerequisite for the mutating workflows, so it cannot be provisioned by them. Bootstrap runs once from the operator's laptop against a clean subscription:

1. `azd up` (or `azd provision` + `azd deploy`) to create the resource group and initial resource footprint (F3.5).
2. Create the deploy UAMI, assign RBAC, and configure federated credentials for `main` and `pull_request` (F3.10). See `docs/features/wodbuster-booking-worker/tasks.md` F3.10 for the exact commands.
3. Publish the three GitHub Actions repository variables: `AZURE_CLIENT_ID` (deploy UAMI's `clientId`), `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`.

After bootstrap, **laptop `azd provision` is forbidden by convention**. All infra changes go through pull requests and the `infra.yml` workflow so ARM state and IaC stay in sync. If the operator ever needs to reset from zero (new subscription), that is a fresh bootstrap and stays out of Actions.
