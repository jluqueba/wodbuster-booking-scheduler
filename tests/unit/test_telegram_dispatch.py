"""Unit tests for the Telegram command router (TG.2 / TG.T1).

These exercise the pure ``_route`` classifier in isolation — no
request, no database. They pin down the allow-list contract:

- every supported command maps to its own handler label;
- rule-mutation verbs map to ``rule_mutation`` (explanatory
  rejection, CC-009), never to a handler that mutates state;
- anything else maps to ``unknown`` (polite nudge).

The dispatcher lowercases the command before calling ``_route``, so
these tests feed lowercase tokens (matching the runtime contract).
"""

from __future__ import annotations

import pytest

from wodbuster_worker.notifications.telegram_webhook import (
    _RULE_MUTATION_COMMANDS,
    _route,
)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/start", "start"),
        ("/help", "help"),
        ("/status", "status"),
        ("/next", "next"),
        ("/last", "last"),
        ("/list", "list"),
        ("/cancel", "cancel"),
        ("/ack", "ack"),
        ("/bookclass", "bookclass"),
    ],
)
def test_supported_commands_route_to_their_handler(command: str, expected: str) -> None:
    assert _route(command) == expected


@pytest.mark.parametrize("command", sorted(_RULE_MUTATION_COMMANDS))
def test_rule_mutation_commands_route_to_explanatory_rejection(command: str) -> None:
    # CC-009: rule create/update/delete is web-UI only. The router must
    # single these out so the dispatcher can explain the refusal rather
    # than treat them as unknown.
    assert _route(command) == "rule_mutation"


@pytest.mark.parametrize(
    "command",
    ["/unknown", "/book", "/delete", "hello", "/startx", "", "/"],
)
def test_unrecognised_commands_route_to_unknown(command: str) -> None:
    assert _route(command) == "unknown"
