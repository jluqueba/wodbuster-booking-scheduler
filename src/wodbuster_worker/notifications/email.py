"""Azure Communication Services email sender (ADR-0011).

Mirrors :mod:`notifications.telegram`: one function that sends a
rendered message and classifies failures so the outbox dispatcher can
retry transient errors and give up on permanent ones. Authentication is
the runtime managed identity via ``DefaultAzureCredential`` (no
connection string; ADR-0005), and sending goes through the ACS
``EmailClient.begin_send`` long-running operation.

``PermanentEmailError`` covers 4xx (bad recipient, unverified domain,
missing permission) and a ``Failed`` send result; ``TransientEmailError``
covers transport failures, 429 and 5xx.
"""

from __future__ import annotations

from typing import Any, Protocol

import structlog
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)

_log = structlog.get_logger(__name__)


class EmailError(Exception):
    """Base class for email delivery failures."""


class TransientEmailError(EmailError):
    """Retryable: transport failure, 429, or 5xx."""


class PermanentEmailError(EmailError):
    """Non-retryable: 4xx, auth failure, or a Failed send result."""


class _SendClient(Protocol):
    """Minimal surface of ``azure.communication.email.EmailClient``.

    Declared so tests can inject a fake without the SDK or network.
    """

    def begin_send(self, message: dict[str, Any], **kwargs: Any) -> Any: ...


def send_email(
    *,
    client: _SendClient,
    sender_address: str,
    recipient_address: str,
    subject: str,
    html: str,
    plain_text: str,
    list_unsubscribe_url: str | None = None,
) -> None:
    """Send one email through ACS, raising on failure.

    ``client`` is an already-constructed ACS ``EmailClient`` (or a test
    fake). The caller owns its lifetime and credential so a single
    client is reused across dispatcher ticks.
    """
    message: dict[str, Any] = {
        "senderAddress": sender_address,
        "recipients": {"to": [{"address": recipient_address}]},
        "content": {"subject": subject, "plainText": plain_text, "html": html},
    }
    if list_unsubscribe_url:
        # One-click unsubscribe (RFC 8058) so mailbox providers surface a
        # native unsubscribe control alongside our in-body link.
        message["headers"] = {
            "List-Unsubscribe": f"<{list_unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }

    try:
        poller = client.begin_send(message)
        result = poller.result()
    except (ServiceRequestError, ServiceResponseError) as exc:
        raise TransientEmailError(f"acs transport: {exc}") from exc
    except ClientAuthenticationError as exc:
        raise PermanentEmailError(f"acs auth: {exc}") from exc
    except HttpResponseError as exc:
        code = exc.status_code or 0
        if code == 429 or 500 <= code < 600:
            raise TransientEmailError(f"acs {code}: {exc.message}") from exc
        raise PermanentEmailError(f"acs {code}: {exc.message}") from exc

    send_status = str(_result_status(result)).lower()
    if send_status == "succeeded":
        _log.info("email.send.ok", recipient=recipient_address)
        return
    # The send was accepted but the operation reported a non-success
    # terminal state; retrying the same content will not help.
    raise PermanentEmailError(f"acs send status {send_status!r}")


def _result_status(result: Any) -> Any:
    if isinstance(result, dict):
        return result.get("status")
    return getattr(result, "status", None)


__all__ = [
    "EmailError",
    "PermanentEmailError",
    "TransientEmailError",
    "send_email",
]
