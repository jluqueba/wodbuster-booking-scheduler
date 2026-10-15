# Pull Request

## Summary

What changed, and why.

-

## Linked Issue

Closes #

## Type of Change

- [ ] Feature
- [ ] Bug fix
- [ ] Documentation
- [ ] Refactoring
- [ ] Tooling or CI
- [ ] Other:

## Design Artifacts

Specs, plans, tasks or ADRs this pull request follows or amends (paths under `docs/internal/`, which is encrypted):

- Spec:
- Plan:
- ADR:

## Impact

- Breaking changes: none
- Infrastructure or configuration coordination (Bicep, app settings, Key Vault secrets, migrations to run): none

## Invariants

For code changes, list each invariant this pull request must hold and the test that proves it: bounds on work and size when handling external input, the outcome recorded after an ambiguous WodBuster response, migration upgrade and downgrade behaviour, and what is written to the database and how it is cleaned up on failure. Risky changes (acting on a WodBuster account, credentials or cookies, migrations or data deletion, parsing external responses) had these approved before coding. Write "None" for documentation-only changes.

-

## Checklist

- [ ] One topic, at most about 400 lines of production code (tests excluded).
- [ ] Closes exactly one issue; the milestone is on that issue, not on this pull request. Area and type labels set.
- [ ] `.\check.ps1` or `make check` passes (ruff, mypy, djlint, pytest).
- [ ] Every new regression, security or bound test was mutation-checked: it passes on the final code and fails when the defect is reintroduced.
- [ ] User-visible changes were seen running locally by the maintainer before committing.
- [ ] Documentation is updated where needed.
- [ ] No secrets, cookies, tokens or personal data in code, tests, fixtures or this description.
- [ ] Title follows Conventional Commits and is suitable for the squash commit.

## Validation

List every command that was run and its result (for example `.\check.ps1`, passed). Commands listed without a result do not count; state anything that was not run and why.

| Command | Result |
| ------- | ------ |
|         |        |
