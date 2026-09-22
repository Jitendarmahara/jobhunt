"""Project Builder unit tests.

Exercises the PROJECT_PLANNING -> PROJECT_BUILDING -> PROJECT_TESTING ->
PROJECT_REVIEW -> PROJECT_PUBLISHED -> EVIDENCE_UPDATED branch that
``build_project_activity`` drives when the gap analysis recommends a
demonstrator project. Runs offline against a file-backed SQLite database, no
Temporal server or model required (the gap-analysis artifact is seeded
directly so the test is deterministic regardless of model availability).
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
os.environ["ARTIFACT_DIR"] = tempfile.mkdtemp(prefix="career-art-project-")

from sqlalchemy import create_engine, select

from app import activities
from app import db as appdb
from app.bootstrap import bootstrap_agent_specs
from app.config import get_settings
from app.lifecycle import LifecycleState, record_transition
from app.models import Artifact, Job, JobSource, JobTransition
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


class ProjectBuilderTests(unittest.TestCase):
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
        # This suite must never make a real network call: force GITHUB_TOKEN off
        # for the activity regardless of any ambient token in the process
        # environment (e.g. one exported for unrelated git/gh tooling in the
        # host sandbox) — publishing must only ever be opt-in via real operator
        # configuration, never accidental.
        no_github_token = get_settings().model_copy(update={"github_token": None})
        patcher = patch("app.activities.get_settings", return_value=no_github_token)
        patcher.start()
        self.addCleanup(patcher.stop)

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
                description="Build Python services with Postgres and Docker. Kubernetes experience required.",
                fingerprint="project-1",
            )
            session.add(job)
            session.commit()
            self.job_id = job.id
        finally:
            session.close()

    def _advance_to_evidence_analyzed(self):
        run(activities.mark_normalized_activity(self.job_id))
        run(activities.qualify_job_activity(self.job_id))
        run(activities.match_candidate_activity(self.job_id))

    def _seed_gaps(self, *, project_recommended: bool):
        session = appdb.SessionLocal()
        try:
            session.add(
                Artifact(
                    job_id=self.job_id,
                    kind="resume_gaps",
                    uri=f"inline://resume_gaps/{self.job_id}",
                    content_hash="test",
                    provenance={
                        "strengths": ["Python", "Docker"],
                        "gaps": ["Kubernetes"],
                        "project_recommended": project_recommended,
                        "project_idea": "A small Kubernetes-deployed service demonstrating orchestration basics.",
                    },
                )
            )
            session.commit()
        finally:
            session.close()

    def _state_and_path(self):
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

    def test_skips_when_no_project_recommended(self):
        self._advance_to_evidence_analyzed()
        self._seed_gaps(project_recommended=False)

        result = run(activities.build_project_activity(self.job_id))

        self.assertEqual(result["status"], "skipped")
        state, path = self._state_and_path()
        self.assertEqual(state, S.EVIDENCE_ANALYZED)
        self.assertNotIn(S.PROJECT_PLANNING, path)

    def test_skips_when_no_gap_analysis_exists_yet(self):
        self._advance_to_evidence_analyzed()

        result = run(activities.build_project_activity(self.job_id))

        self.assertEqual(result["status"], "skipped")

    def test_builds_real_scaffold_and_walks_project_states(self):
        self._advance_to_evidence_analyzed()
        self._seed_gaps(project_recommended=True)

        result = run(activities.build_project_activity(self.job_id))

        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(result["artifact_id"])
        self.assertFalse(result["published"], "no GITHUB_TOKEN => must never publish externally")
        self.assertTrue(result["uri"].startswith("file://"))

        state, path = self._state_and_path()
        self.assertEqual(state, S.EVIDENCE_UPDATED)
        for expected in (
            S.PROJECT_PLANNING,
            S.PROJECT_BUILDING,
            S.PROJECT_TESTING,
            S.PROJECT_REVIEW,
            S.PROJECT_PUBLISHED,
            S.EVIDENCE_UPDATED,
        ):
            self.assertIn(expected, path)

        session = appdb.SessionLocal()
        try:
            artifact = session.scalar(select(Artifact).where(Artifact.job_id == self.job_id, Artifact.kind == "project_scaffold"))
            self.assertIsNotNone(artifact)
            self.assertIn("focus_areas", artifact.provenance)
            self.assertFalse(artifact.provenance["published_private"])
        finally:
            session.close()

        # The scaffold is real, non-fabricated content on disk.
        from pathlib import Path
        from urllib.parse import urlparse

        project_dir = Path(urlparse(artifact.uri).path)
        self.assertTrue((project_dir / "README.md").exists())
        self.assertTrue((project_dir / "tests" / "test_evidence.py").exists())
        readme = (project_dir / "README.md").read_text()
        self.assertIn("not a claim of prior employment", readme)

    def test_idempotent_reuses_existing_scaffold_artifact(self):
        self._advance_to_evidence_analyzed()
        self._seed_gaps(project_recommended=True)

        first = run(activities.build_project_activity(self.job_id))
        second = run(activities.build_project_activity(self.job_id))

        self.assertEqual(first["artifact_id"], second["artifact_id"])
        self.assertTrue(second.get("cached"))
        session = appdb.SessionLocal()
        try:
            count = session.query(Artifact).filter(Artifact.job_id == self.job_id, Artifact.kind == "project_scaffold").count()
            self.assertEqual(count, 1)
        finally:
            session.close()

    def test_publishing_requires_both_github_token_and_live_policy(self):
        self._advance_to_evidence_analyzed()
        self._seed_gaps(project_recommended=True)

        # Live actions armed but no GITHUB_TOKEN => still must not attempt to publish.
        session = appdb.SessionLocal()
        try:
            PolicyService.update(session, LivePolicy(enabled=True, daily_application_limit=5, daily_outreach_limit=5))
        finally:
            session.close()

        result = run(activities.build_project_activity(self.job_id))
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["published"], "GITHUB_TOKEN is required even when live actions are armed")

    def test_publishes_only_when_both_gates_armed_and_never_touches_the_real_network(self):
        self._advance_to_evidence_analyzed()
        self._seed_gaps(project_recommended=True)

        session = appdb.SessionLocal()
        try:
            PolicyService.update(session, LivePolicy(enabled=True, daily_application_limit=5, daily_outreach_limit=5))
        finally:
            session.close()

        armed_settings = get_settings().model_copy(update={"github_token": "test-token", "github_org": None})
        with (
            patch("app.activities.get_settings", return_value=armed_settings),
            patch("app.projects.GitHubPublisher.publish_private", return_value="https://github.test/fake/repo") as fake_publish,
        ):
            result = run(activities.build_project_activity(self.job_id))

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["published"])
        fake_publish.assert_called_once()
        self.assertEqual(result["uri"], "https://github.test/fake/repo")


if __name__ == "__main__":
    unittest.main()
