import os
import unittest

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"

from app.models import OutcomeStage
from app.response_analyzer import _stage_from_reply


class ResponseAnalyzerTests(unittest.TestCase):
    def test_classifies_delayed_outcome_signals_conservatively(self):
        self.assertEqual(_stage_from_reply("Could we schedule an interview next week?"), OutcomeStage.INTERVIEW)
        self.assertEqual(_stage_from_reply("Unfortunately we are not moving forward."), OutcomeStage.REJECTED)
        self.assertEqual(_stage_from_reply("We are interested in learning more."), OutcomeStage.POSITIVE_REPLY)
        self.assertEqual(_stage_from_reply("Thanks for reaching out."), OutcomeStage.REPLIED)

