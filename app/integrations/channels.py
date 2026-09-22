"""Outreach/response channel interfaces.

Only Gmail (``app.integrations.gmail.GmailClient``) is actually implemented.
LinkedIn, X (Twitter), and SMS (Twilio) are real, likely future channels for
the Response Analyzer and Job Applier, but each requires its own API
credentials and a compliant integration (rate limits, ToS) this system does
not yet have. Rather than silently no-op or fabricate a response, each stub
below fails loudly and explains exactly what is missing, so a caller (or an
operator reading the logs) never mistakes "not implemented" for "sent" or
"no reply".
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class ChannelNotConfigured(RuntimeError):
    """Raised by a channel stub that has no working credentials/implementation yet."""


@runtime_checkable
class MessagingChannel(Protocol):
    """The shape every outreach/response channel must satisfy.

    ``GmailClient`` conforms to this structurally (duck typing); the stubs
    below conform explicitly so the Response Analyzer can be wired to a
    channel registry (see ``CHANNELS``) without special-casing per channel.
    """

    name: str

    def send(self, recipient: str, subject: str, body: str) -> tuple[str, str | None]:
        """Send a message; returns (provider_message_id, thread_id)."""

    def replies_for_message(self, provider_message_id: str) -> list[dict[str, str]]:
        """Return normalized replies: each with at least sender/subject/snippet."""


class LinkedInChannel:
    name = "linkedin"

    def send(self, recipient: str, subject: str, body: str) -> tuple[str, str | None]:
        raise ChannelNotConfigured(
            "LinkedIn outreach is not implemented yet: it needs a LinkedIn API "
            "integration (Messaging/Marketing API access + credentials)."
        )

    def replies_for_message(self, provider_message_id: str) -> list[dict[str, str]]:
        raise ChannelNotConfigured(
            "LinkedIn reply sync is not implemented yet: it needs a LinkedIn API "
            "integration (Messaging/Marketing API access + credentials)."
        )


class XChannel:
    name = "x"

    def send(self, recipient: str, subject: str, body: str) -> tuple[str, str | None]:
        raise ChannelNotConfigured(
            "X (Twitter) outreach is not implemented yet: it needs the X API "
            "(a developer app + OAuth credentials)."
        )

    def replies_for_message(self, provider_message_id: str) -> list[dict[str, str]]:
        raise ChannelNotConfigured(
            "X (Twitter) reply sync is not implemented yet: it needs the X API "
            "(a developer app + OAuth credentials)."
        )


class TwilioSmsChannel:
    name = "sms"

    def send(self, recipient: str, subject: str, body: str) -> tuple[str, str | None]:
        raise ChannelNotConfigured(
            "SMS outreach is not implemented yet: it needs a Twilio account SID, "
            "auth token, and a sending number."
        )

    def replies_for_message(self, provider_message_id: str) -> list[dict[str, str]]:
        raise ChannelNotConfigured(
            "SMS reply sync is not implemented yet: it needs a Twilio account SID, "
            "auth token, and a sending number."
        )


# A future Response Analyzer / Job Applier pass can iterate this registry
# instead of hardcoding "gmail" — today only Gmail actually works.
CHANNEL_STUBS: dict[str, MessagingChannel] = {
    "linkedin": LinkedInChannel(),
    "x": XChannel(),
    "sms": TwilioSmsChannel(),
}
