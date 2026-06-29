# Hosting Service

**Status**: Proposed
**Date**: 2026-06-29

## Context

The WodBuster Booking Worker is a long-running Python service that must colocate four runtime concerns in a single deployable unit:

1. A FastAPI web UI for rule CRUD, cookie paste-and-validate, history, and alerts (FR-001 through FR-005, FR-013, FR-015, FR-017, FR-020 through FR-024, FR-033).
2. A Telegram webhook endpoint that receives operator commands (FR-014, FR-018, FR-027, FR-031).
3. An APScheduler runtime that fires booking attempts aligned to `SegundosHastaPublicacion` (FR-006 through FR-012) and runs the hourly heartbeat probe (FR-022, FR-026).
4. Persistent state for rules, history, the encrypted cookie blob, alerts, and heartbeat readings (FR-012, FR-021, FR-032). Persistence engine is decided in ADR-0002.

Hard constraints from envisioning section 7:

- 10-second budget from window open to confirmed booking response (SC-002). Phase 0 measured 336 ms warm; 1.3 s cold connection overhead must be neutralized by pre-warming.
- No silent degradation. Every scheduled run produces a positive signal. The hosting service must support a long-lived in-process scheduler plus an externally pinged endpoint for dead-man detection (ADR-0006).
- Cost tolerance: tens of euros per month is acceptable; hundreds is not.

Locked technical choice from envisioning section 8: long-running service with internal scheduler. Pure timer-trigger Functions Consumption is ruled out.

## Priorities and Requirements (ordered)

1. **Single ASGI process colocating web, webhook, scheduler, heartbeat**. Splitting the four concerns across services adds inter-process coordination cost with no benefit at single-user MVP scale.
2. **No cold start at booking time**. The worker must be warm and resident at the booking window. Scale-to-zero is incompatible with the 10-second budget unless pre-warming is provably reliable, which adds complexity.
3. **Persistent volume support** for the SQLite database file decided in ADR-0002. The hosting service must mount a durable file share readable and writable by the container.
4. **Low operational complexity** for a single-user MVP. No node patching, no separate orchestrator, no per-pod resource tuning.
5. **Monthly cost in the single-digit to low-double-digit euro range** for one always-on replica.

## Options Considered

### Option 1: Azure Container Apps with min-replicas=1 (no scale-to-zero), single revision, single container

Run one FastAPI ASGI process (uvicorn) inside one container image. APScheduler runs in-process as a background task. The Telegram webhook is a FastAPI route. Azure Files share is mounted at a known path for the SQLite database file. `min-replicas=1` ensures one warm instance at all times. Single-revision mode avoids dual-revision traffic split during deploys.

**Evaluation against priorities**:
- **Single ASGI process**: Meets. One container hosts the whole runtime. APScheduler, FastAPI routes (web UI plus webhook), and the heartbeat job share memory and the SQLite connection pool.
- **No cold start at booking time**: Meets. `min-replicas=1` keeps the instance warm 24/7. No scale-to-zero. No HTTP-triggered pre-warm needed.
- **Persistent volume**: Meets. Container Apps supports Azure Files volume mounts on the Consumption profile. The SQLite file lives on the share; the container is stateless.
- **Operational complexity**: Meets. Managed runtime, integrated revisions, integrated managed identity, integrated logging to App Insights. No node management.
- **Monthly cost**: Meets. One always-on 0.25 vCPU plus 0.5 GiB replica costs approximately 8 to 12 EUR per month on Consumption metering in West Europe. Azure Files small share (5 GiB Standard) costs under 1 EUR per month. Container Registry Basic costs approximately 4 EUR per month. Total hosting line item under 20 EUR per month.

### Option 2: Azure App Service Linux on Basic B1

Single B1 instance running the FastAPI app under Gunicorn or Uvicorn. APScheduler runs in-process. Azure Files mounted as `/home/site/wwwroot` is not suitable for SQLite (latency, locking); a separate mount or local disk would be needed.

**Evaluation against priorities**:
- **Single ASGI process**: Meets. App Service supports a single Python ASGI app cleanly.
- **No cold start at booking time**: Meets for Basic and above (always-on flag), but App Service restarts the worker on configuration changes and during platform maintenance more visibly than ACA revisions do.
- **Persistent volume**: Partially meets. App Service local disk on B1 is ephemeral across restarts. Mounting an Azure Files share on App Service Linux works but feeds back into latency considerations for SQLite that ACA handles more transparently.
- **Operational complexity**: Meets. Managed PaaS. Less revision discipline than ACA.
- **Monthly cost**: Meets. B1 costs approximately 13 EUR per month, similar order of magnitude to ACA.

### Option 3: Small Linux VM (Standard_B1s) with systemd

A B1s VM running the container under a systemd unit, with an Azure Files share mounted at the OS level.

**Evaluation against priorities**:
- **Single ASGI process**: Meets. Same container, just hosted differently.
- **No cold start at booking time**: Meets. The process is always resident.
- **Persistent volume**: Meets. The VM mounts the file share at boot.
- **Operational complexity**: Fails. The operator becomes responsible for OS patching, kernel updates, container runtime upgrades, certificate rotation for the TLS edge (or a separate Application Gateway), and outbound IP management. None of this work pays for itself at single-user scale.
- **Monthly cost**: Meets the budget on paper (approximately 7 EUR per month for the VM plus disk and bandwidth), but the operational time cost dwarfs the savings.

## Decision

Azure Container Apps with `min-replicas=1`, `max-replicas=1`, scale-to-zero disabled, single revision mode, one container image hosting one FastAPI ASGI process. Storage is a mounted Azure Files share for the SQLite database file (ADR-0002). The same container handles web UI, Telegram webhook, APScheduler, and the heartbeat probe.

This option uniquely meets priority 2 (warm at booking time) with priority 4 (operational simplicity) at the priority-5 cost target. App Service Linux Basic is a close runner-up but the persistent-volume story for SQLite is less clean. A self-managed VM (Option 3) is rejected on operational complexity.

## Implementation Notes

- Container Apps Consumption profile is sufficient for one always-on replica at 0.25 vCPU plus 0.5 GiB.
- Pre-warming the WodBuster TCP and TLS connection happens inside the worker process approximately 30 seconds before each known booking window (envisioning section 5, FR-006). The hosting service makes this trivial because the process is already warm.
- Deployment uses `azd deploy` against the Bicep template defined in ADR-0007.
- The container image is built and pushed to Azure Container Registry by GitHub Actions on PR merge to `main`.
- A user-assigned managed identity (ADR-0005) is attached to the Container App for Key Vault and ACR access. No service principal client secrets are stored.
- `min-replicas=1` is non-negotiable for SC-002 (under 10-second latency budget). Operators must not enable scale-to-zero.

## References

- `docs/features/wodbuster-booking-worker/spec.md` SC-002, FR-006 through FR-012.
- `docs/envisioning/wodbuster-booking-scheduler.md` sections 7 and 8.
- `docs/features/phase-0-api-discovery/feasibility-report.md` warm and cold latency measurements.
