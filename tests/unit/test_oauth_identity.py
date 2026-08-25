"""Provider-supplied identity fields are untrusted input (SEC-006).

Nothing between the OAuth provider and Postgres validated the length of
a name, an email or a subject. The columns are bounded, so an oversized
value used to surface as a failed insert: a sign-up that answered 500
instead of showing the pending page.

Names and emails are capped or dropped here. Subjects are refused
instead, because truncating an identity key would let two subjects
sharing a prefix collapse onto one row.
"""

from __future__ import annotations

import pytest

from wodbuster_worker.auth.oauth import (
    MAX_DISPLAY_NAME_LENGTH,
    MAX_EMAIL_LENGTH,
    MAX_SUBJECT_LENGTH,
    extract_email,
    extract_identity,
)

PROVIDERS = ["microsoft", "google"]


def _user_info(provider: str, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"sub": "subject-123", "name": "Alice", "email": "alice@example.com"}
    if provider == "github":
        base = {"id": 4242, "login": "alice", "email": "alice@example.com"}
    return {**base, **overrides}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_display_name_is_capped_to_the_column_width(provider: str) -> None:
    _, _, name = extract_identity(provider, _user_info(provider, name="A" * 5_000))

    assert len(name) == MAX_DISPLAY_NAME_LENGTH


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_name_within_the_limit_is_untouched(provider: str) -> None:
    _, _, name = extract_identity(provider, _user_info(provider, name="Alice Example"))

    assert name == "Alice Example"


def test_github_login_is_capped_too() -> None:
    """The GitHub branch reads a different field, so it needs its own case."""
    _, _, name = extract_identity("github", _user_info("github", login="g" * 5_000))

    assert len(name) == MAX_DISPLAY_NAME_LENGTH


@pytest.mark.parametrize("provider", PROVIDERS)
def test_an_oversized_subject_is_refused_not_truncated(provider: str) -> None:
    """Truncation would map two distinct subjects onto one identity row."""
    info = _user_info(provider, sub="s" * (MAX_SUBJECT_LENGTH + 1))

    with pytest.raises(ValueError, match="subject exceeds"):
        extract_identity(provider, info)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_subject_at_the_limit_is_accepted(provider: str) -> None:
    subject = "s" * MAX_SUBJECT_LENGTH

    _, extracted, _ = extract_identity(provider, _user_info(provider, sub=subject))

    assert extracted == subject


def test_an_oversized_email_is_dropped() -> None:
    """Half an address is not an address; the profile degrades to no email."""
    local = "e" * MAX_EMAIL_LENGTH

    assert extract_email({"email": f"{local}@example.com"}) is None


def test_an_email_within_the_limit_is_normalised() -> None:
    assert extract_email({"email": "  Alice@Example.COM "}) == "alice@example.com"
