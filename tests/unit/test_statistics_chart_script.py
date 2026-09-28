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

_CSS_VAR_CALL = re.compile(r'cssVar\("(--[a-z0-9-]+)"\s*,\s*"([^"]+)"\)')
_HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
# Functional notations too. The first version of this test looked only
# for hex and missed a literal rgba() in the heatmap, which is exactly
# the drift it exists to catch.
_FUNCTIONAL_COLOUR = re.compile(r"\b(?:rgba?|hsla?|oklch|oklab|color)\(\s*\d")
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
    palette, and the two diverge silently.

    Both hex and functional notations count. Checking only hex is how a
    literal ``rgba()`` survived in the heatmap until review caught it.
    """
    script = _script()
    fallbacks = {colour for _, colour in _CSS_VAR_CALL.findall(script)}

    stray_hex = sorted(set(_HEX_COLOUR.findall(script)) - fallbacks)
    assert stray_hex == [], f"hex colours outside a cssVar fallback: {stray_hex}"

    stray_functional = [
        line.strip()
        for line in script.splitlines()
        if _FUNCTIONAL_COLOUR.search(line) and "cssVar(" not in line
    ]
    assert stray_functional == [], f"functional colours outside a fallback: {stray_functional}"


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


# ---------------------------------------------------------------------------
# Filter script (CC-027, CC-054)
# ---------------------------------------------------------------------------

_FILTERS = _ROOT / "src" / "wodbuster_worker" / "static" / "wb-stats-filters.js"


def _filters() -> str:
    return _FILTERS.read_text(encoding="utf-8")


def test_rendering_charts_destroys_the_previous_instances_first() -> None:
    """CC-027: swapping a section detaches its canvases without telling
    Chart.js. Without the destroy the library keeps instances pointing
    at nodes no longer in the document, and they accumulate one per
    filter click."""
    script = _script()
    body = script[script.index("function render()") :]

    assert "destroyAll()" in body
    assert body.index("destroyAll()") < body.index("builder(canvas")


def test_the_filter_script_redraws_after_swapping_a_section() -> None:
    """A swapped-in canvas is a fresh, empty element. Nothing draws on
    it unless the bootstrap is asked to run again."""
    script = _filters()

    assert "wbCharts.render" in script
    assert "innerHTML" in script


def test_the_swap_keeps_the_live_region_it_updates() -> None:
    """A live region announces only when the element carrying the
    attribute survives the update. Replacing the node itself would
    change the numbers silently for anyone listening."""
    script = _filters()

    assert "host.innerHTML" in script
    assert "replaceWith" not in script


def test_a_failed_fetch_falls_back_to_a_real_navigation() -> None:
    """A filter that silently does nothing is worse than one that costs
    a page load, and the server can always render what was asked for."""
    script = _filters()

    assert "catch" in script
    assert "window.location.assign" in script


def test_the_filter_script_builds_no_copy_and_no_colour() -> None:
    """INV-011 reaches here too: the fragment arrives from the server
    already translated and already styled."""
    script = _filters()

    assert not _HEX_COLOUR.search(script)
    assert not _FUNCTIONAL_COLOUR.search(script)
    assert "innerText" not in script
    assert "textContent =" not in script


def test_the_next_url_is_built_from_the_address_bar_not_the_form() -> None:
    """Only one section is replaced per click, so the hidden inputs in
    the other two still carry the window they had when the page was
    rendered. Building the next URL from a form would undo a change
    made a moment earlier, and the page would look right until a
    reload lost it."""
    script = _filters()

    assert "new URLSearchParams(window.location.search)" in script
    assert "new FormData(form)" not in script


def test_the_forms_are_brought_back_in_step_after_a_swap() -> None:
    """The no-script fallback and the next click both submit whatever
    the hidden inputs say, so they have to match what is on screen."""
    script = _filters()

    assert "syncForms" in script
    assert script.index("function syncForms") < script.index("syncForms(urls.params)")
