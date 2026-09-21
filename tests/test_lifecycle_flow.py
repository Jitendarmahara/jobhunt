"""End-to-end autonomous flow test.

Runs the real lifecycle *activities* in the same order the ``JobLifecycleWorkflow``
sequences them, against a shared file-backed SQLite database (so each activity's
own ``SessionLocal()`` sees the same data). This proves a job advances through the
pipeline on its own and stops safely at APPLICATION_READY while live actions are
off — no Temporal server required.
"""

import asyncio
import os
import tempfile
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ["MODEL_BASE_URL"] = ""
os.environ["MODEL_NAME"] = ""
os.environ["MODEL_API_KEY"] = ""
os.environ["ARTIFACT_DIR"] = tempfile.mkdtemp(prefix="career-art-")

from sqlalchemy import create_engine

from app import activities
from app import db as appdb
from app.bootstrap import bootstrap_agent_specs
from app.lifecycle import LifecycleState, record_transition
from app.models import Application, Job, JobSource, JobTransition
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


class AutonomousFlowTests(unittest.TestCase):
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
                description="Build Python services with Postgres and Docker. FastAPI experience preferred.",
                fingerprint="flow-1",
            )
            session.add(job)
            session.commit()
            self.job_id = job.id
        finally:
            session.close()

    def _snapshot(self):
        session = appdb.SessionLocal()
        try:
            job = session.get(Job, self.job_id)
            path = [
                row.to_state
                for row in session.query(JobTransition)
                .filter(JobTransition.job_id == self.job_id)
                .order_by(JobTransition.created_at.asc(), JobTransition.id.asc())
                .all()
            ]
            return job.lifecycle_state, path
        finally:
            session.close()

    def _drive_to_ready(self):
        run(activities.mark_normalized_activity(self.job_id))
        qualified = run(activities.qualify_job_activity(self.job_id))
        run(activities.match_candidate_activity(self.job_id))
        resume = run(activities.generate_resume_activity(self.job_id))
        application = run(activities.create_application_activity(self.job_id, resume.get("resume_artifact_id")))
        return qualified, resume, application

    def test_pipeline_runs_autonomously_and_stops_safely_when_live_off(self):
        qualified, resume, application = self._drive_to_ready()

        self.assertTrue(qualified["qualified"], f"expected qualification, got {qualified}")
        self.assertEqual(resume["status"], "ok")
        self.assertIsNotNone(resume["resume_artifact_id"])
        self.assertEqual(application["status"], "ok")
        self.assertFalse(application["live_submit"], "live actions off => must not submit externally")

        state, path = self._snapshot()
        self.assertEqual(state, S.APPLICATION_READY)
        for expected in (S.NORMALIZED, S.ANALYZED, S.EVIDENCE_ANALYZED, S.RESUME_GENERATED, S.APPLICATION_READY):
            self.assertIn(expected, path)

        session = appdb.SessionLocal()
        try:
            self.assertEqual(session.query(Application).count(), 1)
        finally:
            session.close()

    def test_live_submit_permitted_only_when_policy_armed(self):
        session = appdb.SessionLocal()
        try:
            PolicyService.update(session, LivePolicy(enabled=True, daily_application_limit=5, daily_outreach_limit=5))
        finally:
            session.close()

        _, resume, application = self._drive_to_ready()
        self.assertEqual(application["status"], "ok")
        self.assertTrue(application["live_submit"], "armed policy => live submit permitted")

    def test_qualify_activity_is_idempotent(self):
        run(activities.mark_normalized_activity(self.job_id))
        first = run(activities.qualify_job_activity(self.job_id))
        second = run(activities.qualify_job_activity(self.job_id))
        self.assertTrue(first["qualified"])
        self.assertTrue(second["qualified"])
        self.assertTrue(second.get("cached"))
        # No duplicate ANALYZED transition.
        session = appdb.SessionLocal()
        try:
            analyzed = (
                session.query(JobTransition)
                .filter(JobTransition.job_id == self.job_id, JobTransition.to_state == S.ANALYZED)
                .count()
            )
            self.assertEqual(analyzed, 1)
        finally:
            session.close()

    def test_ambiguous_guard_never_resubmits(self):
        _, _, application = self._drive_to_ready()
        application_id = application["application_id"]
        # Simulate an in-flight submission: force the job into APPLICATION_EXECUTING.
        session = appdb.SessionLocal()
        try:
            job = session.get(Job, self.job_id)
            record_transition(session, job, S.APPLICATION_EXECUTING, actor="test")
        finally:
            session.close()

        result = run(activities.lifecycle_submit_application_activity(self.job_id, application_id))
        self.assertEqual(result["status"], "ambiguous")
        state, _ = self._snapshot()
        self.assertEqual(state, S.TRACKING)


if __name__ == "__main__":
    unittest.main()
