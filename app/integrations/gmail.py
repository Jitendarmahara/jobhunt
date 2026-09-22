import base64
from email.message import EmailMessage
from typing import Any

import httpx

from app.config import get_settings


class GmailConfigurationError(RuntimeError):
    pass


class GmailClient:
    name = "gmail"

    def _access_token(self) -> str:
        settings = get_settings()
        if not all((settings.gmail_client_id, settings.gmail_client_secret, settings.gmail_refresh_token)):
            raise GmailConfigurationError("Gmail OAuth client ID, secret, and refresh token are required.")
        response = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.gmail_client_id,
                "client_secret": settings.gmail_client_secret,
                "refresh_token": settings.gmail_refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def send(self, recipient: str, subject: str, body: str) -> tuple[str, str | None]:
        settings = get_settings()
        if not settings.gmail_sender:
            raise GmailConfigurationError("GMAIL_SENDER is required.")
        message = EmailMessage()
        message["To"] = recipient
        message["From"] = settings.gmail_sender
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
        response = httpx.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {self._access_token()}"},
            json={"raw": raw},
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
        return result["id"], result.get("threadId")

    def replies_for_message(self, provider_message_id: str) -> list[dict[str, str]]:
        """Read messages in the outbound email's Gmail thread, never mailbox-wide."""
        token = self._access_token()
        headers = {"Authorization": f"Bearer {token}"}
        message = httpx.get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{provider_message_id}", headers=headers, timeout=30)
        message.raise_for_status()
        thread_id = message.json().get("threadId")
        if not thread_id:
            return []
        thread = httpx.get(f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}", headers=headers, timeout=30)
        thread.raise_for_status()
        settings = get_settings()
        replies = []
        for item in thread.json().get("messages", []):
            if item.get("id") == provider_message_id:
                continue
            header_map = {header["name"].lower(): header["value"] for header in item.get("payload", {}).get("headers", [])}
            sender = header_map.get("from", "")
            if settings.gmail_sender and settings.gmail_sender.lower() in sender.lower():
                continue
            replies.append(
                {
                    "id": item["id"],
                    "sender": sender,
                    "subject": header_map.get("subject", ""),
                    "snippet": item.get("snippet", ""),
                }
            )
        return replies
