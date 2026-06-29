# Observability and Heartbeat

**Status**: Proposed
**Date**: 2026-06-29

## Context

Envisioning constraint 4 (no silent degradation) and spec FR-026, FR-025, SC-003 mandate that every scheduled run window produces exactly one terminal signal: a success notification, a failure notification, or a heartbeat-anomaly alert. "No signal" is an alarm condition, not an acceptable outcome.

Two distinct failure modes must each be covered:

1. **Per-run anomaly**: a scheduled window elapses without any outcome being persisted within the grace period. Detection requires knowledge of upcoming windows and is therefore internal to the worker.
2. **Total process or platform failure**: the worker is down, the container is in CrashLoopBackOff, or the Azure region is impaired. Internal heartbeats cannot signal their own absence. An external watchdog is required.

Hosting is one always-on Container App replica (ADR-0001). The container exposes a `/health` route (ADR-0004). Logs and metrics flow to Application Insights by Container Apps default integration.

## Priorities and Requirements (ordered)

1. **External dead-man watchdog** that alerts the operator when the worker is unreachable for an extended period. Cannot share fate with the worker.
2. **Internal per-run anomaly detection**: a scheduled window passes without an outcome signal within a configurable grace period (recommended 60 seconds after window close, FR-026).
3. **Structured logs and metrics** for booking attempt latency, WodBuster response codes, cookie probe results, and notification dispatch.
4. **Alert channels** on Telegram and as a web UI banner (FR-025, FR-026, FR-023). No email, no SMS.
5. **Monthly cost under 5 EUR** for the full observability stack at single-user scale.

## Options Considered

### Option 1: Internal APScheduler heartbeat plus external Healthchecks.io free tier plus Application Insights

Three layers:

1. **Internal**: An APScheduler job runs every 60 seconds. It computes the next scheduled booking window across all active rules and pending manual bookings. After each window close plus grace period, it checks whether at least one outcome row was persisted in the corresponding interval. If not, it writes a heartbeat-anomaly alert row, which the notification dispatcher delivers via Telegram and surfaces as a web banner. The same hourly cookie heartbeat (ADR-0003) writes a `heartbeat_reading` row and pings a Healthchecks.io check.
2. **External**: A Healthchecks.io free-tier check expects a ping from the worker every 10 minutes. If two consecutive pings are missed (20-minute window), Healthchecks.io emails the operator and posts to Telegram via its built-in integration. The worker pings Healthchecks.io from the same APScheduler heartbeat that polls WodBuster.
3. **Logs and metrics**: Structured JSON logs via `structlog` shipped to Application Insights through the Container Apps log stream. Custom metrics for booking-attempt latency (per WodBuster call), cookie probe duration, and notification dispatch lag. A single Azure Monitor alert rule, "no Telegram notification produced in last 24 hours", as a backstop in case both the internal and external watchdogs fail.

**Evaluation against priorities**:
- **External dead-man watchdog**: Meets. Healthchecks.io is fully out-of-band from Azure and from the worker. If the Azure region is impaired, Healthchecks.io still fires.
- **Internal per-run anomaly detection**: Meets. The 60-second tick plus grace-period check covers FR-026 directly.
- **Structured logs and metrics**: Meets. `structlog` plus Application Insights is the canonical Python pattern for ACA.
- **Alert channels**: Meets. Internal alerts produce Telegram messages and web banners. External alerts produce Telegram messages via Healthchecks.io's integration. No email or SMS required.
- **Monthly cost**: Meets. Healthchecks.io free tier is 0 EUR. Application Insights ingestion stays under 1 EUR per month at single-user log volume with a daily cap of 200 MB. Total under 3 EUR per month.

### Option 2: Azure Monitor scheduled log queries only, no external watchdog

A scheduled log-query alert in Azure Monitor runs every 5 minutes, querying for the absence of "outcome persisted" log entries within expected windows. Alerts fire via an Action Group that webhooks the Telegram bot.

**Evaluation against priorities**:
- **External dead-man watchdog**: Fails. Azure Monitor shares fate with the Azure tenant. A regional Azure incident takes both the worker and the watchdog down simultaneously.
- **Internal per-run anomaly detection**: Partially meets. Scheduled log queries can detect missing outcome entries, but the latency floor is the query interval (5 minutes minimum) plus query lag (typically another 2 to 5 minutes), well above the FR-026 grace period of 60 seconds.
- **Structured logs and metrics**: Meets.
- **Alert channels**: Meets via Action Group webhook.
- **Monthly cost**: Borderline. Scheduled log queries cost approximately 1.50 EUR per rule per month at the cheapest cadence, comparable to Healthchecks.io free.

### Option 3: Self-hosted Uptime Kuma on a second Container App instance

Uptime Kuma running in a second container, pinging the worker's `/health` endpoint on a schedule and notifying via Telegram on missed pings.

**Evaluation against priorities**:
- **External dead-man watchdog**: Partially meets. Uptime Kuma in the same subscription is less independent than Healthchecks.io, though it would survive a single container's failure.
- **Internal per-run anomaly detection**: Independent of this option.
- **Structured logs and metrics**: Independent of this option.
- **Alert channels**: Meets.
- **Monthly cost**: Fails the under-5 EUR target. A second always-on Container App replica doubles the hosting line item and costs another 8 to 12 EUR per month for a watchdog that is less independent than the free hosted alternative.

## Decision

Option 1: a three-layer observability stack.

1. **Internal**: APScheduler heartbeat ticking every 60 seconds for per-run anomaly detection. The hourly cookie probe writes `heartbeat_reading` rows and pings Healthchecks.io.
2. **External**: Healthchecks.io free-tier check expecting a ping every 10 minutes; missed pings trigger Telegram notification via Healthchecks.io's built-in integration.
3. **Logs and metrics**: `structlog` structured JSON shipped to Application Insights, plus a single Azure Monitor alert rule "no Telegram notification produced in last 24 hours" as a backstop.

This option uniquely meets every priority. Option 2 fails the independence requirement of priority 1. Option 3 fails the cost target of priority 5.

## Implementation Notes

- The Healthchecks.io project is created at provisioning time. The check UUID is non-secret and lives in app configuration; no Key Vault entry needed.
- The 10-minute ping cadence is conservative: at 6 pings per hour the worker contributes well below 1 percent of Healthchecks.io free-tier capacity.
- The internal anomaly detector handles the case where the notification dispatcher itself is the failure: if the outcome row is persisted but no notification is dispatched within 30 seconds, a separate "dispatch backlog" event is logged and surfaced in the next heartbeat tick.
- Application Insights daily cap is set to 200 MB to bound cost. At single-user log volume the cap is never reached in practice; the cap is insurance against runaway logging.
- The Azure Monitor backstop alert ("no Telegram notification in 24 hours") is the safety net of last resort. Triggering it indicates that both the internal and external watchdogs failed, and is itself a release-blocking incident class.
- Cost ballpark: Application Insights under 1 EUR per month, Azure Monitor alert rule under 2 EUR per month, Healthchecks.io 0 EUR per month. Total under 3 EUR per month.

## References

- `docs/features/wodbuster-booking-worker/spec.md` FR-022, FR-025, FR-026, SC-003.
- `docs/envisioning/wodbuster-booking-scheduler.md` section 7 constraint 4.
- `docs/architecture/decisions/0001-hosting-service.md` for the always-on replica.
- `docs/architecture/decisions/0003-auth-and-session.md` for the cookie heartbeat that doubles as the external ping source.
- `docs/architecture/decisions/0004-configuration-interface.md` for the `/health` endpoint.
