import threading
from unittest import mock

from django.db import connections
from django.test import TransactionTestCase

from deduplication.models import DuplicateCandidate
from deduplication.services import record_candidate, resolve
from deduplication.sources import Candidate, order_pair
from deduplication.tests.helpers import LogInHelper
from individual.models import Individual

PREFIX = "DDRACE"


class RecordCandidateAgainstResolveTest(TransactionTestCase):
    """A scan that read an OPEN candidate before another connection merged the pair does not reopen it."""

    def setUp(self):
        self.user = LogInHelper().get_or_create_user_api()
        self.a = Individual(first_name=f"{PREFIX}A", last_name="One", dob="1990-01-01")
        self.a.save(username=self.user.username)
        self.b = Individual(first_name=f"{PREFIX}B", last_name="Two", dob="1990-01-01")
        self.b.save(username=self.user.username)
        self.subject_a, self.subject_b = order_pair(str(self.a.id), str(self.b.id))
        self.candidate = DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=self.subject_a, subject_b=self.subject_b,
            kind="demographic", source="TestSource", score=0.7, evidence={"columns": {"dob": "1990-01-01"}},
        )

    def _fixture_teardown(self):
        """Only the rows of this test are removed; the harness database keeps its base rows."""
        persons = Individual.objects.filter(first_name__startswith=PREFIX)
        ids = [str(person_id) for person_id in persons.values_list("id", flat=True)]
        DuplicateCandidate.objects.filter(subject_a__in=ids).delete()
        Individual.history.filter(first_name__startswith=PREFIX).delete()
        persons.delete()

    def _merge_in_another_connection(self):
        errors = []

        def body():
            try:
                resolve(DuplicateCandidate.objects.get(id=self.candidate.id), decision="same",
                        keep=str(self.a.id), actor=self.user)
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        thread = threading.Thread(target=body)
        thread.start()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_a_scan_that_read_the_pair_before_its_merge_does_not_reopen_it(self):
        manager = DuplicateCandidate.objects
        original = manager.get_or_create

        def read_then_merge(*args, **kwargs):
            found = original(*args, **kwargs)
            self._merge_in_another_connection()
            return found

        incoming = Candidate(
            subject_model="individual.Individual", subject_a=self.subject_a, subject_b=self.subject_b,
            kind="demographic", score=0.9, evidence={"columns": {"last_name": "One"}},
        )
        with mock.patch.object(manager, "get_or_create", side_effect=read_then_merge):
            record_candidate(incoming, source="TestSource")

        candidate = DuplicateCandidate.objects.get(id=self.candidate.id)
        self.assertEqual(candidate.status, DuplicateCandidate.Status.CONFIRMED)
        self.assertEqual(candidate.reviewed_by, self.user.username)
        self.assertEqual(candidate.score, 0.7)
        self.assertTrue(Individual.objects.get(id=self.b.id).is_deleted)

    def test_an_open_candidate_still_takes_the_higher_score_and_the_merged_evidence(self):
        incoming = Candidate(
            subject_model="individual.Individual", subject_a=self.subject_a, subject_b=self.subject_b,
            kind="demographic", score=0.9, evidence={"columns": {"last_name": "One"}},
        )

        record_candidate(incoming, source="TestSource")

        candidate = DuplicateCandidate.objects.get(id=self.candidate.id)
        self.assertEqual((candidate.status, candidate.score), (DuplicateCandidate.Status.OPEN, 0.9))
        self.assertEqual(candidate.evidence, {"columns": {"dob": "1990-01-01", "last_name": "One"}})
