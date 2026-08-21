"""Render sample notification emails to HTML for visual iteration (ADR-0011).

Run from the repo root:

    .venv\\Scripts\\python scripts/preview_email.py

Writes the rendered emails to a folder in the OS temp directory (outside the
workspace, so VS Code never lints these throwaway HTML files) and opens it.
Use this to iterate on ``templates/email/notification.html.jinja``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from wodbuster_worker.notifications import email_render

_OUT = Path(tempfile.gettempdir()) / "wodbuster-email-preview"
_UNSUB = "https://wodbuster-booking-scheduler.jluqueba.es/unsubscribe?t=sample-token"
_GYM = "Antwork Training Center"

# (filename stem, language, outbox payload)
_SAMPLES: list[tuple[str, str, dict[str, object]]] = [
    (
        "booking-granted-en",
        "en",
        {
            "kind": "booking_result",
            "terminal_status": "granted",
            "class_type": "CrossFit",
            "outcome_id": 4213,
            "target_slot": "2026-08-25T16:30:00+00:00",
        },
    ),
    (
        "booking-granted-es",
        "es",
        {
            "kind": "booking_result",
            "terminal_status": "granted",
            "class_type": "CrossFit",
            "outcome_id": 4213,
            "target_slot": "2026-08-25T16:30:00+00:00",
        },
    ),
    (
        "booking-full-en",
        "en",
        {
            "kind": "booking_result",
            "terminal_status": "full",
            "class_type": "Halterofilia",
            "outcome_id": 4218,
            "target_slot": "2026-08-26T18:00:00+00:00",
        },
    ),
    (
        "cookie-expiring-es",
        "es",
        {"kind": "cookie_expiring", "next_window_at": "2026-08-22T20:40:00+00:00"},
    ),
    (
        "anomaly-en",
        "en",
        {
            "kind": "heartbeat_anomaly",
            "missed": [{"target_class": "WOD", "target_slot": "2026-08-21T18:00:00+00:00"}],
        },
    ),
]


def main() -> None:
    _OUT.mkdir(exist_ok=True)
    for stem, lang, payload in _SAMPLES:
        content = email_render.render_email(
            payload, lang=lang, gym_name=_GYM, unsubscribe_url=_UNSUB
        )
        if content is None:
            print(f"{stem}: (unrenderable)")
            continue
        path = _OUT / f"email-{stem}.html"
        path.write_text(content.html, encoding="utf-8")
        print(f"{stem}: {content.subject}  ->  {path}")
    # Open the folder so the rendered files are one click away.
    if hasattr(os, "startfile"):
        os.startfile(_OUT)  # type: ignore[attr-defined]  # Windows only
    print(f"\nPreviews in: {_OUT}")


if __name__ == "__main__":
    main()
