"""Web authentication and session management (User Story 9).

Wires:

- Starlette's ``SessionMiddleware`` reading the encrypted cookie key
  from Key Vault (``session-encryption-secret``).
- An idle-timeout ASGI wrapper enforcing ``session_idle_minutes`` and
  ``session_absolute_hours`` from :class:`Settings`.
- Authlib OAuth clients for Microsoft personal, GitHub, and Google.
- Login / callback / logout routes at ``/auth/{provider}/...``.
- CSRF double-submit protection compatible with HTMX.
- The ``require_session`` FastAPI dependency and its redirect-on-anon
  exception handler.

See ADR-0005 for secret custody and the plan Phase 6 for the surface.
"""

from __future__ import annotations
