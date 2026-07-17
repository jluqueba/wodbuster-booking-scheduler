"""Two-language i18n helper (English + Spanish).

Language is selected purely from the URL: paths under ``/es/...``
render Spanish, everything else renders English. A middleware
strips the ``/es`` prefix from the ASGI ``scope["path"]`` before
downstream routing sees it, so route handlers stay language
agnostic. When an anonymous visitor lands on ``/`` and their
browser prefers Spanish (``Accept-Language: es*``), the middleware
issues a one-shot 302 to ``/es`` so the URL matches the language.

Deliberately not Babel: single-user app, two locales, no plural
forms or ICU MessageFormat. The catalogs live in
:mod:`wodbuster_worker.i18n.catalog` as flat Python dicts.

Usage:

- Route handler: ``t("dashboard.title.hero")`` — reads the
  contextvar set by :class:`LanguageMiddleware`.
- Template: ``{{ t("nav.rules") }}`` and ``{{ lang_url("/rules")
  }}`` — the same helpers registered as Jinja globals by
  :func:`register_jinja_globals`.
- Python redirects: ``RedirectResponse(url=lang_url("/rules"))``
  keeps the visitor on the same language branch.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Any

from .catalog import CATALOGS, DEFAULT_LANG, SUPPORTED_LANGUAGES

_current_language: ContextVar[str] = ContextVar("wb_language", default=DEFAULT_LANG)


def set_language(lang: str) -> str:
    """Bind ``lang`` to the current request's context.

    Unknown or empty values collapse to :data:`DEFAULT_LANG` so a
    malformed URL prefix can never break the render pipeline.
    Returns the normalised language actually set (useful for tests).
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


def lang_prefix() -> str:
    """Return the URL prefix for the current language (``""`` or ``/es``).

    Kept in sync with :data:`SUPPORTED_LANGUAGES` and the
    stripping logic in :class:`LanguageMiddleware`. English is the
    "prefix-free" default; every other supported language sits
    under ``/<lang>``.
    """
    lang = _current_language.get()
    if lang == DEFAULT_LANG:
        return ""
    return f"/{lang}"


def lang_url(path: str) -> str:
    """Prepend the current language prefix to an internal ``path``.

    Absolute paths (starting with ``/``) get the prefix; anything
    else (external URLs, fragments, empty) is returned unchanged so
    the helper is safe to sprinkle everywhere.
    """
    if not path or not path.startswith("/"):
        return path
    prefix = lang_prefix()
    if not prefix:
        return path
    # Avoid double-prefixing when the caller already handed us a
    # language-scoped path (defensive; the sweep should never do it).
    if path == prefix or path.startswith(f"{prefix}/"):
        return path
    return f"{prefix}{path}"


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

    Templates then call ``{{ t("nav.rules") }}``,
    ``{{ current_language() }}``, and ``{{ lang_url("/rules") }}``
    without extra plumbing per render.
    """
    env.globals["t"] = t
    env.globals["current_language"] = get_language
    env.globals["lang_url"] = lang_url
    env.globals["lang_prefix"] = lang_prefix
    env.globals["supported_languages"] = list(SUPPORTED_LANGUAGES)
    # Gym timezone (WORKER_TIMEZONE) surfaced to templates so
    # base.html can emit the <meta name="wb-timezone"> that the
    # client-side date formatter pins every instant to. Read from the
    # environment directly to avoid importing the scheduler here.
    env.globals["worker_timezone"] = os.environ.get("WORKER_TIMEZONE", "Europe/Madrid")


__all__ = [
    "DEFAULT_LANG",
    "SUPPORTED_LANGUAGES",
    "get_language",
    "lang_prefix",
    "lang_url",
    "normalize_language",
    "register_jinja_globals",
    "set_language",
    "t",
]
