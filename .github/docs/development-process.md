# Development Process

This file defines how work is planned, built, reviewed and merged in this repository. It is adapted from the process used in the sibling SphereLoom project and recorded in ADR-0016 (`docs/internal/architecture/decisions/0016-development-and-review-process.md`).

It complements [coding-guidelines.md](coding-guidelines.md), which covers how code is written. Where the two overlap, they state the same rule.

## Language

- Talk to the maintainer in the language the maintainer uses.
- Write every repository artifact in English: code, comments, commit messages, branch names, issues, pull requests, specs, plans, task lists, ADRs and documentation.

## Work Tracking

Issues are the unit of work. Milestones group issues into phases. The private GitHub Project "WodBuster Booking Scheduler" (`jluqueba` project 3) holds status, priority and size. Each piece of information lives in exactly one place.

- **One issue per pull request.** Every planned pull request has an issue, and every pull request closes exactly one issue with a closing keyword (`Closes #N`) in its description. Work without an issue gets one before it starts, including work discovered during another task. Dependabot pull requests are exempt.
- **The milestone goes on the issue, never on the pull request.** GitHub counts both in a milestone, so setting it on both counts every task twice.
- **Labels.** Issues carry one or more `area: *` labels, a type label (`enhancement`, `bug`, `documentation`) and a size label (`size: S`, `size: M`, `size: L`). Pull requests carry the same area and type labels.
- **Board status.** `Todo` when planned, `In Progress` when work starts (assign the issue at the same time), `In review` when the pull request opens, `Done` when the pull request merges and closes the issue. Set each transition explicitly and verify it after merging.
- **Priority.** The `Priority` field (`P1`, `P2`, `P3`) is set only to override the planned order. An empty priority means issue number order within the milestone. The next task is the highest priority `Todo` issue whose dependencies are closed, in the milestone in progress.
- **Progress report.** After every merge, report the open and closed issue counts of the milestone.
- **Rolling wave detail.** Before work on a milestone starts, split it into pull-request-sized issues, each with acceptance criteria taken from its spec, a size and its dependencies. A milestone without a spec keeps a few high-level issues, the first of which is to write that spec.
- **Issues are public.** Describe behaviour, not file paths, and summarise design context instead of copying anything from `docs/internal/**`, which is encrypted. Never paste cookies, tokens, gym credentials or personal data.
- A task that spans two milestones is too large. Split it.

The feature `tasks.md` files under `docs/internal/features/` remain the design breakdown. Issues and the board are the source of truth for order, scope and status.

## Design Before Code

- Read the relevant spec, plan and ADRs before coding. Do not implement behaviour that contradicts an accepted spec, plan, task list or ADR. If an instruction and an approved artifact conflict, stop and align the artifacts first.
- **ADR threshold.** Write an ADR only for a decision that changes an accepted ADR or sets a rule that crosses features. Record other design decisions as an amendment to the feature spec or plan.
- **Risky changes.** A change is risky when it does any of the following:
  - acts on a real WodBuster account (booking, cancelling, renewing the session);
  - handles credentials, session cookies, OAuth tokens or secrets;
  - adds a schema migration, or deletes or rewrites stored data;
  - parses HTML or JSON returned by WodBuster or another external service.

  When in doubt, treat the change as risky.
- **Invariants.** Before writing code, list the invariants the change must hold and the test that proves each one. Typical invariants here: bounds on work and size when handling external input; the outcome recorded after an ambiguous booking response (a booking WodBuster may have accepted is never retried blindly); what a migration does on upgrade and downgrade; what is written to the database and how it is cleaned up on failure. Every pull request lists them in its description. A risky change also gets them approved by the maintainer before coding starts. Other changes go straight to code.
- **Vertical slice first.** Deliver a working end-to-end path before broadening the foundations under it.

## Checkpoints With the Maintainer

Batch questions into checkpoints instead of asking them one at a time.

| Checkpoint | When | Content |
| --- | --- | --- |
| Design | Risky changes only, before coding | Approach, trade-offs, invariants and their tests |
| Preview | User-visible changes only, before committing | The application running locally, as described in [coding-guidelines.md](coding-guidelines.md) |
| Merge | Every pull request | CI and review status, triaged findings, anything left open |

## Pull Request Scope

- One topic, and at most about 400 lines of production code plus its tests. Split before opening, not during review.
- Do not combine more than one of these in the same pull request: session cookie and authentication custody, the booking executor, a schema migration.
- A design decision that emerges during review lands in its own small documentation pull request (a spec or plan amendment), not inside the feature pull request. Closing the feature pull request must never lose the decision.
- Use the pull request template. List every validation command that was run together with its result.

## Branches, Commits and Merging

- Branch from `main` with one of these prefixes and a short slug: `feat/`, `fix/`, `docs/`, `chore/`, `refactor/`, `test/`, `ci/`.
- Commit messages and pull request titles follow Conventional Commits.
- Pull requests are squash-merged using the pull request title, so the title must be a valid Conventional Commit.
- Never commit to or push `main` directly.

## Before Pushing

These checks are mandatory for every change, whatever the review triage below decides:

- `.\check.ps1` on Windows or `make check` on Linux passes. CI runs the same checks on Linux.
- Never assert a platform's own behaviour. When behaviour differs between Windows and Linux, inject the failure through a seam instead of provoking it, so the test asserts the guarantee rather than the quirk. Use `skipif` only when injection is impossible.
- Inject time, sleeping and failures through seams the production code already defines. Never synchronise a test on wall-clock time, and never prove that work is bounded with a stopwatch.
- **Mutation check.** For every test written to catch a regression, or to guard a security property or a bound, first confirm it passes on the unmodified code, then reintroduce the defect and confirm it fails. A test that cannot fail protects nothing.
- **Adversarial review of the diff** before opening the pull request, for example with the code review agent.

## Automated Review and Merging

Copilot code review runs on every push to an open pull request (`review_on_push`). A pull request is ready to merge when:

1. CI (`ci-gate`) is green;
2. the most recent Copilot review examined the current head commit;
3. that review, including its "Previously missed" section, contains no unresolved correctness or security finding.

A fixing push starts a new review, which can surface new findings, including ones the fix introduced. Wait for it and triage it the same way. Do not widen the scope of a pull request while it is under review.

Allow at most **three review cycles** per pull request. If blocking findings remain after the third, stop, explain why the change is not converging, and propose splitting or narrowing it.

### Triage Findings Before Acting

Only two kinds of finding block a merge:

- **Correctness.** Wrong behaviour on some input, a wrong or misleading outcome or error, a test that cannot fail or exercises a different branch than it claims, a CI or merge gate that can pass when it should not.
- **Security.** A leak of secrets, cookies or personal data, a missing authentication or authorization check, a cross-user data leak, unbounded work or memory on external input, an off-origin request.

Everything else is **minor** and does not block: wording, typos, style, naming, consistency, and hardening against a condition no realistic input can trigger. Reply on the thread that it was judged minor under this rule, resolve it, and do not push a commit for it. A minor finding can be folded into a later change that touches the same code.

Reproduce a claimed defect before fixing it. A finding that does not reproduce is answered on its thread with the evidence and resolved. When a defect is real, find every occurrence of its class before fixing it, and add an automated rule (a check in `tests/unit/test_codebase_rules.py`) where one is possible.

Resolve a thread only after the finding is addressed, never to unblock a merge.

### Read the Review Body, Not Only the Inline Comments

Findings are reported in two places, and reading only one of them hides the rest.

| Where | How to read it | What only it contains |
| --- | --- | --- |
| Inline threads | `gh api --paginate repos/jluqueba/wodbuster-booking-scheduler/pulls/<n>/comments`, filtered to the login `Copilot` | Findings attached to changed lines |
| Review body | `gh pr view <n> --json reviews,headRefOid`, newest review whose author is `copilot-pull-request-reviewer` | Severity counts and "Previously missed", which lists findings in code that has not changed since the last review |

To read the latest Copilot review together with the commit it examined:

```powershell
gh pr view <n> --json reviews,headRefOid --jq '([.reviews[] | select(.author.login == "copilot-pull-request-reviewer")] | last) as $r | {head: .headRefOid, reviewed: $r.commit.oid, body: $r.body}'
```

If `reviewed` differs from `head`, the review of the latest push has not arrived yet.

- Never take the latest review or comment without filtering by author and reading every page. The REST endpoints return 30 items per page and include maintainer replies. `gh pr view` fetches every review.
- Never filter findings by "created after my last push". That filter cannot show an older finding that is still outstanding. Use timestamps only to identify which review cycle is current.
- The reviewer appears under three logins: `copilot-pull-request-reviewer[bot]` in `/pulls/N/reviews`, `Copilot` in `/pulls/N/comments`, and `copilot-pull-request-reviewer` in GraphQL. Match the set exactly.
- Thread resolution state (`isResolved`) is available only through GraphQL.
- Treat anything unreadable as not clean. A check that cannot read its input never reports success.
- Automated review cannot read `docs/internal/**`, which is encrypted. Changes there need the maintainer.

## After Merging

- Verify the issue is closed and its board item is `Done`.
- Report the open and closed counts of the milestone.
- Delete the local branch and switch back to an up-to-date `main`.
