# Reasoning Log: WodBuster Booking Worker

Amendments and non-trivial decisions logged during implementation of this feature. Entries are appended, never rewritten. Ordered newest-first.

---

## 2026-07-02 — Postgres firewall opened to all Azure services (ADR-0005 minor amendment, platform-driven)

**Trigger**: first end-to-end deploy against Postgres with the runtime UAMI succeeded through Alembic connection *only after* adding an `AllowAllAzureServices` firewall rule (start=end=0.0.0.0). The rule I had authored in `postgres.bicep` — one rule per entry in a `containerAppsOutboundIps` array plus an operator IP — was empty (default) for the automatic runs, so Postgres refused the connection at the TCP layer and Alembic hung with no error. Investigation showed:

- The ACA managed environment's `staticIp` property is the **inbound** ingress IP (verified: `158.158.80.169` in Spain Central).
- The **outbound** IP used by container replicas when they call Key Vault or Postgres is different (observed `158.158.84.228` in Key Vault access logs) and is not exposed as a property of the environment.
- Stable outbound IPs on Azure Container Apps are only available on **Workload Profiles** environments (adds ~15 EUR/month on top of the current Consumption env). This price delta was explicitly rejected during the 2026-07-02 Postgres pivot (Q1 answer: keep public server + firewall, not Private Endpoint / Workload Profiles).
- No documented API to enumerate the Consumption env's egress subnet or IP pool.

Without a stable outbound IP the "allowlist only the ACA env" approach in ADR-0005 (as originally written: `AllowAzureServices is deliberately not enabled`) is not implementable on the SKU we chose. Options were: (a) accept `AllowAllAzureServices`, leaning on Entra ID auth + TLS as the boundary; (b) reprovision to Workload Profiles and pay the delta; (c) reprovision with Private Endpoint (rejected in the pivot amendment). Option (a) was chosen for the MVP.

**Scope classification**: low-impact amendment. Touches ADR-0005 (one section rewritten with a "Platform limitation" note), one Bicep module (`postgres.bicep` gains the rule), one task text (F3.11 mentions the rule explicitly), and this reasoning log. No cost delta. No code change. No new operator step (the rule is now created by Bicep on every provision).

**Cascade check**: this is the third 2026-07-02 platform-limitation amendment on ADR-0005 today (the first was `pgaadauth`, the second was the runtime UAMI as Entra admin, this third one is the firewall). Pattern noted: Azure PG Flexible Server on the Burstable SKU + ACA Consumption combo has multiple documented rough edges that the plan's initial priority sweep did not anticipate. Not a signal to re-envision — the design still works, it just accepts more platform-imposed compromises than expected. Any fourth compound limitation on this same slice should trigger a `devsquad.refine` health check rather than another in-place amendment.

**Options considered**:

- **A. `AllowAllAzureServices` in the Postgres firewall (chosen)**. One rule (0.0.0.0-0.0.0.0). Bicep-managed so the state reproduces from IaC. Security boundary = Entra ID auth + TLS.
- **B. Reprovision to Workload Profiles + stable outbound IP**. Rejected: violates the Q1 answer of the Postgres pivot (public + firewall, ~30 EUR/month total). Would push total to ~45+ EUR/month.
- **C. Private Endpoint on Postgres**. Rejected in the pivot amendment for the same cost reason and because Consumption ACA can't reach a Private Endpoint anyway without VNet integration.
- **D. Firewall rule with a dynamic list refreshed by a scheduled job**. Rejected: the ACA env does not expose a query-able outbound IP set, so the "dynamic refresh" would be a guess-and-check loop.

**Decision**: apply option A. ADR-0005 firewall subsection rewritten with a "Platform limitation" note. `postgres.bicep` now emits an unconditional `AllowAllAzureServices` firewall rule alongside the (still-supported) per-ACA-outbound-IP array (which is now expected to be empty in Consumption env deployments and non-empty only if we ever move to Workload Profiles). `containerAppsOutboundIps` param stays: it costs nothing when empty, and it forward-compatibilizes the module for the day we may move off Consumption. tasks.md F3.11 wording updated to make the compromise explicit.

**Verification**: post-amendment, the container revision reached "Uvicorn running on http://0.0.0.0:8000" and `GET /health` returns `200 OK {"status":"ok"}` from the public FQDN.

**Propagation checklist**:

| Artifact | Present? | Invalidated? | Follow-up |
|----------|----------|--------------|-----------|
| ADR-0005 | Yes | Yes, firewall subsection. Amended in same pass. | None. |
| ADR-0002 | Yes | No: persistence layer unchanged. | None. |
| tasks.md F3.11 | Yes | Yes, wording refined. Amended in same pass. | None. |
| tasks.md F3.12 | Yes | Yes, verification bullet reworded (mention AllowAllAzureServices). Amended in same pass. | None. |
| postgres.bicep | Yes | Yes, gains the firewall rule resource. Amended in same pass. | None. |
| plan.md | Yes | No. | None. |
| Code | Yes | No: runtime does not care about the firewall shape. | None. |

---

## 2026-07-02 — Runtime UAMI granted Postgres Entra admin (ADR-0005 minor amendment, platform-driven)

**Trigger**: F3.12 (operator manual role grant) attempted to enrol the runtime UAMI as a non-admin Entra principal in Postgres. The Azure Database for PostgreSQL Flexible Server we provisioned (API `2024-08-01`, PG 16, Spain Central, Burstable B1ms) does not expose the `pgaadauth` extension: it is missing from both `pg_available_extensions` schemas that user roles can install, and the `azure.extensions` allow-list rejects `pgaadauth` with "not allow-listed for `azure_pg_admin` users". No user-callable function `pgaadauth_create_principal(...)` exists on the server. The only working path to enrol an Entra principal is `az postgres flexible-server microsoft-entra-admin create --type ServicePrincipal`, which promotes the principal to `azure_pg_admin` group membership (server admin).

**Scope classification**: low-impact amendment. Touches ADR-0005 "Postgres connection identity" subsection only. No new tasks, no code change, no cost change. The runtime UAMI simply carries broader privileges than the ADR originally allowed. Cascade check: none — no downstream artifact assumed the specific privilege set at code level yet.

**Options considered**:

- **A. Runtime UAMI as Entra admin (chosen)**. One `az` command. Preserves the Entra token / no-secrets-at-runtime posture from ADR-0005. Trade-off: the runtime UAMI has superuser-equivalent rights on the `wodbuster` database, so a compromised container revision could rewrite or delete anything in it. Blast radius bounded by the UAMI's Azure scoping (assigned to one Container App only).
- **B. Runtime uses `wodbadmin` password from Key Vault**. Would keep the password in KV, never in repo or image, but re-introduces a password on the runtime path. Same effective DB privileges as A. Rejected because it undermines the "no secrets at runtime" priority in ADR-0005 for no security gain over A.
- **C. Reprovision under a different tier / region / API version until `pgaadauth` is exposed**. Uncertain outcome (`pgaadauth` may have been removed platform-wide), non-trivial rework, delays end-to-end validation. Rejected as speculative.

**Decision**: apply option A. ADR-0005 "Postgres connection identity" section updated with the platform-limitation note. Runtime privilege set is accepted at superuser level for the MVP. The `wodbadmin` password login stays as a break-glass path (already in KV) but is no longer strictly needed for schema recovery. If Azure later re-exposes `pgaadauth`, revisit and reduce privileges via the `pgaadauth_create_principal(name, false, false)` path plus schema-scoped `GRANT`s.

**Propagation checklist**:

| Artifact | Present? | Invalidated? | Follow-up |
|----------|----------|--------------|-----------|
| ADR-0005 | Yes | Yes, one row + new "Platform limitation" note. Amended in same pass. | None. |
| ADR-0002 | Yes | No: the privilege posture referenced there ("`GRANT USAGE, CREATE ON SCHEMA public`") is now moot because the UAMI has broader rights, but the ADR body reads as aspirational and does not need textual change. | None. |
| tasks.md F3.12 | Yes | Partially: the F3.12 wording described a `pgaadauth_create_principal` + `GRANT` sequence. Replaced with the `microsoft-entra-admin create` command. | Updated in same pass. |
| plan.md | Yes | No. | None. |
| Code (`engine.py`, `models.py`) | Not yet implemented (F4.1/F4.2). | No. | Runtime connection logic in F4.2 does not need to change: it still uses `DefaultAzureCredential` + Entra token as `password`; the DB just accepts a broader set of DDL from that connection. |

---

## 2026-07-02 — Persistence pivot from SQLite-on-Azure-Files to Azure Database for PostgreSQL (ADR-0002 in-place rewrite)

**Trigger**: the WAL-to-DELETE amendment earlier the same day (see entry below) resolved the immediate `database is locked` crash but exposed that the whole SQLite-on-SMB substrate was fragile in ways the original ADR-0002 priority sweep had not anticipated: Alembic bootstrap is brittle on SMB, `journal_mode=DELETE` costs write latency, correctness is hard-coupled to `max-replicas=1`, backups are a hand-rolled snapshot script, and there is no PITR. The operator triaged the option space with the freshly-discovered constraints and chose to pivot to managed Postgres despite the cost delta from roughly 0.30 EUR per month to roughly 13-15 EUR per month. Discovery happened during the same `devsquad.refine` session that produced the WAL-to-DELETE amendment; no in-flight branch on F4.1/F4.2/F4.3 (the persistence code has not been implemented yet), so the pivot rewrites the plan cleanly without invalidating merged work.

**Scope classification**: high-impact amendment. Touches one ADR (0002, in-place rewrite of Options, Decision, Implementation Notes; Priorities section preserved verbatim per operator direction), one ADR subsection add (0005 "Postgres connection identity"), the feature `plan.md` (Reasoning Log row, cost ballpark, ADR reference table, Mermaid runtime diagram, Data Model header, Risks and Mitigations, Test Strategy, Manual One-Time Setup), and `tasks.md` (five new tasks F1.7/F3.11/F3.12/F3.13/F3.14, five modified tasks F3.3/F3.4/F4.1/F4.2/F4.3, one rewritten test F4.T2, plus foundational rollup and counts). No implementation code edited in this amendment; F4.1/F4.2/F4.3 rewrites, the deletion of `alembic/versions/c7808dfff47d_baseline.py`, the deletion of `infra/modules/storage.bicep`, and the creation of `docker-compose.yml` and `infra/modules/postgres.bicep` are delegated to `devsquad.implement` on the next turn.

**Whole-feature escalation check**: no signal tripped.

| Signal | Fires? | Notes |
|--------|--------|-------|
| Affects more than one priority story | No | Persistence is cross-cutting infrastructure; no user story is invalidated. All FRs (FR-001 through FR-033) survive verbatim. |
| Outcome shift (KPI or headline NFR) | No | The 10-second booking latency budget is unchanged. Cost priority (5) is deliberately relaxed by 8-10 EUR/mo, documented in ADR-0002; not a re-envisioning. |
| Task invalidation majority | No | Of 85 previous tasks, 5 are modified in wording, 5 are added, 0 are removed. |
| Hierarchy shift | No | No epic, feature, or ownership change. |
| Plan-level rewrite (more than one ADR replaced) | No | One ADR rewritten in place (0002); one ADR gains a subsection (0005). Others untouched. |
| User research contradicts envisioning | No | Envisioning constraint 6 concerns credentials, not persistence. Constraint 8 names Key Vault; that stands. |

**Cascade check**: two prior amendments on this feature earlier the same day (WAL-to-DELETE at 2026-07-02 morning; GitHub Actions provisioning at 2026-07-02 midday). This is the third amendment. Per the cascade guard in the `refine.instructions`, three high-impact amendments on the same slice trigger a pause. Applied nuance: the WAL-to-DELETE amendment was low-to-medium impact (one bullet edit); this pivot subsumes it. Counting the two high-impact amendments (GitHub Actions provisioning, and this pivot), we are at two on the same day, not three. Proceeding without escalation; noting the pattern.

**Amendment applied**:

1. **ADR-0002** rewritten in place per operator direction (Q7 = b). New `## Change history` section at the top with two dated 2026-07-02 entries (WAL-to-DELETE, then the pivot). `## Priorities and Requirements` preserved verbatim. `## Options Considered` reconsiders the three original options with what we learned on 2026-07-02, plus two new options (SQLite on Container App emptyDir, SQLite on Azure Blob via BlobFuse2). `## Decision` states the pivot and explicitly relaxes priority 5. `## Implementation Notes` covers sizing (Burstable B1ms, PG 16, Spain Central AZ1, 32 GiB autogrow, 7-day backup, no HA, public network + firewall + Entra ID auth), the three-principal authentication model, the SQLAlchemy driver (`psycopg[binary]` v3 sync), the Alembic regeneration, the local `docker compose` Postgres substrate, and the consistency contract (unchanged). Status remains `Proposed` per adrs.instructions.md (no ADR is Accepted before implementation completes).\n2. **ADR-0005** gains a `### Postgres connection identity` subsection under Implementation Notes: three-principal model with a table of principal-to-privilege mappings, the runtime UAMI's token-based connection flow, rationale for the split, and the firewall rule policy (Container Apps outbound IPs + operator's home IP, no `AllowAzureServices`). Existing "Allow-list lives in the SQLite database (ADR-0002)" references updated to "Postgres database (ADR-0002)".\n3. **plan.md** — Summary paragraph rewritten to say Postgres; cost ballpark added (13-15 EUR/mo); ADR reference table row 0002 updated; Mermaid runtime component diagram updated (`store` node now says `SQLAlchemy + psycopg v3 sync engine, Entra ID auth`; the `files` Azure Files node replaced with a `pg` Postgres node; the `store --- files` edge replaced with `store -- TCP 5432, Entra token --> pg`); Data Model header rewritten to describe Postgres-native types; Risks and Mitigations table's Azure-Files-latency row replaced with three Postgres-specific rows (CPU credits, Entra token acquisition failure, forgotten F3.12 grant); APScheduler `SQLAlchemyJobStore` risk row updated (Postgres, not SQLite); Test Strategy unit and component rows updated to say Postgres via docker compose or testcontainers (Q6 = all-Postgres); Manual One-Time Setup gained one new step (Postgres Entra admin + role grant, renumbered) and the Key Vault seeding step now includes `postgres-admin-password`; Reasoning Log persistence row rewritten to reference the pivot and the four newly-considered alternatives.\n4. **tasks.md** — Foundational F1 gains `F1.7` (docker-compose.yml plus README dev instructions). F3.3 amended to remove `storage.bicep` from the module set. F3.4 amended to remove the `/data` volume mount and add Postgres env vars. F3.11 (postgres.bicep) added. F3.12 (operator manual Entra admin + role grant) added. F3.13 (postgres-admin-password KV seed) added. F3.14 (cost re-check after first billing cycle) added. F4.1 rewritten for Postgres-native types (JSONB, native enums, `postgresql_where=`). F4.2 rewritten for `psycopg[binary]` v3 with an Entra-token connect listener. F4.3 rewritten: delete the existing SQLite baseline, regenerate against Postgres 16, remove `render_as_batch=True` from `alembic/env.py`. F4.T2 rewritten to run Alembic against real Postgres 16. Foundational rollup, task counts (24 -> 29), and total (85 -> 90) updated.

**Q&A summary** (operator inputs, turn 2, this amendment):

| # | Question | Answer |
|---|----------|--------|
| 1 | Networking | Public server + firewall rules + Entra ID authentication. No VNet integration, no private endpoint. |
| 2 | Auth model | Three principals: password admin (`wodbadmin`, break-glass, KV-seeded) + operator's Entra user (server-level DBA, `azure_pg_admin`) + runtime UAMI (application + Alembic identity, `USAGE, CREATE` on schema `public`). |
| 3 | Sizing | Burstable B1ms, Postgres 16, 32 GiB autogrow, 7-day backup, no HA, Spain Central AZ1, public network enabled. Whole row as recommended. |
| 4 | Alembic + driver | Delete the existing SQLite baseline migration, regenerate fresh baseline against Postgres 16. Driver: `psycopg[binary]` v3, sync API (keeps the existing sync `Session` interface). |
| 5 | Housekeeping | Remove `storage.bicep`, remove `/data` volume mount, direct connections (no PgBouncer), use Postgres for local dev via `docker compose` (not SQLite). Models use Postgres-native types freely. Provide `docker-compose.yml` and update README plus tasks. |
| 6 | Tests | All tests run against real Postgres via `docker compose` (or `testcontainers` for per-session isolation). No SQLite anywhere in the codebase after this amendment. |
| 7 | ADR handling | Amend ADR-0002 in place (rewrite Decision, Options, Implementation Notes). Do not create a new ADR. Keep ADR number 0002 as the canonical persistence ADR. Add `## Change history` section at the top with dated entries summarizing the pivot and the earlier WAL-to-DELETE amendment. Status stays `Proposed` (per adrs.instructions.md, no ADR is Accepted before implementation completes). |
| 8 | Task scope | Apply the task delta as proposed: F3.11 (postgres.bicep), F3.12 (Entra admin + role grant), F3.13 (KV admin password seed), F3.14 (cost re-check), modified F3.3/F3.4/F4.1/F4.2/F4.3, rewrite F4.T2 for Postgres. Add local-dev docker-compose task as F1.7. |

**Task ID stability**: preserved. F1.1 through F1.6, F2.x, F3.1 through F3.10, F4.4 through F4.6, all US-x, TG.x, H.x unchanged. New IDs (F1.7, F3.11, F3.12, F3.13, F3.14) appended. F3.3, F3.4, F4.1, F4.2, F4.3 amended in place with a "2026-07-02 (persistence pivot)" annotation in the task text. F4.T2 rewritten in place. This preserves any board work item IDs that map to the unchanged task IDs.

**Re-decomposition**: not required. The new tasks and modified tasks are self-contained; no need to invoke `devsquad.decompose`. `devsquad.implement` picks up from F3.11 next.

**Propagation checklist**:

| Artifact | Present? | Invalidated by amendment? | Follow-up |
|----------|----------|---------------------------|-----------|
| `docs/features/wodbuster-booking-worker/spec.md` | Yes | No. No FR touches the persistence substrate. | None. |
| `docs/features/wodbuster-booking-worker/plan.md` | Yes | Yes, five sections. Amended in same pass. | None. |
| `docs/features/wodbuster-booking-worker/data-model.md` | Inlined in `plan.md`. | Yes, header rewritten in same pass. | None. |
| `docs/features/wodbuster-booking-worker/contracts/` | Not present. | Not applicable. | None. |
| `docs/features/wodbuster-booking-worker/research.md` | Not present. | Not applicable. | None. |
| ADR-0002 | Yes | Yes, in-place rewrite. Applied in same pass. | None. |
| ADR-0005 | Yes | Yes, subsection added. Applied in same pass. | None. |
| ADR-0001, ADR-0003, ADR-0004, ADR-0006, ADR-0007 | Yes | No. | None. |
| Phase 0 feasibility report | Yes | No. | None. |
| Implementation code (`src/wodbuster_worker/persistence/{models,engine,base}.py`) | Yes | Yes (SQLite-specific). | Deferred to `devsquad.implement` (F4.1, F4.2). |
| `alembic/versions/c7808dfff47d_baseline.py` | Yes | Yes, must be deleted and regenerated. | Deferred to `devsquad.implement` (F4.3). |
| `alembic/env.py` | Yes | Yes, `render_as_batch=True` must go. | Deferred to `devsquad.implement` (F4.3). |
| `infra/modules/storage.bicep` | Yes | Yes, must be deleted. | Deferred to `devsquad.implement` (F3.3 rewrite covers). |
| `infra/modules/containerapp.bicep` | Yes | Yes, `/data` volume mount must be removed. | Deferred to `devsquad.implement` (F3.4 rewrite covers). |
| `infra/modules/postgres.bicep` | Not present. | Not applicable. | To be created by `devsquad.implement` (F3.11). |
| `docker-compose.yml` | Not present. | Not applicable. | To be created by `devsquad.implement` (F1.7). |
| `src/wodbuster_worker/config.py` | Yes | Yes (`sqlite_path` field). | Deferred to `devsquad.implement` (F4.2 covers; add `postgres_host`/`postgres_port`/`postgres_db`/`postgres_user`/`postgres_local_password`, drop `sqlite_path`). |
| `tests/component/test_migrations.py`, `tests/conftest.py`, `tests/unit/test_smoke.py` | Yes (some SQLite-specific setup). | Yes. | Deferred to `devsquad.implement` (F4.T2 rewrite covers migrations test; other tests adjusted alongside). |

**No cascade**: no downstream design artifacts invalidated beyond the ones handled in this pass. No open PR on the affected tasks (F4.1/F4.2/F4.3 have not started).

---

## 2026-07-02 — SQLite journal mode changed from WAL to DELETE (ADR-0002 amendment)

**Trigger**: first end-to-end deploy from GitHub Actions (F2.2) succeeded through the image-push stage, but the new Container App revision crashed on startup during Alembic bootstrap with `sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked` on `CREATE TABLE alembic_version`. Root cause: `PRAGMA journal_mode=WAL` requires POSIX shared memory (a `.db-shm` file with proper `mmap` semantics), and the Azure Files share mounted by Container Apps uses SMB/CIFS which does not reliably support that. The very first write attempt on a fresh database therefore deadlocks. ADR-0002 documented WAL as a considered decision but did not anticipate the SMB constraint.

**Scope classification**: low-to-medium impact amendment. One ADR (0002) implementation-notes bullet reworded; one code change (`src/wodbuster_worker/persistence/engine.py` pragma); no schema change; no task additions; no external contract change. Not a supersede — the tool choice (SQLite on Azure Files with application-layer AES-256-GCM cookie encryption) stands. Whole-feature escalation check: no signal tripped.

**Cascade check**: one prior 2026-07-02 amendment on this feature (GitHub Actions provisioning). Different area; no compounding pattern.

**Fix**:

1. `engine.py`: `PRAGMA journal_mode=WAL` → `PRAGMA journal_mode=DELETE`. Kept `foreign_keys=ON` and `synchronous=NORMAL`.
2. ADR-0002 "Implementation Notes" bullet rewritten to reflect the new journal mode and the SMB rationale.

**Trade-off**: DELETE mode has higher write latency than WAL because every commit rewrites the rollback journal. Acceptable for our workload — a handful of transactions per booking cycle (roughly one per minute at peak) — and further mitigated by the `max-replicas=1` single-writer invariant that guarantees no read-writer or writer-writer contention.

**Alternatives rejected**:

- **Add `nobrl` mount option to the Azure Files share**: Container Apps does not expose CIFS mount options in its `managedEnvironments/storages` resource; this is not a configurable knob for us.
- **Move to `journal_mode=MEMORY`**: crash-unsafe, would lose the last transaction on any container kill.
- **Move SQLite off Azure Files (to a Container App emptyDir volume)**: violates ADR-0002's persistence priority. The DB must survive revision rollovers.

**Propagation checklist**:

| Artifact | Present? | Invalidated? | Follow-up |
|----------|----------|--------------|-----------|
| ADR-0002 | Yes | Yes, one bullet. Amended in same pass. | None. |
| `plan.md` | Yes | No, no reference to WAL specifically. | None. |
| `spec.md` | Yes | No. | None. |
| `tasks.md` | Yes | No, F4.2 wording is generic ("configure the SQLite engine"). | None. |
| Other ADRs | Yes | No. | None. |
| Tests | Yes | No test asserts `journal_mode=WAL` today. | None. |

---

## 2026-07-02 — GitHub Actions as provisioning source of record (amendment)

**Trigger**: agent classification `spec` — CD deployment surface described in `plan.md` and referenced from ADR-0007 did not match the operator's decision to run both `azd provision` and `azd deploy` from GitHub Actions after bootstrap. Detected during `devsquad.refine` health analysis (turn 1). No task had started implementation on F2.2 or F3.5 yet, so no in-flight rework.

**Scope classification**: medium-to-high impact amendment. Touches two ADRs (0005, 0007), one plan section (Engineering Practices CD row), and one task file (adds F2.3, F2.4, F3.10; rewrites F2.2; annotates F3.5). Not a new feature. Whole-feature escalation check: no signal tripped (single feature, no outcome shift, no story invalidation, no hierarchy change, no priority story rewrite, no re-envisioning).

**Cascade check**: no prior amendment on this slice. Not a repeated pattern.

**Propagation checklist**:

| Artifact | Present? | Invalidated by amendment? | Follow-up |
|----------|----------|---------------------------|-----------|
| `docs/features/wodbuster-booking-worker/plan.md` | Yes | Yes, CD row only. Amended in same pass. | None. |
| `docs/features/wodbuster-booking-worker/spec.md` | Yes | No. No FR touches CI/CD topology. | None. |
| `docs/features/wodbuster-booking-worker/data-model.md` | Inlined in `plan.md`. | No. | None. |
| `docs/features/wodbuster-booking-worker/contracts/` | Not present. | Not applicable. | None. |
| `docs/features/wodbuster-booking-worker/research.md` | Not present. | Not applicable. | None. |
| ADR-0001 through ADR-0004, ADR-0006 | Yes | No. | None. |
| Phase 0 feasibility report | Yes | No. | None. |

**Q&A summary** (operator inputs, turn 2):

| # | Question | Answer |
|---|----------|--------|
| 1 | Deployment triggers | Auto on push to `main` when `infra/` or `azure.yaml` change (infra workflow), plus `workflow_dispatch` on both mutating workflows. |
| 2 | Workflow layout | Two mutating workflows: `.github/workflows/infra.yml` and `.github/workflows/deploy.yml`. Plus one PR-preview workflow (see Q4). |
| 3 | Approval gate | None. `azd provision` applies immediately on trigger. Operator accepts the risk for MVP; revisit later. |
| 4 | PR preview | Yes. `azd provision --preview` on PRs touching infra, diff posted as a PR comment. Read-only. |
| 5 | Identity model | Separate deploy UAMI dedicated to GitHub Actions. Runtime UAMI (`id-yrv2tv7mfjvma`) unchanged (Key Vault Secrets User + AcrPull). |
| 6 | RBAC scope | Contributor + User Access Administrator on the resource group only. No subscription-scope permissions. RG creation stays a one-time laptop step. |
| 7 | Federated credentials | `main` branch + `pull_request` on the deploy UAMI. No fork PR support. No environment-scoped credential. |
| 8 | Laptop provisioning | Forbidden by convention after cutover. Only allowed for zero-subscription bootstrap. Documented in README and amended ADRs. |

**Amendment applied**:

1. ADR-0007 — Priority 4 rewritten; Decision paragraph rewritten; Implementation Notes gained a CI/CD topology subsection (three workflows, no approval gate, no fork PR support, laptop forbidden after F3.10).
2. ADR-0005 — Implementation Notes gained a "CI/CD identity (deploy UAMI)" subsection with two-identity model (runtime + deploy), federated credential subjects, deliberate exclusion of the deploy UAMI from Bicep, and OIDC-only authentication.
3. `plan.md` — Engineering Practices CD row rewritten to reflect Actions-driven provisioning and the three workflows.
4. `tasks.md` — F2.2 narrowed to app-code deploy only; F2.3 (infra workflow) and F2.4 (preview workflow) added; F3.5 annotated as bootstrap-only; F3.10 added as one-time operator setup for the deploy UAMI, RBAC, and federated credentials.

**Task ID stability**: preserved. F2.1, F3.1–F3.9, F4.x, all US-x, TG.x, H.x unchanged. New IDs (F2.3, F2.4, F3.10) appended.

**Re-decomposition**: not required. F2.3, F2.4, and F3.10 are small and self-contained; no need to invoke `devsquad.decompose`.

**No cascade**: no downstream design artifacts invalidated. No open PR or in-flight branch exists on the affected tasks (F2.2 and F3.5 have not started implementation).
