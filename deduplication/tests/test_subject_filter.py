from unittest import mock

from django.db import connection
from django.test import TestCase

from deduplication.models import DuplicateCandidate, ScanState
from deduplication.services import run_scan, scan_subject
from deduplication.sources import Candidate, CandidateSource, register_subject_filter, rescan
from deduplication.sources.subject import subject_watermark
from deduplication.tests.helpers import LogInHelper, override_deduplication_config
from individual.models import Individual

PERSON = "individual.Individual"


class _TwoPairSource(CandidateSource):
    kind = "two-pairs"

    def watermark(self):
        return subject_watermark()

    def scan(self, since):
        for a, b in (("kept-a", "kept-b"), ("kept-c", "out-d")):
            yield Candidate(subject_model=PERSON, subject_a=a, subject_b=b, kind=self.kind, score=None, evidence={})


def _leave_out(ids):
    def subject_filter(subject_model, subject_ids):
        return {subject_id for subject_id in subject_ids if subject_model == PERSON and subject_id in ids}
    return subject_filter


class SubjectFilterTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def setUp(self):
        register_subject_filter("test", _leave_out({"out-d"}))
        self.addCleanup(register_subject_filter, "test", None)

    def test_a_scan_records_no_candidate_with_a_subject_left_out(self):
        with mock.patch("deduplication.services.registered_sources", return_value=[_TwoPairSource()]):
            result = run_scan(actor=self.user)

        self.assertEqual(result, {"two-pairs": 1})
        pairs = set(DuplicateCandidate.objects.filter(kind="two-pairs").values_list("subject_a", "subject_b"))
        self.assertEqual(pairs, {("kept-a", "kept-b")})

    def test_a_subject_scan_records_no_candidate_with_a_subject_left_out(self):
        with mock.patch("deduplication.services.registered_sources", return_value=[_TwoPairSource()]):
            self.assertEqual(scan_subject(PERSON, "out-d"), [])
            self.assertEqual(len(scan_subject(PERSON, "kept-c")), 0)
            self.assertEqual(len(scan_subject(PERSON, "kept-a")), 1)

    def test_a_removed_filter_leaves_nothing_out(self):
        register_subject_filter("test", None)
        with mock.patch("deduplication.services.registered_sources", return_value=[_TwoPairSource()]):
            self.assertEqual(run_scan(actor=self.user), {"two-pairs": 2})


class RescanTest(TestCase):
    """A subject left out while it was scanned is scanned again once rescan moves the cursors back."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def setUp(self):
        if connection.vendor == "microsoft":
            self.skipTest("This test can only be executed for PSQL database")
        self.first = self.person()
        self.second = self.person()
        self.left_out = {str(self.second.id)}
        register_subject_filter("test", _leave_out(self.left_out))
        self.addCleanup(register_subject_filter, "test", None)
        patch = override_deduplication_config(demographic_columns=["first_name", "last_name", "dob"])
        patch.__enter__()
        self.addCleanup(patch.__exit__, None, None, None)

    def person(self):
        person = Individual(first_name="Twin", last_name="Rescan", dob="1991-04-05")
        person.save(username=self.user.username)
        return person

    def pair_recorded(self):
        return DuplicateCandidate.objects.filter(kind="demographic", subject_a__in=[
            str(self.first.id), str(self.second.id)], subject_b__in=[str(self.first.id), str(self.second.id)]).exists()

    def test_the_next_scan_after_rescan_records_the_pair(self):
        run_scan(kinds=["demographic"], actor=self.user)
        self.assertFalse(self.pair_recorded())
        self.left_out.clear()

        run_scan(kinds=["demographic"], actor=self.user)
        self.assertFalse(self.pair_recorded())

        rescan(PERSON, [str(self.second.id)])
        state = ScanState.objects.get(kind="demographic")
        self.assertLess(state.updated_at, self.second.date_created)
        self.assertEqual(state.last_id, "")

        run_scan(kinds=["demographic"], actor=self.user)
        self.assertTrue(self.pair_recorded())

    def test_rescan_never_moves_a_cursor_forward_nor_sets_one(self):
        ScanState.objects.create(kind="unscanned")
        run_scan(kinds=["demographic"], actor=self.user)
        early = ScanState.objects.get(kind="demographic")
        early.updated_at = self.first.date_created.replace(year=2000)
        early.last_id = "x"
        early.save()

        rescan(PERSON, [str(self.second.id)])

        self.assertEqual(ScanState.objects.get(kind="demographic").updated_at, early.updated_at)
        self.assertEqual(ScanState.objects.get(kind="demographic").last_id, "x")
        self.assertIsNone(ScanState.objects.get(kind="unscanned").updated_at)

    def test_rescan_of_no_subject_changes_nothing(self):
        run_scan(kinds=["demographic"], actor=self.user)
        before = ScanState.objects.get(kind="demographic").updated_at
        rescan(PERSON, [])
        self.assertEqual(ScanState.objects.get(kind="demographic").updated_at, before)
