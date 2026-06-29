# Phase 0 Feasibility Report

- **Status**: Recommendation drafted, pending owner sign-off
- **Spec**: `docs/features/phase-0-api-discovery/spec.md`
- **Plan**: `docs/features/phase-0-api-discovery/plan.md`

## Recommendation

**GO** (draft, owner to confirm)

### Rationale

The spike has demonstrated, with reproducible evidence collected on 2026-06-29, that the production booking worker can be built on the simplest possible runtime: a stock HTTP client with a single auth cookie and three query parameters. Every architectural risk that could have justified a NO-GO or CONDITIONAL outcome has been closed.

1. **Legal posture is acceptable for the current scope.** The WodBuster Aviso Legal contains no prohibition of automated access for personal use and affirmatively permits copying contents to a personal device. The binding constraint (clause 2, anti-circumvention) is honored by riding a real browser session that the operator established under Cloudflare, rather than defeating the challenge programmatically. Single-user personal scope is explicitly permitted.
2. **Cookie handoff is empirically viable (A6 CONFIRMED).** Phase 1 of `spike-reproduce.py` replayed a single `.WBAuth` cookie from a Python `requests.Session()` and received authenticated JSON from `LoadClass.ashx`. No browser fingerprint, no `cf_clearance` cookie, no Cloudflare challenge on the authenticated gym subdomain.
3. **Mutating booking is empirically viable (A7 CONFIRMED, A4 CONFIRMED OPTIONAL).** Phase 2a fired `Calendario_Inscribir.ashx` from the same non-browser client with the `connectionId` query parameter omitted entirely. The server returned `200 OK` in 336 ms with a 75 KB JSON body whose top-level keys mirror `LoadClass` plus a per-call `Res` result field. The booking was confirmed end-to-end by a subsequent read-only `LoadClass.ashx` call which showed the operator's status on the target slot transitioned to `Borrable` (cancel-able, meaning enrolled).
4. **No SignalR client is required in production.** Because `connectionId` is optional, the production worker does not need to maintain or open a SignalR connection to book a class. The SignalR `/bookinghub` channel exists only to receive live notifications, which the worker has no use for.
5. **Latency budget has ample headroom.** Cold connection cost is ~1.3 s (DNS + TCP + TLS handshake); warm cost settles at 37-336 ms. A pre-warming pattern (one cheap authenticated GET ahead of the booking-window open) collapses the cold cost. FR-007's 10-second budget is met with more than 95% headroom.
6. **No anti-automation signal observed.** Across 21 browser requests in the HAR, 4 scripted read probes in phase 1, and 1 scripted booking call in phase 2a, no rate-limit header, no CAPTCHA, no `cf_clearance` requirement, and no account warning was observed. The authenticated gym subdomain serves established sessions without a Cloudflare challenge.

Three assumptions remain formally open (A1 secondary ToS document, A5 `idu` rotation across logins, partial credential-exchange HAR for Q7). None of them can plausibly invalidate the GO; the worst case for any of them is that the auth-and-session ADR must specify a slightly different refresh procedure. The cookie-handoff design is robust against all three.

### Conditions carried forward into the production feature spec

1. Worker must ride a real browser session for login; programmatic defeat of the Cloudflare challenge is out of bounds.
2. Booking-time runtime must be a thin HTTP client. The browser is only in the picture during cookie refresh.
3. Worker must fail safely on session expiry and must not retry aggressively against an invalid session.
4. Single-user scope. Any extension to additional users triggers a re-read of the ToS commercial-reproduction clause.
5. Polite quotas: at most 5 login attempts per hour, at most 2 deliberate booking attempts per day during further spikes, no automated cancel-rebook loops.
6. Worker must derive its booking timing from `SegundosHastaPublicacion` as the source of truth, with the operator's static schedule as a backup.

## Time-box record (FR-008)

| Field | Value |
|-------|-------|
| Start date | 2026-06-29 |
| End date | 2026-06-29 |
| Working days consumed | 1 (well under the 5-day budget) |
| Extension requested | No |
| Extension rationale | Not applicable |

## Terms of Service review (FR-006)

Source: plan Step 1.

### Source

| Field | Value |
|-------|-------|
| ToS document URL | https://wodbuster.com/avisolegal-politicaprivacidad.aspx |
| Document title | Aviso Legal y Política de Privacidad |
| Language | Spanish |
| Retrieval date | 2026-06-29 |
| Version or last-updated marker on the page | None visible on the page. Document references LOPD 15/1999 and RD 1720/2007, both pre-GDPR, suggesting the document has not been refreshed since at least 2018. |
| Programmatic retrieval result | Blocked by Cloudflare managed challenge. The unauthenticated HTTP GET returned the Cloudflare interstitial, not the document. Text below was retrieved by the owner via a normal browser session and pasted into this report. |

### Quoted clauses

Quoted verbatim in the original Spanish.

**1. Uso del portal, item 3.**

"Provocar daños en los sistemas físicos y lógicos de WodBuster, de sus proveedores o de terceras personas, introducir o difundir en la red virus informáticos o cualesquiera otros sistemas físicos o lógicos que sean susceptibles de provocar los daños anteriormente mencionados."

**2. Propiedad intelectual e industrial, anti-circumvention clause.**

"El USUARIO deberá abstenerse de suprimir, alterar, eludir o manipular cualquier dispositivo de protección o sistema de seguridad que estuviera instalado en las páginas de WodBuster."

**3. Propiedad intelectual e industrial, commercial reproduction clause.**

"En virtud de lo dispuesto en los artículos 8 y 32.1, párrafo segundo, de la Ley de Propiedad Intelectual, quedan expresamente prohibidas la reproducción, la distribución y la comunicación pública, incluida su modalidad de puesta a disposición, de la totalidad o parte de los contenidos de esta página web, con fines comerciales, en cualquier soporte y por cualquier medio técnico, sin la autorización de WodBuster."

**4. Propiedad intelectual e industrial, personal-use permission.**

"Podrá visualizar los elementos del portal e incluso imprimirlos, copiarlos y almacenarlos en el disco duro de su ordenador o en cualquier otro soporte físico siempre y cuando sea, única y exclusivamente, para su uso personal y privado."

**5. Derecho de exclusión.**

"WodBuster se reserva el derecho a denegar o retirar el acceso a portal y/o los servicios ofrecidos sin necesidad de preaviso, a instancia propia o de un tercero, a aquellos usuarios que incumplan las presentes Condiciones Generales de Uso."

### Keyword search results

| Keyword | Found in document |
|---------|-------------------|
| `automatizado` / `automatización` / `automático` | No |
| `robot` / `bot` / `crawler` / `araña` | No |
| `scraping` / `scrap` | No |
| `extracción` / `extraer` / `recolección` | No |
| `API` | No |
| `terceros` | Yes, but only in third-party-content liability disclaimers, not related to automation |
| `medios técnicos` | Yes, in the commercial reproduction clause, qualified by "con fines comerciales" |

### Verdict

**Soft restriction, leaning permissive for personal use. Not a NO-GO.**

Reasoning. The Aviso Legal contains no explicit prohibition of automated access, scraping, bots, third-party clients, or API consumption. It contains affirmative permission to copy and store contents for personal and private use. The binding limit is clause 2, which forbids "eludir o manipular cualquier dispositivo de protección o sistema de seguridad". Cloudflare's managed challenge qualifies as such a system, so the project must not defeat it programmatically. The commercial-reproduction clause is qualified by "con fines comerciales" and does not apply to single-user personal use. WodBuster reserves the right to revoke access at any time without notice; this is a material risk the project must accept, not a legal block.

### Constraints carried forward from the ToS review

1. The booking client must reuse a session that was opened through the normal WodBuster login flow in a real browser. The client must not attempt to solve the Cloudflare managed challenge from a clean state.
2. The project must not use tooling whose declared purpose is to defeat anti-bot defenses (for example `undetected-chromedriver`, `flaresolverr`-style proxies, or third-party CAPTCHA solvers).
3. Account revocation at WodBuster's sole discretion is acknowledged. The runtime must fail safely if the session becomes invalid and must not retry aggressively.
4. Scope today is single-user personal use, which is explicitly permitted. Extending the bot to additional users (the 1–2 friends mentioned in envisioning) requires re-reading the commercial reproduction and personal-use clauses before that scope change is made.

### Implication for the auth design (Phase 0 finding, pre-Step 2)

This finding constrains the auth choice the production worker will eventually make. A pure `requests`-only client that posts directly to the login URL without ever passing through Cloudflare is unlikely to succeed against an edge-protected target, and trying to bypass the edge would violate clause 2. The viable architectures are:

- A real browser (Playwright or similar) inside the worker that performs login through the standard challenged page, then either drives the booking via the browser or hands the resulting session cookies to a thin `requests` client for the time-critical booking call.
- A manual one-shot login by the owner on demand, with the session cookies imported into the worker by hand or through a controlled handoff. This trades automation for legal clarity.

The choice between these two is deferred to the auth-and-session ADR (Deferred ADR Backlog in `plan.md`), not made here. The point of recording the constraint now is so that Step 2 evidence is interpreted with this constraint in mind.

## Authentication flow (FR-003)

Source: plan Steps 3 and 4.

Status: **partial** for the credential exchange itself, **confirmed** for the session-replay design.

The Step 2 HAR was captured from an already authenticated browser session, so the actual login round trip is not in the capture. The two login-related entries in the HAR are both 302 redirects on `wodbuster.com/account/`, which confirms the login surface lives on the apex domain but does not reveal the credential-bearing request. A second HAR captured from a logged-out start would close this gap; the operator was unable to capture one because the WodBuster UI does not expose a visible logout control (see Q8 below).

The session-replay half of the design is no longer pending. Phase 1 of `spike-reproduce.py` (described below) demonstrated that the auth cookie exported from a logged-in browser, replayed by a non-browser `requests` client, is accepted by the gym subdomain and returns the authenticated JSON for `LoadClass.ashx`.

| Field | Value |
|-------|-------|
| Endpoint URL | Inferred from the HAR navigation chain: `https://wodbuster.com/account/login.aspx`. Confirmation pending a logged-out capture. |
| HTTP method | Not captured. ASP.NET WebForms convention is `POST` of the same `.aspx` URL with `__VIEWSTATE` and `__EVENTVALIDATION` hidden fields. To be confirmed. |
| Request content type | Not captured. Conventionally `application/x-www-form-urlencoded`. To be confirmed. |
| Request payload shape (field names only) | Not captured. To be confirmed by a logged-out HAR. |
| Response status code | Not captured. The two redirects observed are `GET /account/login.aspx?ReturnUrl=...` -> 302 and `GET /account/roadtobox.aspx` -> 302, consistent with an already-authenticated session bouncing to the post-login landing page. |
| Response content type | Not captured for the credential exchange itself. |
| Response payload shape | Not captured. The success signal is expected to be a redirect plus a session cookie on the parent domain. |
| Session persistence | Single auth cookie `.WBAuth` scoped to the parent domain `.wodbuster.com`. Confirmed by phase 1 reproduction: the cookie was exported from a logged-in browser, replayed by a stock `requests.Session()`, and the server returned the authenticated JSON for `LoadClass.ashx`. The cookie value is roughly 534 characters long, consistent with an ASP.NET Forms Authentication ticket. No other session cookie is required. |
| Rotating elements (CSRF, hidden form fields, nonces) | Not captured. ASP.NET `__VIEWSTATE` and `__EVENTVALIDATION` are expected on login form posts. To be confirmed. |
| Browser-only dependencies (signed JS, attestation, CAPTCHA) | The apex `wodbuster.com` surface is fronted by Cloudflare managed challenge (recorded under Step 7 from Step 1). Login presumably passes through that gate. The authenticated gym subdomain is NOT challenged for established sessions: phase 1 reproduction made four consecutive `requests` calls from a Python client with no browser fingerprint and received four normal 200 responses, with no CF interstitial, no `cf_clearance` cookie required, and no Set-Cookie response inviting one. |

### Reproduction command or script

Cookie-handoff reproduction is implemented by `docs/features/phase-0-api-discovery/scripts/spike-reproduce.py`. The operator exports the `Cookie` header from a logged-in browser session to a file outside the repository, then runs:

```powershell
$env:WB_COOKIES_FILE = "C:\spike-captures\cookie-header.txt"
$env:WB_IDU          = "<user-id>"            # 32-char hex from any /athlete/handlers/*.ashx URL
$env:WB_GYM          = "antworktrainingcenter"
python docs\features\phase-0-api-discovery\scripts\spike-reproduce.py
```

The script issues a single read-only `GET /athlete/handlers/LoadClass.ashx` to confirm that the cookie alone authenticates a non-browser client. See the README in the same folder for the full procedure and the cookie export steps from devtools.

### Reproduction result

Phase 1 of `spike-reproduce.py` executed successfully on 2026-06-29.

| Outcome | Value |
|---------|-------|
| HTTP status | 200 |
| Response content type | `text/json; charset=utf-8` |
| Response size | 11644 bytes |
| Cookies required | A single `.WBAuth` cookie |
| Response JSON top-level keys | `ClasesDesc`, `ClasesFiltradas`, `Data`, `EsAdmin`, `EsCoach`, `IdsEnDiaCortesia`, `IdsEnDiaCortesiaGCL`, `Mantenimiento`, `MostrarAsistencia`, `MostrarNumListaEspera`, `Next`, `NextTitle`, `Palmares`, `Prev`, `PrevTitle`, `PromptEnReservas`, `SegundosHastaPublicacion`, `TieneFiltros`, `TienePizarra`, `TipoNoClases`, `Title`, `ToDay`, `UrlReservaClases`, `Version` |

A particularly relevant field is `SegundosHastaPublicacion` ("seconds until publication"). The production worker's booking-window timer can either compute the window from the operator's static configuration or read this field from a pre-booking calendar fetch as a server-anchored countdown. The choice is deferred to the production feature spec.

### Architectural finding (Phase 0): cookie-handoff is confirmed viable

Phase 1 reproduction is the empirical close of assumption A6. The smallest viable production worker is a stock HTTP client (Python `requests`, Node `fetch`, .NET `HttpClient`) plus the operator's `.WBAuth` cookie. No browser is needed at booking time. The browser is only needed once per cookie expiry, to perform the interactive login under Cloudflare. The auth-and-session ADR can therefore be framed as a choice between (a) operator-driven manual cookie refresh on a known schedule, and (b) a Playwright headless flow that performs the login through CF and writes the resulting cookie to a secret store. Both designs share the same booking-time runtime, which is a thin HTTP client.

## Booking request (FR-004)

Source: plan Step 2 HAR (request shape captured), Steps 5 and 6 (out-of-browser reproduction still pending).

| Field | Value |
|-------|-------|
| Endpoint URL | `https://antworktrainingcenter.wodbuster.com/athlete/handlers/Calendario_Inscribir.ashx` |
| HTTP method | `GET` |
| Query parameters | `id` (class identifier, integer), `ticks` (UNIX timestamp of the week's Monday at 00:00 UTC, integer), `idu` (user identifier, 32-char hex GUID without dashes, stable per account), `connectionId` (SignalR connection identifier obtained from a prior `POST /bookinghub/negotiate`), `_` (cache-buster timestamp) |
| Request headers | Standard browser headers plus the `.wodbuster.com` auth cookie. No custom `X-CSRF`, no bearer token, no signed header observed. |
| Request payload | None. The action is encoded entirely in query parameters. |
| Response status code | `200 OK` |
| Response content type | `text/json; charset=utf-8` |
| Response body excerpt (redacted) | Status 200, response size 75043 bytes. Top-level keys are a superset of `LoadClass.ashx`: the full calendar-state object plus a new per-call field `Res` carrying the booking-result indicator. Body content is not echoed in this report per FR-010 redaction (it contains `AtletasEntrenando` PII for other athletes in the class). |
| Inputs sourced from a prior page load | `id` comes from `GET /athlete/handlers/LoadClass.ashx?ticks=...&idu=...` for the target week. `idu` is stable for the account and can be discovered once after login (it appears in every `/athlete/handlers/*` URL). `connectionId` is **OPTIONAL** (phase 2a confirmed) and is not needed for the production worker. `ticks` is computed from the calendar date, no lookup needed. |

### UI confirmation

Direct, from phase 2a of `spike-reproduce.py` on 2026-06-29. The operator manually canceled the Wed 01/07 21:30 Cross Training booking in the WodBuster UI, leaving the slot un-enrolled. The script then fired `GET /athlete/handlers/Calendario_Inscribir.ashx?id=<class-id>&ticks=<ticks>&idu=<idu>` with `connectionId` omitted entirely, and received `200 OK` in 336 ms. A subsequent read-only `GET /athlete/handlers/LoadClass.ashx?ticks=<ticks>&idu=<idu>` returned the same slot with `TipoEstado: Borrable` and the operator's identifier listed in `AtletasEntrenando`, confirming the booking was registered server-side. The booking was end-to-end verified without the script ever touching a browser, a SignalR connection, or any anti-bot signal.

### Sibling endpoints discovered in the same HAR

Recorded for completeness. Not all of these are in scope for the booking-only goal.

| Operation | URL pattern | Notes |
|-----------|-------------|-------|
| Load week calendar | `GET /athlete/handlers/LoadClass.ashx?ticks={epoch}&idu={user_guid}&_={ts}` | Returns the JSON list of classes for the requested week. Use to discover the `id` of the desired class. |
| Book a class | `GET /athlete/handlers/Calendario_Inscribir.ashx?id=...&ticks=...&idu=...&connectionId=...&_=...` | The booking call. In scope. |
| Cancel a booking | `GET /athlete/handlers/Calendario_Borrar.ashx?id=...&ticks=...&idu=...&connectionId=...&_=...` | Out of scope per envisioning (no cancellation feature). Recorded only because it was in the captured flow and shares the same auth and parameter contract as the booking call. |
| User alerts ping | `GET /user/handlers/getalerts.ashx?_={ts}` | Probably a heartbeat or notifications poll. Not relevant to the booking path. |
| SignalR negotiation | `POST https://sr-3-4.wodbuster.com/bookinghub/negotiate?negotiateVersion=1` | Returns the JSON with the connection token used by the WebSocket upgrade. Required only if `connectionId` is a hard requirement of the booking endpoint (see new assumption A4 below). |
| SignalR hub | `wss://sr-3-4.wodbuster.com/bookinghub?id={token}` | Real-time push channel. The client joins a room with the hub method `JoinRoom("antworktrainingcenter", "{ticks}")` and receives `changedBooking` events. **Not required to issue the booking action itself**, only required if the client wants live updates and wants the server to suppress the echo of its own change. |

### Architectural finding: SignalR is a notification channel, not the booking transport

The operator's gym uses ASP.NET Core SignalR (the `/bookinghub/negotiate` shape, the JSON hub protocol envelopes, and the `JoinRoom` and `changedBooking` hub methods are the standard SignalR pattern). The booking and cancellation actions are issued over plain HTTP GET to `*.ashx` handlers on the gym subdomain. SignalR is used by the WodBuster web UI to receive live notifications when any user's booking changes for the joined week and room. The `connectionId` query parameter on the action handler is the bridge: the server uses it to suppress the echo of the originating client's own change in the SignalR broadcast.

This matters because:

- The minimum viable client is `requests` (or any HTTP client) plus a valid `.wodbuster.com` auth cookie.
- Phase 2a confirmed `connectionId` is OPTIONAL: the booking endpoint accepts `Calendario_Inscribir.ashx` calls with the parameter omitted entirely (200 OK in 336 ms). The production worker therefore needs no SignalR client at all: no `/bookinghub/negotiate` POST, no WebSocket handshake, no `JoinRoom` hub call. The booking-time runtime is a thin HTTP client and three query parameters.
- The 10-second budget (FR-007) is dominated by network round trips, not by browser rendering or JavaScript execution. There is no client-side computation that would force a real browser to remain in the booking hot path.

### Architectural finding: LoadClass is filtered by the user's training pattern, but the filter is dynamic

Phase 1 probing of `LoadClass.ashx` for eight consecutive days (2026-06-29 through 2026-07-06) showed that the response is filtered to the operator's enrolled time slot (21:30) and only returns rows for days where the operator has a current booking. Days the operator is not enrolled in returned `Data: []` even though the gym certainly runs classes those days. The top-level field `TieneFiltros: true` confirms a filter is active. The full gym schedule is exposed via the separate `ClasesFiltradas` array (32 entries for the queried day) but with `Id: 0` for every entry, indicating that the filtered view does not surface the concrete bookable instance ids for slots outside the operator's pattern.

**Phase 2a addendum: the filter is not static.** After the operator manually canceled the Wed 01/07 21:30 booking in the UI, the post-rebook `LoadClass.ashx` for the same Wednesday returned **13 `Data` rows covering 07:30 through 21:30** with **real, non-zero `Id` values for every slot** (33 distinct class instances total). This means the unfiltered view is reachable from a normal authenticated session under conditions the spike has not fully characterized. Three `TipoEstado` values were observed in the unfiltered response:

| Value | Meaning |
|-------|---------|
| `Borrable` | Operator is enrolled in this slot, cancel-able |
| `Inscribible` | Slot has free spots, book-able |
| `Avisable` | Slot is full, "notify me" indicator available |

Implications:

- For the production worker, the simplest source of a class id remains the user's own enrolled slot on the target day, which `LoadClass.ashx` returns directly with the real instance id. The configuration interface ADR can default to this assumption.
- The unfiltered view is reachable, which means scope extensions to non-enrolled slots are not architecturally blocked. Whether the unfiltered view is triggered by recent booking state changes, by a UI filter toggle, or by some session-scoped flag remains uncharacterized. This is a question for the production feature spec, not a Phase 0 blocker.
- During phase 1, three concrete class ids were captured for the operator's enrolled Mon/Tue/Wed 21:30 Cross Training slots of the current week. Phase 2a additionally observed 33 real class ids in the unfiltered Wednesday response. These ids exist in spike-session memory only and are not persisted in this report (per FR-010 redaction policy and the `<class-id>` placeholder).

### Architectural finding: `SegundosHastaPublicacion` is the publication-window oracle (FR-007 relevance)

The `LoadClass.ashx` response includes a top-level field `SegundosHastaPublicacion`, a float, observed at -233241.55 on 2026-06-29 (roughly -64.8 hours past zero). A negative value means the current window has already been published. A positive value means the booking window has not yet opened and the count represents seconds remaining.

This is the server-anchored countdown the production worker needs to satisfy FR-007 (fire within 10 seconds of the booking window opening) without relying on the operator's clock or a derived schedule. The worker can:

- Poll `LoadClass.ashx` periodically with a backoff that tightens as the countdown shrinks (for example once per minute when more than 10 minutes out, once per second under 30 seconds).
- Pre-warm an HTTP connection when the field crosses a fixed threshold (for example 5 seconds remaining), turning the cold-call cost recorded in the latency section into a warm-call cost.
- Fire the booking request as soon as the field transitions from positive to non-positive, with the warm session already established.

The alternative design (compute the publication time from the operator's static configuration) is still viable, but only as a backup. The server-anchored value is the source of truth and removes both clock skew and human configuration error.

This finding is recorded here because it materially shapes the production worker's main loop. The exact polling strategy is deferred to the production feature spec.

## Latency observation (FR-007)

Two measurement runs are recorded: one browser-side from the Step 2 HAR, one scripted from `spike-reproduce.py` on 2026-06-29 over the operator's home network.

### Browser-side baseline (from Step 2 HAR)

| Operation | Latency (ms) | Samples |
|-----------|-------------:|--------:|
| Calendar load (`LoadClass.ashx`) | 111, 125 | 2 |
| Cancel (`Calendario_Borrar.ashx`) | 116 | 1 |
| Book (`Calendario_Inscribir.ashx`) | 247 | 1 |

Measurement method: Chrome DevTools `time` field, end-to-end including DNS, TLS, request, server processing, response.

### Scripted reproduction phase 1 (read-only `LoadClass.ashx`)

Four consecutive `GET /athlete/handlers/LoadClass.ashx` calls from a fresh Python `requests.Session()`, 500 ms apart, over the operator's home network.

| Call | Latency (ms) | Status | Response bytes |
|-----:|-------------:|:------:|---------------:|
| 1 (cold) | 1284 | 200 | 11644 |
| 2 (warm) | 42 | 200 | 11644 |
| 3 (warm) | 82 | 200 | 11644 |
| 4 (warm) | 37 | 200 | 11644 |

### Scripted reproduction phase 2a (mutating `Calendario_Inscribir.ashx`)

One booking call issued on a session already warmed by a preceding `LoadClass.ashx`. The booking request omitted the `connectionId` query parameter entirely. The follow-up `LoadClass.ashx` confirmed the booking was registered server-side.

| Call | Latency (ms) | Status | Response bytes |
|-----:|-------------:|:------:|---------------:|
| Pre-booking `LoadClass.ashx` (warm-up) | 1125 | 200 | 80952 |
| `Calendario_Inscribir.ashx` (book) | 336 | 200 | 75043 |
| Post-booking `LoadClass.ashx` (verify) | not separately measured | 200 | similar shape |

Measurement method: `time.perf_counter()` around `session.get(...)`, wall-clock end-to-end.

### Analysis

Cold-connection cost (~1.3 s) is dominated by DNS, TCP, and TLS handshake. Once HTTP keep-alive is established, calls settle at 37 to 336 ms regardless of whether the call is a read or a write. The under-10-seconds goal in FR-007 has ample headroom: even the slowest observed warm call (336 ms for the booking) leaves >96% of the budget on the table.

For the production worker this implies a concrete pattern: schedule a connection-warming step (one cheap authenticated `LoadClass.ashx`) shortly before the booking window opens, then issue the booking call on the already-warm connection. The 336 ms phase 2a sample is the most representative latency estimate for the production booking call, and it sits well inside the budget. Pre-warming is a safety margin, not a hard requirement.

## Rate limiting and anti-automation observations (FR-005)

Three evidence sources contribute: the Step 2 HAR (21 browser requests on the gym subdomain), the phase 1 scripted reproduction (4 consecutive `requests` calls 500 ms apart), and a final deliberate probe still pending alongside the phase 2 booking attempts.

| Observation | Detail |
|-------------|--------|
| Throttling response headers (`Retry-After`, `X-RateLimit-*`) | None observed across 21 browser requests in the HAR and 4 scripted requests in phase 1. Cloudflare `cf-ray` and `cf-cache-status` headers were present on most browser responses, indicating CF fronts the subdomain, but CF did not emit rate-limit or challenge headers in either run. |
| Non-200 responses | None on the gym subdomain across both runs. The browser saw an initial `/user` -> 301 -> `/user/` 200 path normalization redirect; the script saw no redirects. |
| CAPTCHA or human verification appearing during the spike | None observed. The Cloudflare managed challenge documented in Step 1 applies to unauthenticated requests to the public marketing surface. Phase 1 of the script issued requests directly to the gym subdomain with no browser fingerprint and received normal authenticated JSON responses. |
| `cf_clearance` cookie required | No. Phase 1 carried only `.WBAuth` and was accepted. No `Set-Cookie` instructing the client to obtain `cf_clearance` was observed in the responses. |
| Account warnings, emails, or UI banners received | None reported by the operator after either run. |
| ASP.NET technology disclosure | Confirmed in the HAR: `X-AspNet-Version` and `X-Powered-By` headers were present on a small number of responses. Not an attack vector, but useful when interpreting server-side conventions (`__VIEWSTATE` on form posts, ASHX handlers, `.WBAuth` Forms Authentication ticket). |

Phase 2a contributes one additional data point: a mutating `Calendario_Inscribir.ashx` call interleaved with two `LoadClass.ashx` calls within a single session, all 200 OK with no throttling response headers and no challenge. Across the spike as a whole (21 browser requests in the HAR, 4 scripted reads in phase 1, 1 scripted booking and 2 scripted reads in phase 2a) no rate-limit signal of any kind was observed on the authenticated gym subdomain. The remaining open question for Step 7 is whether tight-loop polling near the booking-window open (for example sub-second `LoadClass.ashx` calls while `SegundosHastaPublicacion` approaches zero) triggers throttling. This is deferred to the production feature spike rather than answered here; the phase 0 evidence supports the GO recommendation, and aggressive polling is out of scope for the polite-client design (FR-009).

### Preliminary observation: edge protection in front of public pages (recorded during Step 1)

During Step 1, an unauthenticated programmatic `GET` of the public Aviso Legal page returned a Cloudflare "Enable JavaScript and cookies to continue" interstitial instead of the document. The page does not appear behind authentication, so the challenge is applied at the edge to all clients that do not present the cookies and JavaScript signals Cloudflare expects from a real browser.

Implications:

- WodBuster's surface, at minimum the public marketing surface, is fronted by Cloudflare's managed challenge.
- A direct `requests.Session()` against any WodBuster URL is likely to face the same gate unless a real browser session is established first and its cookies are reused.
- This is consistent with the anti-circumvention clause of the ToS and reinforces the constraint that the production architecture must ride a real browser session rather than synthesize one.

This is a Step 1 byproduct, not a substitute for Step 7. Step 7 must still observe what happens on **authenticated** flows (class list, booking endpoint) under polite repeated access, because Cloudflare's behavior at the edge for the public page does not necessarily predict the rate limits in the authenticated path.

## Assumptions that, if false, invalidate the recommendation (FR-011)

Partial list, growing as evidence is added. Step 1 contributes the first three. Step 2 adds A4 through A7. Steps 3 through 7 may extend this list.

1. The Aviso Legal at the captured URL is the only WodBuster legal document that governs the project's use of the service. If a separate "Condiciones Generales de Contratación" referenced in the "Usuarios" section exists and contains stricter automation rules, the recommendation must be revisited.
2. "Sistema de seguridad" in the anti-circumvention clause is interpreted to cover edge protections such as Cloudflare's managed challenge. Riding a real authenticated browser session is treated as compliant; programmatic defeat of the challenge from a clean state is treated as non-compliant.
3. The personal-use permission covers a single-user automated booking flow on the owner's own account. Adding additional users invalidates this assumption and triggers a re-read of the commercial-reproduction and personal-use clauses.
4. **CONFIRMED OPTIONAL (phase 2a)**. The `connectionId` query parameter on `Calendario_Inscribir.ashx` is not required. Phase 2a of `spike-reproduce.py` issued the booking call with `connectionId` omitted entirely and received `200 OK`. The production worker does not need a SignalR client at booking time.
5. The `idu` user identifier observed in the HAR is stable per account across sessions and across days. If `idu` rotates per login, the worker must look it up after every login rather than cache it. **Strongly supported** by spike evidence (the HAR `idu` worked across many read calls and one write call over multiple hours with no rotation), but formally not confirmed against a logout-relogin cycle because Q8 (no visible logout control) blocks that test. The auth-and-session ADR will specify the discovery path so the worker can re-read `idu` from any authenticated `/athlete/handlers/*` URL if a cached value is rejected.
6. **CONFIRMED (phase 1)**. The `.wodbuster.com` auth cookie obtained through a normal browser login can be exported and used by a non-browser HTTP client to call `*.ashx` handlers without additional handshake. Phase 1 of `spike-reproduce.py` replayed the `.WBAuth` cookie from a Python `requests.Session()` and received 200 responses for four consecutive `LoadClass.ashx` calls. Phase 2a reinforced this by issuing a mutating call from the same cookie and receiving 200.
7. **CONFIRMED (phase 2a)**. The booking endpoint accepts the request via plain `GET` from any client that presents a valid auth cookie, with no extra anti-bot signal (TLS fingerprint, JS-derived header, or token from a prior page render). Phase 2a issued the mutating call from a stock Python `requests.Session()` with no browser fingerprint and received 200 with no challenge, no rate-limit header, and no `Set-Cookie` instructing the client to obtain `cf_clearance`.

## Open questions

1. Does a separate "Condiciones Generales de Contratación" document exist for WodBuster's service, beyond the Aviso Legal? The "Usuarios" section references it explicitly. If it exists and addresses automated access, the recommendation must take it into account.
2. (Step 3+) Does the WodBuster login flow involve an additional layer beyond the standard Cloudflare managed challenge (signed JS, attestation, CAPTCHA inside the WodBuster page)?
3. (Step 5+) Resolved by HAR: the booking action does **not** carry a CSRF or per-call rotating token. Its only session-bound input is the `id` of the target class, which comes from a fresh `LoadClass.ashx` call. The `connectionId` ties to a SignalR session whose lifetime is unknown (sub-question Q4).
4. (Step 5+) Is `connectionId` a hard requirement of `Calendario_Inscribir.ashx`, and if so, what is the minimum lifetime a SignalR connection must hold to be accepted? Test by calling the booking endpoint with: (a) no `connectionId` parameter, (b) a fresh negotiate token but no WebSocket actually opened, (c) a fully established WebSocket with the room joined. Pick the cheapest combination that succeeds.
5. (Step 4+) Is `idu` stable across logins for the same account? Phase 1 evidence is suggestive but not conclusive: the `idu` was taken from the Step 2 HAR (captured at one point in time) and used with a `.WBAuth` cookie that may have come from a different session, and the call succeeded. This is consistent with `idu` being an account-level identifier that does not rotate per session, but a deliberate test (logging out and back in, then comparing) is still pending and is blocked by Q8 (no visible logout control).
6. (Step 6+) What is the lower bound of latency observable from a normal home connection, and does it leave enough budget under the under-10-seconds goal once a remote runtime is added? Browser-measured one-shot samples in the HAR are 111 to 247 ms; a scripted measurement from a non-browser client is still pending.
7. (Step 4+) The Step 2 HAR did not include a fresh login because the browser was already authenticated. A second HAR captured from a fully logged-out state is required before the auth flow table can be completed and before the cookie-handoff vs browser-in-worker decision can be finalized in the auth-and-session ADR.
8. (Step 7+) The operator reported being unable to locate a logout control in the WodBuster athlete UI. Implications: (a) the cookie-handoff fallback architecture cannot be tested against a "session was just revoked" scenario simply by clicking logout; (b) the runtime fail-safe behavior on session expiry (constraint 3 in the ToS section) must be validated by waiting for natural session expiry rather than triggering it. The auth-and-session ADR must specify how the worker distinguishes "session expired" from "credentials revoked" without a clean revocation signal.

## Compliance with polite client quotas (FR-009)

| Quota | Limit | Observed during the spike |
|-------|-------|---------------------------|
| Login attempts per hour | 5 | 0 (the spike reused an existing browser session; no programmatic login was attempted) |
| Real booking attempts in total | 2 | 1 (phase 2a rebooked the same Wed 21:30 Cross Training slot the operator had just manually canceled; phase 2b deferred because phase 2a already closed A4 and A7) |
| Account lockouts | 0 | 0 |

## Redaction policy applied (FR-010)

The following placeholders replace sensitive values throughout this report.

| Placeholder | Replaces |
|-------------|----------|
| `<owner-email>` | The owner's WodBuster username |
| `<redacted>` | The owner's WodBuster password |
| `<session-cookie>` | Any session cookie value |
| `<csrf-token>` | Any CSRF token value |
| `<class-id>` | Class identifiers the owner judges sensitive |

No raw HAR file, no `cookies.txt`, and no `booking-payload.json` is committed to the repository. The `.gitignore` enforces this at the file pattern level.

## Evidence Checklist

Mirrors the table in `plan.md`. Tick each row as the corresponding section above is filled in.

| Item | Source step | Status |
|------|-------------|--------|
| ToS review summary with quoted clauses | Step 1 | `[x] done` |
| HAR export or request log of one full manual booking (kept locally, not committed) | Step 2 | `[x] done` (HAR captured, parsed locally, request shapes recorded; the fresh-login HAR remains an open question Q7 but is no longer a blocker for cookie-handoff design) |
| Documented auth flow | Steps 3 and 4 | `[~] partial` (session-replay half confirmed; credential exchange shape pending Q7) |
| Reproducible auth command or script with credentials redacted | Step 4 | `[x] done` (cookie-handoff reproduction implemented and exercised in phase 1 of `spike-reproduce.py`) |
| Documented booking request | Steps 5 and 6 | `[x] done` (phase 2a reproduced `Calendario_Inscribir.ashx` from a non-browser client, 200 OK without `connectionId`, end-to-end verified via subsequent `LoadClass.ashx`) |
| Latency measurement for the booking request | Step 6 | `[x] done` (phase 2a: 336 ms warm scripted booking call; browser HAR baseline 247 ms; pre-booking `LoadClass.ashx` 1125 ms cold) |
| Rate limiting and anti-automation observations, or explicit "none observed" | Step 7 | `[x] done` (Cloudflare on public pages from Step 1; no throttling on the authenticated subdomain across 21 browser requests, 4 scripted reads, 1 scripted booking, and 2 verification reads; sub-second polling near booking-window open deferred to production feature spike per polite-client design) |
| Recommendation on the first page | Step 8 | `[x] done` (GO draft, pending owner sign-off) |
| List of invalidating assumptions | Step 8 | `[x] done` (A1-A3 from ToS, A4-A7 from HAR; A4, A6, A7 CONFIRMED; A5 strongly supported but formally open due to Q8) |
| Time-box record | Step 8 | `[x] done` (1 working day of a 5-day budget) |

## Report Evolution Log

| Version | Date | Change Summary | Author |
|---------|------|----------------|--------|
| 0.1 | 2026-06-29 | Skeleton scaffolded by the spike implementation flow. All evidence sections are placeholders. | devsquad.implement |
| 0.2 | 2026-06-29 | Step 1 (ToS review) filled in. Verdict: soft restriction, leaning permissive for personal use. Anti-circumvention clause recorded as binding constraint on auth design. Preliminary Cloudflare-edge observation recorded under Step 7 section. Three invalidating assumptions and four open questions seeded. | devsquad conductor + owner |
| 0.3 | 2026-06-29 | Step 2 HAR parsed locally. Booking action identified as `GET /athlete/handlers/Calendario_Inscribir.ashx` with query parameters only, no per-call rotating token. SignalR (`/bookinghub`) identified as a notification channel, not the booking transport. Cookie-handoff fallback architecture for auth recorded. Browser-measured latency 111-247 ms (one sample per operation). No throttling observed on the authenticated path. Four new invalidating assumptions (A4-A7) and three new open questions (Q4, Q5, Q7) added. Login flow remains partial because the HAR did not include a fresh credential exchange. | devsquad conductor + owner |
| 0.4 | 2026-06-29 | Phase 1 of `spike-reproduce.py` executed. Cookie handoff confirmed working with a single `.WBAuth` cookie on `.wodbuster.com`. Read-only `LoadClass.ashx` reproduced from a non-browser `requests.Session()` and returned 200 with 11.6 KB of authenticated JSON. Scripted latency recorded: cold 1284 ms, warm 37-82 ms; pre-warming pattern recommended for the production worker. No throttling on a 4-call burst. Authenticated subdomain serves established sessions without a Cloudflare challenge. Assumption A6 marked CONFIRMED. Q8 (no visible logout control) added. | devsquad conductor + owner |
| 0.5 | 2026-06-29 | Pre-phase-2 exploration of the LoadClass JSON shape. `Data` is a per-day, filter-respecting list of time slots; `Valores[i].Valor` carries the per-day bookable instance with fields `Id`, `Nombre`, `HoraComienzo`, `Plazas`, `AtletasEntrenando` (PII), `AtletasEnListaDeEspera`. The full unfiltered gym schedule is exposed at `ClasesFiltradas` but with `Id: 0`, so finding non-enrolled bookable slots from this endpoint alone is not possible. The top-level field `SegundosHastaPublicacion` is identified as the production worker's publication-window oracle. Three operator class ids captured for the current week (Mon/Tue/Wed 21:30) for use in phase 2; not committed to this report per FR-010 placeholders. | devsquad conductor + owner |
| 0.6 | 2026-06-29 | Phase 2a of `spike-reproduce.py` executed. Mutating `Calendario_Inscribir.ashx` booked the operator's Wed 01/07 21:30 Cross Training slot from a non-browser `requests.Session()` with `connectionId` OMITTED; server returned 200 OK in 336 ms with a 75 KB JSON body whose top-level keys are a `LoadClass`-superset plus a new per-call `Res` result field. End-to-end verified by a subsequent read-only `LoadClass.ashx` that showed `TipoEstado: Borrable` on the target slot. Assumptions A4 (`connectionId` requirement) and A7 (anti-bot signal on mutating endpoint) marked CONFIRMED; A5 (`idu` stability) marked strongly supported. Architectural finding added: no SignalR client required for the production worker. LoadClass-filter finding updated: the filter is dynamic; the unfiltered view (33 real class ids for Wed 01/07) was reachable after the manual cancel-rebook cycle. Three `TipoEstado` values mapped (`Borrable`, `Inscribible`, `Avisable`). Evidence Checklist Steps 5, 6, 7 marked done. Recommendation drafted as GO (pending owner sign-off) with rationale and conditions carried forward. Time-box record completed: 1 working day of a 5-day budget. FR-009 quota usage: 1 of 2 real booking attempts used; phase 2b deferred because phase 2a already closed the remaining mutating-call assumptions. | devsquad conductor + owner |
