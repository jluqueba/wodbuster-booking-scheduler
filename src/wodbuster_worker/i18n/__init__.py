"""Two-language i18n helper (English + Spanish).

Kept intentionally small: a ``ContextVar`` for the request-scoped
language, a :func:`t` lookup that formats with keyword arguments,
and a middleware that reads the language off the session cookie
(or falls back to ``Accept-Language`` / :data:`DEFAULT_LANG`).

Deliberately not Babel: single-user app, two locales, no plural
forms or ICU MessageFormat. The catalogs live in
:mod:`wodbuster_worker.i18n.catalog` as flat Python dicts.

Usage:

- Route handler: ``t("dashboard.title.hero")`` — reads the
  contextvar set by :class:`LanguageMiddleware`.
- Template: ``{{ t("nav.rules") }}`` — the same helper registered
  as a Jinja global by :func:`register_jinja_globals`.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from .catalog import CATALOGS, DEFAULT_LANG, SUPPORTED_LANGUAGES

_current_language: ContextVar[str] = ContextVar("wb_language", default=DEFAULT_LANG)


def set_language(lang: str) -> str:
    """Bind ``lang`` to the current request's context.

    Unknown or empty values collapse to :data:`DEFAULT_LANG` so a
    malformed session cookie can never break the render pipeline.
    Returns the normalised language actually set (useful for tests
    and for writing the value back to the session).
    """
    normalised = normalize_language(lang)
    _current_language.set(normalised)
    return normalised


def get_language() -> str:
    """Return the language bound to the current context."""
    return _current_language.get()


def normalize_language(lang: str | None) -> str:
    """Return a supported language code or the default.

    Accepts a bare code (``"es"``), an ``Accept-Language``-style
    fragment (``"es-ES"``), or an empty / None value. Anything not
    on :data:`SUPPORTED_LANGUAGES` collapses to
    :data:`DEFAULT_LANG`.
    """
    if not lang:
        return DEFAULT_LANG
    primary = lang.strip().lower().split("-")[0].split("_")[0]
    if primary in SUPPORTED_LANGUAGES:
        return primary
    return DEFAULT_LANG


def t(key: str, **format_args: Any) -> str:
    """Return the localised string for ``key`` in the current language.

    Fallback chain: current language → :data:`DEFAULT_LANG` → the
    literal key. A missing ``{placeholder}`` in ``format_args``
    swallows the ``KeyError`` and returns the raw template so a
    typo cannot 500 the render.
    """
    lang = _current_language.get()
    catalog = CATALOGS.get(lang) or CATALOGS[DEFAULT_LANG]
    value = catalog.get(key)
    if value is None:
        value = CATALOGS[DEFAULT_LANG].get(key, key)
    if format_args:
        try:
            return value.format(**format_args)
        except (KeyError, IndexError):
            return value
    return value


def register_jinja_globals(env: Any) -> None:
    """Attach the i18n helpers to a Jinja2 environment.

    Templates then call ``{{ t("nav.rules") }}`` and
    ``{{ current_language() }}`` without extra plumbing per
    render.
    """
    env.globals["t"] = t
    env.globals["current_language"] = get_language
    env.globals["supported_languages"] = list(SUPPORTED_LANGUAGES)


__all__ = [
    "DEFAULT_LANG",
    "SUPPORTED_LANGUAGES",
    "get_language",
    "normalize_language",
    "register_jinja_globals",
    "set_language",
    "t",
]
