# Configuration Interface

**Status**: Proposed
**Date**: 2026-06-29

## Context

The web UI is the operator's primary configuration surface: scheduler rule CRUD (FR-001 through FR-005), cookie paste-and-validate (FR-020 through FR-024), booking history (FR-033), vacation mode (FR-015), manual ad-hoc booking (FR-017), and alert banners (FR-023, FR-026). The Telegram bot is a complementary read-and-act surface (FR-014, FR-018, FR-027, FR-031).

ADR-0001 commits to a single ASGI process hosting the web UI, the Telegram webhook endpoint, and the APScheduler runtime in one container. This ADR decides how the operator-facing UI is built on top of that ASGI process.

The interaction style required by the spec is form-driven CRUD plus list views and banners. Realtime collaboration, complex client-side state, and offline behavior are out of scope. The user is a single operator on a desktop or phone browser.

## Priorities and Requirements (ordered)

1. **Single ASGI process colocation**. The UI must share the FastAPI process with the Telegram webhook and APScheduler. No second runtime.
2. **No JavaScript build pipeline**. The maintenance cost of a Node toolchain and SPA framework is unjustified at single-user MVP scale.
3. **Partial-update interactivity sufficient for forms, lists, and banners**. Full-page reloads are acceptable but not desirable; targeted DOM swaps cover the interactivity needed.
4. **Pythonic stack** for templates and routes. The operator is the developer and reads Python natively.
5. **Operator-only authentication via federated identity** (FR-028 through FR-030). The framework choice must integrate cleanly with the OAuth flow decided in ADR-0005.

## Options Considered

### Option 1: FastAPI plus Jinja2 server-rendered templates plus HTMX for partial updates

Routes are FastAPI endpoints returning either full HTML pages (Jinja2) on initial load or HTML fragments on HTMX-triggered events. Form submissions post to the same routes; the server returns the updated fragment. No client-side framework, no Node, no bundler. The Telegram webhook is another FastAPI route on the same app. APScheduler is started as part of the FastAPI lifespan.

**Evaluation against priorities**:
- **Single ASGI process colocation**: Meets. Web UI, Telegram webhook, and APScheduler share the same `app` object and process.
- **No JavaScript build pipeline**: Meets. HTMX is one `<script>` tag served as a static asset; no npm, no bundler, no transpilation.
- **Partial-update interactivity**: Meets. HTMX covers form posts that return updated table rows, banner toggles, TTL countdown refresh, and "validate cookie" results without page reloads.
- **Pythonic stack**: Meets. All logic in Python; templates in Jinja2.
- **Federated identity integration**: Meets. The OAuth callback is a FastAPI route; Authlib or a similar library handles the protocol; session cookies are managed by Starlette middleware. Standard.

### Option 2: Single-page application (React or Vite) plus FastAPI JSON backend

The frontend is a React app served as static files, calling FastAPI JSON endpoints. The operator builds the SPA with Vite on each release.

**Evaluation against priorities**:
- **Single ASGI process colocation**: Meets at runtime, but the build pipeline introduces a second toolchain.
- **No JavaScript build pipeline**: Fails. Node, npm, Vite, and a TypeScript or Babel transpilation step become mandatory. This is the largest single source of maintenance drift for a one-person project.
- **Partial-update interactivity**: Meets, but the interactivity actually required is well below what an SPA delivers.
- **Pythonic stack**: Fails. Frontend logic lives in JavaScript or TypeScript.
- **Federated identity integration**: Meets, but requires either a backend-for-frontend pattern or storing tokens in the SPA, each with its own complexity.

### Option 3: Streamlit application

A Streamlit app hosted alongside the FastAPI webhook process, or as a second container.

**Evaluation against priorities**:
- **Single ASGI process colocation**: Fails. Streamlit runs its own server. Co-hosting with FastAPI and APScheduler in one process is not the supported pattern; a second process or container is required.
- **No JavaScript build pipeline**: Meets.
- **Partial-update interactivity**: Partially meets. Streamlit re-runs the script on every interaction, which is fine for small forms but awkward for a banner-and-table layout with persistent state. Authentication flows are not Streamlit's strong suit.
- **Pythonic stack**: Meets.
- **Federated identity integration**: Partially meets. Streamlit's auth story is immature at the time of this ADR; gating each page on an external OAuth identity requires either a reverse-proxy in front (extra component) or community plugins of varying quality.

## Decision

FastAPI plus Jinja2 plus HTMX, server-rendered with partial updates. The same FastAPI application hosts the Telegram webhook endpoint at `/telegram/webhook` and starts APScheduler in its lifespan handler. Operator authentication uses the OAuth flow decided in ADR-0005 (Microsoft personal, GitHub, Google), with the identity provider chosen by the operator at sign-in (FR-028).

This option uniquely meets every priority. The SPA path violates priority 2 and 4. Streamlit violates priority 1 and weakens priority 5.

## Implementation Notes

- Routes are organized by resource (`/rules`, `/cookie`, `/history`, `/alerts`, `/vacation`, `/book-now`, `/telegram/webhook`, `/auth/{provider}`, `/health`).
- The `/health` endpoint is the externally pinged dead-man target referenced in ADR-0006 and is exempt from authentication (FR-028 carves out the sign-in flow and the unauthenticated health probe).
- HTMX is pinned to a specific version and served from the application's static folder. No CDN dependency at runtime.
- Session middleware uses Starlette's `SessionMiddleware` with the session-encryption secret pulled from Key Vault (ADR-0005).
- The operator allow-list (FR-030) is a configuration list checked after the OAuth callback completes. Identities not on the list are denied without leaking which user IDs exist.
- All form submissions are CSRF-protected by an HTMX-friendly token pattern.
- Telegram chat ID registration (FR-031) is a route in the web UI that emits a one-time bind token; the operator pastes it into the bot to complete the binding.

## References

- `docs/features/wodbuster-booking-worker/spec.md` User Stories 5, 6, 7, 8, 9. FR-001 through FR-005, FR-013, FR-015, FR-017, FR-020 through FR-024, FR-028 through FR-031, FR-033.
- `docs/architecture/decisions/0001-hosting-service.md` for the single-ASGI-process commitment.
- `docs/architecture/decisions/0005-secrets-and-identity-access.md` for the federated identity providers and session-secret custody.
- `docs/architecture/decisions/0006-observability-and-heartbeat.md` for the `/health` dead-man endpoint.
