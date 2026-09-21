"""Outcome ingestion with auditable Gmail-thread correlation."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.gmail import GmailClient
from app.models import InboundEmail, Outcome, OutcomeStage, OutreachMessage


def _stage_from_reply(text: str) -> OutcomeStage:
    lowered = text.lower()
    if any(term in lowered for term in ("interview", "schedule a call", "speak with")):
        return OutcomeStage.INTERVIEW
    if any(term in lowered for term in ("unfortunately", "not moving forward", "rejected")):
        return OutcomeStage.REJECTED
    if any(term in lowered for term in ("interested", "next steps", "would love to")):
        return OutcomeStage.POSITIVE_REPLY
    return OutcomeStage.REPLIED


class ResponseAnalyzer:
    def ingest_gmail(self, session: Session) -> dict[str, int]:
        processed = 0
        outcomes = 0
        messages = session.scalars(
            select(OutreachMessage).where(OutreachMessage.status == "sent", OutreachMessage.provider_message_id.is_not(None))
        ).all()
        gmail = GmailClient()
        for outbound in messages:
            for reply in gmail.replies_for_message(outbound.provider_message_id):
                processed += 1
                if session.scalar(select(InboundEmail).where(InboundEmail.provider_message_id == reply["id"])):
                    continue
                session.add(
                    InboundEmail(
                        outreach_message_id=outbound.id,
                        provider_message_id=reply["id"],
                        sender=reply["sender"],
                        subject=reply["subject"],
                        snippet=reply["snippet"],
                    )
                )
                session.add(
                    Outcome(
                        job_id=outbound.job_id,
                        stage=_stage_from_reply(f"{reply['subject']} {reply['snippet']}"),
                        source="gmail_reply",
                        evidence={"provider_message_id": reply["id"], "outreach_message_id": outbound.id, "snippet": reply["snippet"]},
                    )
                )
                outcomes += 1
        session.commit()
        return {"threads_checked": len(messages), "replies_seen": processed, "outcomes_created": outcomes}
