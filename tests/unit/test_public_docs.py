"""Public documentation embeds diagrams as images, and those images are never stale.

GitHub renders Mermaid blocks, but the GitHub Pages site does not, so public documents embed
committed SVG images rendered from a Mermaid source kept beside them in `docs/assets/`.
These tests turn that rule into checks that fail, rather than a convention a reviewer has
to remember.

Every check is a pure function, run once against the repository and once against an
injected case that must fail, so a check that silently stopped working would be noticed.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = REPOSITORY_ROOT / "docs" / "assets"

#: Documents that describe the product and are published on the site.
PUBLIC_DOCUMENTS = ("README.md", "docs/DEVELOPER_GUIDE.md")

#: A fenced Mermaid block, opened with backticks or tildes (CommonMark allows both).
_INLINE_MERMAID = re.compile(r"^\s{0,3}(?:`{3,}|~{3,})\s*mermaid\b", re.MULTILINE | re.IGNORECASE)
_INLINE_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
_REFERENCE_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\]\[(?P<label>[^\]]*)\]")
_REFERENCE_DEFINITION = re.compile(
    r"^\s{0,3}\[(?P<label>[^\]]+)\]:\s*(?P<target>\S+)", re.MULTILINE
)
_HTML_IMAGE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_HTML_ATTRIBUTE = re.compile(
    r"\b(?P<name>alt|src)\s*=\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)')", re.IGNORECASE
)


def _load_script() -> ModuleType:
    path = REPOSITORY_ROOT / "scripts" / "render_diagrams.py"
    spec = importlib.util.spec_from_file_location("render_diagrams", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


render_diagrams = _load_script()


def has_inline_mermaid(markdown: str) -> bool:
    """Whether a document contains a fenced Mermaid block."""
    return _INLINE_MERMAID.search(markdown) is not None


def images(markdown: str) -> tuple[list[tuple[str, str | None]], int]:
    """Return every image as `(alt, target)`, and how many `![` openings were not understood.

    Inline images, reference-style images and HTML `<img>` elements are all recognised. An
    image whose reference has no definition, or an HTML image without `src`, has target
    `None`. Any `![` that matches no recognised form is counted, so a check built on this
    fails closed instead of skipping syntax it does not parse.
    """
    definitions = {
        match["label"].strip().lower(): match["target"]
        for match in _REFERENCE_DEFINITION.finditer(markdown)
    }
    found: list[tuple[str, str | None]] = []
    understood = 0
    for match in _INLINE_IMAGE.finditer(markdown):
        found.append((match["alt"], match["target"]))
        understood += 1
    for match in _REFERENCE_IMAGE.finditer(markdown):
        label = (match["label"] or match["alt"]).strip().lower()
        found.append((match["alt"], definitions.get(label)))
        understood += 1
    for tag in _HTML_IMAGE.finditer(markdown):
        attributes = {
            attribute["name"].lower(): attribute["dq"]
            if attribute["dq"] is not None
            else attribute["sq"]
            for attribute in _HTML_ATTRIBUTE.finditer(tag.group(0))
        }
        found.append((attributes.get("alt", ""), attributes.get("src")))
    return found, markdown.count("![") - understood


def image_problems(markdown: str, base: Path) -> list[str]:
    """Return images without alt text or target, missing files, and unrecognised syntax."""
    found, unrecognised = images(markdown)
    problems: list[str] = []
    for alt, target in found:
        name = target if target is not None else "an image"
        if not alt.strip():
            problems.append(f"{name} has no alt text")
        if target is None:
            problems.append(f"{name} has no target")
        elif "://" not in target and not (base / target).is_file():
            problems.append(f"{target} does not exist")
    if unrecognised:
        problems.append(f"{unrecognised} image(s) in syntax the check does not understand")
    return problems


def diagram_problems(assets: Path) -> list[str]:
    """Return every diagram whose image is missing, unstamped or rendered from older source."""
    sources = {path.stem for path in assets.glob("*.mmd")}
    images = {path.stem for path in assets.glob("*.svg")}
    problems = [f"{name}.mmd has no rendered image" for name in sorted(sources - images)]
    problems += [f"{name}.svg has no Mermaid source" for name in sorted(images - sources)]
    for name in sorted(sources & images):
        expected = render_diagrams.source_digest((assets / f"{name}.mmd").read_bytes())
        recorded = render_diagrams.recorded_digest((assets / f"{name}.svg").read_text("utf-8"))
        if recorded != expected:
            problems.append(f"{name}.svg is stale; run python scripts/render_diagrams.py")
    return problems


def svg_problems(svg: str) -> list[str]:
    """Return constructs that would make an SVG unsafe or unreadable inside `<img>`."""
    markers = {"<foreignobject": "HTML labels", "<script": "a script"}
    lowered = svg.lower()
    return [reason for marker, reason in markers.items() if marker in lowered]


def _read(relative: str) -> str:
    return (REPOSITORY_ROOT / relative).read_text("utf-8")


# Invariant 1: public documents embed no inline Mermaid.


@pytest.mark.parametrize("document", PUBLIC_DOCUMENTS)
def test_public_documents_embed_no_inline_mermaid(document: str) -> None:
    assert not has_inline_mermaid(_read(document))


@pytest.mark.parametrize(
    "markdown",
    ["```mermaid\nflowchart LR\n```\n", "~~~ Mermaid\nflowchart LR\n~~~\n"],
    ids=["backticks", "tildes"],
)
def test_an_inline_mermaid_block_is_reported(markdown: str) -> None:
    assert has_inline_mermaid(markdown)


def test_the_readme_embeds_no_images() -> None:
    found, unrecognised = images(_read("README.md"))

    assert (found, unrecognised) == ([], 0)


@pytest.mark.parametrize(
    "markdown",
    [
        "![Diagram](docs/assets/booking-flow.svg)",
        "![Diagram][flow]\n\n[flow]: docs/assets/booking-flow.svg",
        '<img src="docs/assets/booking-flow.svg" alt="Diagram">',
        "![Diagram]",
    ],
    ids=["inline", "reference", "html", "shortcut"],
)
def test_every_image_syntax_is_seen_by_the_readme_check(markdown: str) -> None:
    found, unrecognised = images(markdown)

    assert found or unrecognised


# Invariant 2: every diagram image matches its current source.


def test_every_diagram_image_is_rendered_from_its_current_source() -> None:
    assert sorted(ASSETS_DIR.glob("*.mmd"))
    assert diagram_problems(ASSETS_DIR) == []


def test_editing_a_source_without_rendering_it_is_reported(tmp_path: Path) -> None:
    source = b"flowchart LR\n  a --> b\n"
    digest = render_diagrams.source_digest(source)
    (tmp_path / "flow.svg").write_text(render_diagrams.stamp("<svg/>", digest), "utf-8")
    (tmp_path / "flow.mmd").write_bytes(source.replace(b"b", b"c"))

    assert diagram_problems(tmp_path) == [
        "flow.svg is stale; run python scripts/render_diagrams.py"
    ]


def test_an_unstamped_image_and_an_image_without_a_source_are_reported(tmp_path: Path) -> None:
    (tmp_path / "flow.mmd").write_text("flowchart LR\n", "utf-8")
    (tmp_path / "flow.svg").write_text("<svg/>", "utf-8")
    (tmp_path / "orphan.svg").write_text("<svg/>", "utf-8")

    assert diagram_problems(tmp_path) == [
        "orphan.svg has no Mermaid source",
        "flow.svg is stale; run python scripts/render_diagrams.py",
    ]


def test_line_endings_do_not_change_a_diagram_digest() -> None:
    source = b"flowchart LR\n  a --> b\n"

    assert render_diagrams.source_digest(source.replace(b"\n", b"\r\n")) == (
        render_diagrams.source_digest(source)
    )


# Invariant 3: images carry no HTML labels and no script.


def test_committed_images_contain_no_html_labels_or_scripts() -> None:
    images = sorted(ASSETS_DIR.glob("*.svg"))

    assert images
    assert {path.name: svg_problems(path.read_text("utf-8")) for path in images} == {
        path.name: [] for path in images
    }


def test_html_labels_and_scripts_in_an_image_are_reported() -> None:
    svg = "<svg><foreignObject><div>x</div></foreignObject><SCRIPT>1</SCRIPT></svg>"

    assert svg_problems(svg) == ["HTML labels", "a script"]


# Invariant 4: embedded images have alt text and exist.


def test_public_images_have_alt_text_and_exist() -> None:
    found = {
        document: image_problems(_read(document), (REPOSITORY_ROOT / document).parent)
        for document in PUBLIC_DOCUMENTS
    }

    assert any(images(_read(document))[0] for document in PUBLIC_DOCUMENTS)
    assert found == {document: [] for document in PUBLIC_DOCUMENTS}


def test_an_image_without_alt_text_or_target_is_reported(tmp_path: Path) -> None:
    markdown = "![](missing.svg)\n![A diagram](https://example.invalid/x.svg)\n"

    assert image_problems(markdown, tmp_path) == [
        "missing.svg has no alt text",
        "missing.svg does not exist",
    ]


def test_reference_and_html_images_are_checked_too(tmp_path: Path) -> None:
    markdown = (
        "![][logo]\n![Badge][undefined]\n<img src='gone.svg'>\n<IMG alt=\"x\">\n![Shortcut]\n\n"
        "[logo]: logo.svg\n"
    )

    assert image_problems(markdown, tmp_path) == [
        "logo.svg has no alt text",
        "logo.svg does not exist",
        "an image has no target",
        "gone.svg has no alt text",
        "gone.svg does not exist",
        "an image has no target",
        "1 image(s) in syntax the check does not understand",
    ]


# Invariant 5: the renderer publishes a complete, stamped image or nothing.


def _fake_cli(svg: str | None) -> object:
    """Stand in for the Mermaid CLI. A failing run writes partial output before failing,
    as a real one can, so writing straight to the final image would be caught."""

    def run(args: Sequence[str], *, check: bool) -> None:
        output = Path(args[list(args).index("--output") + 1])
        if svg is None:
            output.write_text("<svg partial", "utf-8")
            raise subprocess.CalledProcessError(1, list(args))
        output.write_text(svg, "utf-8")

    return run


def test_a_successful_render_replaces_the_image_with_a_stamped_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "flow.mmd"
    source.write_text("flowchart LR\n", "utf-8")
    (tmp_path / "flow.svg").write_text("old", "utf-8")
    monkeypatch.setattr(render_diagrams.subprocess, "run", _fake_cli("<svg/>"))

    render_diagrams.render(source, "npx")

    assert sorted(path.name for path in tmp_path.iterdir()) == ["flow.mmd", "flow.svg"]
    assert diagram_problems(tmp_path) == []


def test_a_failed_render_leaves_the_previous_image_and_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "flow.mmd"
    source.write_text("flowchart LR\n", "utf-8")
    (tmp_path / "flow.svg").write_text("old", "utf-8")
    monkeypatch.setattr(render_diagrams.subprocess, "run", _fake_cli(None))

    with pytest.raises(subprocess.CalledProcessError):
        render_diagrams.render(source, "npx")

    assert sorted(path.name for path in tmp_path.iterdir()) == ["flow.mmd", "flow.svg"]
    assert (tmp_path / "flow.svg").read_text("utf-8") == "old"
