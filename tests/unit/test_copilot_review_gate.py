"""The Copilot review gate passes only once Copilot has reviewed the head commit.

`scripts/copilot_review_gate.py` backs the `copilot-review-gate` required check. It must
never pass by accident: every unreadable, malformed or oversized answer fails, and the only
pass without a review is a pull request opened by Dependabot. Time, sleeping and the
GitHub API are injected, so nothing here waits or leaves the machine; the HTTP tests use an
in-process server on the loopback interface.
"""

from __future__ import annotations

import http.server
import importlib.util
import io
import json
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "copilot-review-gate.yml"

HEAD = "a" * 40
OTHER = "b" * 40
TOKEN = "ghs_ThisTokenMustNeverBePrinted0123456789"


def _load_script() -> ModuleType:
    path = REPOSITORY_ROOT / "scripts" / "copilot_review_gate.py"
    spec = importlib.util.spec_from_file_location("copilot_review_gate", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_script()


def review(
    login: str | None = "copilot-pull-request-reviewer[bot]",
    commit: object = HEAD,
    state: object = "COMMENTED",
) -> dict[str, Any]:
    return {
        "user": None if login is None else {"login": login},
        "commit_id": commit,
        "state": state,
    }


def page(reviews: Sequence[dict[str, Any]]) -> bytes:
    return json.dumps(list(reviews)).encode()


def config(**changes: Any) -> Any:
    base = gate.Config(
        api_url="https://api.github.com",
        repository="jluqueba/wodbuster-booking-scheduler",
        number="60",
        head_sha=HEAD,
        draft=False,
        author="jluqueba",
        token=TOKEN,
    )
    return replace(base, **changes)


class FakeClock:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0, "a wait that does not move time forward never ends"
        self.sleeps.append(seconds)
        self.now += seconds


class Reviews:
    """Serves one page of reviews per poll, from a scripted sequence."""

    def __init__(self, *polls: Sequence[dict[str, Any]]) -> None:
        self.polls = list(polls)
        self.calls = 0

    def __call__(self) -> Sequence[dict[str, Any]]:
        result = self.polls[min(self.calls, len(self.polls) - 1)]
        self.calls += 1
        return result


# Deciding whether the head commit was reviewed.


@pytest.mark.parametrize("state", ["COMMENTED", "APPROVED", "CHANGES_REQUESTED"])
def test_a_completed_copilot_review_of_the_head_commit_counts(state: str) -> None:
    assert gate.is_reviewed([review(state=state)], HEAD)


def test_a_copilot_review_of_an_earlier_commit_does_not_count() -> None:
    assert not gate.is_reviewed([review(commit=OTHER)], HEAD)


@pytest.mark.parametrize(
    "login",
    [
        "jluqueba",
        "Copilot",
        "copilot-pull-request-reviewer",
        "copilot-pull-request-reviewer[bot] ",
        "COPILOT-PULL-REQUEST-REVIEWER[BOT]",
    ],
)
def test_a_review_of_the_head_commit_by_anyone_else_does_not_count(login: str) -> None:
    assert not gate.is_reviewed([review(login=login)], HEAD)


@pytest.mark.parametrize("state", ["PENDING", "DISMISSED", "UNKNOWN", ""])
def test_an_unsubmitted_dismissed_or_unknown_review_does_not_count(state: str) -> None:
    assert not gate.is_reviewed([review(state=state)], HEAD)


def test_a_review_by_a_deleted_account_is_ignored_rather_than_failing() -> None:
    assert not gate.is_reviewed([review(login=None)], HEAD)
    assert gate.is_reviewed([review(login=None), review()], HEAD)


@pytest.mark.parametrize(
    "entry",
    [
        review(commit=None),
        review(state=None),
        review(commit=7),
        {"user": "copilot-pull-request-reviewer[bot]", "commit_id": HEAD, "state": "COMMENTED"},
    ],
    ids=["no-commit", "no-state", "numeric-commit", "user-not-an-object"],
)
def test_a_malformed_copilot_review_fails_closed(entry: dict[str, Any]) -> None:
    with pytest.raises(gate.GateError):
        gate.is_reviewed([entry], HEAD)


# Reading the API.


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b"[1]",
        b"[[]]",
        b"not json",
        b'[{"user": null, "n": NaN}]',
        b'[{"user": null, "n": -Infinity}]',
        b"[" * 100_000,
    ],
    ids=["object", "number-entry", "list-entry", "not-json", "nan", "infinity", "deep"],
)
def test_a_response_that_is_not_a_list_of_objects_fails_closed(body: bytes) -> None:
    with pytest.raises(gate.GateError):
        gate.parse_page(body)


def test_a_review_on_a_later_page_is_found() -> None:
    pages = {1: [review(commit=OTHER)] * 100, 2: [review(commit=OTHER)] * 100, 3: [review()]}
    requested: list[int] = []

    def fetch_page(number: int) -> bytes:
        requested.append(number)
        return page(pages[number])

    reviews = gate.fetch_reviews(fetch_page)

    assert requested == [1, 2, 3]
    assert gate.is_reviewed(reviews, HEAD)


def test_a_full_last_page_stops_at_the_following_empty_page() -> None:
    requested: list[int] = []

    def fetch_page(number: int) -> bytes:
        requested.append(number)
        return page([review(commit=OTHER)] * 100 if number == 1 else [])

    assert len(gate.fetch_reviews(fetch_page)) == 100
    assert requested == [1, 2]


def test_paging_without_end_fails_at_the_page_limit() -> None:
    requested: list[int] = []

    def fetch_page(number: int) -> bytes:
        requested.append(number)
        return page([review(commit=OTHER)] * 100)

    with pytest.raises(gate.GateError):
        gate.fetch_reviews(fetch_page)
    assert requested == list(range(1, gate.MAX_PAGES + 1))


class CountingStream(io.RawIOBase):
    """An endless stream that records how many bytes were requested from it."""

    def __init__(self) -> None:
        self.requested = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> bytes:
        assert size is not None
        assert size >= 0, "an unbounded read would never return"
        self.requested += size
        return b"x" * size


def test_an_oversized_response_fails_after_reading_one_byte_past_the_limit() -> None:
    stream = CountingStream()

    with pytest.raises(gate.GateError):
        gate.read_bounded(stream, 1000)
    assert stream.requested == 1001


def test_a_response_at_the_limit_is_accepted() -> None:
    assert gate.read_bounded(io.BytesIO(b"x" * 1000), 1000) == b"x" * 1000


# Polling.


def test_polling_stops_as_soon_as_the_review_appears() -> None:
    clock = FakeClock()
    reviews = Reviews([], [review(commit=OTHER)], [review()])

    reviewed = gate.wait_for_review(
        reviews, HEAD, sleep=clock.sleep, monotonic=clock.monotonic, interval=30, timeout=600
    )

    assert reviewed
    assert reviews.calls == 3
    assert clock.sleeps == [30, 30]


def test_polling_gives_up_at_the_deadline() -> None:
    clock = FakeClock()
    reviews = Reviews([])

    reviewed = gate.wait_for_review(
        reviews, HEAD, sleep=clock.sleep, monotonic=clock.monotonic, interval=30, timeout=90
    )

    assert not reviewed
    assert reviews.calls == 4
    assert clock.now == 90


def test_the_last_wait_never_overshoots_the_deadline() -> None:
    clock = FakeClock()

    gate.wait_for_review(
        Reviews([]), HEAD, sleep=clock.sleep, monotonic=clock.monotonic, interval=30, timeout=45
    )

    assert clock.sleeps == [30, 15]


# The decision for one pull request.


def decide(
    settings: Any, fetch_page: Callable[[int], bytes], clock: FakeClock | None = None
) -> tuple[int, list[str]]:
    clock = clock or FakeClock()
    output: list[str] = []
    code = gate.run(
        settings, fetch_page, sleep=clock.sleep, monotonic=clock.monotonic, emit=output.append
    )
    return code, output


def must_not_fetch(number: int) -> bytes:
    raise AssertionError("the reviews must not be read")


def test_a_reviewed_pull_request_passes() -> None:
    code, _ = decide(config(), lambda number: page([review()]))

    assert code == 0


def test_an_unreviewed_pull_request_fails_at_the_deadline() -> None:
    clock = FakeClock()

    code, output = decide(config(), lambda number: page([review(commit=OTHER)]), clock)

    assert code == 1
    assert clock.now == gate.POLL_TIMEOUT_SECONDS
    assert output[-1].startswith("::error::")


def test_a_dependabot_pull_request_passes_without_reading_reviews() -> None:
    code, output = decide(config(author="dependabot[bot]"), must_not_fetch)

    assert code == 0
    assert "Dependabot" in output[0]


@pytest.mark.parametrize("author", ["dependabot", "Dependabot[bot]", "dependabot[bot]x"])
def test_a_lookalike_author_is_not_exempt(author: str) -> None:
    code, _ = decide(config(author=author), lambda number: page([review(commit=OTHER)]))

    assert code == 1


def test_a_draft_fails_without_reading_reviews() -> None:
    code, output = decide(config(draft=True), must_not_fetch)

    assert code == 1
    assert output[0].startswith("::error::")


def test_an_unreadable_answer_fails_closed() -> None:
    def broken(number: int) -> bytes:
        raise gate.GateError("the GitHub API answered HTTP 502")

    code, output = decide(config(), broken)

    assert code == 1
    assert "HTTP 502" in output[0]


@pytest.mark.parametrize(
    "fetch_page",
    [lambda n: page([review()]), lambda n: page([]), lambda n: b"{}"],
    ids=["pass", "deadline", "error"],
)
def test_the_token_never_appears_in_the_output(fetch_page: Callable[[int], bytes]) -> None:
    _, output = decide(config(), fetch_page)

    assert output
    assert not any(TOKEN in line for line in output)


# Configuration.

VALID_ENV = {
    "GITHUB_API_URL": "https://api.github.com/",
    "GITHUB_REPOSITORY": "jluqueba/wodbuster-booking-scheduler",
    "PR_NUMBER": "60",
    "HEAD_SHA": HEAD,
    "PR_DRAFT": "false",
    "PR_AUTHOR": "jluqueba",
    "GITHUB_TOKEN": TOKEN,
}


def test_a_valid_environment_is_accepted() -> None:
    assert gate.load_config(VALID_ENV) == config()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITHUB_TOKEN", ""),
        ("PR_AUTHOR", ""),
        ("GITHUB_API_URL", "http://api.github.com"),
        ("GITHUB_API_URL", "https://api.github.com?x=1"),
        ("GITHUB_API_URL", "https://"),
        ("GITHUB_REPOSITORY", "jluqueba/wodbuster-booking-scheduler/x"),
        ("PR_NUMBER", "0"),
        ("PR_NUMBER", "60 "),
        ("HEAD_SHA", HEAD[:39]),
        ("HEAD_SHA", HEAD.upper()),
        ("PR_DRAFT", "yes"),
    ],
)
def test_an_invalid_environment_fails_closed(name: str, value: str) -> None:
    with pytest.raises(gate.GateError):
        gate.load_config({**VALID_ENV, name: value})


# The real fetcher, against an in-process server.


class Server:
    """Answers every request with one scripted status, body and headers."""

    def __init__(self) -> None:
        self.status = 200
        self.body = b"[]"
        self.headers: dict[str, str] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                owner.requests.append((self.path, dict(self.headers.items())))
                self.send_response(owner.status)
                for key, value in owner.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(owner.body)))
                self.end_headers()
                self.wfile.write(owner.body)

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"


@pytest.fixture
def servers() -> Iterator[tuple[Server, Server]]:
    pair = (Server(), Server())
    threads = [
        threading.Thread(target=s.httpd.serve_forever, args=(0.01,), daemon=True) for s in pair
    ]
    for thread in threads:
        thread.start()
    yield pair
    for server in pair:
        server.httpd.shutdown()
        server.httpd.server_close()


def test_the_fetcher_reads_the_requested_page_with_the_token(
    servers: tuple[Server, Server],
) -> None:
    api, _ = servers
    api.body = page([review()])

    body = gate.github_page_fetcher(config(api_url=api.url))(2)

    assert body == api.body
    path, headers = api.requests[0]
    assert (
        path == "/repos/jluqueba/wodbuster-booking-scheduler/pulls/60/reviews?per_page=100&page=2"
    )
    assert headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("status", [201, 204, 304, 404, 500])
def test_any_status_but_200_fails_closed(servers: tuple[Server, Server], status: int) -> None:
    api, _ = servers
    api.status = status
    api.body = b"" if status in {204, 304} else page([review()])

    with pytest.raises(gate.GateError):
        gate.github_page_fetcher(config(api_url=api.url))(1)


def test_a_redirect_is_refused_and_the_token_goes_nowhere_else(
    servers: tuple[Server, Server],
) -> None:
    api, elsewhere = servers
    api.status = 302
    api.headers = {"Location": f"{elsewhere.url}/stolen"}

    with pytest.raises(gate.GateError):
        gate.github_page_fetcher(config(api_url=api.url))(1)
    assert elsewhere.requests == []


# The workflow that runs the script.


def workflow() -> dict[Any, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_job_reports_the_check_name_the_ruleset_requires() -> None:
    jobs = workflow()["jobs"]

    assert list(jobs) == ["copilot-review-gate"]
    assert jobs["copilot-review-gate"]["name"] == "copilot-review-gate"


def test_the_gate_reruns_on_every_push_and_every_submitted_review() -> None:
    # PyYAML reads the bare key `on` as the boolean true.
    triggers = workflow()[True]

    assert set(triggers["pull_request"]["types"]) >= {"opened", "synchronize", "reopened"}
    assert "ready_for_review" in triggers["pull_request"]["types"]
    assert triggers["pull_request_review"]["types"] == ["submitted"]


def test_the_workflow_grants_only_read_access() -> None:
    loaded = workflow()

    assert loaded["permissions"] == {}
    job = loaded["jobs"]["copilot-review-gate"]
    assert job["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert job["timeout-minutes"] * 60 > gate.POLL_TIMEOUT_SECONDS


def test_a_newer_event_cancels_an_older_run_of_the_same_pull_request() -> None:
    concurrency = workflow()["concurrency"]

    assert concurrency["cancel-in-progress"] is True
    assert "github.event.pull_request.number" in concurrency["group"]


def test_event_values_never_reach_the_shell_command() -> None:
    steps = workflow()["jobs"]["copilot-review-gate"]["steps"]

    commands = [step["run"] for step in steps if "run" in step]
    assert commands == ["python3 scripts/copilot_review_gate.py"]
