"""Response Analyzer unit tests: intent classification + conversation state.

``ingest_gmail`` normalizes inbound Gmail replies into an ``InboundEmail`` +
``Outcome`` (append-only), then upserts a per-job ``ConversationState`` and
drives the durable lifecycle
(``TRACKING -> RESPONSE_RECEIVED -> RESPONSE_ANALYZED`` and, for an
actionable intent, on through ``ACTION_PLANNED -> ACTION_EXECUTED`` back to
``TRACKING``, raising an ``Escalation`` rather than ever auto-replying). This
runs offline with ``GmailClient.replies_for_message`` mocked — no network,
model, or Temporal server required.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["MODEL_BASE_URL"] = ""
os.environ["MODEL_NAME"] = ""
os.environ["MODEL_API_KEY"] = ""

from app.db import Base, SessionLocal, engine, initialize_database
from app.lifecycle import LifecycleState, record_transition
from app.models import (
    ConversationState,
    Escalation,
    Job,
    JobSource,
    JobTransition,
    Outcome,
    OutcomeStage,
    OutreachMessage,
)
from app.response_analyzer import ResponseAnalyzer, _stage_from_reply

S = LifecycleState


class IntentClassificationTests(unittest.TestCase):
    def test_classifies_delayed_outcome_signals_conservatively(self):
        self.assertEqual(_stage_from_reply("Could we schedule an interview next week?"), OutcomeStage.INTERVIEW)
        self.assertEqual(_stage_from_reply("Unfortunately we are not moving forward."), OutcomeStage.REJECTED)
        self.assertEqual(_stage_from_reply("We are interested in learning more."), OutcomeStage.POSITIVE_REPLY)
        self.assertEqual(_stage_from_reply("Thanks for reaching out."), OutcomeStage.REPLIED)


class ResponseAnalyzerFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        initialize_database()

    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.session = SessionLocal()
        source = JobSource(provider="greenhouse", name="X", board_url="https://boards.greenhouse.io/x")
        self.session.add(source)
        self.session.commit()
        self.job = Job(
            source_id=source.id, provider_job_id="1", url="https://example.test/1",
            company="X", title="SWE", description="python", fingerprint="fp-response-1",
        )
        self.session.add(self.job)
        self.session.commit()
        self.message = OutreachMessage(
            job_id=self.job.id, agent_spec_id=self._dummy_spec_id(), recipient="hiring@example.test",
            subject="Following up", body="body", status="sent", provider_message_id="gmail-msg-1",
        )
        self.session.add(self.message)
        self.session.commit()

    def _dummy_spec_id(self):
        from app.bootstrap import bootstrap_agent_specs
        from app.models import AgentSpec, Domain

        bootstrap_agent_specs(self.session)
        return self.session.query(AgentSpec).filter(AgentSpec.domain == Domain.APPLICATION).first().id

    def tearDown(self):
        self.session.close()

    def _walk_to_tracking(self):
        for state in (
            S.NORMALIZED, S.QUALIFYING, S.ANALYZED, S.CANDIDATE_MATCHED, S.EVIDENCE_ANALYZED,
            S.APPLICATION_PREPARATION, S.RESUME_GENERATED, S.APPLICATION_READY,
            S.APPLICATION_EXECUTING, S.APPLICATION_VERIFIED, S.TRACKING,
        ):
            record_transition(self.session, self.job, state, actor="test")

    def _path(self):
        return [
            row.to_state
            for row in self.session.query(JobTransition)
            .filter(JobTransition.job_id == self.job.id)
            .order_by(JobTransition.created_at.asc(), JobTransition.id.asc())
            .all()
        ]

    def _ingest(self, replies):
        with patch("app.response_analyzer.GmailClient.replies_for_message", return_value=replies):
            return ResponseAnalyzer().ingest_gmail(self.session)

    def test_non_actionable_reply_returns_straight_to_tracking(self):
        self._walk_to_tracking()

        result = self._ingest([{"id": "r1", "sender": "hr@example.test", "subject": "Thanks", "snippet": "Thanks for reaching out."}])

        self.assertEqual(result, {"threads_checked": 1, "replies_seen": 1, "outcomes_created": 1})
        self.session.refresh(self.job)
        self.assertEqual(self.job.lifecycle_state, S.TRACKING)
        path = self._path()
        self.assertIn(S.RESPONSE_RECEIVED, path)
        self.assertIn(S.RESPONSE_ANALYZED, path)
        self.assertNotIn(S.ACTION_PLANNED, path)

        state = self.session.query(ConversationState).filter(ConversationState.job_id == self.job.id).one()
        self.assertEqual(state.channel, "gmail")
        self.assertEqual(state.stage, OutcomeStage.REPLIED)
        self.assertFalse(state.action_required)

        self.assertEqual(self.session.query(Outcome).count(), 1)
        self.assertEqual(self.session.query(Escalation).count(), 0)

    def test_actionable_reply_escalates_and_never_auto_replies(self):
        self._walk_to_tracking()

        result = self._ingest([{"id": "r2", "sender": "hr@example.test", "subject": "Interview", "snippet": "Could we schedule an interview?"}])

        self.assertEqual(result["outcomes_created"], 1)
        self.session.refresh(self.job)
        self.assertEqual(self.job.lifecycle_state, S.TRACKING)
        path = self._path()
        for expected in (S.RESPONSE_RECEIVED, S.RESPONSE_ANALYZED, S.ACTION_PLANNED, S.ACTION_EXECUTED):
            self.assertIn(expected, path)

        state = self.session.query(ConversationState).filter(ConversationState.job_id == self.job.id).one()
        self.assertEqual(state.stage, OutcomeStage.INTERVIEW)
        self.assertTrue(state.action_required)

        escalations = self.session.query(Escalation).all()
        self.assertEqual(len(escalations), 1)
        self.assertEqual(escalations[0].category, "response_action")
        self.assertIn("interview", escalations[0].question.lower())

    def test_conversation_state_recorded_even_when_job_never_reached_tracking(self):
        """Off by default: no live application submission => nothing to advance in the lifecycle,
        but the conversation is still recorded for visibility."""
        result = self._ingest([{"id": "r3", "sender": "hr@example.test", "subject": "Hi", "snippet": "Thanks for reaching out."}])

        self.assertEqual(result["outcomes_created"], 1)
        self.session.refresh(self.job)
        self.assertEqual(self.job.lifecycle_state, S.DISCOVERED)
        state = self.session.query(ConversationState).filter(ConversationState.job_id == self.job.id).one()
        self.assertEqual(state.stage, OutcomeStage.REPLIED)

    def test_duplicate_reply_is_not_reprocessed(self):
        self._walk_to_tracking()
        reply = {"id": "r4", "sender": "hr@example.test", "subject": "Thanks", "snippet": "Thanks for reaching out."}

        first = self._ingest([reply])
        second = self._ingest([reply])

        self.assertEqual(first["outcomes_created"], 1)
        self.assertEqual(second["outcomes_created"], 0)
        self.assertEqual(self.session.query(Outcome).count(), 1)
        self.assertEqual(self.session.query(ConversationState).count(), 1)


if __name__ == "__main__":
    unittest.main()
