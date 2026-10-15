"""The public documentation site publishes exactly what it should.

GitHub Pages builds `main` with Jekyll, which only offers an exclude list: anything not
excluded is published. These tests turn that into a fail-closed rule, so a new directory or
an internal document cannot reach the public site by omission, and so text that Jekyll
would evaluate as a template cannot break the build or vanish from a page.

Each check is a pure function, run once against the repository and once against an
injected case that must fail, so a check that silently stopped working would be noticed.
"""

from __future__ import annotations

import posixpath
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SITE_CONFIG = REPOSITORY_ROOT / "_config.yml"

#: Top-level entries published on purpose. Everything else must be excluded in `_config.yml`
#: or be one of the hidden entries below.
TOP_LEVEL_PUBLISHED = frozenset(
    {"README.md", "CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "SECURITY.md", "LICENSE", "docs"}
)

#: Entries directly under `docs/` published on purpose.
DOCS_PUBLISHED = frozenset({"DEVELOPER_GUIDE.md", "assets"})

#: Top-level dot and underscore entries known to be inert. Jekyll skips such names, but not
#: all of them are harmless: a root `.nojekyll` turns Jekyll off, so nothing is excluded and
#: the whole tree is published, and directories such as `_posts` generate pages. A new one
#: is therefore reported until it is classified here, rather than exempted by its prefix.
HIDDEN_KNOWN = frozenset(
    {".agerecipients", ".dockerignore", ".env.example", ".gitattributes", ".github"}
    | {".gitignore", ".hintrc", ".markdownlint.yaml", ".vscode", "_config.yml"}
)

#: Entries listed under `include`: exactly these. Published community files that would
#: otherwise be served as raw text (see `RAW_UNLESS_INCLUDED`).
REQUIRED_INCLUDES = frozenset({"CODE_OF_CONDUCT.md", "CONTRIBUTING.md"})

#: File names, compared without extension and case, that GitHub Pages' optional front matter
#: plugin (jekyll-optional-front-matter 0.3.2, pinned by github-pages 232) refuses to turn
#: into pages. A published Markdown file with one of these names is served as raw text unless
#: it has front matter or is listed under `include`. The root README is the exception: the
#: readme-index plugin renders it as the home page.
RAW_UNLESS_INCLUDED = frozenset(
    {"README", "LICENSE", "LICENCE", "COPYING", "CODE_OF_CONDUCT", "CONTRIBUTING"}
    | {"ISSUE_TEMPLATE", "PULL_REQUEST_TEMPLATE"}
)
_HOME_PAGE = "README.md"

#: Exclude patterns this module evaluates exactly: a plain path, optionally ending in a slash,
#: or `*.<extension>`. Anything else is rejected rather than approximated.
_PATTERN_FORM = re.compile(r"^(?:[\w.-]+(?:/[\w.-]+)*/?|\*\.\w+)$")

#: Liquid output and tag delimiters. GitHub's Jekyll renders Markdown pages through Liquid,
#: so either sequence in a published page breaks the build or silently drops text.
_LIQUID = re.compile(r"\{\{|\{%")

#: Jekyll 3.10's own tests for front matter, copied rather than approximated: a file is read
#: as a page only when its first line is `---`, from column zero (`Utils.has_yaml_header?`),
#: and the block is closed by `---` or `...` at the start of a line
#: (`Document::YAML_FRONT_MATTER_REGEXP`). Requiring both errs towards reporting a file.
_JEKYLL_HEADER = re.compile(r"\A---\s*\r?\n")
_JEKYLL_FRONT_MATTER = re.compile(
    r"\A(---\s*\n.*?\n?)^((---|\.\.\.)\s*$\n?)", re.DOTALL | re.MULTILINE
)


def load_config(path: Path = SITE_CONFIG) -> dict[str, list[str]]:
    """Read the `include` and `exclude` lists from a Jekyll configuration file."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    lists: dict[str, list[str]] = {}
    for key in ("include", "exclude"):
        value = raw.get(key, [])
        assert isinstance(value, list)
        assert all(isinstance(item, str) for item in value)
        lists[key] = [str(item) for item in value]
    return lists


def tracked_paths() -> list[str]:
    """Return every path git tracks, failing rather than skipping if git cannot answer."""
    git = shutil.which("git")
    if git is None:
        pytest.fail("git is required to list the tracked files the site could publish.")
    # Fixed arguments; nothing from outside the test reaches the command line.
    result = subprocess.run(
        [git, "ls-files", "-z"], cwd=REPOSITORY_ROOT, capture_output=True, check=False
    )
    if result.returncode != 0 or not result.stdout:
        pytest.fail("git ls-files returned nothing; the site check cannot run blind.")
    return [path for path in result.stdout.decode("utf-8").split("\0") if path]


def is_excluded(path: str, patterns: Iterable[str]) -> bool:
    """Whether Jekyll's exclude list removes `path` from the site."""
    for pattern in patterns:
        if pattern.startswith("*."):
            if fnmatchcase(path.rsplit("/", 1)[-1], pattern):
                return True
            continue
        base = pattern.rstrip("/")
        if path == base or path.startswith(f"{base}/"):
            return True
    return False


def is_visible(name: str, includes: Iterable[str]) -> bool:
    """Whether Jekyll considers an entry at all: it skips dot and underscore names."""
    return not name.startswith((".", "_")) or name in set(includes)


def unclassified_paths(paths: Iterable[str], config: Mapping[str, list[str]]) -> list[str]:
    """Return entries that would be published without anyone having decided so."""
    excludes, includes = config["exclude"], config["include"]
    problems: set[str] = set()
    for path in paths:
        parts = path.split("/")
        top = parts[0]
        if top in TOP_LEVEL_PUBLISHED:
            if top == "docs" and len(parts) > 1:
                entry = f"docs/{parts[1]}"
                if parts[1] not in DOCS_PUBLISHED and not is_excluded(entry, excludes):
                    problems.add(entry)
            continue
        if top in HIDDEN_KNOWN:
            # Skipped by Jekyll unless forced in; forcing one in is a decision of its own.
            if top in includes and top not in REQUIRED_INCLUDES:
                problems.add(top)
            continue
        if top.startswith((".", "_")) or not is_excluded(top, excludes):
            problems.add(top)
    return sorted(problems)


def published_markdown(paths: Iterable[str], config: Mapping[str, list[str]]) -> list[str]:
    """Return the tracked Markdown files the site publishes."""
    return [
        path
        for path in paths
        if path.endswith(".md")
        and all(is_visible(part, config["include"]) for part in path.split("/"))
        and not is_excluded(path, config["exclude"])
    ]


def liquid_markers(markdown: str) -> list[int]:
    """Return the line numbers that contain a Liquid delimiter."""
    return [number for number, line in enumerate(markdown.splitlines(), 1) if _LIQUID.search(line)]


#: Inline links and images, and reference definitions: the forms the public docs use.
_LINK_TARGET = re.compile(
    r"!?\[[^\]]*\]\((?P<inline>[^)\s]+)|^\s{0,3}\[[^\]]+\]:\s*(?P<reference>\S+)", re.MULTILINE
)


def unpublished_links(
    document: str, markdown: str, paths: Iterable[str], config: Mapping[str, list[str]]
) -> list[str]:
    """Return relative link targets in a published document that the site does not publish.

    A link into a path Jekyll skips or excludes, such as `.github/`, works on GitHub but is
    a 404 on the site; link to the file on GitHub instead.
    """
    tracked = list(paths)
    broken: list[str] = []
    for match in _LINK_TARGET.finditer(markdown):
        target = match["inline"] or match["reference"]
        if "://" in target or target.startswith(("#", "mailto:")):
            continue
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(document), target.split("#", 1)[0])
        )
        exists = any(path == resolved or path.startswith(f"{resolved}/") for path in tracked)
        published = (
            exists
            and not resolved.startswith("../")
            and all(is_visible(part, config["include"]) for part in resolved.split("/"))
            and not is_excluded(resolved, config["exclude"])
        )
        if not published:
            broken.append(target)
    return broken


def served_as_raw_text(
    documents: Iterable[str], includes: Iterable[str], has_front_matter: Callable[[str], bool]
) -> list[str]:
    """Return published Markdown files GitHub Pages would serve unrendered."""
    listed = set(includes)
    return [
        path
        for path in documents
        if path != _HOME_PAGE
        and PurePosixPath(path).stem.upper() in RAW_UNLESS_INCLUDED
        and path not in listed
        and not has_front_matter(path)
    ]


def starts_with_front_matter(markdown: str) -> bool:
    """Whether a document opens with a front matter block, which makes Jekyll render it."""
    return bool(_JEKYLL_HEADER.match(markdown) and _JEKYLL_FRONT_MATTER.match(markdown))


def file_has_front_matter(path: str) -> bool:
    return starts_with_front_matter((REPOSITORY_ROOT / path).read_text("utf-8"))


# Invariant 1: nothing is published by omission.


def test_every_tracked_path_is_published_on_purpose_or_excluded() -> None:
    assert unclassified_paths(tracked_paths(), load_config()) == []


@pytest.mark.parametrize(("returncode", "stdout"), [(128, b""), (0, b"")], ids=["error", "empty"])
def test_the_check_fails_rather_than_skips_when_git_cannot_list_files(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: bytes
) -> None:
    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, returncode, stdout, b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Catch any outcome, then require a failure: a skip would also leave this block, and a
    # check that skips when it cannot read its input would report success.
    with pytest.raises(BaseException) as outcome:
        tracked_paths()

    assert isinstance(outcome.value, pytest.fail.Exception)


def test_a_new_top_level_directory_is_reported_until_classified() -> None:
    paths = [*tracked_paths(), "newtool/run.sh"]

    assert unclassified_paths(paths, load_config()) == ["newtool"]


def test_a_new_entry_under_docs_is_reported_until_classified() -> None:
    paths = [*tracked_paths(), "docs/drafts/idea.md"]

    assert unclassified_paths(paths, load_config()) == ["docs/drafts"]


# Invariant 2: internal documents never reach the site.


def test_internal_documents_are_excluded_from_the_site() -> None:
    config = load_config()

    assert is_excluded("docs/internal/adr/README.md", config["exclude"])
    assert all(
        not path.startswith("docs/internal/")
        for path in published_markdown(tracked_paths(), config)
    )


def test_dropping_the_internal_exclusion_is_reported() -> None:
    config = load_config()
    config["exclude"] = [item for item in config["exclude"] if item != "docs/internal/"]

    assert "docs/internal" in unclassified_paths(tracked_paths(), config)


@pytest.mark.parametrize(
    "path",
    [".nojekyll", "_posts/2026-01-01-note.md", "_includes/head.html", ".well-known/x"],
)
def test_an_unknown_hidden_entry_is_reported_whatever_its_prefix(path: str) -> None:
    paths = [*tracked_paths(), path]

    assert unclassified_paths(paths, load_config()) == [path.split("/")[0]]


# Invariant 3: exactly the required entries are forced into the site.


def test_exactly_the_required_entries_are_included() -> None:
    assert set(load_config()["include"]) == REQUIRED_INCLUDES


def test_including_a_dotfile_outside_the_required_list_is_reported() -> None:
    config = load_config()
    config["include"] = [*config["include"], ".agerecipients"]

    assert ".agerecipients" in unclassified_paths(tracked_paths(), config)


# Invariant 4: the exclude list uses only forms this module evaluates exactly.


def test_exclude_patterns_use_only_forms_the_check_evaluates_exactly() -> None:
    assert [p for p in load_config()["exclude"] if not _PATTERN_FORM.match(p)] == []


@pytest.mark.parametrize("pattern", ["docs/[a-z]*", "**/*.py", "src/*/"])
def test_glob_forms_the_check_cannot_evaluate_are_rejected(pattern: str) -> None:
    assert _PATTERN_FORM.match(pattern) is None


# Invariant 5: published Markdown contains nothing Jekyll would evaluate as Liquid.


def test_published_markdown_contains_no_liquid_delimiters() -> None:
    config = load_config()
    documents = published_markdown(tracked_paths(), config)
    found = {
        path: liquid_markers((REPOSITORY_ROOT / path).read_text("utf-8")) for path in documents
    }

    assert "README.md" in found
    assert found == {path: [] for path in documents}


def test_liquid_delimiters_are_reported() -> None:
    markdown = "Plain text.\nThe gate reads ${{ toJSON(needs) }}.\n{% raw %}\n"

    assert liquid_markers(markdown) == [2, 3]


# Invariant 5b: every relative link in published Markdown leads to a published page or file.


def test_published_markdown_links_only_to_published_paths() -> None:
    config = load_config()
    paths = tracked_paths()
    documents = published_markdown(paths, config)
    found = {
        path: unpublished_links(path, (REPOSITORY_ROOT / path).read_text("utf-8"), paths, config)
        for path in documents
    }

    assert "CONTRIBUTING.md" in found
    assert found == {path: [] for path in documents}


def test_links_into_skipped_excluded_or_missing_paths_are_reported() -> None:
    markdown = (
        "[Guidelines](.github/docs/coding-guidelines.md)\n"
        "[Code](../src/wodbuster_worker/app.py)\n"
        "[Gone](missing.md#top)\n"
        "[Guide](DEVELOPER_GUIDE.md#1-project-architecture)\n"
        "![Diagram](assets/booking-flow.svg)\n"
        "[Contributing][c]\n\n[c]: ../CONTRIBUTING.md\n"
        "[Site](https://example.invalid/x) [Top](#top)\n"
    )

    assert unpublished_links("docs/page.md", markdown, tracked_paths(), load_config()) == [
        ".github/docs/coding-guidelines.md",
        "../src/wodbuster_worker/app.py",
        "missing.md#top",
    ]


# Invariant 6: every published Markdown page is rendered, none is served as raw text.


def test_no_published_markdown_is_served_as_raw_text() -> None:
    config = load_config()
    documents = published_markdown(tracked_paths(), config)

    assert {"CONTRIBUTING.md", "CODE_OF_CONDUCT.md"} <= set(documents)
    assert served_as_raw_text(documents, config["include"], file_has_front_matter) == []


@pytest.mark.parametrize(
    "path", ["CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "docs/contributing.md", "docs/README.md"]
)
def test_a_community_file_name_without_include_or_front_matter_is_reported(path: str) -> None:
    includes = [item for item in load_config()["include"] if item != path]

    assert served_as_raw_text([path], includes, lambda _: False) == [path]


def test_front_matter_is_an_accepted_alternative_to_include() -> None:
    assert served_as_raw_text(["docs/contributing.md"], [], lambda _: True) == []


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("---\n---\n# Title\n", True),
        ("---\ntitle: Contributing\n---\n# Title\n", True),
        ("---  \r\ntitle: Contributing\r\n...\r\n# Title\r\n", True),
        ("  ---\n---\n# Title\n", False),
        ("---\ntitle: Contributing\n  ---\n# Title\n", False),
        ("# Title\n\n---\n\nText\n---\n", False),
        ("---\ntitle: never closed\n# Title\n", False),
        ("", False),
    ],
    ids=[
        "empty-block",
        "with-title",
        "crlf-dots-trailing-space",
        "indented-opening",
        "indented-closing",
        "rule-later",
        "unclosed",
        "empty-file",
    ],
)
def test_front_matter_is_recognised_only_at_the_start_of_a_document(
    markdown: str, expected: bool
) -> None:
    assert starts_with_front_matter(markdown) is expected


def test_the_home_page_and_ordinary_names_are_not_reported() -> None:
    documents = ["README.md", "SECURITY.md", "docs/DEVELOPER_GUIDE.md"]

    assert served_as_raw_text(documents, [], lambda _: False) == []
