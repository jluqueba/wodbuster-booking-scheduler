# Feature Specification: Phase 0 API Discovery Spike

- **Created on**: 2026-06-29
- **Status**: Draft

## Executive Summary

- **Objective**: Investigate whether WodBuster exposes a usable API for programmatic class booking and produce a feasibility report that acts as the go or no-go gate for the rest of the project.
- **Primary user**: The project owner, acting as both investigator and decision maker.
- **Value delivered**: Resolves the single unknown that blocks every other piece of work. Cheap to run, expensive to skip. A no-go outcome saves weeks of misdirected engineering. A go outcome unlocks the production worker plan with documented evidence.
- **Scope**: Manual investigation against the live WodBuster site using the owner's own account, producing a written feasibility report with reproducible evidence. No production code, no infrastructure, no notifications, no scheduling.
- **Change type**: new surface
- **Describes AI capability**: no
- **Primary success criterion**: A feasibility report exists at a known path containing an unambiguous go or no-go recommendation backed by reproducible evidence of the authentication flow, at least one booking related request, and a Terms of Service review.

## Non-Scope

| Item | Reason for exclusion |
|------|----------------------|
| Production worker code (scheduler, retry logic, fallback ordering) | Belongs to the `scheduled-booking-worker` feature, which is gated on this spike. |
| Azure infrastructure provisioning (Functions, Container Apps, Key Vault, IaC) | Premature until feasibility is confirmed. |
| Notification channels (Telegram bot, email transport) | Premature until feasibility is confirmed. |
| Multi user configuration design (YAML schema, per user secrets, onboarding) | Premature until feasibility is confirmed. |
| Load or stress testing of the WodBuster API | Out of scope. Polite client constraint forbids it. |
| Any automation that runs unattended or on a schedule | The spike is interactive and operated by the owner. |
| Capacity to book classes the owner does not intend to attend | All bookings during the spike correspond to classes the owner will actually attend or cancel immediately and manually. |

## Assumptions

- The project owner holds an active WodBuster account at a CrossFit box that uses the platform.
- The owner is willing to perform a small number of manual logins and at most one or two real booking attempts on the owner's own account during the investigation.
- The investigation uses the owner's own credentials exclusively. No other user accounts are touched.
- Browser developer tools and a local HTTP capture proxy are sufficient to observe the platform's network traffic. No reverse engineering of native binaries is required.
- The WodBuster Terms of Service are publicly accessible and can be reviewed without contacting the vendor.
- The spike runs entirely on the owner's local workstation. No cloud resources are provisioned.

## User Scenarios & Tests

### User Story 1 — Owner produces the feasibility report (Priority: P1)

The project owner runs the spike end to end and produces a written feasibility report that contains a go or no-go recommendation with supporting evidence.

**Why this priority**: This is the single output of the spike and the trigger for every downstream decision. Without it, the rest of the project cannot start.

**Independent Test**: Anyone with read access to the repository can open the report, read the recommendation, inspect the captured evidence, and understand why the recommendation was made. No follow-up conversation with the investigator is needed.

**Acceptance Scenarios**:

1. **Given** the spike has completed, **When** the owner opens the report, **Then** the report contains a single explicit go or no-go statement on its first page.
2. **Given** the report recommends go, **When** a reader inspects the evidence section, **Then** they can identify the auth endpoint, the booking endpoint, the request shape, and the response shape, with credentials redacted.
3. **Given** the report recommends no-go, **When** a reader inspects the rationale section, **Then** they can identify the specific blocker (no API, CAPTCHA gated, Terms of Service violation, or other) and the evidence supporting that conclusion.

### User Story 2 — Auth flow is captured as a reproducible reference (Priority: P2)

The spike captures the WodBuster authentication flow in enough detail that the production worker can implement it without redoing discovery.

**Why this priority**: Auth is the prerequisite for any booking call. Repeating the discovery during production planning wastes the spike's value and risks fresh account lockouts.

**Independent Test**: A developer (the owner or a future contributor) can open the auth flow notes, follow the documented sequence using curl or Python, and obtain a valid session token against WodBuster, without consulting the original investigator.

**Acceptance Scenarios**:

1. **Given** the auth flow notes exist, **When** a developer follows them step by step, **Then** they obtain a working session token within a single attempt.
2. **Given** the auth flow involves a CSRF token, cookie, or hidden form field, **When** the notes are read, **Then** every such element is named, located, and described with its rotation policy.

### User Story 3 — Latency and rate limiting baseline is captured (Priority: P3)

The spike records observed end to end latency for a booking style request and any observed rate limiting, throttling, or anti-automation defense.

**Why this priority**: The 10 second latency budget is a hard constraint of the project. Knowing the floor and the platform's tolerance for repeated requests informs the hosting choice in the production plan. Marked P3 because a coarse observation is sufficient at this stage.

**Independent Test**: A reader can find one number for observed latency (with the measurement method named) and a yes or no answer to "did the platform throttle, block, or warn during the spike". A null observation is acceptable if the report explicitly says so.

**Acceptance Scenarios**:

1. **Given** the spike performed at least one booking style request, **When** the report is read, **Then** the observed round trip latency for that request appears with a method note (browser devtools, Python timer, or similar).
2. **Given** any throttling, captcha, or anomalous response was observed, **When** the report is read, **Then** the response code, response body excerpt, and conditions are documented.

### Edge Cases

- WodBuster requires CAPTCHA, 2FA, or human verification on login. The report records this as a blocker and recommends no-go unless a non automation path exists.
- The booking endpoint requires a CSRF token that rotates per page load and is not exposed in any JSON response. The report records the rotation policy and assesses whether a single pre warm request can extract it.
- The login form executes JavaScript that derives a request signature in the browser. The report records the observation and assesses the cost of replicating the signature outside the browser.
- The booking endpoint returns HTML rather than JSON. The report records the response shape and notes that parsing HTML is forbidden by the project's hard constraints. This is a no-go.
- The owner's account triggers a temporary lockout during the spike. The report records the threshold, recommends a quota for production, and pauses the spike.
- WodBuster's Terms of Service explicitly forbid programmatic access. The report records the exact clause and recommends no-go pending owner acceptance of the risk.

### Failure Modes

| Condition | Required behavior |
|-----------|-------------------|
| The owner's WodBuster account is temporarily locked during the spike | The investigation pauses immediately. The lockout threshold is recorded. No further login attempts occur until the account is restored. |
| A captured request payload contains the owner's plaintext password | The payload is redacted before being written to the report or committed to the repository. The redaction policy is documented. |
| The local HTTP capture proxy intercepts traffic from unrelated applications | The unrelated traffic is discarded. Only WodBuster traffic is retained and only in the report. |
| The spike reaches the time-box without a clear go or no-go conclusion | The report records the current state, the specific question that remains open, and a recommendation to extend the time-box once or to declare no-go by default. |

## Requirements

### Functional Requirements

- **FR-001**: The spike MUST produce a feasibility report at `docs/features/phase-0-api-discovery/feasibility-report.md`.
- **FR-002**: The feasibility report MUST contain a single explicit recommendation of `GO` or `NO-GO` on its first page.
- **FR-003**: The feasibility report MUST document the observed WodBuster authentication flow, including endpoint URL, HTTP method, request payload shape, response payload shape, session persistence mechanism (cookie, token, or other), and any rotating elements such as CSRF tokens.
- **FR-004**: The feasibility report MUST document at least one observed booking related request, including endpoint URL, HTTP method, headers, payload, and response, with credentials and PII redacted.
- **FR-005**: The feasibility report MUST document any observed rate limiting, throttling, CAPTCHA, or anti-automation defense, or explicitly record that none was observed during the spike.
- **FR-006**: The feasibility report MUST contain a Terms of Service review summary identifying any clause that addresses automated access, scraping, or third party clients.
- **FR-007**: The feasibility report MUST contain at least one observed round trip latency measurement for a booking style request, with the measurement method named.
- **FR-008**: The investigation MUST be time-boxed to a maximum of 5 working days from start, and the report MUST record the actual start and end dates.
- **FR-009**: The spike MUST cap login attempts and booking attempts at conservative limits (no more than 5 login attempts per hour and no more than 2 real booking attempts in total) to avoid account lockout.
- **FR-010**: Credentials, session tokens, and any PII MUST NOT be committed to the repository or pasted into the report. The report MUST use redaction placeholders for these values.
- **FR-011**: The feasibility report MUST list every assumption that, if proven wrong later, would invalidate the recommendation.

### Key Entities

- **Feasibility Report**: A single markdown document. The primary deliverable. Sections: recommendation, evidence, auth flow, sample booking request, rate limiting observations, latency observation, Terms of Service review, redacted captures, open questions, time-box record.
- **Captured Request**: A documented HTTP request observed during the spike. Attributes: method, URL, headers (redacted), payload (redacted), response code, response body excerpt (redacted), observed latency, capture method.

## Success Criteria

### Measurable Outcomes

- **SC-001**: The feasibility report exists at the path defined in FR-001 and contains a single explicit recommendation, verifiable by opening the file.
- **SC-002**: A developer who did not run the spike can reproduce the documented authentication flow against WodBuster on a single attempt, without consulting the investigator.
- **SC-003**: The go or no-go decision is recorded with rationale in fewer than 500 words on the first page of the report.
- **SC-004**: The spike completes within the 5 working day time-box, or the report explicitly records the reason for extension and the new time-box.
- **SC-005**: The owner's WodBuster account remains in good standing throughout and after the spike. Zero lockouts, zero suspensions.

## Conformance Criteria

### Conformance Cases

| ID | Scenario | Input | Expected Output |
|----|----------|-------|-----------------|
| CC-001 | Happy path. Usable API is discovered and the project is greenlit. | Spike runs to completion. WodBuster exposes a usable auth and booking API. Terms of Service do not strictly forbid programmatic access. | Feasibility report exists. Recommendation is `GO`. Auth flow, sample booking request, latency observation, and ToS review are all documented. The `scheduled-booking-worker` feature plan is unblocked. |
| CC-002 | Error path. No usable API. | Spike discovers that the booking endpoint returns HTML, requires CAPTCHA, or otherwise cannot be driven by a polite API client. | Feasibility report exists. Recommendation is `NO-GO`. The specific blocker is named with evidence. The project is paused pending owner decision. |
| CC-003 | Edge case. Auth payload is signed by browser JavaScript that cannot be trivially replicated. | Spike captures requests but cannot reproduce a successful login from curl or Python within the time-box. | Feasibility report exists. Recommendation is `NO-GO` or `CONDITIONAL` with the cost of replicating the signing logic estimated. Owner decides whether to extend or stop. |
| CC-004 | Must NOT happen. The spike causes an account lockout. | Investigation proceeds without observing FR-009 quotas. | This must not occur. If approached, the spike pauses and FR-009 is enforced retroactively. The lockout threshold is documented. The owner's account remains in good standing throughout the spike. |
| CC-005 | Must NOT happen. Plaintext credentials, session tokens, or PII appear in the report or in a committed file. | A captured payload contains plaintext credentials. | This must not occur. Redaction is applied before the report is written or committed. The repository contains no plaintext credentials at any point. |

## Invariants

- The owner's WodBuster account remains in good standing throughout the spike.
- No plaintext credentials, session tokens, cookies, or PII are written to the repository at any point. Redaction placeholders are used in their place.
- The spike acts as a polite client. Single request per attempt, no parallelism, no automated retry loops during exploration.
- Every claim in the feasibility report is backed by a captured request, a captured response, a quoted clause, or an explicit statement that the claim is an unobserved assumption.

## Compatibility and Transition

N/A: purely additive new surface.

## Related Specs

- `scheduled-booking-worker` (deferred, not yet specified). The production worker is gated on a `GO` recommendation from this spike. Its specification, plan, and ADRs are deferred until the spike returns.

## Spec Evolution Log

| Version | Date | Change Summary | Trigger | Author |
|---------|------|----------------|---------|--------|
| 1.0 | 2026-06-29 | Initial draft. Scope locked to a feasibility spike that produces a go or no-go report. | new work | devsquad.plan (sub-agent) |
