# Implementation Plan: Phase 0 API Discovery Spike

- **Created on**: 2026-06-29
- **Status**: Draft
- **Spec**: `docs/features/phase-0-api-discovery/spec.md`
- **Type**: Investigation spike. No production code. No infrastructure.

## Summary

This plan describes how to execute the Phase 0 API discovery spike defined in the feature spec. The deliverable is a single markdown feasibility report containing a go or no-go recommendation backed by reproducible evidence. The spike is operated manually by the project owner against the owner's own WodBuster account on a local workstation. No Azure resources are provisioned. No credentials are committed. The investigation is time-boxed to 5 working days.

## Reconciliation Note

The conductor accepted the proposed list of 6 production ADRs (hosting service, auth and session, configuration interface, secrets and identity access, observability and heartbeat, IaC tooling) and at the same time chose to skip the engineering-practices interview for now. These two answers are reconciled as follows:

1. The 6 ADRs are production concerns. They belong to the `scheduled-booking-worker` feature plan, which is deferred.
2. The current spike is a local investigation with no infrastructure, no secrets, no observability, and no deployment. None of the 6 ADRs is needed to execute it.
3. No ADR is drafted in this turn.
4. The 6 ADRs are carried forward as a backlog and are expected to be drafted in the next planning round that begins after a `GO` recommendation from the spike. See the Deferred ADR Backlog section.

If the owner disagrees with this interpretation, the next planning round can revisit it.

## Engineering Practices

Skipped for this turn. The spike is a local investigation. CI, branch strategy, observability, and IaC choices are not exercised. These decisions will be addressed in the next planning round if the spike returns `GO`.

## Approach

The spike follows a five step investigation pattern:

1. Observe. Use the browser to perform a real login and a real booking attempt on the owner's account. Capture all network traffic with the browser's developer tools.
2. Reproduce. Re-execute the captured auth flow from curl or Python without the browser. Confirm that a session token can be obtained outside the browser.
3. Reproduce again. Re-execute the captured booking flow from curl or Python using the session token from step 2.
4. Probe. Apply gentle probes for rate limiting and anti-automation defenses, within the polite client quotas defined in the spec (FR-009).
5. Decide. Write the feasibility report. Declare `GO`, `NO-GO`, or `CONDITIONAL` with explicit rationale.

Each step produces evidence (captured requests, captured responses, observations, quoted ToS clauses) that is filed in the report.

## Investigation Steps

Numbered for the operator. Each step lists the action, the evidence to capture, and the stop condition.

### Step 1. Terms of Service review

- Action: Locate the WodBuster Terms of Service. Read sections that address third party access, automated access, scraping, or unauthorized use of the platform.
- Evidence to capture: A quoted excerpt of every relevant clause. A summary verdict (no restriction observed, soft restriction, hard restriction, ambiguous).
- Stop condition: A hard restriction explicitly forbidding programmatic access ends the spike with `NO-GO`.

### Step 2. Baseline manual booking

- Action: Perform one normal login and one normal booking through the WodBuster web UI on the owner's account. Use the browser's developer tools (Network tab) with "Preserve log" enabled.
- Evidence to capture: A HAR export or equivalent. The exact list of requests issued during login. The exact list of requests issued during the booking action.
- Stop condition: None. This is the reference run.

### Step 3. Auth flow analysis

- Action: Identify which requests from Step 2 constitute the auth flow. For each, document method, URL, headers, payload, response code, response body shape, and any session persistence mechanism (cookie names, token format).
- Evidence to capture: Auth flow section of the report. Note any CSRF token, hidden form field, JavaScript derived value, or redirect chain.
- Stop condition: The flow is documented or an irreducible browser-only step is identified.

### Step 4. Auth flow reproduction outside the browser

- Action: Re-execute the documented auth flow from curl or from a single Python script using `requests`. Use a fresh session each time.
- Evidence to capture: The minimal command or script that obtains a valid session, with credentials replaced by placeholders. The observed response that confirms the session is valid.
- Stop condition: Either a valid session is obtained outside the browser, or the irreducible browser-only dependency is documented (CAPTCHA, signed JavaScript payload, attestation, or other).

### Step 5. Booking endpoint analysis

- Action: Identify which request from Step 2 actually performs the booking. Document method, URL, headers, payload, response code, response body. Note any input that comes from a prior page load (class identifier, slot token, CSRF token).
- Evidence to capture: Booking request section of the report.
- Stop condition: The request is fully documented.

### Step 6. Booking endpoint reproduction outside the browser

- Action: Issue at most one booking request from curl or Python using the session from Step 4, for a class the owner actually intends to attend or will cancel manually. Measure round trip latency with a Python timer or with `curl -w`.
- Evidence to capture: The minimal command or script. The response code and body excerpt. The measured latency in milliseconds. Confirmation in the WodBuster UI that the booking was registered.
- Stop condition: One successful reproduction is enough. Do not loop.

### Step 7. Polite probing for anti-automation defenses

- Action: Within the quotas defined in spec FR-009, observe whether the platform reacts to a small number of consecutive non-malicious requests (for example three sequential reads of the class list page, spaced one second apart). Do not retry failed logins. Do not parallelize.
- Evidence to capture: Any non-200 response. Any response header that suggests throttling (`Retry-After`, `X-RateLimit-*`). Any sudden CAPTCHA appearance. If nothing is observed, record "no defenses observed during the spike" explicitly.
- Stop condition: Any sign of throttling, or completion of the probe schedule, whichever comes first.

### Step 8. Write the feasibility report

- Action: Compose `feasibility-report.md` using the evidence collected. Place the recommendation (`GO`, `NO-GO`, or `CONDITIONAL`) and the rationale on the first page. Attach captured evidence in dedicated sections.
- Evidence to capture: The report itself.
- Stop condition: The report meets every functional requirement in the spec (FR-001 through FR-011).

## Evidence Checklist

The feasibility report is complete when every item below is present.

| Item | Source step | Required for |
|------|-------------|--------------|
| ToS review summary with quoted clauses | Step 1 | FR-006 |
| HAR export or request log of one full manual booking | Step 2 | FR-003, FR-004 |
| Documented auth flow (endpoint, method, payload shape, session mechanism) | Steps 3 and 4 | FR-003 |
| Reproducible auth command or script (credentials redacted) | Step 4 | User Story 2 |
| Documented booking request (endpoint, method, payload, response) | Steps 5 and 6 | FR-004 |
| Latency measurement for the booking request | Step 6 | FR-007 |
| Rate limiting and anti-automation observations (or explicit "none observed") | Step 7 | FR-005 |
| Recommendation (`GO`, `NO-GO`, or `CONDITIONAL`) on the first page | Step 8 | FR-002 |
| List of assumptions that, if false, invalidate the recommendation | Step 8 | FR-011 |
| Time-box record (start date, end date, days consumed) | Step 8 | FR-008 |

## Exit Criteria

The spike is considered done when all of the following are true:

1. The feasibility report exists at `docs/features/phase-0-api-discovery/feasibility-report.md`.
2. Every item in the Evidence Checklist is present in the report.
3. The recommendation is one of `GO`, `NO-GO`, or `CONDITIONAL`. The rationale is on the first page.
4. The repository contains no plaintext credentials, no session tokens, and no PII.
5. The owner's WodBuster account is in good standing.

If any item is missing at the end of the time-box, the spike is closed as `NO-GO` by default, with the gaps documented as open questions.

## Recommended Toolchain

The toolchain is intentionally minimal. The spike runs locally on the owner's workstation.

| Tool | Purpose | Notes |
|------|---------|-------|
| Browser developer tools (Chromium based recommended) | Capture the baseline manual booking. Export HAR. | Enable "Preserve log" before logging in. |
| `curl` | Reproduce single requests with full control over headers and cookies. | Use `-w "%{time_total}\n"` for coarse latency. |
| Python with `requests` | Reproduce multi step flows that need cookie or session handling. | Use a single `requests.Session` to track cookies. |
| `mitmproxy` (optional) | Inspect traffic from a non-browser client when curl alone is not enough. | Optional. Adds setup cost. Use only if the browser HAR is insufficient. |
| Plain markdown editor | Write the feasibility report. | No special tooling required. |

No package manager state, no virtual environment, and no infrastructure is required to be committed to the repository for this spike. If a small helper script is written during reproduction, it can be checked in under `docs/features/phase-0-api-discovery/scripts/` with credentials replaced by environment variable references.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| The spike's exploration triggers a WodBuster account lockout. | Medium. Repeated login attempts during reproduction are the main cause. | The owner cannot log in manually. Real classes are missed during the lockout. | Enforce the FR-009 quotas (no more than 5 login attempts per hour, no more than 2 real booking attempts in total). Use the browser baseline first. Reuse cookies and sessions across reproduction attempts. Stop immediately at the first sign of throttling. |
| The investigation reveals a Terms of Service clause that forbids programmatic access. | Medium. Several SaaS platforms include such clauses. | Continuing the project becomes a deliberate ToS risk by the owner. | The ToS review is Step 1 and runs before any reproduction. If a hard restriction is found, the spike stops at `NO-GO` and surfaces the clause to the owner for an explicit decision. |
| Plaintext credentials leak into the report or into a committed helper script. | Low if the redaction policy is applied. High if a HAR file is checked in raw. | Credential exposure on a public repository. | Redact before writing. Never commit raw HAR files. Helper scripts read credentials from environment variables. Pre-commit inspection of any file added to the spike folder. |
| The auth payload is signed by browser JavaScript that the spike cannot replicate within the time-box. | Medium. Modern web platforms increasingly do this. | The spike returns `CONDITIONAL` or `NO-GO`. | Documented as an edge case in the spec (User Story 3, Edge Cases). Step 4 explicitly stops at this boundary rather than burning days trying to reverse-engineer signing logic. |
| The booking endpoint exists but returns HTML rather than JSON. | Low to medium. | Parsing HTML is forbidden by the project's hard constraints. The project stops. | Step 5 documents the response shape. If HTML, the spike returns `NO-GO` with the response excerpt. |
| The time-box (5 working days) is insufficient. | Low for a focused single-account investigation. Medium if anti-automation defenses are complex. | The spike risks expanding into a multi-week effort without a decision. | The default behavior at time-box exhaustion is to close as `NO-GO`. Extension requires an explicit owner decision recorded in the report. |

## Deferred ADR Backlog

The following ADRs are expected for the production worker plan and are deferred until the spike returns `GO`. They are not drafted in this turn. The conductor's acceptance of the list is recorded here for traceability.

| ADR | Topic | Decided after the spike because |
|-----|-------|---------------------------------|
| ADR-NNNN | Hosting service for the worker (Azure Functions Premium with always ready, Container Apps Jobs, or Consumption with pre-warm). | The cold start strategy depends on the latency floor observed in spike Step 6. |
| ADR-NNNN | Auth and session model (one-shot login per run, cached session token, or refresh-on-demand). | Depends on the auth flow documented in spike Steps 3 and 4 and on observed session lifetimes. |
| ADR-NNNN | Configuration interface (Git tracked YAML per user, minimal admin UI, or other). | Independent of the spike but premature to lock until the worker is real. |
| ADR-NNNN | Secrets and identity access (Azure Key Vault layout, managed identity scope, rotation policy). | Premature without a hosting choice. |
| ADR-NNNN | Observability and heartbeat (dead man's switch design, notification on missing run). | Premature without a hosting choice. |
| ADR-NNNN | IaC tooling (Bicep or Terraform). | Premature without any infrastructure to provision. |

## Commands

The spike has no application build, test, or lint pipeline. The commands below are the canonical investigation invocations. Use them as starting points, not as a fixed script.

### Capture a baseline auth and booking run

Open the browser's developer tools, enable Network with "Preserve log", log in to WodBuster, perform one booking, then export HAR via the Network tab's context menu.

### Reproduce auth with curl

```bash
curl -i -c cookies.txt \
  -X POST "https://<wodbuster-host>/<login-endpoint>" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data "username=$WB_USER&password=$WB_PASSWORD"
```

Credentials read from environment variables. Cookie jar persisted to `cookies.txt` (kept outside the repository).

### Reproduce a booking request and measure latency with curl

```bash
curl -i -b cookies.txt \
  -X POST "https://<wodbuster-host>/<booking-endpoint>" \
  -H "Content-Type: application/json" \
  -d @booking-payload.json \
  -w "\nTotal time: %{time_total}s\n"
```

Payload file `booking-payload.json` kept outside the repository.

### Reproduce the full flow in Python

```bash
python scripts/spike-reproduce.py
```

Helper script reads `WB_USER` and `WB_PASSWORD` from the environment, drives a single `requests.Session`, and prints the booking response with measured latency. Script is committable when free of credentials.

### Optional: inspect traffic with mitmproxy

```bash
mitmproxy --listen-port 8080
```

Configure the browser or the Python `requests` client to use `http://127.0.0.1:8080` as a proxy. Use only if the browser HAR is insufficient for Steps 3 and 5.

## Reasoning Log

- The spike was scoped down from the full project to a single investigation because every downstream decision depends on its outcome. Planning the production worker before knowing the auth flow would waste effort on the wrong assumptions.
- The 6 production ADRs accepted by the conductor were not drafted in this turn because none of them is needed to execute the spike. Drafting them now would commit the project to decisions whose inputs do not yet exist (latency floor, auth shape, session lifetime).
- The engineering-practices interview was skipped per the user's decision. The spike is a local investigation; CI and IaC choices are not exercised. The interview is expected to run when the production worker plan starts.
- The time-box (5 working days) is a guess calibrated for a single-account, single-platform investigation. The exit rule "close as NO-GO at time-box exhaustion" is deliberately strict to prevent the spike from expanding into a multi-week effort.
- The polite-client quotas (5 logins per hour, 2 real bookings total) are conservative defaults chosen to keep the owner's account safe. They can be revised in a spec amendment if the spike needs more room and the owner explicitly accepts the lockout risk.
- The recommended toolchain was kept minimal (browser devtools, curl, Python `requests`) because every added tool adds setup cost that competes with investigation time.

## Handoff Envelope

| Item | Value |
|------|-------|
| Originating agent | `devsquad.plan` |
| Next agent | `devsquad.decompose` for the spike (the spike has at most 1 to 2 work items: "run the spike" and "write the report"). The user may alternatively go straight to `devsquad.implement` since the spike is investigation-only and does not require formal decomposition. |
| Artifacts produced | `docs/features/phase-0-api-discovery/spec.md`, `docs/features/phase-0-api-discovery/plan.md` |
| ADRs produced | None |
| Deferred ADRs | 6 production ADRs, listed under Deferred ADR Backlog, to be drafted in the planning round that follows a `GO` outcome. |
| Architectural assumptions | The spike is local, manual, single-user, and uses the owner's own account. No cloud, no shared secrets, no third party. |
| Discarded alternatives | (1) Plan the full production worker now and treat the spike as a parallel track. Rejected because every production decision depends on the spike's outcome. (2) Draft the 6 production ADRs as proposals now. Rejected because their inputs (latency, auth shape, session lifetime) do not yet exist. |
