"""Unit tests for the ACS email sender error classification (ADR-0011)."""

from __future__ import annotations

from typing import Any

import pytest
from azure.core.exceptions import HttpResponseError, ServiceResponseError

from wodbuster_worker.notifications import email as email_channel


class _Poller:
    def __init__(self, status: str) -> None:
        self._status = status

    def result(self) -> dict[str, str]:
        return {"status": self._status}


class _Client:
    def __init__(self, *, poller: _Poller | None = None, exc: Exception | None = None) -> None:
        self._poller = poller
        self._exc = exc
        self.captured: dict[str, Any] | None = None

    def begin_send(self, message: dict[str, Any], **_: Any) -> _Poller:
        if self._exc is not None:
            raise self._exc
        self.captured = message
        return self._poller or _Poller("Succeeded")


def _send(client: _Client, **overrides: Any) -> None:
    kwargs: dict[str, Any] = {
        "client": client,
        "sender_address": "no-reply@x",
        "recipient_address": "user@y",
        "subject": "s",
        "html": "<b>h</b>",
        "plain_text": "t",
    }
    kwargs.update(overrides)
    email_channel.send_email(**kwargs)


def test_send_ok_captures_message() -> None:
    client = _Client(poller=_Poller("Succeeded"))
    _send(client)
    assert client.captured is not None
    assert client.captured["senderAddress"] == "no-reply@x"
    assert client.captured["recipients"]["to"][0]["address"] == "user@y"


def test_list_unsubscribe_header_set() -> None:
    client = _Client(poller=_Poller("Succeeded"))
    _send(client, list_unsubscribe_url="https://u/1")
    assert client.captured is not None
    assert client.captured["headers"]["List-Unsubscribe"] == "<https://u/1>"
    assert client.captured["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_failed_result_is_permanent() -> None:
    client = _Client(poller=_Poller("Failed"))
    with pytest.raises(email_channel.PermanentEmailError):
        _send(client)


def test_5xx_is_transient() -> None:
    exc = HttpResponseError(message="boom")
    exc.status_code = 503
    with pytest.raises(email_channel.TransientEmailError):
        _send(_Client(exc=exc))


def test_4xx_is_permanent() -> None:
    exc = HttpResponseError(message="bad recipient")
    exc.status_code = 400
    with pytest.raises(email_channel.PermanentEmailError):
        _send(_Client(exc=exc))


def test_transport_error_is_transient() -> None:
    with pytest.raises(email_channel.TransientEmailError):
        _send(_Client(exc=ServiceResponseError(message="reset")))
