# Tasks: WodBuster Booking Worker

- **Created on**: 2026-06-29
- **Status**: Draft
- **Spec**: [spec.md](spec.md)
- **Plan**: [plan.md](plan.md)
- **ADRs**: [0001](../../architecture/decisions/0001-hosting-service.md), [0002](../../architecture/decisions/0002-persistence.md), [0003](../../architecture/decisions/0003-auth-and-session.md), [0004](../../architecture/decisions/0004-configuration-interface.md), [0005](../../architecture/decisions/0005-secrets-and-identity-access.md), [0006](../../architecture/decisions/0006-observability-and-heartbeat.md), [0007](../../architecture/decisions/0007-iac-tooling.md)
- **Phase 0 evidence**: [feasibility-report.md](../phase-0-api-discovery/feasibility-report.md), [spike-reproduce.py](../phase-0-api-discovery/scripts/spike-reproduce.py)

## Reading Guide

| Symbol | Meaning |
|--------|---------|
| `[P]` | Parallelizable with the previous task (no ordering dependency). |
| S | Small. Around half a session (one to two hours). |
| M | Medium. One focused session (three to five hours). |
| L | Large. Multiple sessions (one to two days). Should be split if possible during implementation. |
| `Phase N` | Plan phase from [plan.md](plan.md). |
| `ADR-000X` | Architectural decision that governs the task. |
| `FR-0XX`, `CC-0XX` | Functional requirement or conformance case from [spec.md](spec.md). |

Estimates assume a single solo developer who is also the end user, working in focused but interruptible sessions. They are deliberately conservative on the persistence, security, and OAuth surfaces; aggressive on areas covered by Phase 0 evidence.

Tests are not separate tasks. Each story carries an explicit "tests" sub-section that lists the unit and component tests required to consider the story acceptance-complete. A single optional live-contract probe is reserved for US-001 (booking), gated by `RUN_LIVE_WODBUSTER=1`, per the plan test strategy.

## Foundational and Cross-Cutting Tasks

These tasks are not tied to a single user story. They unlock everything downstream. They must complete before the corresponding user-story task groups start.

### F1: Repository and tooling skeleton (Phase 1)

Sets up the layout, lint, type, and test runners. Blocks every later story.

| ID | Task | Size | Dependencies |
|----|------|------|--------------|
| F1.1 | Create `pyproject.toml` with runtime dependencies (`fastapi`, `jinja2`, `apscheduler`, `sqlalchemy`, `alembic`, `httpx`, `structlog`, `azure-identity`, `azure-keyvault-secrets`, `authlib`, `python-telegram-bot`, `cryptography`, `pydantic-settings`) and dev dependencies (`pytest`, `pytest-asyncio`, `ruff`, `mypy`, `types-*`). | S | None |
| F1.2 | Create `src/wodbuster_worker/` package layout per Phase 1 (`app.py`, `routes/`, `scheduler/`, `persistence/`, `wodbuster_client/`, `notifications/`, `security/`) with empty `__init__.py` files and a minimal `app.py` that instantiates `FastAPI()` and exposes `GET /health` returning 200. | S | F1.1 |
| F1.3 | [P] Configure `ruff` (`ruff.toml`) and `mypy` (`mypy.ini`) in strict-but-pragmatic mode. Wire a `make check` or `pytask` equivalent that runs `ruff check`, `mypy src`, and `pytest`. | S | F1.1 |
| F1.4 | [P] Create `tests/{unit,component,contract}/` with `conftest.py`, register the `live_contract` pytest marker in `pyproject.toml`, and add a trivial passing test to verify the runner is green. | S | F1.1 |
| F1.5 | Author the production `Dockerfile` (Python 3.12-slim, non-root user, multi-stage build, `uvicorn` entrypoint). Build locally and confirm the container starts and answers `GET /health`. | M | F1.2 |
| F1.6 | [P] Add `pydantic-settings` `Settings` class reading from environment with `WODBUSTER_ENV` switching between `local` (`.env` file) and `prod` (Key Vault loader, stubbed for now). Add `.env.example`. | S | F1.2 |

### F2: GitHub Actions CI (Phase 1)

| ID | Task | Size | Dependencies |
|----|------|------|--------------|
| F2.1 | Author `.github/workflows/ci.yml` running `ruff check`, `mypy`, and `pytest` (excluding `live_contract`) on every PR and push to `main`. Cache `pip` between runs. | S | F1.3, F1.4 |
| F2.2 | Author `.github/workflows/deploy.yml` triggered on push to `main` when `src/**` or `Dockerfile` changes, and via `workflow_dispatch`. Builds and pushes the container image to ACR, then runs `azd deploy`. Scope is app-code deploys only; infra provisioning is handled by F2.3. Authenticates to Azure via OIDC federation against the **deploy UAMI** (ADR-0005). No client secrets in GitHub. | M | F1.5, F3.5, F3.10 |
| F2.3 | Author `.github/workflows/infra.yml` triggered on push to `main` when `infra/**` or `azure.yaml` changes, and via `workflow_dispatch`. Runs `azd provision` immediately (no approval gate for MVP, per ADR-0007). Authenticates via OIDC against the deploy UAMI (ADR-0005). | M | F3.5, F3.10 |
| F2.4 | Author `.github/workflows/infra-preview.yml` triggered on `pull_request` events touching `infra/**` or `azure.yaml`. Runs `azd provision --preview` and posts the what-if diff as a PR comment. Read-only; never mutates Azure state. Authenticates via OIDC against the deploy UAMI's `pull_request` federated credential (ADR-0005). | M | F3.5, F3.10 |

### F3: Bicep and azd infrastructure (Phase 2)

Manual setup steps (BotFather, OAuth registrations, Healthchecks.io, secret seeding) are listed as F3.6 through F3.8 and are explicitly operator-driven, not coded.

| ID | Task | Size | Dependencies | ADRs |
|----|------|------|--------------|------|
| F3.1 | Author `azure.yaml` for `azd` with a single service `worker` pointing at the Dockerfile. Define environment `prod`. | S | F1.5 | 0007 |
| F3.2 | Author `infra/main.bicep` and `infra/main.parameters.json` with subscription scope, resource group, and `targetScope = 'subscription'`. | S | F3.1 | 0007 |
| F3.3 | Author Bicep modules: `infra/modules/observability.bicep` (Log Analytics workspace plus Application Insights), `infra/modules/identity.bicep` (user-assigned managed identity plus role assignments for Key Vault Secrets User and AcrPull), `infra/modules/keyvault.bicep` (Key Vault with RBAC mode, soft-delete on), `infra/modules/storage.bicep` (storage account plus Azure Files share for SQLite per ADR-0002), `infra/modules/registry.bicep` (Azure Container Registry Basic SKU). | L | F3.2 | 0001, 0002, 0005, 0006 |
| F3.4 | Author `infra/modules/containerapp.bicep`: Container Apps environment, single Container App with `min-replicas=1`, `max-replicas=1`, single revision mode, mounted Azure Files volume at `/data` for SQLite, UAMI bound, env vars wired to Key Vault references via `secretRef`. | M | F3.3 | 0001, 0002, 0005 |
| F3.5 | **Bootstrap only.** Run `azd init`, `azd env new prod`, `azd up` from the operator's laptop against a clean subscription. This is the one-time bootstrap that creates the resource group and the initial resource footprint. After F3.10 lands, laptop `azd provision` is forbidden by convention (ADR-0007) and all subsequent provisioning runs through GitHub Actions (F2.3). Verify the Container App responds on `/health`. Capture the FQDN. | M | F3.4 | 0007 |
| F3.6 | Operator manual setup: register three OAuth clients (Microsoft personal, GitHub, Google) with the redirect URI `https://<fqdn>/auth/{provider}/callback`. Record IDs and secrets in a local password manager. | M | F3.5 | 0005 |
| F3.7 | Operator manual setup: create the Telegram bot via BotFather (`/newbot`), record the token. Sign up to Healthchecks.io free tier, create one check (cadence 10 minutes, grace 20 minutes), record UUID, connect Telegram as a notification channel on Healthchecks.io. | S | F3.5 | 0003, 0006 |
| F3.8 | Operator manual setup: populate Key Vault via `az keyvault secret set`: `wodbuster-cookie-encryption-key` (`openssl rand -base64 32`), `session-encryption-secret` (`openssl rand -base64 32`), `telegram-bot-token`, `oauth-microsoft-client-secret`, `oauth-github-client-secret`, `oauth-google-client-secret`, `healthchecks-ping-url`. Non-secret values (OAuth client IDs, Healthchecks UUID) go to Container App env vars in Bicep. | S | F3.6, F3.7 | 0005 |
| F3.9 | Author `infra/modules/monitor-alert.bicep`: Azure Monitor alert rule "no Telegram notification produced in last 24h" against Application Insights custom metric. | M | F3.3, F8.1 | 0006 |
| F3.10 | **Operator manual setup, one-time per subscription.** Create the **deploy UAMI** (suggested name `id-deploy-wodbuster-prod`) in the existing resource group. Assign `Contributor` and `User Access Administrator` roles scoped to the resource group only (no subscription-scope permissions; resource-group creation stays part of F3.5 bootstrap). Configure two federated credentials on the deploy UAMI, both targeting `jluqueba/wodbuster-booking-scheduler`: (a) subject `repo:jluqueba/wodbuster-booking-scheduler:ref:refs/heads/main` (consumed by `infra.yml` and `deploy.yml`); (b) subject `repo:jluqueba/wodbuster-booking-scheduler:pull_request` (consumed by `infra-preview.yml`). Publish `AZURE_CLIENT_ID` (deploy UAMI's `clientId`), `AZURE_SUBSCRIPTION_ID`, and `AZURE_TENANT_ID` as GitHub Actions repository variables. Document the whole procedure in `README.md`, including the "laptop `azd provision` is forbidden after this task" convention. | M | F3.5 | 0005, 0007 |

### F4: Persistence and security primitives (Phase 3)

| ID | Task | Size | Dependencies | ADRs / FRs |
|----|------|------|--------------|------------|
| F4.1 | Implement SQLAlchemy models in `src/wodbuster_worker/persistence/models.py` for all ten tables from [plan.md](plan.md) (`operator_profile`, `federated_identity`, `scheduler_rule`, `class_preference`, `cookie_credential`, `booking_outcome`, `vacation_window`, `heartbeat_reading`, `alert`, `notification_outbox`). Enforce unique constraint `(operator_id, kind)` on open alerts (one open row per kind per operator). | M | F1.2 | 0002 |
| F4.2 | Configure SQLite engine with `journal_mode=WAL`, `synchronous=NORMAL`, single shared engine, scoped session factory. Wire `pydantic-settings` to point at `/data/wodbuster.db` in prod and a temp file in tests. | S | F4.1 | 0002 |
| F4.3 | Initialize Alembic, generate the baseline migration from the models, wire `alembic upgrade head` into the container startup script before `uvicorn` launches. | M | F4.1 | 0002 |
| F4.4 | [P] Implement the Key Vault secret loader in `src/wodbuster_worker/security/keyvault.py` using `DefaultAzureCredential` (resolves to UAMI in prod, `AzureCliCredential` locally). Read all secrets once at startup into the `Settings` object. | S | F1.6, F3.8 | 0005 |
| F4.5 | Implement AES-256-GCM cipher in `src/wodbuster_worker/security/cipher.py`. API: `encrypt(plaintext: bytes) -> (ciphertext, nonce)` and `decrypt(ciphertext, nonce) -> plaintext`. Key sourced from Key Vault. | S | F4.4 | 0002, 0005 |
| F4.6 | Implement structured logging bootstrap in `src/wodbuster_worker/observability/logging.py` using `structlog` with JSON renderer. Bind `operator_id` and `request_id` context vars. (Wiring to App Insights happens in US-002 / Phase 10.) | S | F1.2 | 0006 |

### Foundational tests

| ID | Task | Size | Dependencies |
|----|------|------|--------------|
| F4.T1 | Unit test cipher round-trip: `decrypt(encrypt(x)) == x` for representative cookie payloads. Unit test that decrypting with a different key fails cleanly (no plaintext leak, no partial decryption). | S | F4.5 |
| F4.T2 | Component test that Alembic `upgrade head` against a fresh temp SQLite file produces every table from F4.1 and that a basic insert plus select round-trips on each. | S | F4.3 |
| F4.T3 | Unit test that the Settings loader falls back to `.env` when `WODBUSTER_ENV=local` and reads from a fake Key Vault stub when `WODBUSTER_ENV=prod`. | S | F4.4 |

**Foundational complexity rollup**: 6 S + 4 M + 1 L plus 3 S tests. Total: roughly 1.5 to 2 weeks of solo dev time including the operator-driven manual setup and the first `azd up` round-trip.

---

## User Story 1 (P1): Automated booking at window open

**User story**: As the operator, I want the worker to book my preferred class the instant its booking window opens, walking my ordered fallbacks, so I never miss a popular slot that fills in under ten seconds.

**Acceptance summary**: Top slot booked within ten seconds of window open when available (CC-001); fallback walk works (CC-002); failure recorded with reason when all fallbacks full (CC-003); class-not-visible retry policy (CC-004, CC-005); fail-fast on cookie-invalid (CC-006).

**FRs**: FR-006, FR-007, FR-008, FR-009, FR-010, FR-011, FR-012. **ADRs**: 0001, 0003. **Plan phase**: 4.

**Complexity**: **L**. The booking core carries the project's hardest requirement (sub-ten-second latency, deterministic fallback walk, ordered retry policies). Mitigated by Phase 0 evidence showing the three HTTP endpoints behave as expected and warm latency near 336 ms. Risk concentrated in scheduling alignment (`SegundosHastaPublicacion`) and the WAL-on-Azure-Files write path.

### Tasks

| ID | Task | Size | Dependencies | Notes |
|----|------|------|--------------|-------|
| US1.1 | Implement `WodBusterClient` in `src/wodbuster_worker/wodbuster_client/client.py` with a shared `httpx.Client` (connection pool, HTTP/2 off per Phase 0), and the three endpoint methods: `load_class(...)`, `inscribir(id, ticks, idu)`, `borrar(...)`. Inject the `.WBAuth` cookie from the cookie store. Parse the `Res` field per Phase 0 spike. | M | F4.1, F4.5, [spike-reproduce.py](../phase-0-api-discovery/scripts/spike-reproduce.py) |
| US1.2 | Implement response classifier: map `Res` values to typed outcomes (`Granted`, `Full`, `CookieInvalid`, `Unknown`). Map HTTP-level failures (timeout, 5xx) to `UpstreamUnavailable`. Capture the full response payload for persistence (FR-012). | S | US1.1 |
| US1.3 | Implement `BookingExecutor.book(rule, cookie)` in `src/wodbuster_worker/scheduler/booking.py`: orchestrates pre-warm, countdown alignment, ordered fallback walk, and class-not-visible retry. Single in-flight request at all times (FR-008). | L | US1.2 |
| US1.4 | Implement the pre-warm tick: 30 seconds before a window, issue one `LoadClass.ashx` to warm TCP and TLS (FR-006). | S | US1.3 |
| US1.5 | Implement countdown alignment: poll `LoadClass.ashx` until `SegundosHastaPublicacion <= 1`, then schedule the booking request via APScheduler `DateJob` at `t=0` (FR-007). | M | US1.4 |
| US1.6 | Implement the ordered fallback walk: iterate `class_preference` rows by `order_index`, break on `Granted`, continue on `Full`, fail-fast and emit "cookie invalid" alert on `CookieInvalid` (FR-009, FR-011, CC-006). | M | US1.3, F4.1 |
| US1.7 | Implement the class-not-visible retry policy: when the target class is absent from `LoadClass.ashx`, retry every 5 seconds for up to 2 minutes, then emit a "class not visible" terminal outcome (FR-010, CC-004, CC-005). | M | US1.5 |
| US1.8 | Implement `BookingOutcome` writer: single SQLAlchemy transaction that persists the outcome row and the corresponding `notification_outbox` row together (plan cross-cutting rule). | S | US1.3, F4.1 |
| US1.9 | Implement APScheduler bootstrap in `src/wodbuster_worker/scheduler/scheduler.py` with `SQLAlchemyJobStore` pointing at the same SQLite database (so restarts rehydrate the schedule). Register an `IntervalTrigger` per active rule. | M | F4.2, US1.3 |
| US1.10 | Implement rule-change hot reload: on every rule create, update, or delete (US5), the scheduler removes and re-adds the affected jobs in memory and in `SQLAlchemyJobStore` without restart (FR-004). | S | US1.9 |

### Tests for User Story 1

| ID | Test | Size | Covers |
|----|------|------|--------|
| US1.T1 | Unit tests for `WodBusterClient` against a mocked `httpx` transport: granted response, full response, cookie-invalid response, malformed `Res`, HTTP timeout, HTTP 503. | S | US1.1, US1.2 |
| US1.T2 | Unit tests for the fallback walk state machine with synthetic preference lists (one slot, three slots first-granted, three slots last-granted, all-full, cookie-invalid on second slot). | M | US1.6 |
| US1.T3 | Unit tests for the class-not-visible retry policy: never-visible (terminates at 2 min), visible at 70 seconds (one attempt after retry), visible at 5 seconds (one attempt within first cycle). | S | US1.7 |
| US1.T4 | Component test (file-backed SQLite, mocked WodBuster): full booking pipeline from APScheduler tick to outcome row plus outbox row, verifying single transaction durability before notification dispatch (plan consistency model). | M | US1.3, US1.8, US1.9 |
| US1.T5 | Conformance tests CC-001, CC-002, CC-003, CC-004, CC-005, CC-006 against the mocked WodBuster client. | M | US1.1 through US1.8 |
| US1.T6 | Live-contract test (gated by `RUN_LIVE_WODBUSTER=1`, marker `live_contract`): one real booking attempt against the operator's account on an agreed-upon test class. Operator cancels manually afterward. Detects WodBuster response-shape drift on each release. | M | US1.1, US1.2 |

---

## User Story 2 (P1): Outcome notifications and silent-run alarm

**User story**: As the operator, I want every scheduled run to produce a positive signal (success, failure, or anomaly) on Telegram and the web UI, so a silent worker is immediately visible.

**Acceptance summary**: Telegram and web UI signal within 30 seconds for success (CC-001, AS1) and failure (CC-003, AS2); heartbeat-anomaly alert after grace period when a run window passes without signal (CC-008, AS3).

**FRs**: FR-025, FR-026. **ADRs**: 0003, 0006. **Plan phases**: 8, 10.

**Complexity**: **M**. Mechanics are simple (an outbox dispatcher plus a per-run anomaly detector), but the consistency contract (durable outcome before dispatch, no signal lost, no duplicate dispatch) plus three integration surfaces (Telegram, web banner, Healthchecks.io, App Insights) needs careful testing.

### Tasks

| ID | Task | Size | Dependencies | Notes |
|----|------|------|--------------|-------|
| US2.1 | Implement `NotificationDispatcher` APScheduler job ticking every 5 seconds. Reads pending rows from `notification_outbox` (where `dispatched_at IS NULL`), dispatches per `kind`, updates `dispatched_at` and `attempt_count` on success. | M | F4.1, US1.8 |
| US2.2 | Implement Telegram delivery in `src/wodbuster_worker/notifications/telegram.py` using `python-telegram-bot` Bot API. Exponential backoff up to 1 hour on transient failures. | S | F4.4 (bot token) |
| US2.3 | Implement web banner delivery: `notification_outbox` rows with `kind='banner'` write into the `alert` table (or update the matching open row), surfaced by the dashboard route. | S | US2.1, F4.1 |
| US2.4 | Implement per-run anomaly detector: APScheduler job ticking every 60 seconds. For every expected window in the next 5 minutes, if `(window_close_expected + grace_period) < now` and no `booking_outcome` row exists for that `(rule_id, window)`, write a `heartbeat-anomaly` alert plus outbox rows (FR-026). | M | F4.1, US1.9 |
| US2.5 | Implement Healthchecks.io ping job: APScheduler every 10 minutes posts to the URL from `healthchecks-ping-url` Key Vault secret. Wraps each call in a 5-second timeout (per ADR-0006). | S | F4.4 |
| US2.6 | Wire `structlog` to Application Insights via the Azure Monitor OpenTelemetry distro. Emit custom metrics: `booking_attempt_latency_ms`, `cookie_probe_duration_ms`, `notification_dispatch_lag_seconds`, `outbox_queue_depth`. | M | F4.6, F3.3 |
| US2.7 | Implement the dashboard banner partial (`templates/_banners.html`) listing open `alert` rows for the current operator, with acknowledge buttons where applicable. (Renders inside US-009 once auth lands.) | S | US2.3 |

### Tests for User Story 2

| ID | Test | Size | Covers |
|----|------|------|--------|
| US2.T1 | Unit test outbox dispatcher: pending row → Telegram mock called once → `dispatched_at` set. Failure → `attempt_count` incremented, exponential delay respected. | S | US2.1, US2.2 |
| US2.T2 | Unit test anomaly detector: synthetic schedule of three windows, last expected window passed without outcome → exactly one anomaly alert and one outbox row produced. Repeat detector tick → no duplicate (open `alert` uniqueness from F4.1). | M | US2.4 |
| US2.T3 | Conformance tests CC-008 (silent-run alarm). | S | US2.4 |
| US2.T4 | Component test: end-to-end booking from US-001 produces both a Telegram dispatch (via mock) and a banner row within 30 seconds. | S | US2.1, US2.3, US1.8 |
| US2.T5 | Component test: Healthchecks.io ping job posts to a mock endpoint every 10 minutes, ignoring transient failures. | S | US2.5 |

---

## User Story 3 (P1): Cookie paste, validate, and live status

**User story**: As the operator, I want to paste a `.WBAuth` cookie via the web UI, have it validated, encrypted at rest, and surface a TTL countdown, so I know exactly when a refresh is needed.

**Acceptance summary**: Valid cookie validated, stored encrypted, TTL surfaced (AS1); invalid cookie rejected with no state mutation (AS2); TTL countdown reflects latest heartbeat (AS3).

**FRs**: FR-020, FR-021, FR-024. **ADRs**: 0002, 0003, 0005. **Plan phase**: 5.

**Complexity**: **M**. Cookie cipher and persistence already done in F4. The work is the validation flow, the paste form, and surfacing the heartbeat result. Auth dependency is the largest unknown (US-009).

### Tasks

| ID | Task | Size | Dependencies | Notes |
|----|------|------|--------------|-------|
| US3.1 | Implement `CookieValidator.validate(value: str) -> ValidationResult` in `src/wodbuster_worker/security/cookie.py`: calls `LoadClass.ashx` with the candidate cookie, classifies as `Valid` or `Rejected(reason)` per Phase 0 evidence. | S | US1.1 |
| US3.2 | Implement `CookieStore.save(operator_id, value)`: validates first; on success encrypts via F4.5 and inserts or updates the single active `cookie_credential` row for the operator in one transaction. Reject with no state mutation on validation failure (FR-020). | S | US3.1, F4.5 |
| US3.3 | Implement `CookieStore.load(operator_id) -> bytes`: reads the active row, decrypts via F4.5, returns plaintext. Caches the plaintext in process memory for the lifetime of the booking attempt only (no long-lived in-memory cache). | S | US3.2 |
| US3.4 | Implement projected TTL estimator: combines `last_validated_at`, a configurable ceiling (default 30 days per plan assumption), and the last `heartbeat_reading.projected_ttl_at`. Updates `cookie_credential.projected_ttl_at` on every heartbeat. | M | US3.2, US4.1 |
| US3.5 | Implement `GET /cookie` route: renders the cookie status partial (current TTL countdown, last probe timestamp, last probe result) plus the paste form. HTMX-powered partial refresh on every heartbeat. | M | US3.4, US9.x (auth) |
| US3.6 | Implement `POST /cookie` route: calls `CookieStore.save`, returns the cookie status partial with a positive validation banner on success or a clear rejection banner on failure (FR-020). | S | US3.2, US3.5 |
| US3.7 | Implement the cross-account warning: when the validated cookie's identity (from `LoadClass.ashx` response payload, if exposed) differs from the operator's expected WodBuster account, surface a "mismatch confirm" two-step flow (spec edge case, last bullet). | M | US3.1, US3.6 |

### Tests for User Story 3

| ID | Test | Size | Covers |
|----|------|------|--------|
| US3.T1 | Unit test `CookieValidator` against a mocked `WodBusterClient`: valid response, rejected response, network failure (classified as unknown, surfaced as "could not validate, please retry"). | S | US3.1 |
| US3.T2 | Unit test `CookieStore.save`: invalid cookie → no row mutated (FR-020 invariant). Valid cookie → exactly one active row per operator (upsert semantics). | S | US3.2 |
| US3.T3 | Component test: paste valid cookie via `POST /cookie` → `cookie_credential` row written with non-empty ciphertext and nonce, plaintext never appears in any persisted column. | M | US3.6 |
| US3.T4 | Component test: paste invalid cookie via `POST /cookie` → response shows rejection banner, no row mutated, no notification emitted. | S | US3.6 |
| US3.T5 | Unit test TTL estimator with synthetic heartbeat history: bounded by ceiling, monotonic non-increasing between heartbeats, jumps back to ceiling on successful re-paste. | S | US3.4 |

---

## User Story 4 (P1): Proactive cookie-expiry alert

**User story**: As the operator, I want an alert at least twenty-four hours before any scheduled booking window whose cookie will not survive, so I never face a booking-time surprise.

**Acceptance summary**: No alert when TTL exceeds 24h-lead requirement (AS1); alert emitted on both surfaces in the right window (CC-007, AS2); re-emission on every cycle until refreshed or acknowledged (FR-027, AS3).

**FRs**: FR-022, FR-023, FR-024, FR-027. **ADRs**: 0003, 0006. **Plan phase**: 5.

**Complexity**: **M**. Heartbeat cadence and TTL math are straightforward. The repeat-emission with acknowledge semantics has subtle state transitions worth covering exhaustively in tests.

### Tasks

| ID | Task | Size | Dependencies | Notes |
|----|------|------|--------------|-------|
| US4.1 | Implement hourly cookie heartbeat APScheduler job: calls `CookieValidator` against the stored cookie, writes a `heartbeat_reading` row, updates `cookie_credential.last_validated_at` and `projected_ttl_at` (FR-022). | M | US3.1, F4.1 |
| US4.2 | Implement next-window lookahead: for the operator, compute the next scheduled booking window across all active rules (uses `scheduler_rule.day_of_week` and `window_offset_hours`). | S | F4.1, US1.9 |
| US4.3 | Implement the 24h-lead alert evaluator: on every heartbeat, if `projected_ttl_at < next_window - 24h`, emit (or re-emit) a `cookie-expiring` alert via the open-alert pattern from F4.1, with one outbox row per channel (Telegram, banner). Suppress re-emission for one cycle if `acknowledged_at` was set since the last heartbeat (FR-023, FR-027). | M | US4.1, US4.2, F4.1, US2.1 |
| US4.4 | Implement clear-on-refresh: when `CookieStore.save` succeeds with a new valid cookie, set `cleared_at` on any open `cookie-expiring` alert in the same transaction. | S | US3.2, US4.3 |

### Tests for User Story 4

| ID | Test | Size | Covers |
|----|------|------|--------|
| US4.T1 | Unit test alert evaluator with synthetic TTLs and next-window times: well-above-threshold → no alert (CC-007 negative path); below threshold → alert emitted; re-tick without acknowledge → re-emitted (AS3); re-tick with `acknowledged_at` set this cycle → suppressed; cycle after that → re-emitted. | M | US4.3 |
| US4.T2 | Unit test next-window lookahead with three rules across the week → returns the earliest. | S | US4.2 |
| US4.T3 | Component test: heartbeat runs against a mock cookie projected to expire in 12 hours, next window in 24 hours → exactly one `cookie-expiring` alert plus two outbox rows (Telegram, banner). | M | US4.1, US4.3, US2.1 |
| US4.T4 | Conformance tests CC-007. | S | US4.3 |
| US4.T5 | Component test: paste a new valid cookie → open `cookie-expiring` alert moves to `cleared_at`, no further re-emission. | S | US4.4 |

---

## User Story 5 (P2): Scheduler rule CRUD via web UI

**User story**: As the operator, I want to create, list, edit, and delete recurring weekly rules from the web UI with changes taking effect on the next window, with no rule mutation allowed via Telegram.

**Acceptance summary**: Create persists and appears in listing (AS1); edits affect the next run (AS2, FR-004); Telegram rule mutations rejected (CC-009, AS3, FR-003).

**FRs**: FR-001, FR-002, FR-003, FR-004, FR-005. **ADRs**: 0004. **Plan phase**: 6.

**Complexity**: **M**. CRUD on a small schema. The hot-reload hook into APScheduler (US1.10) is the only non-trivial part and is already implemented in US-001.

### Tasks

| ID | Task | Size | Dependencies | Notes |
|----|------|------|--------------|-------|
| US5.1 | Implement Jinja2 base layout (`templates/base.html`) and minimal CSS (no framework). Wire HTMX as a static asset, register the CSRF middleware compatible with HTMX (token in cookie plus header). | M | F1.2, US9.x (auth gate) |
| US5.2 | Implement `GET /rules` and `templates/rules/list.html` rendering the operator's rules, scoped via `operator_id` from the session (FR-005). | S | US5.1, F4.1 |
| US5.3 | Implement `POST /rules` and `templates/rules/form.html`: server-side validation of day-of-week, offset hours, and preference list ordering. On success persist `scheduler_rule` plus `class_preference` rows in one transaction and trigger APScheduler hot reload (FR-001, FR-004 via US1.10). | M | US5.2, US1.10 |
| US5.4 | Implement `GET /rules/{id}` and `POST /rules/{id}` for edit: HTMX-powered inline form, validates ownership before mutating (FR-005). Triggers APScheduler hot reload (FR-004). | M | US5.3 |
| US5.5 | Implement `POST /rules/{id}/delete`: validates ownership, soft-deletes or hard-deletes per `active=False`, triggers APScheduler hot reload. | S | US5.4 |
| US5.6 | Implement the Telegram rule-mutation rejection: any command from the Telegram surface that looks like rule create, update, or delete returns an explanatory rejection message and writes nothing (FR-003, CC-009). Lives in US-007 (Telegram bot) but specified here for traceability. | S | US7.x |

### Tests for User Story 5

| ID | Test | Size | Covers |
|----|------|------|--------|
| US5.T1 | Component test: authenticated operator A creates a rule, lists rules, sees their rule only. Operator B sees only theirs (cross-operator isolation, FR-005, partial CC-012). | M | US5.2, US5.3, US9.x |
| US5.T2 | Component test: create, edit, delete a rule; after each, the APScheduler job store reflects the change without restart (FR-004). | M | US5.3, US5.4, US5.5, US1.10 |
| US5.T3 | Unit tests for rule form validation: missing day, negative offset, empty preference list, duplicate `order_index` all rejected with field-level errors. | S | US5.3 |
| US5.T4 | Conformance tests CC-009 (Telegram rule mutation rejected). | S | US5.6 |

---

## User Story 6 (P2): Cancel a single booking

**User story**: As the operator, I want to cancel an upcoming booking from either the web UI or Telegram, idempotently and reflected on both surfaces within thirty seconds.

**Acceptance summary**: Telegram cancel reflects on both surfaces (AS1); web UI cancel reflects on both surfaces (AS2); duplicate cancel is idempotent (CC-015, AS3, FR-016).

**FRs**: FR-013, FR-014, FR-016. **ADRs**: 0004. **Plan phase**: 9.

**Complexity**: **S**. Single endpoint call to `Calendario_Borrar.ashx`, single transaction state update, idempotent guard.

### Tasks

| ID | Task | Size | Dependencies | Notes |
|----|------|------|--------------|-------|
| US6.1 | Implement `CancellationService.cancel(booking_id, operator_id)`: validates ownership, short-circuits with "already cancelled" if `booking_outcome.terminal_status` is already `cancelled`, otherwise calls `WodBusterClient.borrar(...)` and updates the row plus an outbox row in one transaction (FR-013, FR-014, FR-016, CC-015). | M | US1.1, F4.1 |
| US6.2 | Implement `POST /bookings/{id}/cancel` route. Returns the booking history row partial via HTMX. | S | US6.1, US5.1 |
| US6.3 | Implement the Telegram `/cancel <booking-id>` command handler (lives in US-007 Telegram bot infra, surface here). | S | US6.1, US7.x |

### Tests for User Story 6

| ID | Test | Size | Covers |
|----|------|------|--------|
| US6.T1 | Unit test idempotency: cancel an already-cancelled booking → service short-circuits, no WodBuster call, response is "already cancelled" (CC-015). | S | US6.1 |
| US6.T2 | Component test: web UI cancel of a granted booking → `booking_outcome.terminal_status='cancelled'`, outbox row created, Telegram mock called once. | M | US6.2 |
| US6.T3 | Component test: Telegram `/cancel` of a granted booking → same end state as US6.T2. | S | US6.3, US7.x |
| US6.T4 | Conformance tests CC-015. | S | US6.1 |

---

## User Story 7 (P2): Vacation mode bulk cancellation

**User story**: As the operator, I want vacation mode to bulk-cancel granted bookings in a date range and suppress automated runs during the range, auto-resuming when the range ends.

**Acceptance summary**: Only bookings inside the range cancelled (AS1, CC-014); rule fires inside the range record "skipped: vacation mode" (AS2); auto-resume after end date (AS3).

**FRs**: FR-015. **ADRs**: 0004. **Plan phase**: 9.

**Complexity**: **M**. Bulk cancel walks N bookings (where N is small for a single user). The skip semantics inside the scheduler need a single guard. Auto-resume is a date comparison.

### Tasks

| ID | Task | Size | Dependencies | Notes |
|----|------|------|--------------|-------|
| US7.1 | Implement `VacationService.enable(operator_id, start_date, end_date)`: persists a `vacation_window` row, finds every granted `booking_outcome` whose target class start is in `[start_date, end_date]`, walks them through `CancellationService.cancel`. | M | US6.1, F4.1 |
| US7.2 | Implement the scheduler skip guard: before launching a booking attempt, the booking executor checks whether the current window falls inside any open `vacation_window`. If yes, writes a `skipped: vacation mode` outcome plus outbox row in one transaction and exits (FR-015, CC-014). | S | US1.3, F4.1 |
| US7.3 | Implement `GET /vacation` and `templates/vacation.html` listing open windows and the enable form. | S | US5.1 |
| US7.4 | Implement `POST /vacation` (enable) and `POST /vacation/{id}/close` (end early) routes. | S | US7.1, US7.3 |

### Tests for User Story 7

| ID | Test | Size | Covers |
|----|------|------|--------|
| US7.T1 | Component test of CC-014: three granted bookings on three different days, vacation covering the first two → first two cancelled, third intact; scheduled run inside the range records "skipped: vacation mode". | M | US7.1, US7.2 |
| US7.T2 | Unit test the skip guard: synthetic vacation windows and rule windows → correct skip vs run decisions across boundary conditions (inclusive start, inclusive end). | S | US7.2 |
| US7.T3 | Component test of auto-resume: advance the clock past `end_date` → next scheduled rule fires normally. | S | US7.2 |

---

## User Story 8 (P2): Manual ad-hoc booking

**User story**: As the operator, I want to trigger a one-off booking from the web UI or Telegram for a specific class and time, rejected with a clear message when outside the booking window.

**Acceptance summary**: Telegram manual book within window → granted on both surfaces (AS1, CC-013); web UI manual book within window → granted on both surfaces (AS2); outside the booking window → rejected with no WodBuster call (CC-010, AS3, FR-019).

**FRs**: FR-017, FR-018, FR-019. **ADRs**: 0003, 0004. **Plan phase**: 9 (alongside cancellation).

**Complexity**: **M**. Reuses the booking executor from US-001. New work is the window-open precondition check and the two surface forms / commands.

### Tasks

| ID | Task | Size | Dependencies | Notes |
|----|------|------|--------------|-------|
| US8.1 | Implement `ManualBookingService.book(operator_id, target_date, target_time)`: verifies the class is currently within its booking window via `LoadClass.ashx` `SegundosHastaPublicacion` (rejects with "window not open" when not, no WodBuster booking call issued, FR-019, CC-010). Then delegates to a single-attempt path of the booking executor. Outcome persisted with `rule_id IS NULL`. | M | US1.3, US1.1 |
| US8.2 | Implement `GET /book-now` and `POST /book-now` routes plus form. | S | US8.1, US5.1 |
| US8.3 | Implement Telegram `/bookclass <YYYY-MM-DD> <HH:MM>` command handler. (Lives in US-007 Telegram infra.) | S | US8.1, US7.x |

### Tests for User Story 8

| ID | Test | Size | Covers |
|----|------|------|--------|
| US8.T1 | Unit test the window-open precondition: class not within booking window → rejection, no WodBuster booking call (FR-019, CC-010). | S | US8.1 |
| US8.T2 | Component test of CC-013: Telegram `/bookclass` for a class within its window with an available slot → granted on both surfaces. | M | US8.3 |
| US8.T3 | Component test of CC-013 via web UI form: same outcome as US8.T2. | S | US8.2 |
| US8.T4 | Conformance tests CC-010, CC-013. | S | US8.1 |

---

## User Story 9 (P1): Operator-only access via federated identity

**User story**: As the operator, I want every web UI route gated by federated identity (Microsoft personal, GitHub, Google) with an allow-list and no local passwords, so my cookie management and booking history are not exposed.

**Acceptance summary**: Anonymous requests redirect to sign-in (CC-011, AS1); allowed identity sees only own data (CC-012, AS2); disallowed identity denied without data leak (AS3, FR-030).

**FRs**: FR-028, FR-029, FR-030, FR-031. **ADRs**: 0005. **Plan phase**: 6 (auth) plus 7 (Telegram chat-ID binding).

**Complexity**: **L**. OAuth across three providers, session encryption, allow-list enforcement, CSRF on HTMX forms, plus Telegram chat-ID binding. Largest source of subtle security mistakes in the project. Cannot be parallelized with US-003, US-005, US-006, US-007, US-008 (every form route depends on it).

### Tasks

| ID | Task | Size | Dependencies | Notes |
|----|------|------|--------------|-------|
| US9.1 | Implement `SessionMiddleware` reading and writing an encrypted session cookie via the `session-encryption-secret` from Key Vault. `HttpOnly`, `Secure`, `SameSite=Lax`. Idle and absolute lifetimes configurable. | M | F4.4 |
| US9.2 | Implement Authlib OAuth clients for Microsoft personal, GitHub, and Google. Configure per-provider scopes (`openid email profile` baseline) and redirect URIs. Read client IDs from env, client secrets from Key Vault. | M | F4.4, F3.6, F3.8 |
| US9.3 | Implement `GET /auth/{provider}/login` (state cookie, redirect to provider) and `GET /auth/{provider}/callback` (state validation, token exchange, fetch user info, allow-list check via `federated_identity` table). On allow-list hit, attach `operator_id` to session. On miss, render a denial page with no operator data in the response body (FR-030, AS3). | L | US9.1, US9.2, F4.1 |
| US9.4 | Implement `POST /auth/logout` clearing the session cookie. | S | US9.1 |
| US9.5 | Implement the `require_session` FastAPI dependency that 302-redirects anonymous requests to `/auth/{default_provider}/login` and exposes `operator_id` to route handlers (FR-028, CC-011). | S | US9.1, US9.3 |
| US9.6 | Implement CSRF protection compatible with HTMX: double-submit cookie pattern, HTMX header propagation via `hx-headers`. Apply to every state-mutating POST. | M | US9.1 |
| US9.7 | Implement the bootstrap command `python -m wodbuster_worker.bootstrap` that prompts for a federated identity tuple `(provider, subject_id, display_name)` and inserts it into `federated_identity` plus a new `operator_profile`. Idempotent. Documented in the plan as the manual seeding step. | M | F4.1 |
| US9.8 | Implement the Telegram chat-ID binding: `/start <one-time-token>` consumes a token generated from the web UI and binds the sender's `chat_id` to the operator's `operator_profile.telegram_chat_id`. Subsequent commands from unbound chat IDs are rejected with no state change and no data leak (FR-031). | M | US7.x, F4.1 |
| US9.9 | Implement the Telegram per-update shared-secret validator on `POST /telegram/webhook`: webhook URL contains a secret path component matching a `telegram-webhook-secret` Key Vault value. Reject mismatches with 404. | S | F4.4 |

### Tests for User Story 9

| ID | Test | Size | Covers |
|----|------|------|--------|
| US9.T1 | Component test of CC-011: unauthenticated request to every protected route returns a 302 to the sign-in flow and the response body contains no operator data (no leaked usernames, no rule names, no booking history). | M | US9.5 |
| US9.T2 | Component test of CC-012: operator A authenticated, attempts to read `/rules/{id}` of a rule belonging to operator B → 403 or 404 with no operator B data in the body. Same for `POST /rules/{id}` mutation and `GET /history`. | L | US9.3, US9.5, US5.2 |
| US9.T3 | Component test of the allow-list rejection (AS3, FR-030): mock the OAuth callback to return an identity not in `federated_identity` → denial page rendered, no `operator_profile` created, no session established. | M | US9.3 |
| US9.T4 | Unit test CSRF protection: POST without the CSRF token is rejected with 403 across every state-mutating route. | S | US9.6 |
| US9.T5 | Component test of Telegram chat-ID binding: `/start <valid-token>` binds the chat ID and the token is then consumed (single-use). `/start <invalid-token>` is rejected. Subsequent commands from unbound chat IDs are rejected with no state change (FR-031). | M | US9.8 |
| US9.T6 | Component test of the webhook shared-secret: POST to `/telegram/webhook/<wrong-secret>` returns 404; POST to the correct path is accepted. | S | US9.9 |

---

## Telegram Bot Infrastructure (cross-story, supporting US-002, US-006, US-007, US-008, US-009)

These tasks are not a user-facing story on their own. They are listed under the "US-007" placeholder in user-story cross-references and live in Phase 7.

| ID | Task | Size | Dependencies | Notes |
|----|------|------|--------------|-------|
| TG.1 | Implement `POST /telegram/webhook/{secret}` route validated via US9.9, parses the update via `python-telegram-bot`. Dispatches to the command handler registry. | M | US9.9 |
| TG.2 | Implement command parser and dispatcher with explicit allow-list of recognized commands (`/start`, `/next`, `/last`, `/bookclass`, `/cancel`, `/ack`, `/help`). Unknown commands → polite rejection. Rule-mutation-looking commands → explanatory rejection per US5.6. | M | TG.1 |
| TG.3 | Implement `/next` and `/last` informational handlers. | S | TG.2 |
| TG.4 | Implement `/help` listing supported commands and pointing at the web UI for rule management. | S | TG.2 |
| TG.5 | Implement `/ack` handler: sets `acknowledged_at` on the operator's current open `cookie-expiring` alert for the current heartbeat cycle (FR-027). | S | TG.2, US4.3 |
| TG.6 | One-time setup task: register the Telegram webhook URL with the Bot API via `setWebhook` (operator runs this once after first deploy, documented in the README). | S | F3.7, TG.1 |

### Tests for Telegram bot infrastructure

| ID | Test | Size | Covers |
|----|------|------|--------|
| TG.T1 | Unit tests for the command dispatcher: every supported command routes to its handler. Unknown commands → polite rejection. Rule-mutation commands → explanatory rejection (FR-003, CC-009). | M | TG.2, US5.6 |
| TG.T2 | Unit test for `/ack` updating `acknowledged_at` on exactly the one open `cookie-expiring` alert for the operator and not on other operators' alerts (FR-027, FR-005). | S | TG.5 |

---

## History Surface (supporting all stories)

| ID | Task | Size | Dependencies | Notes |
|----|------|------|--------------|-------|
| H.1 | Implement `GET /history` and `templates/history.html`: lists `booking_outcome` rows for the operator, newest first, paginated. Includes status, target class, timestamp, and granted fallback index (FR-033). | M | F4.1, US5.1, US9.5 |
| H.2 | Implement the dashboard at `GET /`: next windows (from US4.2), open banners (from US2.7), and the 10 most recent outcomes (from H.1). | M | H.1, US2.7, US4.2 |

### Tests for the history surface

| ID | Test | Size | Covers |
|----|------|------|--------|
| H.T1 | Component test: outcomes from US-001, US-006, US-007, US-008 all appear in `GET /history` ordered by `attempted_at DESC` (FR-033). | S | H.1 |
| H.T2 | Component test: dashboard renders for an operator with zero data (no rules, no cookie, no outcomes) without error. | S | H.2 |

---

## Cross-Cutting Invariants and Conformance Coverage

Tracks which spec invariants and conformance cases are covered by which task or test, so nothing is dropped during implementation.

| Invariant or CC | Covered by |
|-----------------|------------|
| Exactly one booking request per attempt | US1.3, US1.6, US1.T2 |
| Outcome durable before notification | US1.8, US2.1, US1.T4 |
| Booking history never pruned | F4.1 (no pruning job exists), H.1 |
| No password storage; cookie only | F4.5, US3.2 (no password fields anywhere in the schema) |
| Cookie encrypted at rest | F4.5, US3.2, US3.T3 |
| Rule mutation web UI only | US5.6, TG.2, US5.T4, TG.T1 |
| Federated auth only | US9.2, US9.3, US9.7 (no password fields) |
| Telegram bound to registered chat ID | US9.8, US9.T5 |
| Cross-operator isolation | F4.1 (`operator_id` on every row), US5.2, US9.3, US9.T2 |
| Idempotent cancel | US6.1, US6.T1 |
| Every window produces a signal | US2.4, US2.T2, US2.T3 |
| CC-001 | US1.T5 |
| CC-002 | US1.T5 |
| CC-003 | US1.T5 |
| CC-004 | US1.T5 |
| CC-005 | US1.T5 |
| CC-006 | US1.T5 |
| CC-007 | US4.T4 |
| CC-008 | US2.T3 |
| CC-009 | US5.T4, TG.T1 |
| CC-010 | US8.T4 |
| CC-011 | US9.T1 |
| CC-012 | US9.T2 |
| CC-013 | US8.T4 |
| CC-014 | US7.T1 |
| CC-015 | US6.T4 |

---

## Summary

### Counts

| Bucket | Tasks | Tests |
|--------|-------|-------|
| Foundational (F1 through F4) | 24 | 3 |
| US-001 | 10 | 6 |
| US-002 | 7 | 5 |
| US-003 | 7 | 5 |
| US-004 | 4 | 5 |
| US-005 | 6 | 4 |
| US-006 | 3 | 4 |
| US-007 | 4 | 3 |
| US-008 | 3 | 4 |
| US-009 | 9 | 6 |
| Telegram infra | 6 | 2 |
| History surface | 2 | 2 |
| **Total** | **85** | **49** |

### Rough complexity rollup

| Story | Size | Notes |
|-------|------|-------|
| Foundational | Roughly two weeks | Mostly Bicep, OAuth registrations, persistence primitives. Front-loaded operator manual setup. |
| US-001 | L | Largest single story. Booking core, scheduler alignment, fallback walk. |
| US-002 | M | Outbox plus anomaly detector plus App Insights wiring. |
| US-003 | M | Cookie paste flow plus partial. |
| US-004 | M | Heartbeat plus alert evaluator with acknowledge semantics. |
| US-005 | M | CRUD on a small schema; hot reload already done in US-001. |
| US-006 | S | Single endpoint plus idempotent guard. |
| US-007 | M | Bulk cancel plus skip guard. |
| US-008 | M | Reuses booking executor; new window-open precondition. |
| US-009 | L | Auth across three providers, CSRF, allow-list, chat-ID binding. |
| Telegram infra | S to M | Webhook plus dispatcher plus five handlers. |
| History surface | S to M | Two read-only routes. |

### Suggested execution order

Sequential where the dependency graph forces it; parallel where it does not. The order favors the shortest path to the first end-to-end booking in production.

1. **Foundational F1 plus F2** (skeleton, lint, CI). Unblocks every subsequent task.
2. **Foundational F3** (Bicep, `azd up`, operator manual setup F3.6 through F3.8, F3.9 deferred until US-002 metrics exist). Run the operator manual steps in parallel with F4 below.
3. **Foundational F4** (persistence and security primitives). Can start immediately after F1; runs in parallel with F3.
4. **US-009** (auth). Blocks every form route in US-003, US-005, US-006, US-007, US-008, and the Telegram chat-ID binding. Front-loaded.
5. **US-003** (cookie paste). First user-facing surface beyond auth. Required for any booking attempt.
6. **US-001** (booking core). The hard one. Includes the `live_contract` test that requires the operator's cookie from US-003.
7. **US-002** (notifications and anomaly detector). Required for SC-003 ("zero silent failures") and unblocks F3.9 (the App Insights alert rule).
8. **US-004** (proactive cookie alert). Builds on US-001's scheduler and US-002's dispatcher.
9. **Telegram bot infrastructure (TG.1 through TG.6)**. Required for the Telegram surfaces of US-006, US-008.
10. **History surface (H.1, H.2)**. Required so US-006 and US-008 have something to display.
11. **US-005** (rule CRUD). The first P2. Optional for the first production booking if the operator is willing to seed a rule directly into SQLite during bootstrap, but a strong nice-to-have.
12. **US-006** (single cancel), **US-008** (manual ad-hoc booking), **US-007** (vacation mode). Order within these three is flexible; pick by operator need.

### Stories that can be deferred without breaking the P1 path

The minimal path to the first granted booking in production with full silent-failure coverage is: Foundational, US-009, US-003, US-001, US-002, US-004 plus the necessary fragment of Telegram infrastructure for outbound notifications only (TG.6 webhook registration plus the outbound delivery path from US2.2, no command dispatcher required).

Deferrable without compromising the P1 acceptance:

- US-005: the operator can seed one rule via SQL until the web UI lands. Acceptable for a single user.
- US-006, US-007, US-008: convenience features. None blocks the recurring weekly booking that the project exists for.
- Telegram inbound commands (TG.2 through TG.5): outbound notifications are sufficient for the dead-man signal contract. Inbound commands are required only for US-006, US-008, US-009 (chat-ID binding) and US-004 (`/ack`).

### Definition of done for the feature

Per the Exit Criteria in [plan.md](plan.md): all ten phases complete, all conformance cases CC-001 through CC-015 passing, the gated `live_contract` test passing once against the operator's WodBuster account, the first end-to-end production booking succeeds and produces both a Telegram message and a web UI history entry, a manually paused worker produces the Healthchecks.io dead-man alert within 20 minutes, the cookie paste flow completes in under two minutes from a cold start, and zero secrets exist in the repository, container image, or env manifests.

### Next phase

`devsquad.implement` against the Foundational task group, starting with F1.1.
