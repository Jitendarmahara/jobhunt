"""Job Applier outreach unit tests.

``prepare_outreach_activity`` drafts an outreach message from the Response
Builder's ``contacts`` artifact once an application is verified as submitted.
It must never fabricate a real recipient and must never itself place a live
send — sending stays a deliberate, separately-gated action (see
``send_outreach_activity`` / the outreach API+workflow). These tests run
offline against a file-backed SQLite database; no Temporal server, model, or
network access is required or permitted.
"""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ["MODEL_BASE_URL"] = ""
os.environ["MODEL_NAME"] = ""
os.environ["MODEL_API_KEY"] = ""
os.environ["ARTIFACT_DIR"] = tempfile.mkdtemp(prefix="career-art-outreach-")

from sqlalchemy import create_engine

from app import activities
from app import db as appdb
from app.bootstrap import bootstrap_agent_specs
from app.lifecycle import LifecycleState, record_transition
from app.models import Artifact, Job, JobSource, JobTransition, OutreachMessage
from app.safety import LivePolicy
from app.services import CandidateProfileService, PolicyService

S = LifecycleState


def run(coro):
    return asyncio.run(coro)


PROFILE_FACTS = {
    "name": "Test Candidate",
    "email": "candidate@example.test",
    "phone": "+1-555-0100",
    "summary": "Python backend engineer building services with Postgres and Docker.",
    "skills": ["Python", "Postgres", "Docker", "FastAPI"],
    "projects": ["A backend API service"],
    "application_answers": {"first_name": "Test", "last_name": "Candidate", "email": "candidate@example.test", "phone": "+1-555-0100"},
}


class OutreachTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.tmp.close()
        cls.engine = create_engine(
            f"sqlite+pysqlite:///{cls.tmp.name}", connect_args={"check_same_thread": False}
        )
        cls._orig_engine = appdb.engine
        appdb.SessionLocal.configure(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        appdb.SessionLocal.configure(bind=cls._orig_engine)
        os.unlink(cls.tmp.name)

    def setUp(self):
        appdb.Base.metadata.drop_all(type(self).engine)
        appdb.Base.metadata.create_all(type(self).engine)
        session = appdb.SessionLocal()
        try:
            bootstrap_agent_specs(session)
            CandidateProfileService.create(session, PROFILE_FACTS, approve=True)
            source = JobSource(provider="greenhouse", name="Example", board_url="https://boards.greenhouse.io/example")
            session.add(source)
            session.commit()
            job = Job(
                source_id=source.id,
                provider_job_id="1",
                url="https://example.test/job/1",
                company="Example",
                title="Python Backend Engineer",
                description="Build Python services with Postgres and Docker.",
                fingerprint="outreach-1",
            )
            session.add(job)
            session.commit()
            self.job_id = job.id
        finally:
            session.close()

    def _seed_contacts(self):
        session = appdb.SessionLocal()
        try:
            session.add(
                Artifact(
                    job_id=self.job_id,
                    kind="contacts",
                    uri=f"inline://contacts/{self.job_id}",
                    content_hash="test",
                    provenance={
                        "target_roles": ["Engineering Manager", "Recruiter"],
                        "outreach_angle": "Directly relevant Python/Docker experience for this role.",
                        "search_queries": ["Example Engineering Manager"],
                    },
                )
            )
            session.commit()
        finally:
            session.close()

    def _force_state(self, state):
        session = appdb.SessionLocal()
        try:
            job = session.get(Job, self.job_id)
            record_transition(session, job, state, actor="test")
        finally:
            session.close()

    def _path(self):
        session = appdb.SessionLocal()
        try:
            return [
                row.to_state
                for row in session.query(JobTransition)
                .filter(JobTransition.job_id == self.job_id)
                .order_by(JobTransition.created_at.asc(), JobTransition.id.asc())
                .all()
            ]
        finally:
            session.close()

    def _advance_to_application_verified(self):
        for state in (
            S.NORMALIZED, S.QUALIFYING, S.ANALYZED, S.CANDIDATE_MATCHED, S.EVIDENCE_ANALYZED,
            S.APPLICATION_PREPARATION, S.RESUME_GENERATED, S.APPLICATION_READY,
            S.APPLICATION_EXECUTING, S.APPLICATION_VERIFIED,
        ):
            self._force_state(state)

    def test_skips_when_no_contacts_suggested(self):
        self._advance_to_application_verified()

        result = run(activities.prepare_outreach_activity(self.job_id))

        self.assertEqual(result["status"], "skipped")
        session = appdb.SessionLocal()
        try:
            self.assertEqual(session.query(OutreachMessage).count(), 0)
        finally:
            session.close()
        self.assertIn(S.TRACKING, self._path())

    def test_drafts_outreach_but_never_sends_when_policy_off(self):
        self._advance_to_application_verified()
        self._seed_contacts()

        with patch("app.integrations.gmail.GmailClient.send") as fake_send:
            result = run(activities.prepare_outreach_activity(self.job_id))

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["attempt_send"], "outreach must be off by default")
        fake_send.assert_not_called()

        session = appdb.SessionLocal()
        try:
            message = session.get(OutreachMessage, result["outreach_id"])
            self.assertEqual(message.status, "draft")
            self.assertEqual(message.recipient, "pending-human-verification")
        finally:
            session.close()

        path = self._path()
        self.assertIn(S.OUTREACH_EXECUTING, path)
        self.assertIn(S.TRACKING, path)

    def test_armed_policy_marks_ready_but_still_never_sends_without_a_verified_recipient(self):
        self._advance_to_application_verified()
        self._seed_contacts()

        session = appdb.SessionLocal()
        try:
            PolicyService.update(session, LivePolicy(enabled=True, daily_application_limit=5, daily_outreach_limit=5))
        finally:
            session.close()

        with patch("app.integrations.gmail.GmailClient.send") as fake_send:
            result = run(activities.prepare_outreach_activity(self.job_id))

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["attempt_send"], "policy is armed, so readiness should reflect that")
        fake_send.assert_not_called()
        session = appdb.SessionLocal()
        try:
            message = session.get(OutreachMessage, result["outreach_id"])
            self.assertEqual(message.recipient, "pending-human-verification", "never fabricate a real recipient")
            self.assertEqual(message.status, "draft")
        finally:
            session.close()

    def test_idempotent_does_not_create_a_second_draft(self):
        self._advance_to_application_verified()
        self._seed_contacts()

        first = run(activities.prepare_outreach_activity(self.job_id))
        second = run(activities.prepare_outreach_activity(self.job_id))

        self.assertEqual(first["outreach_id"], second["outreach_id"])
        self.assertTrue(second.get("cached"))
        session = appdb.SessionLocal()
        try:
            self.assertEqual(session.query(OutreachMessage).count(), 1)
        finally:
            session.close()

    def test_submitted_application_chains_into_outreach_draft(self):
        """End-to-end wiring: a verified submission drafts outreach and reaches TRACKING."""
        for state in (
            S.NORMALIZED, S.QUALIFYING, S.ANALYZED, S.CANDIDATE_MATCHED, S.EVIDENCE_ANALYZED,
            S.APPLICATION_PREPARATION, S.RESUME_GENERATED, S.APPLICATION_READY,
        ):
            self._force_state(state)
        self._seed_contacts()

        session = appdb.SessionLocal()
        try:
            from app.models import AgentSpec, Application, ApplicationStatus, Domain
            spec = session.query(AgentSpec).filter(AgentSpec.domain == Domain.APPLICATION).first()
            application = Application(
                job_id=self.job_id, agent_spec_id=spec.id, idempotency_key=f"test:{self.job_id}",
                status=ApplicationStatus.READY,
            )
            session.add(application)
            session.commit()
            application_id = application.id
        finally:
            session.close()

        with patch("app.activities.execute_application_browser_activity", return_value={"status": "submitted", "evidence_uri": "file:///tmp/x.png"}):
            result = run(activities.lifecycle_submit_application_activity(self.job_id, application_id))

        self.assertEqual(result["status"], "submitted")
        session = appdb.SessionLocal()
        try:
            job = session.get(Job, self.job_id)
            self.assertEqual(job.lifecycle_state, S.TRACKING)
            self.assertEqual(session.query(OutreachMessage).filter(OutreachMessage.job_id == self.job_id).count(), 1)
        finally:
            session.close()
        self.assertIn(S.OUTREACH_EXECUTING, self._path())


if __name__ == "__main__":
    unittest.main()
