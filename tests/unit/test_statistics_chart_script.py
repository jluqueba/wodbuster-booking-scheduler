"""Structural checks on the chart bootstrap (INV-011, INV-012 support).

The chart script is the one place in this feature where a colour or a
sentence could quietly stop coming from the stylesheet and the catalog.
Neither drift raises anything at runtime: the chart simply renders in a
colour nobody chose, or in English for a Spanish reader. These tests are
what makes either one fail here instead.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "src" / "wodbuster_worker" / "static" / "wb-charts.js"
_STYLE_SOURCES = (
    _ROOT / "src" / "wodbuster_worker" / "static" / "brand.css",
    _ROOT / "src" / "wodbuster_worker" / "templates" / "statistics" / "page.html",
)

_CSS_VAR_CALL = re.compile(r'cssVar\("(--[a-z0-9-]+)"\s*,\s*"(#[0-9a-fA-F]{3,8})"\)')
_HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_CUSTOM_PROPERTY = re.compile(r"(--[a-z0-9-]+)\s*:")


def _script() -> str:
    return _SCRIPT.read_text(encoding="utf-8")


def _declared_properties() -> set[str]:
    declared: set[str] = set()
    for path in _STYLE_SOURCES:
        declared.update(_CUSTOM_PROPERTY.findall(path.read_text(encoding="utf-8")))
    return declared


def test_every_colour_the_script_uses_is_declared_in_the_stylesheet() -> None:
    """INV-011: the palette belongs to the stylesheet.

    A fallback that names a variable nobody declares is a colour chosen
    in JavaScript wearing a stylesheet's clothes, and it survives every
    theme change untouched.
    """
    declared = _declared_properties()
    used = {name for name, _ in _CSS_VAR_CALL.findall(_script())}

    assert used, "expected the script to read its palette from custom properties"
    undeclared = sorted(name for name in used if name not in declared)
    assert undeclared == [], f"read from the script but never declared: {undeclared}"


def test_no_colour_sits_in_the_script_outside_a_stylesheet_fallback() -> None:
    """A literal anywhere else is a second source of truth for the
    palette, and the two diverge silently."""
    script = _script()
    inside_fallbacks = {colour for _, colour in _CSS_VAR_CALL.findall(script)}
    every_colour = set(_HEX_COLOUR.findall(script))

    stray = sorted(every_colour - inside_fallbacks)
    assert stray == [], f"colour values outside a cssVar fallback: {stray}"


def test_the_script_takes_its_sentences_from_the_server() -> None:
    """INV-011: user-visible strings come from the catalog.

    Asserted on the shape of the code rather than on its output: every
    sentence the chart shows is read from the payload the server
    rendered, so a string added in JavaScript has nowhere to live.
    """
    script = _script()

    assert "cfg.strings" in script, "expected chart copy to arrive from the server payload"
    # The catalog is reachable from Python only. A script that built a
    # sentence would have to concatenate its own words.
    assert "gettext" not in script
    assert "innerText" not in script
