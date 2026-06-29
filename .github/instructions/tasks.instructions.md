<!-- devsquad-template: .github/instructions/tasks.instructions.md vunknown sha=494e6b01bfb9 -->
---
name: 'Task Lists'
description: 'Guidelines for creating and editing task decomposition files'
applyTo: 'docs/features/**/tasks.md'
---

When editing task lists, follow these rules:

- Tasks MUST be organized by user story to enable independent implementation.
- Format for each task: `- [ ] [P?] Description with file path`
- [P] indicates a parallelizable task.
- Required phases: Setup, Foundational, User Stories (P1, P2, P3...), Polish.
- Within each story: Models -> Services -> Endpoints -> Integration.
- Each phase must be a complete and independently testable increment. The first task in each user story should be a tracer bullet: a minimal end-to-end vertical slice through all layers that proves the architecture works. Subsequent tasks fill in remaining cases, error handling, and edge cases.
- DO NOT generate separate test tasks. Tests are part of each task's acceptance criteria — the implement agent verifies coverage upon completion.
- Missing ADRs must be blocking tasks in the Foundational phase.
- When creating work items on the board, apply the checklist from the `work-item-creation` skill.
