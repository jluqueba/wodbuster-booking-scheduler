# Persistence Engine

**Status**: Proposed
**Date**: 2026-06-29
**Last amended**: 2026-07-02

## Change history

- **2026-07-02 (later that day)** — pivot from SQLite-on-Azure-Files to Azure Database for PostgreSQL Flexible Server. Trigger: the WAL-to-DELETE amendment earlier the same day (see next entry) resolved one symptom but left the whole persistence substrate on shared-file storage whose failure modes accumulated faster than the "operational simplicity" priority anticipated. Reconsidering the option matrix with the freshly-discovered SMB constraint, plus two options previously discarded without full evaluation (SQLite on a Container App emptyDir volume, SQLite on Azure Blob Storage via BlobFuse2), tipped the balance to a managed Postgres. Priority 5 (monthly cost under 5 EUR) is deliberately relaxed to roughly 13-15 EUR per month; the operator accepted the delta in exchange for a Postgres-grade substrate with PITR backups, real transactional isolation, native JSON, and no coupling between the write-safety story and the ACA `max-replicas=1` invariant. Full option re-analysis in "## Options Considered" below.
- **2026-07-02 (earlier that day)** — SQLite journal mode changed from `WAL` to `DELETE`. Trigger: the first end-to-end deploy from GitHub Actions succeeded through the image-push stage but the new Container App revision crashed on startup with `sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked` on `CREATE TABLE alembic_version`. Root cause: `PRAGMA journal_mode=WAL` requires POSIX shared-memory (a `.db-shm` file with proper `mmap` semantics) and the Azure Files share mounted by Container Apps uses SMB/CIFS, which does not reliably support that. `DELETE` mode uses only a plain `.db-journal` file (regular POSIX I/O) that SMB handles correctly. This amendment is superseded by the pivot above and is retained here as history.

## Context

The worker persists eight entity families (operator profile, scheduler rule, class preference, cookie credential, booking outcome, vacation window, heartbeat reading, alert) declared in the spec's Key Entities section. The total expected row count for a single-user MVP is on the order of a few thousand rows over the first year (one heartbeat per hour, a few booking outcomes per week, a handful of rules, a handful of alerts). Booking history is retained indefinitely (FR-032).

Two invariants tighten the choice:

1. The persisted `.WBAuth` cookie value is encrypted at rest, and the encryption key is never co-located with the encrypted blob in operator-visible storage (spec Invariants).
2. The booking-outcome write must be durable before any notification is dispatched (spec Failure Modes, consistency model).

Hosting is Azure Container Apps with one always-on replica and a mounted Azure Files share (ADR-0001).

## Priorities and Requirements (ordered)

1. **Durability across container restarts and revisions**. State must survive replica replacement.
2. **Transactional write-then-notify semantics**. A single SQL transaction must encompass the outcome write before the notification dispatcher reads it.
3. **Operational simplicity at single-user scale**. No DBA. No connection-pool tuning. No backup pipelines to design.
4. **Encryption at rest of the cookie blob**. Application-layer encryption with the key held in Key Vault (ADR-0005), independent of the storage layer's at-rest encryption.
5. **Monthly cost under 5 EUR**. The dataset is small; a managed RDBMS would be overkill at this scale.

## Options Considered

The three original options are reconsidered below with what we learned during the first end-to-end deploy on 2026-07-02. Two new options (SQLite on a Container App emptyDir volume, SQLite on Azure Blob Storage via BlobFuse2) are added because they were dismissed during the original write-up without a full priority sweep and the pivot decision hinges on comparing them side-by-side with the retained candidates.

### Option 1: SQLite on a mounted Azure Files share (SMB), application-layer encryption for the cookie column

The worker writes to a single SQLite file located on the Azure Files share mounted at a fixed path inside the container. Schema owned by Alembic. Cookie column encrypted at the application layer with a Key Vault-held key.

**Evaluation against priorities**:

- **Durability**: Meets. ZRS-replicated, survives revision replacement.
- **Transactional write-then-notify**: Partially meets. ACID over one file, but see the write-latency and journal-mode caveats.
- **Operational simplicity**: Fails after the 2026-07-02 discovery. `journal_mode=WAL` is not usable because SMB does not reliably provide the POSIX shared-memory semantics WAL needs; the concrete crash is `database is locked` on the first `CREATE TABLE`. Fallback to `journal_mode=DELETE` works but forces higher write latency and hard-couples correctness to the `max-replicas=1` single-writer invariant. Any accidental scale-out silently corrupts the file. There is no PITR; the backup path is a hand-rolled snapshot job. Alembic bootstrap is brittle in a way the priority set explicitly wanted to avoid.
- **Encryption at rest of the cookie blob**: Meets via application-layer AES-GCM.
- **Monthly cost**: Meets at approximately 0.30 EUR per month.

### Option 2: SQLite on a Container App emptyDir volume

The SQLite file lives on an emptyDir volume provisioned by the Container App. No shared storage.

**Evaluation against priorities**:

- **Durability**: Fails. emptyDir is scoped to the revision's lifetime; any revision rollover, restart, or scale-in loses the entire database. Booking history retention (FR-032) becomes impossible.
- **Transactional write-then-notify**: Meets while the volume lives, but is moot given the durability failure.
- **Operational simplicity**: Meets on paper (no shared-storage caveats), but the durability failure disqualifies the option.
- **Encryption at rest**: Meets via application layer.
- **Monthly cost**: Meets (no separate storage cost).

Discarded due to durability failure.

### Option 3: SQLite on Azure Blob Storage via BlobFuse2

The SQLite file lives on a blob container mounted into the Container App via BlobFuse2.

**Evaluation against priorities**:

- **Durability**: Meets in principle (blobs are LRS or ZRS-replicated).
- **Transactional write-then-notify**: Fails. BlobFuse2 is documented as unsuitable for databases and other applications requiring POSIX file semantics, `fsync` durability, or locking; SQLite explicitly falls in that category. Data-loss and corruption modes are documented failure modes, not tail risks.
- **Operational simplicity**: Fails. Additional sidecar, additional failure surface, no Microsoft support statement for the SQLite-on-BlobFuse2 combination.
- **Encryption at rest**: Meets via application layer.
- **Monthly cost**: Meets.

Discarded due to correctness failure.

### Option 4: Azure Database for PostgreSQL Flexible Server, Burstable B1ms

A managed Postgres instance with one Burstable B1ms node, accessed by the worker over public network with firewall rules and Microsoft Entra ID authentication (no password on the runtime path). SQLAlchemy as ORM, `psycopg[binary]` v3 as sync driver. Cookie column encrypted by application layer the same way as Option 1.

**Evaluation against priorities**:

- **Durability**: Meets and improves. Managed backups, 7-day PITR retention, ZRS-backed storage. No dependence on ACA `max-replicas=1` for data safety.
- **Transactional write-then-notify**: Meets. Real Postgres transactions, real advisory locks if we ever need them, real row-level MVCC. `SELECT ... FOR UPDATE SKIP LOCKED` unlocks a proper outbox dispatcher pattern later.
- **Operational simplicity**: Meets more cleanly than Option 1 in practice. Yes, we now depend on firewall-rule management and Entra ID role grants, but neither is dynamic: both are one-time operator steps. In exchange we drop the SMB-vs-WAL trap, the manual snapshot backup script, the ACA scale-out footgun, and the Alembic-on-SMB fragility. On balance, less surprising surface, not more.
- **Encryption at rest of the cookie blob**: Meets via application layer, unchanged from Option 1. Postgres native TDE is additional defense in depth.
- **Monthly cost**: Fails the original priority-5 target of 5 EUR. Actual is roughly 13-15 EUR per month for B1ms + 32 GiB autogrow + 7-day backup. Priority 5 is deliberately relaxed by this ADR (the operator chose to pay the delta for the substrate quality).

### Option 5: Azure Cosmos DB free tier (NoSQL API)

Unchanged from the original write-up.

- **Durability**: Meets.
- **Transactional write-then-notify**: Partially meets. Cross-document transactions require the same partition key. Viable but constrained.
- **Operational simplicity**: Fails for the relational entity shape (rules with ordered preferences, outcomes joined to rules).
- **Encryption at rest**: Meets via application layer.
- **Monthly cost**: Meets (free tier).

Discarded due to poor fit for the relational entity model.

## Decision

Azure Database for PostgreSQL Flexible Server, Burstable B1ms, Postgres 16, in Spain Central AZ1, with 32 GiB autogrow storage, 7-day backup retention, no HA, public network access enabled, firewall-rules-plus-Entra-ID-authentication. Application-layer AES-256-GCM encryption for the `.WBAuth` cookie value is retained; the encryption key remains in Azure Key Vault (ADR-0005) and is fetched at container startup via the runtime user-assigned managed identity.

This is a pivot from the original decision (SQLite on Azure Files) made after the first end-to-end deploy revealed that SMB-backed SQLite is unsafe for our Alembic-bootstrap and rollback-journal patterns and that the mitigation stack keeps growing. The three retained SQLite-on-shared-storage variants (Options 1, 2, 3) each fail at least one non-negotiable priority once that lens is applied. Postgres (Option 4) meets every priority except cost, and the operator has accepted the cost delta in exchange for the substrate quality. Cosmos DB (Option 5) remains a poor fit for the relational model.

Priority 5 (monthly cost under 5 EUR) is deliberately relaxed to roughly 13-15 EUR per month by this decision. The three-priority argument that made SQLite attractive originally (durability, transactional semantics, operational simplicity) is now the argument that makes Postgres attractive: at a single-user scale the failure modes are what matter, not the ORM's SQL dialect.

## Implementation Notes

### Sizing and topology

- **Instance**: Postgres 16, Burstable B1ms (1 vCore, 2 GiB RAM), Spain Central, availability zone 1, no HA.
- **Storage**: 32 GiB with storage autogrow enabled; single-user workload is well under 1 GiB projected over the first year, so autogrow is a headroom guarantee, not a running cost driver.
- **Backups**: 7-day PITR retention, locally-redundant. No geo-redundant backups.
- **Network**: public network access enabled, firewall-rule based. No VNet integration, no private endpoint. Firewall allows the Container Apps environment's outbound IPs plus the operator's home IP (for DBA access from psql). See ADR-0005 for the identity/network coupling.

### Authentication and role model (three-principal)

Three principals hold three disjoint sets of privileges. ADR-0005 documents the identity plumbing; this ADR documents what each principal is allowed to do inside the database.

| Principal | Type | Purpose | Privileges |
|-----------|------|---------|------------|
| `wodbadmin` | Postgres password login. | Break-glass admin. Password seeded from Key Vault secret `postgres-admin-password`. Never used on the runtime path. | Superuser (server admin login). Used only when the operator is locked out of the Entra path. |
| Operator's Entra user | Microsoft Entra ID (AAD) group or user. | Day-to-day DBA. Runs Alembic manually if the container path breaks, inspects data with psql, grants privileges to the runtime UAMI. | `azure_pg_admin` (Entra admin role on the server). Effective owner of the `public` schema after first bootstrap. |
| Runtime UAMI (`id-{token}`) | Microsoft Entra managed identity. | The Container App revision's runtime principal. Used by the app for CRUD and by Alembic (executed inside the container startup) for migrations. | `LOGIN`, `CONNECT` on the database, `USAGE` and `CREATE` on schema `public`. Manually granted by the operator after F3.12. No superuser, no other role membership. |

Rationale: the operator's Entra user is the schema owner so that DBA sessions do not depend on Key Vault at all (Entra token acquisition is enough). The runtime UAMI has just enough grant to run `alembic upgrade head` at container startup and to CRUD every table. The password admin exists only for the case where the Entra path itself is broken; it is never in the container's environment.

### Driver and ORM

- SQLAlchemy 2.x, sync API (unchanged from the original design; the worker is not I/O-bound on the DB).
- Driver: `psycopg[binary]` version 3, sync. The `binary` extra ships the C extension so no build toolchain is required in the container image.
- Connection URL is composed at startup from Container App env vars (`POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`) plus an Entra access token obtained via `DefaultAzureCredential` (the runtime UAMI). The token is short-lived; the engine is configured to fetch a fresh one on connection acquisition.
- No PgBouncer, no pooler. Direct connections to the Flexible Server. Single-writer replica keeps concurrent connection count trivially low.

### Schema migrations

- Alembic, applied at container startup before uvicorn accepts traffic (unchanged pattern; new substrate).
- The existing SQLite baseline migration (`alembic/versions/c7808dfff47d_baseline.py`) is deleted. A fresh baseline is generated from the SQLAlchemy models against a live Postgres 16 instance. Migration numbering restarts at the new baseline.
- Postgres-native types are used freely in the models now that SQLite compatibility is not a constraint: `JSONB` for `notification_outbox.payload` and `alert.payload`, native `ENUM` types where enumerated status columns exist today as `VARCHAR + CHECK`, `TIMESTAMPTZ` for all timestamps, arrays where the shape naturally calls for one, and partial unique indexes via `postgresql_where=` (replacing the current `sqlite_where=` on the `alert` open-row uniqueness index).

### Local development

- Local development runs against a real Postgres 16 container via `docker compose`, not SQLite. This eliminates the local/prod dialect gap and lets every test exercise the same substrate as production.
- The `docker-compose.yml` provides one Postgres 16 service with a named volume for `pgdata`, exposing 5432 to the host, seeded with a local-only password. The worker's local `.env` points `POSTGRES_HOST=localhost` and the driver connects via password locally (Entra auth is not exercised in the dev loop; that path is unit-tested at the token-acquisition layer).
- Tests run against Postgres too (see F4.T2, rewritten). Choice between a session-scoped docker-compose Postgres or per-session `testcontainers` is left to the implementation task; both are equivalent for correctness.

### Consistency contract (unchanged)

- Every state-mutating write that produces an operator-visible signal writes the entity row and the corresponding `notification_outbox` row in the same SQLAlchemy transaction. The dispatcher polls the outbox after commit. This survives the substrate change verbatim.
- `cookie_credential.cookie_ciphertext` remains AES-256-GCM under the Key Vault-held key. Plaintext never persists. The nonce is stored alongside the ciphertext.
- The `alert` table's "at most one open row per `(operator_id, kind)`" invariant is now expressed as a partial unique index using `postgresql_where=text("closed_at IS NULL")`.

## References

- `docs/features/wodbuster-booking-worker/spec.md` Key Entities, Invariants, Failure Modes consistency model.
- `docs/features/wodbuster-booking-worker/reasoning-log.md` for the dated amendment history of this ADR, including the WAL-to-DELETE amendment and the Postgres pivot.
- `docs/architecture/decisions/0001-hosting-service.md` for the always-on single-replica hosting decision.
- `docs/architecture/decisions/0005-secrets-and-identity-access.md` for the cookie encryption key custody, the runtime UAMI, and the Postgres connection identity subsection.
- `docs/architecture/decisions/0007-iac-tooling.md` for the Bicep module `infra/modules/postgres.bicep` that provisions the server described here.

