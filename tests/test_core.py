import os
import tempfile
import unittest
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
# Keep tests hermetic from a local .env: never call a live model endpoint.
os.environ["MODEL_BASE_URL"] = ""
os.environ["MODEL_NAME"] = ""
os.environ["MODEL_API_KEY"] = ""

from app.bootstrap import bootstrap_agent_specs
from app.db import Base, SessionLocal, engine, initialize_database
from app.evolution import EvolutionService, InsufficientEvidenceError
from app.models import AgentSpec, Assessment, Domain, Job, JobSource, SpecStatus
from app.safety import LivePolicy, PolicyBlockedError, enforce_application_policy
from app.qualification import QualificationService
from app.resumes import ResumeService
from app.services import AgentSpecService, CandidateProfileService, ExperimentService


class CoreSystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        initialize_database()

    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.session = SessionLocal()
        bootstrap_agent_specs(self.session)

    def tearDown(self):
        self.session.close()

    def test_bootstrap_creates_champion_and_challengers_for_every_domain(self):
        specs = self.session.query(AgentSpec).all()
        self.assertEqual(len(specs), 9)
        for domain in Domain:
            population = [spec for spec in specs if spec.domain == domain]
            self.assertEqual(len(population), 3)
            self.assertEqual(sum(spec.status == SpecStatus.CHAMPION for spec in population), 1)

    def test_experiment_assignment_is_reproducible_and_records_propensity(self):
        finder_specs = self.session.query(AgentSpec).filter(AgentSpec.domain == Domain.JOB_FINDER).all()
        source = JobSource(provider="greenhouse", name="Example", board_url="https://boards.greenhouse.io/example")
        self.session.add(source)
        self.session.commit()
        job = Job(
            source_id=source.id,
            provider_job_id="1",
            url="https://example.test/job/1",
            company="Example",
            title="Software Engineer",
            description="Python",
            fingerprint="job-1",
        )
        self.session.add(job)
        self.session.commit()
        experiment = ExperimentService.create(self.session, Domain.JOB_FINDER, "finder eval", [spec.id for spec in finder_specs], 0.2)
        first = ExperimentService.assign(self.session, experiment, job)
        second = ExperimentService.assign(self.session, experiment, job)
        self.assertEqual(first.id, second.id)
        self.assertAlmostEqual(first.propensity, 1 / 3, places=5)

    def test_policy_blocks_external_submission_when_cap_is_zero(self):
        with self.assertRaises(PolicyBlockedError):
            enforce_application_policy(self.session, LivePolicy(enabled=True, daily_application_limit=0, daily_outreach_limit=10))

    def test_evolution_creates_immutable_child_only_after_audits(self):
        champion = self.session.query(AgentSpec).filter(AgentSpec.domain == Domain.JOB_FINDER, AgentSpec.status == SpecStatus.CHAMPION).one()
        with self.assertRaises(InsufficientEvidenceError):
            EvolutionService().run(self.session, Domain.JOB_FINDER, minimum_audits=1)
        source = JobSource(provider="lever", name="Example", board_url="https://jobs.lever.co/example")
        self.session.add(source)
        self.session.commit()
        for index in range(3):
            job = Job(
                source_id=source.id,
                provider_job_id=str(index),
                url=f"https://example.test/job/{index}",
                company="Example",
                title="Backend Engineer",
                description="Python distributed systems",
                fingerprint=f"evo-{index}",
            )
            self.session.add(job)
            self.session.flush()
            self.session.add(
                Assessment(
                    job_id=job.id,
                    agent_spec_id=champion.id,
                    relevance_score=0.8,
                    rationale="Evidence is explicit.",
                    evidence={"skills": ["python"]},
                    audit_score=0.9,
                    audit_notes="High-quality match.",
                )
            )
        self.session.commit()
        child = EvolutionService().run(self.session, Domain.JOB_FINDER, minimum_audits=3)
        self.assertEqual(child.parent_spec_id, champion.id)
        self.assertEqual(child.generation, champion.generation + 1)
        self.assertEqual(child.status, SpecStatus.CHALLENGER)
        self.assertNotEqual(child.fingerprint, champion.fingerprint)

    def test_qualification_uses_approved_facts_when_no_model_is_configured(self):
        CandidateProfileService.create(self.session, {"skills": ["Python", "Postgres", "Docker"], "projects": ["backend API"]}, approve=True)
        source = JobSource(provider="greenhouse", name="Example", board_url="https://boards.greenhouse.io/example")
        self.session.add(source)
        self.session.commit()
        job = Job(
            source_id=source.id,
            provider_job_id="qualify-1",
            url="https://example.test/job/qualify-1",
            company="Example",
            title="Python Backend Engineer",
            description="Build Python services with Postgres and Docker.",
            fingerprint="qualify-1",
        )
        self.session.add(job)
        self.session.commit()
        spec = QualificationService().default_spec(self.session)
        assessment = QualificationService().assess(self.session, job, spec)
        self.assertGreater(assessment.relevance_score, 0.5)
        self.assertEqual(assessment.evidence["mode"], "heuristic_fallback")

    def test_resume_renderer_creates_a_pdf_artifact_from_approved_facts(self):
        CandidateProfileService.create(
            self.session,
            {"name": "Test Candidate", "email": "candidate@example.test", "skills": ["Python"], "experience": [{"title": "Engineer", "company": "Example", "bullets": ["Built a factual test system."]}]},
            approve=True,
        )
        source = JobSource(provider="lever", name="Example", board_url="https://jobs.lever.co/example")
        self.session.add(source)
        self.session.commit()
        job = Job(
            source_id=source.id,
            provider_job_id="resume-1",
            url="https://example.test/job/resume-1",
            company="Example",
            title="Software Engineer",
            description="Python service work",
            fingerprint="resume-1",
        )
        self.session.add(job)
        self.session.commit()
        spec = self.session.query(AgentSpec).filter(AgentSpec.domain == Domain.RESPONSE_BUILDER, AgentSpec.status == SpecStatus.CHAMPION).one()
        artifact = ResumeService().render(self.session, job, spec)
        resume_path = Path(artifact.uri.removeprefix("file://"))
        self.assertEqual(artifact.kind, "tailored_resume_pdf")
        self.assertTrue(resume_path.is_file())
        self.assertTrue(resume_path.read_bytes().startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
