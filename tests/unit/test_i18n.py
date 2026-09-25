"""Unit tests for the two-language i18n helper."""

from __future__ import annotations

from string import Formatter

import pytest

from wodbuster_worker.i18n import (
    DEFAULT_LANG,
    SUPPORTED_LANGUAGES,
    get_language,
    lang_prefix,
    lang_url,
    normalize_language,
    set_language,
    t,
)
from wodbuster_worker.i18n.catalog import CATALOGS, EN, ES


def test_default_language_is_english() -> None:
    set_language("en")
    assert get_language() == "en"


def test_set_language_switches_context() -> None:
    set_language("es")
    assert get_language() == "es"
    set_language("en")


def test_normalize_accepts_bare_code() -> None:
    assert normalize_language("es") == "es"
    assert normalize_language("en") == "en"


def test_normalize_accepts_accept_language_fragment() -> None:
    assert normalize_language("es-ES") == "es"
    assert normalize_language("en_US") == "en"


def test_normalize_falls_back_on_unknown() -> None:
    assert normalize_language("fr") == DEFAULT_LANG
    assert normalize_language("") == DEFAULT_LANG
    assert normalize_language(None) == DEFAULT_LANG


def test_t_returns_english_by_default() -> None:
    set_language("en")
    assert t("nav.rules") == EN["nav.rules"]


def test_t_returns_spanish_when_switched() -> None:
    set_language("es")
    assert t("nav.rules") == ES["nav.rules"]
    set_language("en")


def test_t_formats_placeholders() -> None:
    set_language("en")
    assert "42" in t("telegram.chat_id_label", chat_id=42)


def test_t_missing_placeholder_returns_raw_template() -> None:
    set_language("en")
    # Missing kwarg does not raise; returns the template unchanged.
    assert t("telegram.chat_id_label") == EN["telegram.chat_id_label"]


def test_t_falls_back_to_english_when_key_missing_in_es() -> None:
    # Insert an EN-only key at runtime and verify ES falls back.
    EN["__test.only_en"] = "english"
    try:
        set_language("es")
        assert t("__test.only_en") == "english"
    finally:
        EN.pop("__test.only_en", None)
        set_language("en")


def test_t_falls_back_to_literal_key_when_missing_everywhere() -> None:
    set_language("en")
    assert t("__totally.missing.key") == "__totally.missing.key"


@pytest.mark.parametrize("lang", SUPPORTED_LANGUAGES)
def test_catalogs_share_the_same_keys(lang: str) -> None:
    # Every catalog must define the same keys — a missing key would
    # fall back to English at runtime but is still a copy bug.
    diff = set(EN) ^ set(CATALOGS[lang])
    assert diff == set(), f"key drift in {lang}: {sorted(diff)[:10]}"


def test_lang_prefix_empty_for_default_language() -> None:
    set_language("en")
    assert lang_prefix() == ""


def test_lang_prefix_carries_code_for_non_default() -> None:
    set_language("es")
    try:
        assert lang_prefix() == "/es"
    finally:
        set_language("en")


def test_lang_url_prepends_prefix_when_non_default() -> None:
    set_language("es")
    try:
        assert lang_url("/rules") == "/es/rules"
        assert lang_url("/") == "/es/"
    finally:
        set_language("en")


def test_lang_url_leaves_default_language_paths_untouched() -> None:
    set_language("en")
    assert lang_url("/rules") == "/rules"


def test_lang_url_avoids_double_prefixing() -> None:
    set_language("es")
    try:
        assert lang_url("/es/rules") == "/es/rules"
        assert lang_url("/es") == "/es"
    finally:
        set_language("en")


def test_lang_url_returns_non_absolute_paths_unchanged() -> None:
    set_language("es")
    try:
        assert lang_url("") == ""
        assert lang_url("https://example.com/") == "https://example.com/"
        assert lang_url("#anchor") == "#anchor"
    finally:
        set_language("en")


# ---------------------------------------------------------------------------
# Single-day override catalog parity (T-BDO-019, FR-030, SC-007)
# ---------------------------------------------------------------------------

# Every namespace the single-day override feature introduced. Kept as
# prefixes rather than a literal key list so a key added later to any of
# them is covered without editing this test.
_OVERRIDE_PREFIXES: tuple[str, ...] = (
    "override.",
    "flash.override.",
    "banner.booking_fallback.",
    "booking.reason.",
    "chip.source.",
    "tg.booking.override_",
    "tg.booking.fallback_",
)

# Standalone keys that do not sit under a namespace of their own.
_OVERRIDE_KEYS: tuple[str, ...] = ("chip.modified", "chip.skipped_day")


def _override_keys(catalog: dict[str, str]) -> set[str]:
    keys = {key for key in catalog if key.startswith(_OVERRIDE_PREFIXES)}
    keys.update(key for key in _OVERRIDE_KEYS if key in catalog)
    return keys


def test_override_namespaces_are_populated() -> None:
    # Guards the test itself: a renamed namespace would silently turn
    # every assertion below into a comparison of two empty sets.
    for prefix in _OVERRIDE_PREFIXES:
        assert any(key.startswith(prefix) for key in EN), f"no key under {prefix}"
    for key in _OVERRIDE_KEYS:
        assert key in EN


@pytest.mark.parametrize("lang", SUPPORTED_LANGUAGES)
def test_override_keys_exist_in_every_language(lang: str) -> None:
    # A key present in one language only resolves through the English
    # fallback at runtime, so it never raises. FR-030 wants it to fail here.
    diff = _override_keys(EN) ^ _override_keys(CATALOGS[lang])
    assert diff == set(), f"override key drift in {lang}: {sorted(diff)}"


def test_override_spanish_strings_are_translated() -> None:
    # An untranslated copy passes the key-parity check above, so assert on
    # the values too. Emoji-only or placeholder-only strings would be
    # legitimate collisions; the feature has none, so any match is a gap.
    copies = sorted(key for key in _override_keys(EN) if EN[key] == ES[key])
    assert copies == [], f"Spanish left as an English copy: {copies}"


def test_override_placeholders_match_across_languages() -> None:
    # These strings are rendered at send time from the recipient's
    # language (ADR-0008), so a placeholder that only exists in one
    # catalog degrades to the raw template instead of raising.
    for key in sorted(_override_keys(EN)):
        assert _placeholders(EN[key]) == _placeholders(ES[key]), f"placeholder drift in {key}"


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


# ---------------------------------------------------------------------------
# Attendance statistics catalog parity (T-AST-026, FR-036, SC-007)
# ---------------------------------------------------------------------------

# Namespaces the statistics feature introduced. Prefixes rather than a
# literal key list, so a key added later is covered without editing this.
_STATISTICS_PREFIXES: tuple[str, ...] = (
    "statistics.",
    "day.short.",
    "month.",
    "profile.streak_days.",
)

# The strings that exist to stop the reader over-trusting a number. A
# literal translation of any of these loses the warning, so they are
# asserted by name rather than only by namespace.
_QUALIFYING_KEYS: tuple[str, ...] = (
    "statistics.points.estimate.label",
    "statistics.streak.at_least",
    "statistics.points.balance.hint",
    "statistics.legend.uncaptured",
)


def _statistics_keys(catalog: dict[str, str]) -> set[str]:
    return {key for key in catalog if key.startswith(_STATISTICS_PREFIXES)}


def test_statistics_namespaces_are_populated() -> None:
    """Guards the test itself: a renamed namespace would turn every
    assertion below into a comparison of two empty sets."""
    for prefix in _STATISTICS_PREFIXES:
        assert any(key.startswith(prefix) for key in EN), f"no key under {prefix}"


@pytest.mark.parametrize("lang", SUPPORTED_LANGUAGES)
def test_statistics_keys_exist_in_every_language(lang: str) -> None:
    diff = _statistics_keys(EN) ^ _statistics_keys(CATALOGS[lang])
    assert diff == set(), f"statistics key drift in {lang}: {sorted(diff)}"


def test_statistics_placeholders_match_across_languages() -> None:
    """A placeholder present in one catalog only degrades to the raw
    template at runtime instead of raising, so it has to fail here."""
    for key in sorted(_statistics_keys(EN)):
        assert _placeholders(EN[key]) == _placeholders(ES[key]), f"placeholder drift in {key}"


def test_statistics_spanish_strings_are_translated() -> None:
    """An untranslated copy passes the key-parity check above.

    A template with no prose of its own, such as ``{month} {year}``, is
    legitimately identical in both languages, so it is excluded by
    shape rather than by being listed.
    """
    copies = sorted(key for key in _statistics_keys(EN) if EN[key] == ES[key] and _letters(EN[key]))
    assert copies == [], f"Spanish left as an English copy: {copies}"


def test_the_qualifying_strings_exist_in_both_languages() -> None:
    """Each of these exists to prevent over-trust in a figure. Losing
    one in translation loses the warning, not just the wording."""
    for key in _QUALIFYING_KEYS:
        assert key in EN, f"missing qualifier {key}"
        assert key in ES, f"missing qualifier {key}"
        assert EN[key].strip() and ES[key].strip()


def _letters(template: str) -> str:
    """The prose of a template, with placeholders and digits removed."""
    without_fields = "".join(literal for literal, _, _, _ in Formatter().parse(template) if literal)
    return "".join(ch for ch in without_fields if ch.isalpha())
