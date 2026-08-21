"""Unit tests for email rendering (ADR-0011)."""

from __future__ import annotations

from wodbuster_worker.notifications import email_render

_BOOKING = {
    "kind": "booking_result",
    "terminal_status": "granted",
    "class_type": "CrossFit",
    "outcome_id": 7,
    "target_slot": "2026-08-25T16:30:00+00:00",
}


def test_render_booking_produces_all_parts() -> None:
    content = email_render.render_email(
        _BOOKING, lang="en", gym_name="Antwork", unsubscribe_url="https://x/u"
    )
    assert content is not None
    assert "Antwork" in content.subject
    assert "Booked" in content.text
    assert "Antwork" in content.html
    assert content.html.lstrip().startswith("<!DOCTYPE html>")


def test_html_body_drops_bracketed_gym_but_chip_keeps_it() -> None:
    content = email_render.render_email(_BOOKING, lang="en", gym_name="Antwork")
    assert content is not None
    # Option (a): the "[Antwork]" label the Telegram copy carries is gone
    # from the HTML body, while the plain-text part (no chip) keeps it.
    assert "[Antwork]" not in content.html
    assert "[Antwork]" in content.text


def test_html_includes_hero_image() -> None:
    content = email_render.render_email(_BOOKING, lang="en", gym_name="Antwork")
    assert content is not None
    assert "images.unsplash.com" in content.html


def test_render_includes_unsubscribe_link_when_url_given() -> None:
    content = email_render.render_email(
        {"kind": "cookie_invalid"}, lang="en", gym_name="G", unsubscribe_url="https://x/u"
    )
    assert content is not None
    assert "https://x/u" in content.html
    assert "unsubscribe from these emails" in content.html.lower()


def test_render_omits_unsubscribe_link_without_url() -> None:
    content = email_render.render_email(
        {"kind": "cookie_invalid"}, lang="en", gym_name="G", unsubscribe_url=None
    )
    assert content is not None
    assert "unsubscribe from these emails" not in content.html.lower()


def test_render_is_localized() -> None:
    content = email_render.render_email({"kind": "cookie_invalid"}, lang="es", gym_name="G")
    assert content is not None
    assert "sesión" in content.subject.lower()


def test_render_unknown_kind_returns_none() -> None:
    assert email_render.render_email({"kind": "nope"}, lang="en", gym_name="G") is None


def test_render_account_email_has_no_gym_chip() -> None:
    content = email_render.render_email({"kind": "account_approved"}, lang="en", gym_name="")
    assert content is not None
    assert "approved" in content.subject.lower()
    assert "approved" in content.text.lower()
    # No gym chip markup for account mail (the chip pill is border-radius:999px).
    assert "border-radius:999px" not in content.html


def test_render_account_email_is_localized() -> None:
    content = email_render.render_email({"kind": "account_received"}, lang="es", gym_name="")
    assert content is not None
    assert "solicitud" in content.subject.lower()
