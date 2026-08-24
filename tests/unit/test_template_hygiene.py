"""Structural guards over the Jinja templates.

Both guards exist because of the same defect class: putting code where
text belongs. A confirmation message interpolated into an ``onsubmit``
attribute was silently disabled by a single apostrophe, and the same
construct makes the editor's TypeScript analysis report the Jinja
delimiters as syntax errors. Script blocks are fine, and several
templates have one; what is banned is Jinja reaching an event attribute,
where the browser parses server-rendered text as JavaScript.
"""

from __future__ import annotations

import re
from pathlib import Path

import html5lib

_TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "wodbuster_worker" / "templates"

# Any inline event handler attribute: on<name>="..." or on<name>='...'.
# Anchored on whitespace rather than a word boundary, which would also
# match the tail of a name like ``data-onclick``.
_EVENT_ATTRIBUTE = re.compile(
    r"""(?:^|\s)on[a-z]+\s*=\s*(?P<quote>["'])(?P<body>(?!(?P=quote)).*?)(?P=quote)""",
    re.DOTALL | re.IGNORECASE,
)

# Every Jinja construct, comments included: a comment delimiter derails
# the editor's JavaScript parse exactly like an expression does.
_JINJA_DELIMITERS = ("{{", "{%", "{#")


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
            if any(delimiter in body for delimiter in _JINJA_DELIMITERS):
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


def test_head_survives_parsing_the_template_source() -> None:
    """Whatever sits inside ``<head>`` in the source must parse into it.

    A parser has no idea what Jinja is, so ``{# ... #}`` above a real
    element in the head is character data: it ends the head early and
    every element below it is reparented into an implied body. The page
    renders correctly, which is why the rendered sweep cannot see this,
    but the editor analyses the source and reports each displaced
    element. Use an HTML comment in that region.

    Asserting the whole head rather than one meta keeps this from
    becoming a list of individually discovered symptoms.
    """
    offenders: list[str] = []
    for template in _html_templates():
        source = template.read_text(encoding="utf-8")
        head = re.search(r"<head[^>]*>(?P<body>.*?)</head>", source, re.DOTALL | re.IGNORECASE)
        if head is None:
            continue
        declared = set(re.findall(r"<(meta|title|link|style)\b", head.group("body"), re.IGNORECASE))
        parsed = html5lib.parse(source, treebuilder="etree", namespaceHTMLElements=False)
        parsed_head = parsed.find("head")
        survived = (
            {child.tag for child in parsed_head if isinstance(child.tag, str)}
            if parsed_head is not None
            else set()
        )
        displaced = {tag.lower() for tag in declared} - survived
        if displaced:
            offenders.append(
                f"{template.relative_to(_TEMPLATES)}: {', '.join(sorted(displaced))} "
                "declared in <head> but parsed outside it"
            )
    assert not offenders, "head content displaced by a Jinja construct:\n" + "\n".join(offenders)
