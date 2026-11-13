import datetime
import threading
import time
from unittest import mock

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase

from deduplication.models import DuplicateCandidate, ScanState
from deduplication.services import run_scan, scan_subject
from deduplication.sources import Candidate, CandidateSource, Watermark, register_subject_filter, rescan
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


WAIT_SECONDS = 10
WAITING_ON_A_LOCK = (
    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
    "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
)
RACE_KIND = "rescan-race"
RACE_NAME = "RescanRace"


class _SlowSource(CandidateSource):
    """Its scan waits, inside the scan's transaction, until another connection waits on a lock."""
    kind = RACE_KIND

    def __init__(self, cursor_at, in_scan, waited):
        self.cursor_at, self.in_scan, self.waited = cursor_at, in_scan, waited

    def watermark(self):
        return Watermark(updated_at=self.cursor_at, last_id="")

    def scan(self, since):
        self.in_scan.set()
        self.waited.append(_wait_for_a_lock_waiter())
        return iter(())


def _wait_for_a_lock_waiter():
    """Waits until another backend of this database waits on a lock; False after WAIT_SECONDS."""
    deadline = time.monotonic() + WAIT_SECONDS
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            # The statistics views keep one snapshot per transaction unless it is cleared.
            cursor.execute("SELECT pg_stat_clear_snapshot()")
            cursor.execute(WAITING_ON_A_LOCK)
            if cursor.fetchone()[0]:
                return True
            time.sleep(0.05)
    return False


class RescanDuringScanTest(TransactionTestCase):
    """Two connections: a rescan while a scan of the same source runs moves back the cursor that scan writes."""

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("This test can only be executed for PSQL database")
        self.user = LogInHelper().get_or_create_user_api()
        self.person = Individual(first_name="Race", last_name=RACE_NAME, dob="1992-02-02")
        self.person.save(username=self.user.username)
        ScanState.objects.create(kind=RACE_KIND, updated_at=self.person.date_created - datetime.timedelta(days=1))

    def _fixture_teardown(self):
        ScanState.objects.filter(kind=RACE_KIND).delete()
        with connection.cursor() as cursor:
            for model in (Individual, Individual.history.model):
                table = connection.ops.quote_name(model._meta.db_table)
                cursor.execute(f"DELETE FROM {table} WHERE last_name = %s", [RACE_NAME])

    @staticmethod
    def in_thread(name, target, results, start=None):
        def body():
            try:
                if start is not None:
                    start.wait(WAIT_SECONDS)
                results[name] = target()
            except Exception as exc:
                results[name] = exc
            finally:
                connections.close_all()

        return threading.Thread(target=body, name=name)

    def test_a_rescan_waits_for_the_running_scan_and_moves_its_new_cursor_back(self):
        in_scan, waited, results = threading.Event(), [], {}
        source = _SlowSource(self.person.date_created + datetime.timedelta(seconds=1), in_scan, waited)
        scan = self.in_thread("scan", lambda: run_scan(kinds=[RACE_KIND], actor=self.user), results)
        again = self.in_thread("rescan", lambda: rescan(PERSON, [str(self.person.id)]), results, start=in_scan)
        with mock.patch("deduplication.services.registered_sources", return_value=[source]):
            for thread in (scan, again):
                thread.start()
            for thread in (scan, again):
                thread.join(4 * WAIT_SECONDS)
                self.assertFalse(thread.is_alive(), thread.name)

        self.assertEqual(results["scan"], {RACE_KIND: 0}, results)
        self.assertIsNone(results["rescan"], results)
        self.assertEqual(waited, [True])
        self.assertLess(ScanState.objects.get(kind=RACE_KIND).updated_at, self.person.date_created)
