# Feature Specification: WodBuster Booking Worker

- **Created on**: 2026-06-29
- **Status**: Draft

## Executive Summary

- **Objective**: Provide an unattended service that books a preferred class on the WodBuster platform the instant its booking window opens, controlled by a web UI for rule management and a Telegram bot for on-the-go action, with end-to-end notification coverage.
- **Primary user**: The project owner (a single athlete). Designed so that one to two additional users can be onboarded later without code changes.
- **Value delivered**: Eliminates manual booking failure on popular classes that fill in under ten seconds, removes the cognitive load of remembering booking windows, and guarantees that every scheduled run produces a positive signal (success, failure, or heartbeat anomaly).
- **Scope**: Scheduler rule CRUD (web UI), recurring weekly automated booking, manual ad-hoc booking (web UI and Telegram), single and bulk cancellation, cookie paste-and-validate handoff with proactive expiry alert, dual-channel notifications, and operator-only authentication via federated identity.
- **Change type**: new surface
- **Describes AI capability**: no
- **Primary success criterion**: Booking success rate for the top preferred slot greater than 95 percent over a rolling four-week window when the gym has capacity, with zero silent failures.

## Non-Scope

- WodBuster username and password handling. The system never stores, transmits, or processes WodBuster credentials. Only the `.WBAuth` session cookie is persisted.
- HTML scraping, DOM automation, headless browser at booking time, or SignalR. Phase 0 confirmed three stock HTTP endpoints are sufficient.
- Scheduler rule create, update, or delete operations via Telegram. Rule mutation is exclusive to the web UI.
- Local password storage for operator accounts. Operator authentication relies on federated identity providers.
- Multi-box or multi-tenant management. The system targets one WodBuster box per operator.
- Concurrent or parallel booking requests against the same slot. The client issues one request per attempt by design.
- Auto-cancel triggered by complex business rules (for example, "cancel if weather is bad"). Cancellation is always operator-initiated.
- Onboarding of additional operators in this iteration. The data model must keep records isolated per operator, but the onboarding workflow is post-MVP.
- Indefinite booking-history pruning, archival, or export pipelines. History is retained without scheduled deletion.

## Assumptions

- The operator has a personal account with at least one federated identity provider among Microsoft, GitHub, or Google.
- The operator owns a Telegram account and can register a chat ID against their operator profile.
- The Phase 0 measurements hold in production: warm booking call near 336 ms, cold connection adds about 1.3 s, single `.WBAuth` cookie suffices on the authenticated subdomain.
- WodBuster exposes the `SegundosHastaPublicacion` field as the canonical countdown for booking-window opening. The worker uses it as the timing source instead of clock-only triggers.
- The `.WBAuth` cookie behaves as a sliding session under routine polling. Natural expiry happens on the order of weeks, not hours.
- Class definitions appear on the WodBuster calendar at or shortly before their booking window opens. Two minutes of post-window retry is sufficient to absorb late publication in normal operation.

## User Scenarios & Tests

### User Story 1 - Automated booking at window open (Priority: P1)

The operator has a saved scheduler rule for a recurring weekly class (for example, Monday 19:00 CrossFit, ordered fallbacks 20:00 then 18:00). The booking window for that class opens 48 hours before class start. The worker pre-warms its HTTP connection ahead of the window, polls the WodBuster countdown to align with the opening instant, fires a booking request at t equals zero, walks the ordered fallback list if the top slot is full, and stops at the first granted booking.

**Why this priority**: This is the core problem the project exists to solve. Without it the rest of the system has no purpose.

**Independent Test**: Configure one rule, wait for or simulate the booking window, observe a booking record persisted with a granted status and a notification emitted on the operator's Telegram chat.

**Acceptance Scenarios**:

1. **Given** an active rule with a valid cookie and an available top slot, **When** the booking window opens, **Then** the worker books the top slot within ten seconds of window open and persists the result.
2. **Given** an active rule with a valid cookie and a full top slot but an available second fallback, **When** the booking window opens, **Then** the worker books the second fallback within the same ten-second budget and the persisted record names the fallback that was granted.
3. **Given** an active rule with a valid cookie and all fallbacks full, **When** the booking window opens, **Then** the worker records a failure with reason "all fallbacks full" and emits a failure notification.

---

### User Story 2 - Outcome notifications and silent-run alarm (Priority: P1)

Every scheduled run produces a positive signal. The operator receives a Telegram message on success, a Telegram message on failure with the reason, and an alarm if a scheduled run window passes without any signal being emitted (dead-man pattern). The same outcome is visible in the web UI booking history.

**Why this priority**: Hard constraint number four ("no silent degradation"). A booking system the user cannot trust is worse than no booking system.

**Independent Test**: Disable the worker process during a scheduled run window and verify that within the configured grace period a heartbeat-anomaly alert reaches both Telegram and the web UI.

**Acceptance Scenarios**:

1. **Given** a successful booking, **When** the booking response is granted, **Then** a Telegram success message and a web UI history entry are produced within thirty seconds.
2. **Given** a failed booking attempt, **When** all fallbacks are exhausted, **Then** a Telegram failure message naming the rule and the reason and a web UI history entry are produced within thirty seconds.
3. **Given** a scheduled run window that elapsed without any outcome signal, **When** the grace period after window close elapses, **Then** a heartbeat-anomaly alert is emitted on Telegram and surfaced as a banner in the web UI.

---

### User Story 3 - Cookie paste, validate, and live status (Priority: P1)

The operator opens the web UI, pastes the `.WBAuth` cookie value extracted from a real browser session, and the system validates the cookie against WodBuster, stores it encrypted at rest, binds it to the operator profile, and surfaces a countdown to estimated expiry.

**Why this priority**: Without a valid cookie no booking can occur. This is the only manual touch-point in the steady state.

**Independent Test**: Open the web UI, paste a valid cookie, observe a "validated" status and a non-empty TTL countdown; paste an invalid cookie and observe a clear "rejected" status with no state mutation.

**Acceptance Scenarios**:

1. **Given** a logged-in operator with no cookie on file, **When** the operator pastes a valid `.WBAuth` value and submits, **Then** the cookie is validated against WodBuster, stored encrypted, and the UI shows a positive validation status with a TTL countdown.
2. **Given** a logged-in operator pasting an invalid or expired value, **When** the operator submits, **Then** the UI shows a rejection message identifying the failure mode and no cookie is persisted.
3. **Given** an operator with a stored cookie, **When** the operator re-opens the web UI later, **Then** the current TTL countdown reflects the latest heartbeat probe result.

---

### User Story 4 - Proactive cookie-expiry alert (Priority: P1)

The system runs a recurring heartbeat probe that checks cookie validity against WodBuster. When the probe detects that the cookie will not survive until the operator's next scheduled booking window, the system alerts the operator at least twenty-four hours before that window via Telegram and via a web UI banner, repeating the alert until the cookie is refreshed.

**Why this priority**: Hard constraint number five ("no time-pressured manual intervention"). Cookie expiry must not first surface as a booking-time failure.

**Independent Test**: Force-expire the cookie or shorten the projected TTL such that it falls below twenty-four hours before the next scheduled booking window and observe alerts on both surfaces within one heartbeat cycle.

**Acceptance Scenarios**:

1. **Given** a valid cookie and a next scheduled booking window more than twenty-four hours away, **When** the heartbeat probe runs, **Then** no alert is emitted and the TTL countdown updates.
2. **Given** a cookie whose projected expiry falls within twenty-four hours of the next scheduled booking window, **When** the heartbeat probe runs, **Then** a Telegram alert and a web UI banner are emitted naming the affected window and a paste-and-validate call to action.
3. **Given** an alert already emitted for an unrefreshed cookie, **When** subsequent heartbeat probes run, **Then** the alert is re-emitted on each cycle until the cookie is refreshed or acknowledged via the dedicated acknowledgement command.

---

### User Story 5 - Scheduler rule CRUD via web UI (Priority: P2)

The operator creates, edits, lists, and deletes recurring weekly scheduler rules from the web UI. Each rule names the day of the week, the booking window offset (for example, 48 hours before class start), and an ordered list of class preferences (class type plus time slot, primary plus fallbacks). Rule changes take effect for the next scheduled window without restart.

**Why this priority**: Required for the operator to direct the worker. Web UI exclusive per project policy.

**Independent Test**: Create one rule via the web UI, list rules, edit the rule to add a second fallback, delete the rule, and confirm the next scheduled run reflects each state change.

**Acceptance Scenarios**:

1. **Given** an authenticated operator, **When** the operator submits a new rule with day, window offset, and at least one preference, **Then** the rule is persisted and appears in the listing.
2. **Given** an existing rule, **When** the operator edits its preference list and saves, **Then** the next scheduled run uses the new ordering.
3. **Given** an attempt to perform any rule mutation through Telegram, **When** the command is received, **Then** the system responds with an explanatory rejection and performs no state change.

---

### User Story 6 - Cancel a single booking (Priority: P2)

The operator cancels an upcoming booking from either the web UI (button on the booking entry) or Telegram (`/cancel <booking-id>` or equivalent). The worker calls the cancellation endpoint and reflects the new state in the booking history and on both surfaces.

**Why this priority**: Cancellation pattern CT-A in envisioning. Common operator action.

**Independent Test**: Book a slot, cancel from Telegram, observe the cancellation in the web UI history within thirty seconds; repeat the cancel from the web UI button.

**Acceptance Scenarios**:

1. **Given** an upcoming granted booking, **When** the operator triggers cancel from Telegram, **Then** the worker cancels and reports the new state on both surfaces.
2. **Given** an upcoming granted booking, **When** the operator clicks cancel in the web UI, **Then** the worker cancels and reports the new state on both surfaces.
3. **Given** a booking already cancelled, **When** the operator triggers cancel again, **Then** the system responds idempotently with a clear "already cancelled" message and emits no duplicate cancel request.

---

### User Story 7 - Vacation mode bulk cancellation (Priority: P2)

The operator enables vacation mode for a date range via the web UI. The worker cancels all granted bookings in that range and suppresses automated rule execution for the range. When the range ends, automated booking resumes without further action.

**Why this priority**: Cancellation pattern CT-B in envisioning. High-value convenience.

**Independent Test**: Have bookings in three different days, enable vacation mode covering two of those days, observe those two cancelled and the third left intact; advance time past the range end and observe the next scheduled rule fire normally.

**Acceptance Scenarios**:

1. **Given** granted bookings on days inside and outside a vacation range, **When** the operator enables vacation mode for that range, **Then** only bookings inside the range are cancelled.
2. **Given** an active vacation range, **When** a rule would otherwise fire inside the range, **Then** the worker skips the run and records a "skipped: vacation mode" entry instead of attempting a booking.
3. **Given** an active vacation range, **When** the end date passes, **Then** automated booking resumes without operator intervention.

---

### User Story 8 - Manual ad-hoc booking (Priority: P2)

The operator triggers a one-off booking for a specific class on a specific date and time, either from the web UI ("book now" form) or Telegram (`/bookclass <date> <time>` or equivalent). The system executes the booking under the same rules as scheduled runs and reports the outcome on both surfaces.

**Why this priority**: Required for operator agility when a rule is missing or out-of-pattern (substitute class, special workout, schedule change).

**Independent Test**: Issue a manual book command from Telegram for a class that is currently within its booking window and observe the granted booking on both surfaces; repeat the same from the web UI.

**Acceptance Scenarios**:

1. **Given** a class currently within its booking window with an available slot, **When** the operator issues a manual book command from Telegram, **Then** the booking is executed and the granted result appears on both surfaces.
2. **Given** a class currently within its booking window, **When** the operator submits the manual book form in the web UI, **Then** the booking is executed and the granted result appears on both surfaces.
3. **Given** a class not yet within its booking window, **When** the operator issues a manual book command, **Then** the system rejects the command with a clear "window not open" message and performs no booking attempt.

---

### User Story 9 - Operator-only access via federated identity (Priority: P1)

The web UI is gated by an authentication step that requires the operator to sign in through a personal Microsoft, GitHub, or Google account. The system maps the federated identity to an operator profile. Unauthenticated requests cannot read or mutate any operator data. No local password is stored.

**Why this priority**: The web UI exposes the cookie management surface, the scheduler rules, and the booking history. Anonymous access is unacceptable.

**Independent Test**: Visit any protected route without a session and confirm redirection to the sign-in flow; sign in with an allowed identity and confirm access; sign in with a disallowed identity and confirm denial.

**Acceptance Scenarios**:

1. **Given** an anonymous visitor, **When** they request any protected route, **Then** they are redirected to a sign-in flow against the configured federated identity providers.
2. **Given** an authenticated session bound to an allowed operator identity, **When** the operator navigates the UI, **Then** the operator sees only their own data.
3. **Given** an authenticated session bound to an identity not on the allow-list, **When** the user attempts any action, **Then** the system denies access without leaking operator data.

---

### Edge Cases

- The class the rule targets has not yet been published on the WodBuster calendar when the booking window opens. The worker retries every five seconds for up to two minutes after window open, then emits a failure notification with reason "class not visible after retry window".
- The stored cookie becomes invalid between heartbeats and is discovered invalid at booking time. The worker makes a single fail-fast booking attempt, on cookie-invalid response emits "booking failed: cookie invalid" and a "paste new cookie" alert to both Telegram and the web UI, and performs no further retries for that run.
- Two scheduler rules collide on the same booking window (for example, both want 19:00 Monday). The worker processes them in deterministic rule-ID order and records both outcomes; the second attempt may receive an "already booked" response, which is recorded as a non-error informational outcome.
- The operator deletes a rule while its booking window is open. The in-flight attempt completes (or is skipped if not yet started) and the deletion takes effect for subsequent windows.
- Telegram is unreachable. Notifications queue and retry; the web UI history remains authoritative for outcome record. Heartbeat-anomaly behavior still applies if the queue cannot drain within the configured grace period.
- The heartbeat probe itself fails repeatedly. Repeated probe failure is treated as a heartbeat anomaly and surfaces an alert on both channels.
- The operator pastes a cookie that validates against WodBuster but belongs to a different account than the one whose rules are configured. The system surfaces a clear mismatch warning and stores the cookie only if the operator confirms.

### Failure Modes

- **WodBuster unavailable or times out at booking time**: The worker records a "booking failed: upstream unavailable" outcome and emits the standard failure notification. No automatic retry within the same window beyond the class-not-visible retry policy.
- **WodBuster returns an unexpected response shape**: Treated as a failure with reason "unexpected response", full response captured in booking history for post-mortem. No retry.
- **Concurrent operator actions** (for example, web UI cancel and Telegram cancel on the same booking simultaneously): The second action is idempotent. Persistence layer enforces single-effect semantics.
- **Heartbeat probe runs while a booking attempt is in flight**: Both proceed; the probe must not block or perturb the booking request path.
- **Cookie store partially fails to encrypt or persist**: The paste-and-validate flow returns an error to the operator; no half-committed state is retained.
- **Notification dispatcher backlog**: Outcomes are recorded synchronously to the persistence layer first, then dispatched. A backlog never causes loss of a recorded outcome.
- **Consistency model**: Booking-outcome write to the persistence layer must be durable before any notification is dispatched. Notification dispatch is eventually consistent within thirty seconds under normal conditions.

## Requirements

### Functional Requirements

#### Scheduler rule management

- **FR-001**: The system MUST allow an authenticated operator to create a scheduler rule specifying day-of-week, booking-window offset, and an ordered list of one or more class preferences (each preference identifying a class type plus a target time slot).
- **FR-002**: The system MUST allow an authenticated operator to list, read, update, and delete their own scheduler rules through the web UI.
- **FR-003**: The system MUST reject any scheduler rule create, update, or delete operation received through Telegram and respond with an explanatory message.
- **FR-004**: The system MUST apply rule changes to the next scheduled booking window without requiring a service restart.
- **FR-005**: The system MUST isolate scheduler rules per operator profile. No operator may read or mutate another operator's rules.

#### Automated booking execution

- **FR-006**: The system MUST pre-warm an HTTP connection to WodBuster before a known booking window in time to neutralize cold-start latency.
- **FR-007**: The system MUST align the booking request with the WodBuster `SegundosHastaPublicacion` countdown so that the request reaches WodBuster at or immediately after t equals zero.
- **FR-008**: The system MUST issue exactly one booking request per attempt and MUST NOT issue parallel requests for the same slot.
- **FR-009**: The system MUST walk the ordered fallback list in order, stopping at the first granted booking.
- **FR-010**: When the targeted class is not visible on the WodBuster calendar at window open, the system MUST retry visibility checks every five seconds for up to two minutes after window open, then terminate with a "class not visible" failure outcome.
- **FR-011**: When the cookie is rejected by WodBuster during a booking attempt, the system MUST make a single fail-fast attempt, MUST NOT retry, and MUST emit a "cookie invalid" alert in addition to the standard failure outcome.
- **FR-012**: The system MUST persist every scheduled run outcome (granted, full, cookie-invalid, upstream-unavailable, class-not-visible, skipped-vacation, or other) in the booking history.

#### Cancellation

- **FR-013**: The system MUST allow an authenticated operator to cancel a single upcoming booking from the web UI.
- **FR-014**: The system MUST allow an authenticated operator to cancel a single upcoming booking from Telegram.
- **FR-015**: The system MUST allow an authenticated operator to enable vacation mode covering a date range in the web UI. Vacation mode MUST cancel all granted bookings whose class start is within the range and MUST skip scheduled runs that fall within the range.
- **FR-016**: The system MUST treat duplicate cancel requests for the same booking idempotently and MUST NOT issue more than one cancel request to WodBuster for the same booking.

#### Manual ad-hoc booking

- **FR-017**: The system MUST allow an authenticated operator to trigger a one-off booking for a specified class date and time from the web UI.
- **FR-018**: The system MUST allow an authenticated operator to trigger a one-off booking from Telegram using a deterministic command shape.
- **FR-019**: The system MUST reject a manual booking command when the target class is not within its booking window, without issuing any request to WodBuster.

#### Cookie handoff and heartbeat

- **FR-020**: The system MUST allow an authenticated operator to paste a `.WBAuth` cookie value via the web UI. The system MUST validate the value against WodBuster before persisting and MUST reject invalid values without persisting any state.
- **FR-021**: The system MUST persist the validated cookie encrypted at rest, bound to the operator profile.
- **FR-022**: The system MUST run a recurring heartbeat probe that verifies cookie validity against WodBuster on a fixed cadence (recommended: hourly).
- **FR-023**: When the projected cookie expiry falls within twenty-four hours of the operator's next scheduled booking window, the system MUST emit a "cookie expiring" alert via Telegram and surface a banner in the web UI on each heartbeat cycle until the cookie is refreshed or the alert is explicitly acknowledged.
- **FR-024**: The system MUST surface the most recent heartbeat result and an estimated cookie TTL in the web UI.

#### Notifications and silent-run alarm

- **FR-025**: The system MUST emit a notification on Telegram and record a corresponding entry in the web UI history for every successful booking, every failed booking, every successful cancellation, every cookie-expiring alert, and every heartbeat anomaly.
- **FR-026**: The system MUST emit a heartbeat-anomaly alert when a scheduled run window elapses without producing any outcome signal within a grace period (recommended: sixty seconds after the window's expected completion).
- **FR-027**: The system MUST allow the operator to acknowledge an active "cookie expiring" alert via a Telegram command. Acknowledgement suppresses re-emission of that specific alert for the current heartbeat cycle only and does not clear the underlying condition.

#### Operator authentication

- **FR-028**: The system MUST require authentication via federated identity (personal Microsoft, GitHub, or Google account) for every web UI route except the sign-in flow itself and any unauthenticated health probe.
- **FR-029**: The system MUST NOT store, accept, or process operator passwords locally.
- **FR-030**: The system MUST maintain an allow-list of operator identities. Authenticated identities not on the allow-list MUST be denied access without leaking operator data.
- **FR-031**: The system MUST bind every Telegram interaction to the operator profile via a registered chat ID and MUST reject commands from unknown chat IDs.

#### Booking history

- **FR-032**: The system MUST retain booking history records indefinitely. No scheduled deletion, pruning, or archival job operates on booking history.
- **FR-033**: The system MUST expose the operator's booking history (granted, failed, cancelled, skipped) in the web UI, ordered by most recent first.

### Key Entities

- **Operator Profile**: Represents a single human user. Holds the federated identity binding, the Telegram chat ID, and references to the operator's rules, cookie, and booking history. All other entities scope by operator.
- **Scheduler Rule**: A recurring weekly intent. Holds day-of-week, booking-window offset (relative to class start), and an ordered list of class preferences. Owned by exactly one operator.
- **Class Preference**: A primary or fallback target within a rule. Holds class type identifier and target time slot. Ordering within the rule defines fallback priority.
- **Cookie Credential**: The persisted `.WBAuth` value. Stored encrypted at rest, bound to one operator, carries metadata (paste timestamp, last validation timestamp, estimated TTL). At most one active cookie per operator.
- **Booking Outcome**: A record of one execution attempt. Holds rule reference (nullable for manual or vacation entries), target slot, timestamp, terminal status (granted, full, cookie-invalid, upstream-unavailable, class-not-visible, skipped-vacation, cancelled, error), and captured response payload for post-mortem.
- **Vacation Window**: A date range during which automated runs are skipped and existing granted bookings inside the range are cancelled.
- **Heartbeat Reading**: A record of one probe execution. Holds timestamp, validation result, estimated TTL, and link to any alert emitted.
- **Alert**: A pending or acknowledged operator-facing condition (cookie expiring, heartbeat anomaly, repeated booking failure). Holds emission history per channel and acknowledgement state.

## Success Criteria

### Measurable Outcomes

- **SC-001**: Booking succeeds on the top preferred slot in greater than 95 percent of executed scheduled runs over a rolling four-week window, measured only over runs where the gym had capacity at window open.
- **SC-002**: End-to-end latency from booking-window open (WodBuster `SegundosHastaPublicacion` reaching zero) to a confirmed booking response is under ten seconds for every successful run.
- **SC-003**: Zero scheduled runs in any rolling four-week window produce no signal. Every run yields either a success notification, a failure notification, or a heartbeat-anomaly alert.
- **SC-004**: The operator receives a cookie-expiring alert at least twenty-four hours before the next scheduled booking window in 100 percent of cases where the cookie does not survive that window.
- **SC-005**: The operator can complete a cookie paste-and-validate flow end-to-end in under two minutes once they have copied the cookie from a browser.
- **SC-006**: The operator can create a new scheduler rule end-to-end in under three minutes through the web UI.
- **SC-007**: Cancellation (single or vacation-mode bulk) reflects on both surfaces within thirty seconds of the operator action.
- **SC-008**: Zero unauthorized accesses to any operator's data. Every read or write through the web UI is bound to an allow-listed federated identity, and every Telegram interaction is bound to a registered chat ID.

## Conformance Criteria

### Conformance Cases

| ID | Scenario | Input | Expected Output |
|----|----------|-------|-----------------|
| CC-001 | Happy-path scheduled booking | Active rule with valid cookie, top slot available at window open | Top slot booked within ten seconds of window open; granted outcome persisted; success notification on Telegram and web UI |
| CC-002 | Fallback walk | Active rule with valid cookie, top slot full at window open, second fallback available | Second fallback booked within ten seconds of window open; outcome record identifies the granted fallback |
| CC-003 | All fallbacks full | Active rule with valid cookie, every fallback in the ordered list full | Failure outcome with reason "all fallbacks full"; failure notification on Telegram and web UI |
| CC-004 | Class not yet visible at window open | Active rule with valid cookie, target class absent from calendar at window open, class appears on calendar at t plus seventy seconds | Booking attempted at t plus seventy to seventy-five seconds; outcome reflects attempt result; no further retries after two minutes |
| CC-005 | Class never visible within retry window | Active rule with valid cookie, target class never appears on calendar | Failure outcome with reason "class not visible" emitted at two minutes after window open; failure notification on both surfaces |
| CC-006 | Cookie invalid at booking time (fail-fast, negative) | Active rule with cookie that heartbeat marked valid but WodBuster rejects on the booking attempt | Exactly one booking request issued; on cookie-invalid response no further booking retries occur; "booking failed: cookie invalid" notification and "paste new cookie" alert emitted on both surfaces |
| CC-007 | Proactive cookie alert | Stored cookie whose projected expiry falls within twenty-four hours of the operator's next scheduled booking window | "Cookie expiring" alert emitted on Telegram and as a web UI banner on the next heartbeat cycle |
| CC-008 | Heartbeat silent-run alarm | Scheduled run window passes with no outcome signal produced within the grace period | Heartbeat-anomaly alert emitted on Telegram and surfaced as a web UI banner |
| CC-009 | Telegram rule mutation (negative, must not) | Authenticated operator sends a rule create, update, or delete command via Telegram | Command rejected with explanatory message; persistence layer is not mutated |
| CC-010 | Manual booking outside window (negative, must not) | Operator issues manual book command for a class whose booking window has not opened | Command rejected with "window not open" message; no request issued to WodBuster |
| CC-011 | Unauthorized access (negative, must not) | Unauthenticated request to any protected web UI route | Request redirected to the sign-in flow; no operator data is returned in the response body or headers |
| CC-012 | Cross-operator isolation (negative, must not) | Authenticated operator A attempts to read or mutate operator B's rules, cookie, or history | Request denied; no operator B data returned; no operator B state mutated |
| CC-013 | Manual booking via Telegram | Authenticated operator issues `/bookclass <date> <time>` (or equivalent) for a class within its booking window with an available slot | Booking executed under the standard policy; granted outcome appears on both surfaces |
| CC-014 | Vacation mode bulk cancel | Three granted bookings on three different days, operator enables vacation mode covering the first two | Only the first two bookings cancelled; the third remains granted; scheduled runs inside the range record "skipped: vacation mode" |
| CC-015 | Idempotent cancellation | Operator triggers cancel on a booking that is already cancelled | System responds with "already cancelled"; no additional cancel request issued to WodBuster |

## Invariants

- Exactly one booking request is issued to WodBuster per booking attempt. The system never fires parallel requests for the same slot.
- Booking history records are durable before any notification is dispatched. A notification implies a persisted outcome.
- Booking history records are never deleted, archived, or pruned by any scheduled process.
- WodBuster username and password are never stored, transmitted, or processed by the system. Only the `.WBAuth` cookie value is persisted.
- The persisted cookie is encrypted at rest. The encryption key is never co-located with the encrypted blob in operator-visible storage.
- Scheduler rule create, update, and delete operations are accepted exclusively through the web UI. The Telegram surface never mutates rules.
- Operator authentication on the web UI happens exclusively through federated identity providers. No local password material is accepted, stored, or processed.
- Every Telegram-originated action is bound to a registered chat ID mapped to an operator profile. Commands from unknown chat IDs produce no state change and no operator-data leak.
- Every operator's data (rules, cookie, history, alerts) is accessible only to that operator's authenticated session. Cross-operator reads or writes are forbidden.
- Cancellation operations are idempotent. Replaying a cancel for the same booking produces no additional WodBuster request.
- Every scheduled run window produces exactly one terminal signal: a success notification, a failure notification, or a heartbeat-anomaly alert. "No signal" is never an acceptable outcome.

## Spec Evolution Log

| Version | Date | Change Summary | Trigger | Author |
|---------|------|----------------|---------|--------|
| 1.0 | 2026-06-29 | Initial draft. Captures the production worker scope (scheduler rules, automated booking, cancellation patterns CT-A and CT-B, manual booking, cookie handoff with twenty-four-hour proactive alert, dual-surface UX, federated-identity operator auth, indefinite booking history). Defers hosting service, persistence engine, identity provider mechanism, secrets binding, observability stack, and IaC tooling to planning ADRs. | new work after envisioning v1.1 and Phase 0 GO | devsquad.specify |
