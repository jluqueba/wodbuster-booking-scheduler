# WodBuster Booking Scheduler — Developer Guide

Comprehensive, developer-facing reference for the WodBuster Booking Scheduler: an unattended service that books CrossFit / box classes on the WodBuster platform the instant each booking window opens.

This guide is public. Internal specs, ADRs, envisioning notes, and migration records under `docs/internal/**` are also versioned, but Git stores them as age ciphertext through the `git-age` clean and smudge filter. Contributors with the private key see normal Markdown in the working tree; GitHub and GitHub Pages never receive the plaintext.

## Table of contents

1. [Project architecture](#1-project-architecture)
2. [Getting started](#2-getting-started)
3. [Features and pages](#3-features-and-pages)
4. [Documentation site and diagrams](#4-documentation-site-and-diagrams)

---

## 1. Project architecture

### 1.1 What the system does

Popular classes open at a fixed time and fill in under 10 seconds. A human who logs in and clicks manually loses the spot. The worker removes the human from the booking-time critical path: the operator configures recurring rules once, and a background scheduler fires a single pre-warmed HTTP booking request the moment the reservation window opens.

Design principles that shape the whole codebase:

- API-only client. The worker calls WodBuster HTTP handlers directly. No HTML scraping, no headless browser.
- No credential storage. Only the `.WBAuth` session cookie is persisted, encrypted at rest with AES-256-GCM. WodBuster username and password never touch the system.
- No silent failure. Every scheduled run produces a success notification, a failure notification, or a heartbeat anomaly alert. "No notification" is itself an alarm condition.
- Single long-running process. One FastAPI ASGI application hosts the web UI, the Telegram webhook, the APScheduler runtime, and the heartbeat probe together.
- Isolated user ownership. Every gym, rule, cookie, booking, alert, and notification belongs to one approved user. New OAuth identities remain pending until an administrator approves them.
- Recipient-controlled communication. The web UI and outbound messages support English and Spanish; operational email categories are editable per user while account lifecycle mail remains transactional.

### 1.2 Runtime shape

The application is a single FastAPI process running on Azure Container Apps. Inside that process:

| Concern | Component | Location |
|---------|-----------|----------|
| HTTP surface (web UI, OAuth, admin, Telegram webhook) | FastAPI routers | `src/wodbuster_worker/routes`, `auth`, `admin`, `rules`, `booking`, `cookie`, `notifications` |
| Background jobs | APScheduler `BackgroundScheduler` | `src/wodbuster_worker/scheduler` |
| WodBuster integration | Stock HTTP client | `src/wodbuster_worker/wodbuster_client` |
| Persistence | SQLAlchemy + Alembic against Postgres 16 | `src/wodbuster_worker/persistence`, `alembic/` |
| Secrets and crypto | Key Vault loader + AES-256-GCM cipher | `src/wodbuster_worker/security` |
| Notifications | Transactional outbox + Telegram and ACS Email dispatcher | `src/wodbuster_worker/notifications` |
| Localization and profiles | URL/session language resolver + translated catalog | `src/wodbuster_worker/i18n`, `routes/profile.py` |
| Observability | structlog + Azure Monitor OpenTelemetry | `src/wodbuster_worker/observability` |

The scheduler registers five job families at startup (`src/wodbuster_worker/app.py` lifespan):

- Heartbeat probe (cookie validity, hourly cadence).
- Notification dispatcher (drains independent Telegram and email outbox rows; dashboard banners are read from their durable rows).
- Per-run anomaly detector (opens a `heartbeat_anomaly` alert when a booking window passed with no recorded outcome anywhere on that day, re-notifies at most once per re-fire interval, and closes the alert once the outcome lands or the window ages out).
- External dead-man ping to Healthchecks.io (every 10 minutes) so a crashed or partitioned worker trips an out-of-band alarm.
- Per-rule booking jobs (bootstrapped from the active scheduler rules).

### 1.3 System context and communication

![System context diagram. The operator uses the web UI over HTTPS, signs in through the Microsoft, GitHub, or Google OAuth providers, and sends Telegram commands. Inside Azure, the Container App wodbuster-worker runs the FastAPI HTTP surface, the APScheduler runtime, the heartbeat probe, and the notification dispatcher. They read and write PostgreSQL Flexible Server, authenticating with an Entra token from the runtime managed identity. The HTTP surface reads secrets from Key Vault and sends telemetry to Application Insights and Log Analytics, and the image is pulled from Container Registry. Outbound, the scheduler books and the heartbeat probes against the WodBuster API, the dispatcher sends through the Telegram Bot API and Azure Communication Services Email, Telegram calls back through a webhook, and the heartbeat pings Healthchecks.io.](assets/system-context.svg)

Communication summary:

- Inbound to the worker: operator browser (HTTPS), Telegram webhook (`POST /telegram/webhook/{secret}`), OAuth callbacks.
- Outbound from the worker: WodBuster booking and probe calls, Telegram `sendMessage`, ACS Email sends, Healthchecks.io pings, Key Vault secret reads, Postgres queries, and Application Insights telemetry.
- The runtime authenticates to Postgres with a per-connection Entra token acquired by its user-assigned managed identity. No database password is stored.

### 1.4 Azure services

| Service | Role | Bicep module |
|---------|------|--------------|
| Azure Container Apps (managed environment + app) | Hosts the single worker process | `infra/modules/containerapp.bicep` |
| Azure Database for PostgreSQL Flexible Server | Scheduler rules, booking history, encrypted cookie blob, alerts, outbox | `infra/modules/postgres.bicep` |
| Azure Key Vault | Cookie encryption key, Telegram bot token, webhook secret, OAuth client secrets, session secret, Healthchecks URL | `infra/modules/keyvault.bicep` |
| Azure Container Registry | Stores the worker container image | `infra/modules/registry.bicep` |
| User-assigned managed identity | Runtime identity for Key Vault reads and Entra-token Postgres auth | `infra/modules/identity.bicep` |
| Log Analytics workspace + Application Insights | Logs, metrics, distributed traces, the `outbox_queue_depth` gauge | `infra/modules/observability.bicep` |
| Azure Communication Services + Email Communication Service | Sends localized HTML and text email from the verified custom domain | `infra/modules/communication.bicep` |

Infrastructure is composed at resource-group scope in `infra/resources.bicep`, orchestrated from `infra/main.bicep`, and deployed with the Azure Developer CLI (azd) per ADR-0007.

### 1.5 Booking flow

![Booking flow sequence diagram. The APScheduler job loads the active rule and next window from Postgres, decrypts the .WBAuth cookie from the AES-256-GCM cookie store, and pre-warms the connection about 30 seconds before the window. It then requests the primary slot from the WodBuster API. If granted, it records a granted BookingOutcome. If the slot is full, it tries the second shot and records granted or full. Finally it enqueues a notification in the same transaction, and the dispatcher sends the Telegram and email rows independently.](assets/booking-flow.svg)

Every state-mutating write that produces an operator-visible signal writes a `notification_outbox` row in the same transaction. A dispatcher job drains the outbox, so a delivery failure never loses the underlying record.

For a multi-gym user, the scheduler resolves each rule's gym account first and books against that gym's own subdomain, athlete `idu`, and encrypted cookie through a per-gym client factory (`src/wodbuster_worker/wodbuster_client/client.py`, `booking/executor.py`). A deactivated gym account books nothing; the booking job skips it.

### 1.6 Data model

![Entity relationship diagram. operator_profile has many federated_identity rows, many gym_account rows, and many notification_outbox rows. Each gym_account has exactly one cookie_credential and many scheduler_rule, booking_outcome, vacation_window, heartbeat_reading, and alert rows. A scheduler_rule produces many booking_outcome rows, and a heartbeat_reading may raise an alert.](assets/data-model.svg)

Key tables (`src/wodbuster_worker/persistence/models.py`):

- `operator_profile`: the human user. Stores lifecycle status, administrator flag, optional ban expiry, display and short names, communication language, editable email, email preferences, and Telegram binding. Owns login identities, gym accounts, and the notification stream.
- `federated_identity`: OAuth identities bound to an operator; unique on `(provider, subject_id)`. A previously unseen identity creates a pending profile through the OAuth callback.
- `gym_account`: one row per WodBuster gym the user books at, carrying the gym subdomain slug, the discovered athlete `idu`, a display name, and an `active` flag. Unique on `(user_id, gym_slug)`. Every booking-scoped table keys off `gym_account_id`, so each gym books, probes, and raises alerts independently.
- `scheduler_rule`: recurring weekly booking intent (scoped to a gym account) with a primary target and an optional second shot; the window opens `booking_opens_days_before` days before the class at `booking_opens_at`.
- `cookie_credential`: one encrypted `.WBAuth` blob per gym account (ciphertext + nonce, no plaintext column). The ciphertext is AES-256-GCM bound to the gym account, so a stored row cannot be replayed against another gym.
- `booking_outcome`: one row per attempt with a terminal status (`granted`, `full`, `cookie_invalid`, `class_not_visible`, `upstream_unavailable`, `cancelled`, `skipped`).
- `vacation_window`: date ranges (per gym account) with skip-and-cancel semantics.
- `heartbeat_reading`, `alert`: the per-gym observability pipeline. At most one open alert exists per `(gym_account_id, kind)`, enforced by a partial unique index.
- `notification_outbox`: durable channel-specific signals scoped to `user_id`, with an optional `gym_account_id` for gym context. Telegram and email rows have independent attempt and completion state.

### 1.7 Deployment and CI/CD

![Deployment and CI/CD diagram. A developer opens a pull request. ci.yml runs ruff, mypy, and pytest when code changed, and infra-preview.yml posts an azd provision preview with a what-if comment. After the merge to main, infra.yml runs azd provision when infra or azure.yaml changed, and deploy.yml builds the image, pushes it to ACR, and runs azd deploy when src or the Dockerfile changed. Both reach Azure.](assets/deployment.svg)

Both provisioning and application deploys run from GitHub Actions using OIDC federation against a dedicated deploy user-assigned managed identity, separate from the runtime identity (ADR-0005, ADR-0007). After a one-time bootstrap, laptop `azd provision` is forbidden by convention so ARM state and IaC stay in sync.

| Workflow | Trigger | Action |
|----------|---------|--------|
| `.github/workflows/ci.yml` | Every pull request | Runs `ruff`, `mypy`, and `pytest` against Postgres 16 when non-Markdown files changed. The lightweight `ci-gate` still reports success for documentation-only PRs and is the required check for `main`. |
| `.github/workflows/infra-preview.yml` | Pull request touching `infra/**` or `azure.yaml` | `azd provision --preview`; posts a what-if diff as a sticky PR comment. Read-only. |
| `.github/workflows/infra.yml` | Push to `main` touching `infra/**` or `azure.yaml`; manual dispatch | `azd provision`. |
| `.github/workflows/deploy.yml` | Push to `main` touching `src/**` or `Dockerfile`; manual dispatch | Build image, push to ACR, `azd deploy`. |

---

## 2. Getting started

### 2.1 Prerequisites

- Python 3.12 or newer.
- Docker Desktop (for the local Postgres 16 container).
- Azure CLI and the Azure Developer CLI (azd), only for infrastructure work.

### 2.2 Clone and set up the local environment

Windows (PowerShell):

```powershell
git clone https://github.com/jluqueba/wodbuster-booking-scheduler.git
cd wodbuster-booking-scheduler
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
docker compose up -d postgres
.\check.ps1
```

Linux or macOS:

```bash
git clone https://github.com/jluqueba/wodbuster-booking-scheduler.git
cd wodbuster-booking-scheduler
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d postgres
make check
```

`docker compose up -d postgres` starts the Postgres 16 container declared in `docker-compose.yml`, listening on `localhost:5432` and matching the `POSTGRES_*` block in `.env.example`. Wipe it with `docker compose down -v` for a clean slate.

`check` runs `ruff check`, `mypy src`, `djlint` over the Jinja templates, and `pytest` (excluding the `live_contract` marker). It is the same gate the CI workflow enforces.

The dev extras in `pyproject.toml` are pinned to exact versions so that gate is reproducible: CI and a developer's machine run the same linter, the same type checker and the same template linter. A range would let a new release add a rule mid-branch and turn an unrelated pull request red, which is how djlint 1.46 (rule H044) broke a change that touched no templates. Bump a pin in its own commit, run the full suite, and resolve the new findings there.

The template step can be run on its own, which is useful before committing a markup change:

```powershell
djlint src/wodbuster_worker/templates --lint
```

It is configured under `[tool.djlint]` in `pyproject.toml` and reports malformed HTML, unclosed tags, invalid nesting, and Jinja syntax errors. Purely stylistic rules are off: djlint never reformats the templates here.

### 2.3 Configuration

Configuration is driven by `pydantic-settings` (`src/wodbuster_worker/config.py`) and switched by `WODBUSTER_ENV`:

- `local`: values come from environment variables, optionally seeded by a `.env` file at the repo root. Copy `.env.example` to `.env` to start.
- `prod`: values are wired by Container Apps; secrets resolve through Key Vault references.

Notable settings include `POSTGRES_*` connection coordinates, OAuth client IDs, session lifetime knobs, `WORKER_TIMEZONE` (default `Europe/Madrid`), the ACS endpoint and sender identity, and the public app origin used for email assets and unsubscribe links. WodBuster gym coordinates are discovered per user from the submitted `.WBAuth` cookie; the legacy `WODBUSTER_GYM` and `WODBUSTER_IDU` values remain optional deployment inputs.

### 2.4 Apply database migrations

The schema is managed by Alembic (`alembic/`). With the local Postgres running:

```powershell
alembic upgrade head
```

Component tests in `tests/component/test_migrations.py` exercise the real baseline against a real Postgres, so migrations stay verifiable.

### 2.5 Create the first administrator

The bootstrap command creates the first active administrator and their OAuth identity:

```powershell
python -m wodbuster_worker.bootstrap
```

The command prompts for provider, provider subject ID, and display name. It is idempotent for an existing provider and subject pair. Once this administrator can sign in, every new OAuth identity uses the normal signup flow: the callback creates a pending profile, administrators review it at `/admin/users`, and the applicant receives transactional email when the request is received, approved, or rejected.

The provider subject ID can be obtained from the structured OAuth callback log produced by an initial sign-in attempt. Do not promote a pending row directly in production; use the administration page so lifecycle behavior and notifications remain consistent.

### 2.6 Run the app locally

```powershell
uvicorn wodbuster_worker.app:app --reload --port 8000
```

`GET http://localhost:8000/health` returns `200 OK`. The web UI is served from `http://localhost:8000/`.

### 2.7 Build and run the container

```powershell
docker build -t wodbuster-worker:dev .
docker run --rm -p 8000:8000 wodbuster-worker:dev
```

### 2.8 Deploy the infrastructure

Deployment is normally driven by GitHub Actions, but the first provision on a clean subscription is a one-time bootstrap from an operator laptop:

1. `azd up` (or `azd provision` followed by `azd deploy`) creates the resource group and the initial resource footprint.
2. Create the deploy user-assigned managed identity, assign its RBAC, and configure federated credentials for `main` and `pull_request`.
3. Publish the GitHub Actions repository variables consumed by the workflows, including the deploy identity, tenant, subscription, operator principal, OAuth client IDs, custom domain values, and ACS domain-link state.

After bootstrap, all infrastructure changes go through pull requests and the `infra.yml` workflow. Seed the runtime secrets (cookie encryption key, Telegram bot token, webhook secret, OAuth client secrets) into Key Vault, then let `deploy.yml` build and roll out the container image. ACS Email uses a two-phase provision: create the custom sender domain, publish the emitted SPF/DKIM/DMARC records at the DNS provider, wait for verification, set `AZURE_EMAIL_DOMAIN_LINKED=true`, and provision again. The runtime sends through managed identity, so there is no ACS connection string to rotate.

---

## 3. Features and pages

### 3.1 Feature overview

| Feature | Summary |
|---------|---------|
| Recurring scheduler rules | Weekly booking intents with a primary target class and an optional second shot; the worker fires a single request at window open. |
| Multiple gyms | The user adds more than one WodBuster gym; each gym account carries its own subdomain, athlete `idu`, and encrypted cookie, and books, probes, and alerts independently. Accounts can be deactivated (stops bookings, keeps history) and reactivated. |
| Cookie paste-and-validate | The operator pastes the `.WBAuth` cookie per gym account; it is validated against that gym and stored encrypted, bound to the gym account. |
| Manual booking | One-off booking for a specific date and time, from the web UI or Telegram. |
| Cancellation | Single-booking cancel (idempotent) from the web UI or Telegram, plus bulk cancel by date range (vacation mode). |
| Heartbeat and alerts | Hourly cookie probe projects time-to-expiry and alerts the operator with lead time before the next booking window. |
| Multi-channel notifications | Booking outcomes, cookie-expiry warnings, and anomaly alerts delivered as dashboard banners and, when configured, through Telegram and email. |
| Dead-man safety | External Healthchecks.io ping plus an internal anomaly detector so a missed run never fails silently. |
| Federated sign-in and approval | OAuth via Microsoft, GitHub, or Google. New identities remain pending until an administrator approves them. |
| User profiles and localization | Editable display name, short name, email, English or Spanish communication language, WodBuster avatar, and email preferences. |
| User administration | Administrators approve or reject requests and can time-limit, indefinitely suspend, restore, or permanently delete regular users. Self-administration and actions against another administrator are blocked. |
| Email notifications | Localized HTML and text mail through ACS for booking results, session alerts, and transactional account lifecycle messages, with one-click unsubscribe for operational categories. |

### 3.2 Telegram bot

The bot is a read-and-act channel plus the delivery target for all notifications. The webhook (`POST /telegram/webhook/{secret}`) is gated by a Key Vault-sourced path secret; a mismatch returns 404 so only Telegram can reach the handler. It is intentionally not session-gated, because Telegram never carries the web session cookie.

The dispatcher routes on an explicit allow-list of seven commands (`src/wodbuster_worker/notifications/telegram_webhook.py`):

| Command | Category | Requires bound chat | Purpose |
|---------|----------|---------------------|---------|
| `/start <token>` | Binding | No | Bind this chat to the operator using a one-shot token from the web UI. |
| `/help` | Information | No | List the supported commands. |
| `/status` | Information | No | Report whether this chat is bound. |
| `/next` | Information | Yes | Next scheduled booking plus up to five upcoming slots. |
| `/last` | Information | Yes | Most recent booking outcome. |
| `/cancel <booking-id>` | Action | Yes | Idempotent cancel of a booking. |
| `/ack` | Action | Yes | Acknowledge the open cookie-expiring alert. |

Information-extraction commands: three commands return operator data (`/status`, `/next`, `/last`), and `/help` enumerates the full set. Every stateful command requires the chat to be bound to an operator; unbound chats receive a no-data-leak rejection that never surfaces another operator's data.

Rule create, edit, and delete are deliberately web-UI only. Rule-mutation verbs sent to the bot (for example `/newrule`, `/editrule`, `/deleterule`) are recognised and rejected with an explanation instead of a generic unknown-command reply. Anything else outside the allow-list gets a polite nudge toward `/help`.

### 3.3 Web pages

Booking pages require an active, non-suspended authenticated session (`require_session`). Administrator routes additionally require `is_admin`; they return 404 to regular users. CSRF protection guards all state-mutating POSTs. The OAuth callback, pending/suspended pages, health endpoint, and signed unsubscribe endpoint are deliberately public.

Gym-scoped pages (rules, history, cookie, vacation) act on the operator's currently selected gym, resolved by `gyms/context.py` and surfaced as a switcher in the top navigation. Gyms are discovered automatically from WodBuster (on login and on cookie paste) using the shared `.WBAuth`; there is no manual add and no per-gym management page. There is no persisted default gym: a sole gym is auto-selected, and an operator who owns more than one must pick explicitly (a `POST /gyms/select` stores the choice in the session). Until a multi-gym operator picks one, those pages render a "choose a gym" prompt. Telegram, which has no web session, still resolves the operator's sole gym.

| Path | Template | Contents |
|------|----------|----------|
| `/` | `landing.html` / `dashboard.html` | Landing hero for anonymous visitors; for a signed-in operator, a dashboard showing next booking, cookie health, and banners. |
| `/auth/{provider}/login`, `/auth/{provider}/callback`, `/auth/logout` | (redirects) | OAuth sign-in and sign-out for Microsoft, GitHub, and Google. |
| `/auth/pending`, `/auth/suspended` | auth state templates | Explain that a signup awaits approval or that administrator access is suspended. |
| `/profile` | `profile.html` | Edit display name, short name, communication language, email address, and operational email preferences; displays the active gym's WodBuster avatar read-only. |
| `/admin/users` | `admin/users.html` | Administrator-only review of pending requests and management of active regular users, including timed or indefinite suspension, restoration, and permanent deletion. |
| `/gyms/select` | (redirect) | Select the active gym for the web session. This is the only gym route; ownership is asserted (404 otherwise). Gyms themselves are discovered automatically on login and on cookie paste (shared `.WBAuth`), never added by hand. |
| `/cookie` | `cookie/` | Paste-and-validate the `.WBAuth` cookie; it is stored for every gym the identity can access and triggers discovery of any new ones; shows the projected time-to-expiry countdown. |
| `/rules`, `/rules/new`, `/rules/{id}` | `rules/` | List, create, edit, and delete scheduler rules; a class-picker helper reads available class types from WodBuster. |
| `/history` | `history.html` | Booking attempt history with terminal statuses. |
| `/vacation` | `vacation.html` | Create and close vacation windows; bulk-cancels bookings in the range. |
| `/telegram` | `telegram.html` | Bind status, one-shot deep-link generation, test message, and unbind. |
| `/faq` | `faq.html` | Static frequently-asked-questions content. |
| `/unsubscribe?token=...` | `unsubscribe.html` | Public signed-link endpoint that disables booking and session-alert email while preserving transactional account mail. |
| `/health` | (JSON) | Liveness probe used by the Container App and the Healthchecks.io dead-man target. |

Shared templates (`_nav.html`, `_banners.html`, `_confirm_modal.html`, `_datetime.html`, `_time_picker.html`) provide the navigation shell, banner pool rendering, and reusable form widgets, all extending `base.html`.

---

## 4. Documentation site and diagrams

### 4.1 Public site

The README and this guide are published with GitHub Pages straight from `main`. GitHub builds the site with Jekyll on every merge, configured by `_config.yml`; no workflow in this repository is involved.

Jekyll publishes everything it is not told to exclude, so `tests/unit/test_public_site.py` fails when a tracked top-level path, or an entry under `docs/`, is neither published on purpose nor excluded:

- A new top-level code or tooling directory goes in the `exclude` list in `_config.yml`.
- A new top-level entry whose name starts with a dot or an underscore is skipped by Jekyll, so it is classified rather than excluded: add it to the test's list of known hidden entries or, if it must be published, to the `include` list and the test's list of required includes.
- A new entry under `docs/` goes either in the test's list of published `docs/` entries or in the `exclude` list.
- A published Markdown file named like a community file, such as `CONTRIBUTING.md`, is served as raw text unless it is listed under `include` or starts with front matter.

The same test fails on Liquid template delimiters in published Markdown (two opening braces, or an opening brace followed by a percent sign), because Jekyll evaluates them; describe such syntax in words or link to the file instead.

### 4.2 Diagrams

Public documentation embeds diagrams as committed SVG images rendered from a Mermaid source kept beside them in `docs/assets/`, never as inline Mermaid blocks: GitHub renders Mermaid, but the published site does not. Keep diagrams in this guide, not in the README.

Edit the `.mmd` file, then regenerate the image with Node.js installed:

```bash
python scripts/render_diagrams.py
```

Each image records the digest of the source it was rendered from, and `tests/unit/test_public_docs.py` fails when a source changes without its image being regenerated. Give every embedded diagram alt text that states what it shows.
