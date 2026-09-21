import os
import unittest

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["MODEL_BASE_URL"] = ""
os.environ["MODEL_NAME"] = ""
os.environ["MODEL_API_KEY"] = ""

from app.db import Base, SessionLocal, engine, initialize_database
from app.lifecycle import InvalidTransition, record_transition, transition_exists
from app.models import Job, JobSource, JobStatus, JobTransition, LifecycleState

S = LifecycleState


class LifecycleStateMachineTests(unittest.TestCase):
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
            source_id=source.id,
            provider_job_id="1",
            url="https://example.test/1",
            company="X",
            title="SWE",
            description="python",
            fingerprint="fp-1",
        )
        self.session.add(self.job)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def _walk(self, states):
        for state in states:
            record_transition(self.session, self.job, state, actor="test")

    def test_happy_transition_records_row_and_updates_state(self):
        transition = record_transition(self.session, self.job, S.NORMALIZED, actor="discovery")
        self.assertIsNotNone(transition)
        self.assertEqual(self.job.lifecycle_state, S.NORMALIZED)
        rows = self.session.query(JobTransition).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].from_state, S.DISCOVERED)
        self.assertEqual(rows[0].to_state, S.NORMALIZED)
        self.assertEqual(rows[0].actor, "discovery")

    def test_coarse_status_projection(self):
        self.assertEqual(self.job.status, JobStatus.DISCOVERED)
        self._walk([S.NORMALIZED, S.QUALIFYING, S.ANALYZED])
        self.assertEqual(self.job.status, JobStatus.QUALIFIED)
        self._walk([S.CANDIDATE_MATCHED, S.EVIDENCE_ANALYZED, S.APPLICATION_PREPARATION, S.RESUME_GENERATED])
        self.assertEqual(self.job.status, JobStatus.PREPARED)
        self._walk([S.APPLICATION_READY, S.APPLICATION_EXECUTING, S.APPLICATION_VERIFIED])
        self.assertEqual(self.job.status, JobStatus.APPLIED)

    def test_idempotency_key_makes_transition_a_noop(self):
        first = record_transition(self.session, self.job, S.NORMALIZED, actor="t", idempotency_key="k1")
        # Same key, different target: must return the original and not advance.
        second = record_transition(self.session, self.job, S.QUALIFYING, actor="t", idempotency_key="k1")
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.job.lifecycle_state, S.NORMALIZED)
        self.assertEqual(self.session.query(JobTransition).count(), 1)
        self.assertTrue(transition_exists(self.session, "k1"))

    def test_move_to_current_state_is_noop(self):
        record_transition(self.session, self.job, S.NORMALIZED, actor="t")
        self.assertIsNone(record_transition(self.session, self.job, S.NORMALIZED, actor="t"))

    def test_illegal_transition_raises(self):
        with self.assertRaises(InvalidTransition):
            record_transition(self.session, self.job, S.APPLICATION_VERIFIED, actor="t")

    def test_skip_branch_from_qualifying(self):
        self._walk([S.NORMALIZED, S.QUALIFYING])
        record_transition(self.session, self.job, S.SKIPPED, actor="job_finder", reason="below threshold")
        self.assertEqual(self.job.lifecycle_state, S.SKIPPED)
        self.assertEqual(self.job.status, JobStatus.SKIPPED)


if __name__ == "__main__":
    unittest.main()
