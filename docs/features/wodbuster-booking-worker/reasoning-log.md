# Reasoning Log: WodBuster Booking Worker

Amendments and non-trivial decisions logged during implementation of this feature. Entries are appended, never rewritten. Ordered newest-first.

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
