from __future__ import annotations

import pytest

from wodbuster_worker.gyms import discovery
from wodbuster_worker.gyms.discovery import DiscoveredGym, GymSelectorError, parse_gym_selector


def test_parse_gym_selector_extracts_named_unique_gyms() -> None:
    html = """
    <h2>Elite Fitness</h2>
    <a href="https://elitefitness.wodbuster.com/user">Enter</a>
    <a href="https://elitefitness.wodbuster.com/user">Enter again</a>
    <h2>Antwork Training Center</h2>
    <a href="https://antworktrainingcenter.wodbuster.com/user/">Enter</a>
    """

    assert parse_gym_selector(html) == [
        DiscoveredGym(slug="elitefitness", display_name="Elite Fitness"),
        DiscoveredGym(
            slug="antworktrainingcenter",
            display_name="Antwork Training Center",
        ),
    ]


def test_parse_gym_selector_rejects_untrusted_destinations() -> None:
    html = """
    <h2>Untrusted</h2>
    <a href="http://plain.wodbuster.com/user">HTTP</a>
    <a href="https://nested.evil.wodbuster.com/user">Nested host</a>
    <a href="https://wodbuster.com.evil.example/user">Suffix trick</a>
    <a href="https://user:pass@credentials.wodbuster.com/user">Credentials</a>
    <a href="https://port.wodbuster.com:8443/user">Port</a>
    <a href="https://query.wodbuster.com/user?next=evil">Query</a>
    <a href="https://path.wodbuster.com/admin">Wrong path</a>
    """

    assert parse_gym_selector(html) == []


def test_discover_gyms_translates_selector_http_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status_code = 403
        text = "blocked"
        url = "https://wodbuster.com/account/roadtobox.aspx"

    monkeypatch.setattr(discovery.requests, "get", lambda *args, **kwargs: Response())

    with pytest.raises(GymSelectorError, match="HTTP 403"):
        discovery.discover_gyms(".WBAuth-existing")
