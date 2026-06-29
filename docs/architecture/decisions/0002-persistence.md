# Persistence Engine

**Status**: Proposed
**Date**: 2026-06-29

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

### Option 1: SQLite on a mounted Azure Files share, application-layer encryption for the cookie column

The worker writes to a single SQLite file located on the Azure Files share mounted at a fixed path inside the container. Schema is owned by the application and migrated by Alembic. The cookie value column stores the AES-256-GCM ciphertext produced inside the application using a key fetched from Key Vault at startup. The plaintext cookie never touches disk.

**Evaluation against priorities**:
- **Durability**: Meets. Azure Files is replicated (ZRS) and survives container replacement. The SQLite file is the single source of truth.
- **Transactional write-then-notify**: Meets. SQLite provides ACID over a single file. The outcome write and the notification-enqueue write happen in one transaction; the dispatcher polls the queue table after commit.
- **Operational simplicity**: Meets. One file, no server process, no migrations beyond Alembic. The container restarts cleanly: open the database file, replay any pending notifications.
- **Encryption at rest of the cookie blob**: Meets. Application-layer AES-GCM with a Key Vault key satisfies the invariant that key and blob are not co-located in operator-visible storage. Azure Files at-rest encryption is an additional defense in depth.
- **Monthly cost**: Meets. Azure Files Standard 5 GiB ZRS hot tier costs approximately 0.30 EUR per month.

### Option 2: Azure Database for PostgreSQL Flexible Server, Burstable B1ms

A managed Postgres instance with one Burstable B1ms node, accessed by the worker over a private endpoint or VNet integration. SQLAlchemy as ORM. Cookie column encrypted by application layer the same way as Option 1.

**Evaluation against priorities**:
- **Durability**: Meets. Managed backups and PITR.
- **Transactional write-then-notify**: Meets. Postgres provides full ACID.
- **Operational simplicity**: Partially meets. Managed PaaS reduces work, but the worker now depends on networking configuration (firewall rules or VNet integration), connection-pool tuning under burstable CPU, and a separate maintenance schedule.
- **Encryption at rest of the cookie blob**: Meets via application layer, same approach as Option 1.
- **Monthly cost**: Fails the priority-5 target. B1ms is approximately 13 EUR per month plus storage and backup cost, roughly an order of magnitude above what the dataset requires.

### Option 3: Azure Cosmos DB free tier (NoSQL API)

A Cosmos DB account in free-tier configuration (1000 RU/s, 25 GB storage included). Documents per entity family. Application-layer encryption for the cookie property.

**Evaluation against priorities**:
- **Durability**: Meets.
- **Transactional write-then-notify**: Partially meets. Cross-document transactions in Cosmos require the same partition key. The natural partition key is the operator ID, which keeps the design viable, but the model is more constrained than SQL and requires careful design of the outcome-plus-notification pair.
- **Operational simplicity**: Fails for this workload. The relational shape of the entities (rules with ordered preferences, outcomes joined to rules) is a poor fit for a document store. SQL access patterns (booking history listing with filters, vacation-window overlaps) become awkward.
- **Encryption at rest of the cookie blob**: Meets via application layer.
- **Monthly cost**: Meets (free tier covers single-user usage).

## Decision

SQLite on a mounted Azure Files share, with application-layer AES-256-GCM encryption for the `.WBAuth` cookie value. The encryption key is held in Azure Key Vault (ADR-0005) and fetched once at container startup via the user-assigned managed identity.

This option uniquely meets every priority. Postgres adds operational and cost overhead unjustified by the dataset size. Cosmos DB is a poor fit for the relational entity model and the transactional outcome-plus-notification pattern.

## Implementation Notes

- Schema migrations: Alembic, applied at container startup before the FastAPI app accepts traffic. The migration step runs idempotently against the mounted file.
- WAL mode: SQLite is opened with `journal_mode=WAL` and `synchronous=NORMAL` to balance durability and write latency on Azure Files. Crash safety against container kill is provided by WAL; corruption risk from mounted-share semantics is mitigated by single-replica hosting (ADR-0001 `max-replicas=1`).
- Single writer: ACA with `max-replicas=1` is a hard invariant for this design. Scaling out would require switching to Option 2 because Azure Files plus SQLite is not safe with concurrent writers across nodes.
- Backup strategy: A daily snapshot of the SQLite file is copied to a sibling folder on the same Azure Files share by an APScheduler job. For a single-user MVP this is adequate. A future ADR can introduce Postgres if multi-user demands appear.
- The notification dispatcher reads pending outcomes from an `outcome` table joined with a `notification_outbox` table. Outcome plus outbox row are written in one transaction (FR-012 plus FR-025).

## References

- `docs/features/wodbuster-booking-worker/spec.md` Key Entities, Invariants, Failure Modes consistency model.
- `docs/architecture/decisions/0001-hosting-service.md` for the always-on single-replica constraint.
- `docs/architecture/decisions/0005-secrets-and-identity-access.md` for the cookie encryption key custody.
