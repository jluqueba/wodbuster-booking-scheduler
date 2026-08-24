"""Accessibility invariants asserted over rendered HTML.

Template sources cannot be audited for accessibility: ``{% if %}`` blocks
read as text nodes to any HTML parser, and half the markup only exists
after a branch is taken. What the browser receives is the only artefact
that can be judged, so every check here runs against a response body.

The parser is :mod:`html5lib` because it implements the WHATWG parsing
algorithm, tree-construction errors included. A regular-expression scan
would agree with the markup as written rather than with the DOM the
browser builds, and neither ``html.parser`` nor ``lxml.html`` reproduce
the spec's implied tags and misnesting recovery. When a check here
passes, it passes on the same tree a screen reader would walk.

:func:`audit` returns every problem it finds rather than raising on the
first, so one run reports the whole page instead of one line at a time.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import pairwise
from xml.etree.ElementTree import Element

import html5lib

# Input types that carry no user-visible field: hidden state, or a
# control whose own label is its text (``value``) rather than a
# separate element.
_UNLABELLED_INPUT_TYPES = {"hidden", "submit", "reset", "button"}

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

# Attributes whose value is a space-separated list of element ids.
_ID_LIST_REFERENCES = ("aria-labelledby", "aria-describedby", "aria-controls")


def parse(markup: str) -> Element:
    """Return the ``<html>`` element of the DOM a browser would build."""
    tree = html5lib.parse(markup, treebuilder="etree", namespaceHTMLElements=False)
    assert isinstance(tree, Element)
    return tree


def _elements(root: Element) -> Iterator[Element]:
    """Yield real elements in document order, skipping comments."""
    for node in root.iter():
        if isinstance(node.tag, str):
            yield node


def _parents(root: Element) -> dict[Element, Element]:
    return {child: parent for parent in _elements(root) for child in parent}


def _is_hidden(element: Element) -> bool:
    """Return whether the browser drops ``element`` from the a11y tree.

    A ``<dialog>`` without ``open`` matters here: the confirmation modal
    is included on every page and carries its own heading and buttons.
    The user agent stylesheet gives it ``display: none`` until it is
    shown, so auditing its contents as part of the page would report
    problems no user can encounter.
    """
    return (
        element.get("aria-hidden") == "true"
        or element.get("hidden") is not None
        or (element.tag == "dialog" and element.get("open") is None)
        or element.tag in {"script", "style", "template"}
    )


def _exposed(root: Element) -> Iterator[Element]:
    """Yield elements a screen reader can reach, in document order."""
    if _is_hidden(root):
        return
    yield root
    for child in root:
        if isinstance(child.tag, str):
            yield from _exposed(child)


def _text_content(element: Element) -> str:
    """Return the text a screen reader would read out of ``element``.

    Follows the parts of the accessible-name computation that these
    templates actually rely on: hidden subtrees contribute nothing, and
    an image contributes its ``alt``. Anything richer would be
    re-implementing the spec to audit seven kinds of page.
    """
    if _is_hidden(element):
        return ""
    parts: list[str] = []
    if element.tag == "img":
        parts.append(element.get("alt", ""))
    if element.text:
        parts.append(element.text)
    for child in element:
        if isinstance(child.tag, str):
            parts.append(_text_content(child))
        if child.tail:
            parts.append(child.tail)
    return " ".join(part for part in parts if part).strip()


def _accessible_name(element: Element, by_id: dict[str, Element]) -> str:
    """Return the name announced for ``element``, or ``""`` when it has none."""
    labelledby = element.get("aria-labelledby")
    if labelledby:
        referenced = [by_id[ref] for ref in labelledby.split() if ref in by_id]
        if referenced:
            name = " ".join(_text_content(target) for target in referenced).strip()
            if name:
                return name
    aria_label = (element.get("aria-label") or "").strip()
    if aria_label:
        return aria_label
    content = _text_content(element)
    if content:
        return content
    return (element.get("title") or "").strip()


def _control_name(
    element: Element,
    by_id: dict[str, Element],
    labels_for: dict[str, list[Element]],
    parents: dict[Element, Element],
) -> str:
    """Return the accessible name of a form control.

    A control differs from a button in where its name may come from: an
    explicit ``<label for>``, an ancestor ``<label>``, or a
    ``placeholder`` as the last resort the HTML standard allows.
    """
    labelledby = element.get("aria-labelledby")
    if labelledby:
        name = " ".join(
            _text_content(by_id[ref]) for ref in labelledby.split() if ref in by_id
        ).strip()
        if name:
            return name
    aria_label = (element.get("aria-label") or "").strip()
    if aria_label:
        return aria_label
    element_id = element.get("id")
    if element_id:
        for label in labels_for.get(element_id, ()):
            name = _text_content(label)
            if name:
                return name
    ancestor: Element | None = parents.get(element)
    while ancestor is not None:
        if ancestor.tag == "label":
            name = _text_content(ancestor)
            if name:
                return name
        ancestor = parents.get(ancestor)
    return (element.get("title") or element.get("placeholder") or "").strip()


def _describe(element: Element) -> str:
    """Return a short locator for ``element`` to put in a failure message."""
    keys = ("id", "name", "class", "href", "src", "value")
    attributes = " ".join(
        f'{key}="{element.get(key)}"' for key in keys if element.get(key) is not None
    )
    return f"<{element.tag} {attributes}>".replace("  ", " ").replace(" >", ">")


def audit(markup: str, *, expected_lang: str) -> list[str]:
    """Return every accessibility invariant ``markup`` breaks.

    Each rule below exists because breaking it makes the page unusable
    with a screen reader, not because a linter mentions it.
    """
    root = parse(markup)
    elements = list(_elements(root))
    # Identifiers and label associations are resolved across the whole
    # document: a hidden element still owns its id and still lends its
    # text to an aria-labelledby reference.
    visible = list(_exposed(root))
    parents = _parents(root)
    problems: list[str] = []

    # Duplicate ids break every id-based association at once: `for`,
    # `aria-labelledby` and `aria-describedby` all resolve to the first
    # match, so the second control silently loses its name. Repeating a
    # partial inside a loop is the usual way in.
    seen: dict[str, int] = {}
    for element in elements:
        element_id = element.get("id")
        if element_id:
            seen[element_id] = seen.get(element_id, 0) + 1
    by_id = {element.get("id", ""): element for element in elements if element.get("id")}
    problems += [
        f"duplicate id {dup!r} ({count} elements)" for dup, count in seen.items() if count > 1
    ]

    # A reference to a missing id produces no name and no error: the
    # control looks labelled in the source and is anonymous in the
    # browser. Without this check the two name rules below can be
    # satisfied by a typo.
    labels_for: dict[str, list[Element]] = {}
    for element in elements:
        if element.tag == "label":
            target = element.get("for")
            if target:
                labels_for.setdefault(target, []).append(element)
                if target not in by_id:
                    problems.append(f"label for={target!r} points at no element")
        for attribute in _ID_LIST_REFERENCES:
            value = element.get(attribute)
            if value:
                missing = [ref for ref in value.split() if ref not in by_id]
                if missing:
                    problems.append(
                        f"{_describe(element)} {attribute} points at missing id(s) "
                        f"{', '.join(missing)}"
                    )

    # The document language drives pronunciation and hyphenation. A
    # Spanish page tagged `en` is read out with English phonetics, which
    # is worse than no tag at all.
    lang = (root.get("lang") or "").strip()
    if not lang:
        problems.append("<html> has no lang attribute")
    elif lang != expected_lang:
        problems.append(f"<html lang={lang!r}> but the page was requested as {expected_lang!r}")

    # The title is the first thing announced on load and the only label
    # the page has in tab lists and history.
    title = root.find("./head/title")
    if title is None or not (title.text or "").strip():
        problems.append("<title> is missing or empty")

    for element in visible:
        tag = element.tag

        # An empty header cell leaves its column unnamed, so every data
        # cell under it is announced without context.
        if tag == "th" and not _accessible_name(element, by_id):
            problems.append(f"{_describe(element)} is an empty header cell")

        # Without alt, assistive technology falls back to announcing the
        # file name. alt="" is the correct, explicit way to say
        # "decorative".
        if tag == "img" and element.get("alt") is None:
            problems.append(f"{_describe(element)} has no alt attribute")

        # A control with no name is announced as its bare role ("edit
        # text"), which gives no clue what to type.
        is_named_control = tag in {"select", "textarea"} or (
            tag == "input" and element.get("type", "text").lower() not in _UNLABELLED_INPUT_TYPES
        )
        if is_named_control and not _control_name(element, by_id, labels_for, parents):
            problems.append(f"{_describe(element)} is a form control with no accessible name")

        # Same for actionable elements: an emoji-only button reads as
        # "button", and a link with only a decorative image reads as its
        # href.
        is_actionable = tag in {"button", "summary"} or (tag == "a" and element.get("href"))
        if is_actionable and not _accessible_name(element, by_id):
            problems.append(f"{_describe(element)} has no accessible name")

    # Headings are the table of contents screen-reader users navigate
    # by. A jump from h1 to h3 tells them a section is missing.
    levels = [_HEADINGS[e.tag] for e in visible if e.tag in _HEADINGS]
    if levels:
        if levels[0] != 1:
            problems.append(f"first heading on the page is h{levels[0]}, not h1")
        for previous, current in pairwise(levels):
            if current > previous + 1:
                problems.append(f"heading level jumps from h{previous} to h{current}")

    return problems
