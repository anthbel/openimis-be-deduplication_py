import logging
from unittest import mock

from django.test import TestCase

from deduplication.models import ScanState
from deduplication.services import run_scan
from deduplication.sources import Candidate, CandidateSource
from deduplication.sources.subject import subject_watermark
from deduplication.tests.helpers import LogInHelper
from deduplication.tests.test_candidates import _PairSource
from individual.models import Individual

QUOTED_VALUE = "Nduwimana 1984-03-02"


class _QuotingSource(CandidateSource):
    """Fails with an error whose text quotes a person's value."""

    kind = "quoting"

    def watermark(self):
        return subject_watermark()

    def scan(self, since):
        yield Candidate(
            subject_model="individual.Individual", subject_a="quoting-a", subject_b="quoting-b",
            kind=self.kind, score=None, evidence={},
        )
        raise LookupError(f"no template for {QUOTED_VALUE}")


class ScanFailureTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        Individual(first_name="Scan", last_name="Failure", dob="1980-01-01").save(username=cls.user.username)

    def _scan(self, *sources):
        with mock.patch("deduplication.services.registered_sources", return_value=list(sources)):
            return run_scan(actor=self.user)

    def test_a_failing_source_records_its_error_class_only_and_logs_the_message(self):
        with self.assertLogs("deduplication.services", level=logging.ERROR) as logs:
            self._scan(_QuotingSource(), _PairSource())

        state = ScanState.objects.get(kind="quoting")
        failure = state.summary["last_failure"]
        self.assertEqual(set(failure), {"at", "error_class"})
        self.assertEqual(failure["error_class"], "LookupError")
        self.assertNotIn(QUOTED_VALUE, str(state.summary))
        self.assertIsNone(state.updated_at)
        self.assertEqual(state.last_id, "")
        self.assertIsNone(state.last_scan_at)
        self.assertIn(QUOTED_VALUE, "\n".join(logs.output))
        self.assertNotIn("last_failure", ScanState.objects.get(kind="pairs").summary)

    def test_a_failure_keeps_the_cursor_of_an_earlier_completed_scan(self):
        self._scan(_PairSource())
        completed = ScanState.objects.get(kind="pairs")

        class _FailingPairs(_QuotingSource):
            kind = "pairs"

        with self.assertLogs("deduplication.services", level=logging.ERROR):
            self._scan(_FailingPairs())

        state = ScanState.objects.get(kind="pairs")
        self.assertEqual((state.updated_at, state.last_id, state.last_scan_at),
                         (completed.updated_at, completed.last_id, completed.last_scan_at))
        self.assertEqual(state.summary["count"], 1)
        self.assertEqual(state.summary["last_failure"]["error_class"], "LookupError")

    def test_a_completed_scan_clears_the_last_failure(self):
        class _FailingPairs(_QuotingSource):
            kind = "pairs"

        with self.assertLogs("deduplication.services", level=logging.ERROR):
            self._scan(_FailingPairs())
        self.assertIn("last_failure", ScanState.objects.get(kind="pairs").summary)

        self._scan(_PairSource())

        state = ScanState.objects.get(kind="pairs")
        self.assertNotIn("last_failure", state.summary)
        self.assertIsNotNone(state.last_scan_at)

    def test_a_failure_that_cannot_be_recorded_does_not_stop_the_other_sources(self):
        with mock.patch("deduplication.services._save_scan_failure", side_effect=RuntimeError("cannot record")):
            with self.assertLogs("deduplication.services", level=logging.ERROR):
                result = self._scan(_QuotingSource(), _PairSource())

        self.assertEqual(result["pairs"], 1)
        self.assertIn("quoting", result["failed"])
        self.assertFalse(ScanState.objects.filter(kind="quoting").exists())
