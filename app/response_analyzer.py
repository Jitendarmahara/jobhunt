"""Response Analyzer: normalize inbound messages -> intent -> conversation state.

Today only Gmail is a real, credentialed channel (``GmailClient``); LinkedIn,
X, and SMS are structural interfaces only (see ``app.integrations.channels``)
until those integrations exist. Every newly-seen inbound message is:

1. **normalized** into an ``InboundEmail`` row (append-only, deduped by the
   provider's message id),
2. **classified** into an intent (``OutcomeStage``, via ``_stage_from_reply``)
   and recorded as an ``Outcome`` (append-only evidence), and
3. reflected in the durable **conversation state** (``ConversationState``,
   one row per job, upserted) and the job's lifecycle
   (``TRACKING -> RESPONSE_RECEIVED -> RESPONSE_ANALYZED``, then
   ``-> ACTION_PLANNED -> ACTION_EXECUTED -> TRACKING`` when the intent needs
   a human — this never auto-replies; it raises an ``Escalation`` instead).
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import emit
from app.integrations.gmail import GmailClient
from app.lifecycle import LifecycleState, record_transition
from app.models import ConversationState, Escalation, InboundEmail, Job, Outcome, OutcomeStage, OutreachMessage

# Intents that warrant a human's attention rather than silent tracking.
ACTIONABLE_STAGES = frozenset({OutcomeStage.INTERVIEW, OutcomeStage.POSITIVE_REPLY, OutcomeStage.OFFER})


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
    def _advance_conversation(self, session: Session, job_id: str, channel: str, stage: OutcomeStage, snippet: str) -> None:
        """Persist the conversation-state read model and drive the job's lifecycle.

        Best-effort: a job that never reached TRACKING (e.g. live actions were
        off, so it was never actually applied to) has nothing to advance —
        the conversation state is still recorded for visibility, but the
        lifecycle transition is skipped rather than raised as an error.
        """
        state = session.scalar(select(ConversationState).where(ConversationState.job_id == job_id))
        action_required = stage in ACTIONABLE_STAGES
        now = datetime.now(timezone.utc)
        if state:
            state.channel = channel
            state.stage = stage
            state.action_required = action_required
            state.last_snippet = snippet[:2000]
            state.last_message_at = now
        else:
            session.add(
                ConversationState(
                    job_id=job_id, channel=channel, stage=stage, action_required=action_required,
                    last_snippet=snippet[:2000], last_message_at=now,
                )
            )
        session.commit()

        job = session.get(Job, job_id)
        if not job or job.lifecycle_state != LifecycleState.TRACKING:
            return  # Nothing to advance: never applied live, or a prior action is still in flight.

        record_transition(session, job, LifecycleState.RESPONSE_RECEIVED, actor="response_analyzer")
        record_transition(
            session, job, LifecycleState.RESPONSE_ANALYZED, actor="response_analyzer",
            reason=f"intent={stage.value}",
        )
        emit(f"Response Analyzer: classified reply as {stage.value}", job_id=job_id, detail={"channel": channel})
        if not action_required:
            record_transition(session, job, LifecycleState.TRACKING, actor="response_analyzer")
            return

        record_transition(session, job, LifecycleState.ACTION_PLANNED, actor="response_analyzer", reason=stage.value)
        session.add(
            Escalation(
                job_id=job_id,
                category="response_action",
                question=f"Candidate reply classified as '{stage.value}' — review and respond.",
                context={"channel": channel, "snippet": snippet[:500]},
            )
        )
        session.commit()
        record_transition(
            session, job, LifecycleState.ACTION_EXECUTED, actor="response_analyzer",
            reason="escalated_for_human_response",
        )
        record_transition(session, job, LifecycleState.TRACKING, actor="response_analyzer")

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
                snippet = f"{reply['subject']} {reply['snippet']}"
                stage = _stage_from_reply(snippet)
                session.add(
                    Outcome(
                        job_id=outbound.job_id,
                        stage=stage,
                        source="gmail_reply",
                        evidence={"provider_message_id": reply["id"], "outreach_message_id": outbound.id, "snippet": reply["snippet"]},
                    )
                )
                session.commit()
                outcomes += 1
                self._advance_conversation(session, outbound.job_id, "gmail", stage, snippet)
        return {"threads_checked": len(messages), "replies_seen": processed, "outcomes_created": outcomes}
