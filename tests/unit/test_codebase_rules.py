"""Repository-wide rules that a reviewer should never have to check by hand.

Each rule is a pure function, run once against the repository and once against an injected
case that must fail, so a rule that silently stopped working would be noticed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"

#: The only accepted reference: `owner/repo[/path]@<full lowercase commit SHA>`.
_PINNED_REF = re.compile(r"[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}")
#: Actions inside this repository are reviewed with it, so they need no pin.
_LOCAL_REF = re.compile(r"\./\S+")
#: The full release a pin corresponds to, which Dependabot reads and updates.
_RELEASE_COMMENT = re.compile(r"#\s+v\d+\.\d+\.\d+\s*$")


def unpinned_actions(text: str) -> list[str]:
    """Return every `uses` reference in a workflow that is not pinned with its release.

    The workflow is read as YAML nodes rather than as lines, so quoting a key, using a flow
    mapping or putting `uses:` text inside a `run` block cannot hide an action or fake one.
    Every `uses` key anywhere in the document is checked, which errs towards reporting. The
    release comment is read from the source line of the value itself, because YAML parsing
    drops comments.
    """
    lines = text.splitlines()
    problems: list[str] = []

    def walk(node: yaml.Node) -> None:
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if isinstance(key, yaml.ScalarNode) and key.value == "uses":
                    check(value)
                walk(value)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                walk(item)

    def check(value: yaml.Node) -> None:
        if not isinstance(value, yaml.ScalarNode):
            problems.append(f"line {value.start_mark.line + 1}: `uses` is not a string")
            return
        reference = value.value
        if _LOCAL_REF.fullmatch(reference):
            return
        rest_of_line = lines[value.end_mark.line][value.end_mark.column :]
        if not _PINNED_REF.fullmatch(reference) or not _RELEASE_COMMENT.search(rest_of_line):
            problems.append(f"line {value.start_mark.line + 1}: {reference}")

    root = yaml.compose(text)
    if root is not None:
        walk(root)
    return problems


def workflow_files() -> list[Path]:
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert files, "no workflow found; the pinning rule would pass on nothing"
    return files


def workflow(*steps: str) -> str:
    """Build a one-job workflow from step lines, indented as list items."""
    body = "\n".join(f"      {step}" for step in steps)
    return f"on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n{body}\n"


PINNED = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"


# Rule: every action a workflow runs is pinned to a full commit SHA (ADR-0016).


@pytest.mark.parametrize("path", workflow_files(), ids=lambda path: path.name)
def test_every_action_is_pinned_to_a_commit_sha(path: Path) -> None:
    text = path.read_text("utf-8")

    assert "uses" in text
    assert unpinned_actions(text) == []


@pytest.mark.parametrize(
    "step",
    [
        "- uses: actions/checkout@v7 # v7.0.1",
        "- uses: astral-sh/setup-uv@v10.2.0",
        "- uses: actions/checkout@main # v7.0.1",
        f"- uses: {PINNED}",
        f"- uses: {PINNED[:-1]} # v7.0.1",
        f"- uses: {PINNED.upper()} # v7.0.1",
        f"- uses: {PINNED} # v7",
        f"- uses: {PINNED} # v7.0",
        "- uses: docker://alpine:3.20",
        '- "uses": evil/action@main',
        "- 'uses': evil/action@main",
        "- {uses: evil/action@main}",
        "- uses: [evil/action@main]",
    ],
    ids=[
        "major-tag",
        "exact-tag",
        "branch",
        "no-release-comment",
        "short-sha",
        "upper",
        "major-only-comment",
        "minor-only-comment",
        "docker",
        "double-quoted-key",
        "single-quoted-key",
        "flow-mapping",
        "not-a-string",
    ],
)
def test_an_unpinned_action_is_reported(step: str) -> None:
    assert len(unpinned_actions(workflow(step))) == 1


def test_uses_text_inside_a_run_block_neither_hides_nor_fakes_an_action() -> None:
    text = workflow(
        '- "uses": evil/action@main',
        "- run: |",
        f"    echo uses: {PINNED} # v7.0.1",
        f"    uses: {PINNED} # v7.0.1",
    )

    assert unpinned_actions(text) == ["line 6: evil/action@main"]


def test_a_reusable_workflow_job_is_checked() -> None:
    text = "on: push\njobs:\n  call:\n    uses: org/repo/.github/workflows/x.yml@main\n"

    assert unpinned_actions(text) == ["line 4: org/repo/.github/workflows/x.yml@main"]


@pytest.mark.parametrize(
    "step",
    [
        f"- uses: {PINNED} # v7.0.1",
        "- uses: github/codeql-action/init@3d3c42e5aac5ba805825da76410c181273ba90b1 # v3.0.0",
        "- uses: ./.github/actions/setup",
        "- name: not an action",
    ],
    ids=["pinned", "pinned-subpath", "local", "other-key"],
)
def test_a_pinned_or_local_action_is_accepted(step: str) -> None:
    assert unpinned_actions(workflow(step)) == []


# Shared by the Python source rules below: what a name refers to, through any import alias.
