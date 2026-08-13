"""Discover gyms exposed by WodBuster's authenticated gym selector."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from curl_cffi import requests

_SELECTOR_URL = "https://wodbuster.com/account/roadtobox.aspx"
_GYM_HOST_SUFFIX = ".wodbuster.com"
_GYM_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class GymSelectorError(Exception):
    """Raised when WodBuster's gym selector cannot be consumed safely."""


@dataclass(frozen=True, slots=True)
class DiscoveredGym:
    """A gym destination attested by WodBuster's central selector."""

    slug: str
    display_name: str


class _SelectorParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self._base_url = base_url
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []
        self._last_heading = ""
        self.gyms: dict[str, DiscoveredGym] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading_tag = tag
            self._heading_parts = []
            return
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href is None:
            return
        slug = _trusted_gym_slug(urljoin(self._base_url, href))
        if slug is None or slug in self.gyms:
            return
        display_name = self._last_heading or slug
        self.gyms[slug] = DiscoveredGym(slug=slug, display_name=display_name)

    def handle_data(self, data: str) -> None:
        if self._heading_tag is not None:
            self._heading_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != self._heading_tag:
            return
        self._last_heading = " ".join("".join(self._heading_parts).split())
        self._heading_tag = None
        self._heading_parts = []


def _trusted_gym_slug(url: str) -> str | None:
    target = urlparse(url)
    if (
        target.scheme != "https"
        or target.username is not None
        or target.password is not None
        or target.port not in {None, 443}
        or target.query
        or target.fragment
        or target.path.rstrip("/") != "/user"
    ):
        return None
    host = (target.hostname or "").lower()
    if not host.endswith(_GYM_HOST_SUFFIX):
        return None
    slug = host.removesuffix(_GYM_HOST_SUFFIX)
    if not _GYM_SLUG_RE.fullmatch(slug):
        return None
    return slug


def parse_gym_selector(html: str, *, base_url: str = _SELECTOR_URL) -> list[DiscoveredGym]:
    """Extract unique, trusted gym destinations from selector HTML."""
    parser = _SelectorParser(base_url)
    parser.feed(html)
    return list(parser.gyms.values())


def is_valid_discovered_slug(slug: str) -> bool:
    """Return whether a selector-derived slug is a single DNS label."""
    return _GYM_SLUG_RE.fullmatch(slug) is not None


def discover_gyms(cookie_value: str) -> list[DiscoveredGym]:
    """Load WodBuster's selector with an existing ``.WBAuth`` cookie."""
    if not cookie_value.strip():
        raise GymSelectorError("a non-empty .WBAuth cookie is required")
    try:
        response = requests.get(
            _SELECTOR_URL,
            cookies={".WBAuth": cookie_value},
            impersonate="chrome",
            allow_redirects=False,
            timeout=15,
        )
    except requests.RequestsError as exc:
        raise GymSelectorError("could not reach WodBuster's gym selector") from exc
    if response.status_code != 200:
        raise GymSelectorError(f"gym selector returned HTTP {response.status_code}")
    gyms = parse_gym_selector(response.text, base_url=response.url)
    if not gyms:
        raise GymSelectorError("gym selector returned no trusted gym links")
    return gyms


__all__ = [
    "DiscoveredGym",
    "GymSelectorError",
    "discover_gyms",
    "is_valid_discovered_slug",
    "parse_gym_selector",
]
