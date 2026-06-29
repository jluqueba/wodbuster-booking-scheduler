# Spike reproduction scripts

This folder holds the Python script used by the Phase 0 API discovery spike to reproduce the WodBuster booking flow outside the browser.

## Design: cookie handoff

The Step 2 HAR established that WodBuster's booking and cancellation actions are plain HTTP GETs against `*.ashx` handlers on the gym subdomain, authenticated by a cookie scoped to `.wodbuster.com`. The script does **not** perform the login round trip. Instead, the operator logs in once through a normal browser, exports the resulting `Cookie` header from devtools, and the script replays it from a `requests.Session()`.

This design is the simplest test of assumption A6 in the feasibility report (cookie replayability). If it works, it also collapses A4 (whether `connectionId` is mandatory) and A7 (whether the booking endpoint requires extra anti-bot signals) in one or two further runs.

## Files

| File | Purpose |
|------|---------|
| `spike-reproduce.py` | Cookie-handoff reproduction script. Phase 1 (read-only) issues `GET LoadClass.ashx`. Phase 2 (gated) issues `GET Calendario_Inscribir.ashx`. |
| `README.md` | This file. |

## Prerequisites

Python 3.11 or newer plus the `requests` package in a local virtual environment.

PowerShell on Windows:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install requests
```

The `.venv/` folder is ignored by the repository `.gitignore`.

## Exporting the cookie header from the browser

In the browser session that is already logged in to WodBuster:

1. Open devtools, Network tab. Tick **Preserve log**.
2. Visit `https://antworktrainingcenter.wodbuster.com/athlete/reservas.aspx`.
3. Click the first entry in the network list (the `reservas.aspx` document).
4. Open the **Headers** sub-tab, scroll to **Request Headers**.
5. Right-click the value of `Cookie:` and copy it.
6. Save the copied string to `C:\spike-captures\cookie-header.txt` as a single line.

The file lives outside the repository. The repo `.gitignore` also blocks `cookies.txt` and `*.cookiejar` as a second line of defense.

## Running the script

Phase 1 only, the read-only sanity check:

```powershell
$env:WB_COOKIES_FILE = "C:\spike-captures\cookie-header.txt"
$env:WB_IDU          = "<32-char-hex-from-LoadClass-url-in-the-Step-2-HAR>"
$env:WB_GYM          = "antworktrainingcenter"
python spike-reproduce.py
```

Expected output: a 200 response, `application/json` or `text/json` content type, a few kilobytes of payload, and a printed list of top-level JSON keys. That outcome confirms the cookie handoff works.

Phase 2, a real booking attempt. **Counts against the FR-009 quota of 2 real bookings for the entire spike.** Pick a class you intend to attend, or one you will cancel manually right after:

```powershell
$env:WB_CLASS_ID    = "<id-from-LoadClass-response>"
$env:WB_CLASS_TICKS = "<unix-seconds-of-class-day-at-00:00-UTC>"
$env:WB_BOOK        = "1"
# Optional: set if first attempt without it returns an error.
# $env:WB_CONNECTION_ID = "<from-a-fresh-/bookinghub/negotiate-call>"
python spike-reproduce.py
```

If `WB_BOOK` is not set to `1`, phase 2 runs as a dry run and prints what it would have requested without issuing the call.

## Where redacted output goes

Phase 1 prints status, latency, content type, response size, and top-level JSON keys. None of that is sensitive. Paste it as-is into the feasibility report under the "Booking request" and "Latency observation" sections.

Phase 2 prints the same shape. Do not paste raw response bodies. If you need a body excerpt, redact concrete identifiers first:

| Real value | Placeholder |
|------------|-------------|
| Owner email or username | `<owner-email>` |
| Session cookie value | `<session-cookie>` |
| `idu` value | `<user-id>` |
| Class identifier | `<class-id>` |
| Internal booking id (response field) | `<booking-id>` |

## Forbidden

The `.gitignore` blocks the file patterns, but the operator is the last line of defense.

| Forbidden action | Why |
|------------------|-----|
| Committing the cookie file, any `*.har`, or any `.env` file | Session material would leak into the repository. |
| Hard-coding the cookie value or `idu` inside the script | Same leak risk. Both are loaded from the environment or from a file outside the repo. |
| Running phase 2 more than twice across the entire spike | Spec FR-009. Protects the owner's account from lockout and respects WodBuster's "derecho de exclusión" clause. |
| Running automatic retries on failure | Spike invariants. Inspect the response and decide whether to retry by hand. |
| Pasting raw response bodies into the report without redaction | Response bodies contain class and member identifiers. |

## Polite client quotas

Spec `FR-009` caps usage at no more than 5 login attempts per hour and at most 2 real booking attempts for the entire spike. Phase 1 (read-only `LoadClass.ashx`) does not count against either quota. Phase 2 counts against the booking cap. The script does not enforce the caps. The operator does.
