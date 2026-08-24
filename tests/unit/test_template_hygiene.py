"""Structural guards over the Jinja templates.

Both guards exist because of the same defect class: putting code where
text belongs. A confirmation message interpolated into an ``onsubmit``
attribute was silently disabled by a single apostrophe, and the same
construct makes the editor's TypeScript analysis report the Jinja
delimiters as syntax errors. Templates therefore carry no JavaScript.
"""

from __future__ import annotations

import re
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "wodbuster_worker" / "templates"

# Any inline event handler attribute: on<name>="..." or on<name>='...'.
_EVENT_ATTRIBUTE = re.compile(
    r"""\bon[a-z]+\s*=\s*(?P<quote>["'])(?P<body>(?!(?P=quote)).*?)(?P=quote)""",
    re.DOTALL | re.IGNORECASE,
)


def _html_templates() -> list[Path]:
    return sorted(_TEMPLATES.rglob("*.html"))


def test_no_jinja_inside_event_attributes() -> None:
    """No inline event handler may interpolate Jinja.

    Jinja inside an event attribute means the browser parses a
    server-rendered string as JavaScript, which is how an apostrophe in
    a translation once disabled a confirmation. Pass the value through a
    ``data-`` attribute and read it from a listener instead.
    """
    offenders: list[str] = []
    for template in _html_templates():
        source = template.read_text(encoding="utf-8")
        for match in _EVENT_ATTRIBUTE.finditer(source):
            body = match.group("body")
            if "{{" in body or "{%" in body:
                line = source.count("\n", 0, match.start()) + 1
                offenders.append(f"{template.relative_to(_TEMPLATES)}:{line}: {match.group(0)}")
    assert not offenders, "Jinja inside an event attribute:\n" + "\n".join(offenders)


def test_confirmation_forms_use_the_data_attribute() -> None:
    """The confirmation modal is only reachable through ``data-wb-confirm``."""
    for template in _html_templates():
        source = template.read_text(encoding="utf-8")
        assert "wbConfirm(" not in source, (
            f"{template.relative_to(_TEMPLATES)} still calls wbConfirm"
        )
