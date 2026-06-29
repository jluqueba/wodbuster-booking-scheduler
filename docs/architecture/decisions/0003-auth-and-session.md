# WodBuster Auth and Session

**Status**: Proposed
**Date**: 2026-06-29

## Context

This ADR concerns the worker's session with WodBuster, not operator authentication to the web UI (operator authentication is covered in ADR-0005).

Phase 0 confirmed that a single `.WBAuth` cookie established by a real browser session is sufficient to authenticate against the `LoadClass.ashx` and `Calendario_Inscribir.ashx` endpoints on the gym subdomain. WodBuster fronts the unauthenticated origin with a Cloudflare managed challenge, but the authenticated gym subdomain serves established sessions without a challenge. Programmatic defeat of the Cloudflare challenge is forbidden by envisioning constraint 1 and by the carry-forward conditions of the Phase 0 feasibility report.

Hard constraints from envisioning section 7:

- Constraint 6: WodBuster username and password are never stored, sent, or processed by the system. Only the resulting session cookie is persisted.
- Constraint 5: Cookie expiry must never first surface as a booking-time failure. The operator must be alerted with at least 24 hours of lead time before the next scheduled booking window (FR-023, SC-004).
- Constraint 4: No silent degradation. Heartbeat anomalies must produce a signal (FR-026).

The spec already prescribes the cookie-handoff design in detail (User Stories 3 and 4, FR-020 through FR-024, FR-027). This ADR codifies that design as the architectural decision and records why no other approach was chosen.

## Priorities and Requirements (ordered)

1. **No WodBuster credentials at rest**. Username and password never enter the system (envisioning constraint 6, spec FR-029).
2. **Cookie encrypted at rest**. Application-layer encryption with the key in Key Vault (FR-021, spec Invariants).
3. **Proactive expiry alert at least 24 hours before the next scheduled booking window** (FR-023, SC-004).
4. **Single fail-fast attempt on invalid cookie at booking time**, no aggressive retry against an invalid session (FR-011, Phase 0 condition 3).
5. **Polite client behavior**. One probe per heartbeat cycle, no parallel session-validation requests.

## Options Considered

### Option 1: Paste-validate handoff with sliding heartbeat probe

The operator establishes a real browser session at `wodbuster.com`, copies the `.WBAuth` cookie value, and pastes it into the worker's web UI. The worker validates the value by issuing a `LoadClass.ashx` request against the authenticated gym subdomain. On success, the cookie is encrypted with AES-256-GCM (key in Key Vault, ADR-0005) and stored in the operator-bound row of the `cookie_credential` table (ADR-0002). On failure, no state mutates and the UI reports the rejection. An APScheduler heartbeat job runs hourly. Each cycle issues one `LoadClass.ashx` call. A successful call (1) confirms validity, (2) refreshes the sliding session, and (3) updates the estimated TTL surfaced in the UI. When the projected TTL falls within 24 hours of the operator's next scheduled booking window, an alert is emitted on Telegram and as a web banner on every heartbeat until the operator pastes a new cookie or acknowledges via the dedicated Telegram command (FR-027, acknowledgement suppresses the current cycle only). At booking time, a cookie-invalid response from WodBuster terminates the run after exactly one request and emits the "paste new cookie" alert.

**Evaluation against priorities**:
- **No WodBuster credentials at rest**: Meets. The system never sees the username or password.
- **Cookie encrypted at rest**: Meets. Application-layer AES-GCM with a Key Vault key (ADR-0005).
- **Proactive expiry alert**: Meets. The hourly probe gives 24 alert opportunities per day; the projected-TTL check is wider than the alert window, so the alert lands at least 24 hours before the next scheduled window in all observed cases (SC-004).
- **Single fail-fast on invalid cookie**: Meets. FR-011 mandates the behavior and the worker implements exactly one request on the booking path.
- **Polite client behavior**: Meets. Hourly heartbeat plus one booking attempt per window is well inside the Phase 0 polite quota envelope.

### Option 2: Playwright-driven automated cookie refresh on the operator's workstation

A companion local script runs Playwright against `wodbuster.com`, completes the Cloudflare challenge by manually attaching to a real browser profile, extracts the `.WBAuth` cookie on a schedule, and pushes it to the worker over an authenticated API. The operator paste step is eliminated.

**Evaluation against priorities**:
- **No WodBuster credentials at rest**: Partially meets. The browser profile holds credentials and the local script holds the worker's push-API token. The blast radius expands to the operator's workstation.
- **Cookie encrypted at rest in the worker**: Meets, by the same mechanism as Option 1.
- **Proactive expiry alert**: Partially meets. Automated refresh hides cookie expiry from the operator, reducing the value of the alert. The constraint is about avoiding time-pressured manual intervention; automating the refresh moves the failure mode rather than addresses it. When the local script fails, the operator still gets caught at booking time, now without any prior warning unless the worker treats prolonged silence from the push API as an anomaly.
- **Single fail-fast on invalid cookie**: Meets, same code path.
- **Polite client behavior**: Partially meets. Headless browsing against `wodbuster.com` invites the Cloudflare challenge that envisioning constraint 1 forbids defeating. The whole point of paste-and-validate is to ride a real browser session.

### Option 3: Browser-extension companion that syncs the cookie on each operator browser session

A small browser extension installed in the operator's browser reads `.WBAuth` on every navigation to `wodbuster.com` and pushes the latest value to the worker over an authenticated API.

**Evaluation against priorities**:
- **No WodBuster credentials at rest**: Meets.
- **Cookie encrypted at rest in the worker**: Meets.
- **Proactive expiry alert**: Partially meets. As with Option 2, automated background refresh weakens the operator's mental model of cookie expiry. The 24-hour alert still functions but becomes less informative.
- **Single fail-fast on invalid cookie**: Meets.
- **Polite client behavior**: Meets. The extension reads from the operator's own browser, no extra WodBuster traffic.

Option 3 requires a non-trivial second deliverable (extension, packaging, browser-store publishing or sideload instructions). The complexity is unjustified for a single-user MVP where the paste cadence is on the order of weeks.

## Decision

Option 1: codify the spec's paste-validate handoff design as the WodBuster session mechanism. Single `.WBAuth` cookie, paste-and-validate UI, AES-256-GCM application-layer encryption with the key in Key Vault, hourly APScheduler heartbeat doubling as the sliding-session refresh, projected-TTL check against the next scheduled booking window with a 24-hour lead-time alert on Telegram and web banner, fail-fast on invalid cookie at booking time, Telegram acknowledgement command per FR-027.

This option uniquely meets every priority. Both alternatives reduce operator paste cadence at the cost of expanding the credential blast radius (Option 2) or shipping a second deliverable (Option 3), neither of which is justified at single-user MVP scale.

## Implementation Notes

- The hourly heartbeat doubles as the sliding-session keep-alive. The probe endpoint is `LoadClass.ashx` against the authenticated gym subdomain, as Phase 0 reproduced.
- Projected TTL is derived from a configurable absolute ceiling (default 30 days, adjustable per operator) reset on every successful probe. The actual cookie absolute lifetime is an open question (envisioning section 9); the 30-day default is conservative and revisited after 60 days of production observation.
- The 24-hour-lead-time check compares the projected TTL against the operator's next scheduled booking window across all active rules and any pending manual ad-hoc bookings.
- Acknowledgement (FR-027) suppresses re-emission for the current heartbeat cycle only. It does not clear the underlying condition.
- A cookie-invalid response at booking time produces two distinct outbox entries: a failure outcome ("booking failed: cookie invalid") and a "paste new cookie" alert. Both are persisted in one transaction before the dispatcher reads them (ADR-0002).
- Cost: zero direct infrastructure cost beyond the storage and Key Vault read operations already accounted for in ADR-0002 and ADR-0005.

## References

- `docs/features/wodbuster-booking-worker/spec.md` User Stories 3 and 4, FR-011, FR-020 through FR-024, FR-027, Invariants.
- `docs/envisioning/wodbuster-booking-scheduler.md` section 7 constraints 1, 5, 6.
- `docs/features/phase-0-api-discovery/feasibility-report.md` cookie-handoff reproduction.
- `docs/architecture/decisions/0002-persistence.md` for the encrypted-blob storage.
- `docs/architecture/decisions/0005-secrets-and-identity-access.md` for the encryption key custody.
