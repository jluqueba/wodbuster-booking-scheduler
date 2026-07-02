# Reasoning Log: WodBuster Booking Worker

Amendments and non-trivial decisions logged during implementation of this feature. Entries are appended, never rewritten. Ordered newest-first.

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
