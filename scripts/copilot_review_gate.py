"""Pass only once Copilot has reviewed the head commit of a pull request.

Why this exists
---------------

Copilot code review runs on every push to a pull request, but its check run is not part of
the pull request's status rollup, so the branch ruleset cannot require it: a required
check with that name would stay "expected" forever. Without a gate, a pull request shows
as mergeable while the review of its latest commit is still running.

The `copilot-review-gate` workflow runs this script on every push and on every submitted
review. It passes when a Copilot review of the current head commit exists, and otherwise
polls until one appears or a deadline passes. Combined with the ruleset rule that every
review conversation is resolved, a pull request becomes mergeable only after Copilot has
reviewed its latest commit and every finding has been answered.

What it does not do
-------------------

* It does not read the review's findings. A review with open findings passes; resolving
  their threads is enforced by the ruleset.
* It fails closed. Anything it cannot read or understand is a failure, never a pass.
* Pull requests opened by Dependabot pass without a review. The author of a pull request
  cannot be impersonated, and only maintainers can push to a Dependabot branch.

Usage
-----

Run by `.github/workflows/copilot-review-gate.yml`, which supplies the environment variables
read by `load_config`. It uses only the standard library, so CI installs nothing.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Any, NoReturn

COPILOT_LOGIN = "copilot-pull-request-reviewer[bot]"
DEPENDABOT_LOGIN = "dependabot[bot]"

#: Review states that record a completed review. `PENDING` is an unsubmitted draft and
#: `DISMISSED` was withdrawn, so neither proves the commit was reviewed.
COMPLETED_STATES = frozenset({"COMMENTED", "APPROVED", "CHANGES_REQUESTED"})

PER_PAGE = 100
#: Reading stops here rather than paging without end through a hostile or broken API.
MAX_PAGES = 20
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 30.0
#: Longer than any review observed so far. The workflow's own timeout is set above this.
POLL_TIMEOUT_SECONDS = 30 * 60.0

_SHA = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_NUMBER = re.compile(r"[1-9][0-9]{0,9}")

FetchPage = Callable[[int], bytes]
Review = Mapping[str, Any]


class GateError(Exception):
    """The reviews could not be read or understood, so the gate cannot pass."""


@dataclass(frozen=True)
class Config:
    """Everything the gate needs, validated before any request is made."""

    api_url: str
    repository: str
    number: str
    head_sha: str
    draft: bool
    author: str
    token: str


def load_config(env: Mapping[str, str]) -> Config:
    """Read and validate the configuration the workflow passes in the environment."""

    def required(name: str) -> str:
        value = env.get(name, "")
        if not value:
            raise GateError(f"{name} is not set")
        return value

    api_url = required("GITHUB_API_URL").rstrip("/")
    parsed = urllib.parse.urlsplit(api_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
        raise GateError("GITHUB_API_URL must be an https URL with no query or fragment")
    repository = required("GITHUB_REPOSITORY")
    if not _REPOSITORY.fullmatch(repository):
        raise GateError("GITHUB_REPOSITORY is not in owner/name form")
    number = required("PR_NUMBER")
    if not _NUMBER.fullmatch(number):
        raise GateError("PR_NUMBER is not a pull request number")
    head_sha = required("HEAD_SHA")
    if not _SHA.fullmatch(head_sha):
        raise GateError("HEAD_SHA is not a full commit SHA")
    draft = required("PR_DRAFT")
    if draft not in {"true", "false"}:
        raise GateError("PR_DRAFT must be true or false")
    return Config(
        api_url=api_url,
        repository=repository,
        number=number,
        head_sha=head_sha,
        draft=draft == "true",
        author=required("PR_AUTHOR"),
        token=required("GITHUB_TOKEN"),
    )


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant {name}")


def parse_page(body: bytes) -> list[Review]:
    """Parse one page of the reviews API, rejecting anything but a list of objects."""
    try:
        data = json.loads(body, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise GateError("the reviews response is not valid JSON") from None
    if not isinstance(data, list):
        raise GateError("the reviews response is not a list")
    if not all(isinstance(item, dict) for item in data):
        raise GateError("the reviews response contains an entry that is not an object")
    return data


def fetch_reviews(fetch_page: FetchPage) -> list[Review]:
    """Read every page of reviews, failing rather than truncating past `MAX_PAGES`."""
    reviews: list[Review] = []
    for page in range(1, MAX_PAGES + 1):
        batch = parse_page(fetch_page(page))
        reviews.extend(batch)
        if len(batch) < PER_PAGE:
            return reviews
    raise GateError(f"the pull request has more than {MAX_PAGES * PER_PAGE} reviews")


def is_reviewed(reviews: Sequence[Review], head_sha: str) -> bool:
    """Return whether a completed Copilot review examined exactly `head_sha`.

    Reviews by anyone else are ignored, including a deleted account, which GitHub reports
    with a null user. A review attributed to Copilot must be well formed: an unexpected
    shape there is a failure, never a skipped entry.
    """
    for review in reviews:
        user = review.get("user")
        if user is None:
            continue
        if not isinstance(user, dict):
            raise GateError("a review has a user that is not an object")
        if user.get("login") != COPILOT_LOGIN:
            continue
        commit_id = review.get("commit_id")
        state = review.get("state")
        if not isinstance(commit_id, str) or not isinstance(state, str):
            raise GateError("a Copilot review has no commit or state")
        if commit_id == head_sha and state in COMPLETED_STATES:
            return True
    return False


def wait_for_review(
    read_reviews: Callable[[], Sequence[Review]],
    head_sha: str,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    interval: float = POLL_INTERVAL_SECONDS,
    timeout: float = POLL_TIMEOUT_SECONDS,
) -> bool:
    """Poll until Copilot has reviewed `head_sha`, or return `False` at the deadline."""
    deadline = monotonic() + timeout
    while True:
        if is_reviewed(read_reviews(), head_sha):
            return True
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleep(min(interval, remaining))


def read_bounded(stream: IO[bytes], limit: int) -> bytes:
    """Read at most `limit` bytes, failing while reading rather than after."""
    body = stream.read(limit + 1)
    if len(body) > limit:
        raise GateError(f"the reviews response is larger than {limit} bytes")
    return body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects, which would carry the token to wherever they point."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def github_page_fetcher(config: Config) -> FetchPage:
    """Return a function that reads one page of the pull request's reviews."""
    opener = urllib.request.build_opener(_NoRedirect)
    base = f"{config.api_url}/repos/{config.repository}/pulls/{config.number}/reviews"

    def fetch(page: int) -> bytes:
        request = urllib.request.Request(  # load_config allows https only
            f"{base}?per_page={PER_PAGE}&page={page}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {config.token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise GateError(f"the GitHub API answered HTTP {response.status}")
                return read_bounded(response, MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as error:
            raise GateError(f"the GitHub API answered HTTP {error.code}") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError):
            raise GateError("the GitHub API could not be reached") from None

    return fetch


def run(
    config: Config,
    fetch_page: FetchPage,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    emit: Callable[[str], None],
) -> int:
    """Decide the gate for one pull request and return the process exit code."""
    if config.author == DEPENDABOT_LOGIN:
        emit("Dependabot pull request: a Copilot review is not required.")
        return 0
    if config.draft:
        emit("::error::Draft pull requests are not reviewed. Mark it ready for review.")
        return 1
    try:
        reviewed = wait_for_review(
            lambda: fetch_reviews(fetch_page),
            config.head_sha,
            sleep=sleep,
            monotonic=monotonic,
        )
    except GateError as error:
        emit(f"::error::Cannot confirm the Copilot review: {error}.")
        return 1
    if reviewed:
        emit(f"Copilot has reviewed {config.head_sha}.")
        return 0
    emit(
        f"::error::No Copilot review of {config.head_sha} arrived in time. "
        "Request a review from Copilot, and rerun this check if the review does not."
    )
    return 1


def main() -> int:
    try:
        config = load_config(os.environ)
    except GateError as error:
        print(f"::error::Invalid gate configuration: {error}.")
        return 1
    return run(
        config,
        github_page_fetcher(config),
        sleep=time.sleep,
        monotonic=time.monotonic,
        emit=print,
    )


if __name__ == "__main__":
    sys.exit(main())
