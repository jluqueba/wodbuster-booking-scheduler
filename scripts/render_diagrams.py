"""Render the Mermaid diagrams used by the public documentation to SVG.

Why this exists
---------------

GitHub renders Mermaid blocks, but the GitHub Pages site does not, so a diagram kept inline
in the developer guide appears there as source code. Public documentation therefore
embeds committed SVG images, each rendered from a Mermaid source kept beside it so its
history still diffs as text.

Each SVG starts with a comment recording the SHA-256 of the source it was rendered from. A
unit test compares that digest with the source on every run, so editing a diagram without
regenerating its image fails the build instead of publishing a stale picture.

Requirements
------------

Node.js, for `npx`. The first run downloads the pinned Mermaid CLI and a headless browser.
This is a maintainer tool: CI never runs it, and nothing here is a Python dependency.

Usage
-----

    python scripts/render_diagrams.py

See the Documentation site and diagrams section of docs/DEVELOPER_GUIDE.md.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = REPOSITORY_ROOT / "docs" / "assets"
MERMAID_CONFIG = Path(__file__).resolve().parent / "mermaid-config.json"

#: Pinned so a new release cannot silently change every rendered diagram.
MERMAID_CLI = "@mermaid-js/mermaid-cli@12.0.0"

#: The first line of every rendered SVG. Kept on its own line so the digest can be read
#: without parsing the SVG.
DIGEST_PREFIX = "<!-- wodbuster-diagram-source-sha256: "
DIGEST_SUFFIX = " -->"


def source_digest(source: bytes) -> str:
    """Return the SHA-256 of a diagram source, independent of line endings.

    A Windows checkout may turn LF into CRLF. The digest must not change with it, or the
    drift test would fail on one platform and pass on another for the same diagram.
    """
    return hashlib.sha256(source.replace(b"\r\n", b"\n")).hexdigest()


def stamp(svg: str, digest: str) -> str:
    """Prefix an SVG with the digest of the source it was rendered from."""
    return f"{DIGEST_PREFIX}{digest}{DIGEST_SUFFIX}\n{svg}"


def recorded_digest(svg: str) -> str | None:
    """Return the digest an SVG was stamped with, or `None` when it carries none."""
    first_line = svg.split("\n", 1)[0].strip()
    if first_line.startswith(DIGEST_PREFIX) and first_line.endswith(DIGEST_SUFFIX):
        return first_line[len(DIGEST_PREFIX) : -len(DIGEST_SUFFIX)]
    return None


def render(source: Path, npx: str) -> Path:
    """Render one `.mmd` file to the `.svg` beside it.

    The image is written to a temporary file in the same directory and moved into place
    only once it is complete and stamped, so a failed render never leaves a truncated or
    unstamped SVG under the final name.
    """
    target = source.with_suffix(".svg")
    handle, temp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.stem}.", suffix=".svg"
    )
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        # The command is fixed apart from paths this script built itself; no input is
        # interpolated into a shell.
        subprocess.run(
            [
                npx,
                "--yes",
                MERMAID_CLI,
                "--input",
                str(source),
                "--output",
                str(temp_path),
                "--configFile",
                str(MERMAID_CONFIG),
                "--backgroundColor",
                "white",
                "--quiet",
            ],
            check=True,
        )
        svg = temp_path.read_text(encoding="utf-8")
        temp_path.write_text(stamp(svg, source_digest(source.read_bytes())), encoding="utf-8")
        temp_path.replace(target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return target


def main() -> int:
    """Render every diagram source under `docs/assets`."""
    npx = shutil.which("npx")
    if npx is None:
        print("npx was not found. Install Node.js to render diagrams.", file=sys.stderr)
        return 1

    sources = sorted(ASSETS_DIR.glob("*.mmd"))
    if not sources:
        print(f"No diagram sources found in {ASSETS_DIR.relative_to(REPOSITORY_ROOT)}.")
        return 0

    for source in sources:
        target = render(source, npx)
        print(f"rendered {target.relative_to(REPOSITORY_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
