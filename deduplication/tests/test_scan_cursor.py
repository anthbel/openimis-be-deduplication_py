"""Two connections: a subject committed after a scan's cursor was taken, but stamped before it, is compared by
the next scan and is not covered until then."""
import threading
from datetime import datetime, timedelta

from unittest import mock

from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from deduplication.models import DuplicateCandidate, ScanState
from deduplication.services import run_scan
from deduplication.sources import Watermark, order_pair
from deduplication.sources.demographic import DemographicSource
from deduplication.sources.subject import touched_ids
from deduplication.tests.helpers import LogInHelper, override_deduplication_config
from individual.models import Individual

PERSON = "individual.Individual"
LAST_NAME = "ScanCursorProbe"
WAIT_SECONDS = 10


class LateCommitTest(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("This test can only be executed for PSQL database")
        self.user = LogInHelper().get_or_create_user_api()
        ScanState.objects.filter(kind="demographic").delete()
        config = override_deduplication_config(demographic_columns=["first_name", "last_name", "dob"])
        config.__enter__()
        self.addCleanup(config.__exit__, None, None, None)
        self.registered = self.person("Rose")
        self.other = self.person("Other")

    def _fixture_teardown(self):
        ScanState.objects.filter(kind="demographic").delete()
        DuplicateCandidate.objects.filter(kind="demographic").delete()
        with connection.cursor() as cursor:
            for model in (Individual, Individual.history.model):
                table = connection.ops.quote_name(model._meta.db_table)
                cursor.execute(f"DELETE FROM {table} WHERE last_name = %s", [LAST_NAME])

    def scan(self):
        result = run_scan(kinds=["demographic"], actor=self.user)
        self.assertNotIn("failed", result)

    def person(self, first_name):
        person = Individual(first_name=first_name, last_name=LAST_NAME, dob="1990-01-01")
        person.save(username=self.user.username)
        return person

    def save_in_open_transaction(self, saved, commit, results):
        """In another connection: saves a duplicate of the registered person, waits for ``commit``, commits."""
        def body():
            try:
                with transaction.atomic():
                    late = Individual(first_name="Rose", last_name=LAST_NAME, dob="1990-01-01")
                    late.save(username=self.user.username)
                    results["late"] = late
                    saved.set()
                    if not commit.wait(WAIT_SECONDS):
                        raise TimeoutError("commit never signalled")
            except Exception as exc:
                results["error"] = exc
                saved.set()
            finally:
                connections.close_all()

        return threading.Thread(target=body, name="late-writer")

    def committed_after_a_scan(self):
        """Saves the late person in another connection, commits a later change meanwhile, scans, then commits the
        late person: its stamp is before the scan's cursor, its commit after."""
        saved, commit, results = threading.Event(), threading.Event(), {}
        writer = self.save_in_open_transaction(saved, commit, results)
        writer.start()
        try:
            self.assertTrue(saved.wait(WAIT_SECONDS))
            self.assertNotIn("error", results)
            self.other.first_name = "Other changed"
            self.other.save(username=self.user.username)
            self.scan()
        finally:
            commit.set()
            writer.join(2 * WAIT_SECONDS)
        self.assertFalse(writer.is_alive())
        self.assertNotIn("error", results)
        late = results["late"]
        self.assertLess(late.date_updated, ScanState.objects.get(kind="demographic").updated_at)
        return late

    def recorded(self, late):
        a, b = order_pair(str(self.registered.id), str(late.id))
        return DuplicateCandidate.objects.filter(kind="demographic", subject_a=a, subject_b=b).exists()

    def covered(self, late):
        return DemographicSource().covered(PERSON, [late.id], ScanState.objects.get(kind="demographic"))

    def test_a_subject_committed_after_the_cursor_is_not_covered_by_that_scan(self):
        late = self.committed_after_a_scan()
        self.assertEqual(self.covered(late), set())

    def test_the_next_scan_compares_a_subject_committed_after_the_cursor_and_covers_it(self):
        late = self.committed_after_a_scan()
        self.scan()
        self.assertTrue(self.recorded(late))
        self.assertEqual(self.covered(late), {late.id})
        self.scan()
        self.assertTrue(self.recorded(late))
        self.assertEqual(self.covered(late), {late.id})


class ReadSnapshotTest(TestCase):
    """The scan's snapshot in one transaction: versions written after it are not read, whatever their stamp."""

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()
        cls.persons = []
        for first_name in ("Paul", "Rose", "Rose"):
            person = Individual(first_name=first_name, last_name=LAST_NAME, dob="1990-01-01")
            person.save(username=cls.user.username)
            cls.persons.append(person)

    def setUp(self):
        config = override_deduplication_config(demographic_columns=["first_name", "last_name", "dob"])
        config.__enter__()
        self.addCleanup(config.__exit__, None, None, None)

    def scan(self):
        result = run_scan(kinds=["demographic"], actor=self.user)
        self.assertNotIn("failed", result)
        return result["demographic"], ScanState.objects.get(kind="demographic")

    def test_a_scan_covers_the_versions_its_transaction_wrote_before_it(self):
        _count, state = self.scan()
        ids = [person.id for person in self.persons]
        self.assertEqual(DemographicSource().covered(PERSON, ids, state), set(ids))
        self.assertEqual(self.scan()[0], 0)

    def test_a_version_written_after_the_scan_with_an_older_stamp_is_not_covered_and_is_scanned_again(self):
        _count, state = self.scan()
        paul = self.persons[0]
        Individual.objects.filter(id=paul.id).update(first_name="Rose")
        self.assertLess(Individual.objects.get(id=paul.id).date_updated, state.updated_at)
        self.assertEqual(DemographicSource().covered(PERSON, [paul.id], state), set())

        count, state = self.scan()
        self.assertEqual(count, 3)
        a, b = order_pair(str(self.persons[1].id), str(paul.id))
        self.assertTrue(DuplicateCandidate.objects.filter(kind="demographic", subject_a=a, subject_b=b).exists())
        self.assertEqual(DemographicSource().covered(PERSON, [paul.id], state), {paul.id})

    def test_a_snapshot_of_another_database_cluster_covers_nobody_and_reads_nothing(self):
        from deduplication.sources.snapshot import ReadSnapshot

        _count, state = self.scan()
        state.summary["snapshot"]["system"] = "another cluster"
        self.assertEqual(DemographicSource().covered(PERSON, [self.persons[0].id], state), set())
        since = Watermark(updated_at=state.updated_at, last_id=state.last_id,
                          snapshot=ReadSnapshot.of(state))
        self.assertIsNone(touched_ids(Individual, since))

    def test_a_cursor_without_a_snapshot_scans_everything(self):
        later = datetime.now() + timedelta(days=1)
        self.assertIsNone(touched_ids(Individual, Watermark(updated_at=later, last_id="")))
        ScanState.objects.create(kind="demographic", updated_at=later, last_id="", last_scan_at=datetime.now())
        self.assertEqual(self.scan()[0], 1)

    def test_a_state_without_a_snapshot_covers_nobody(self):
        _count, state = self.scan()
        ids = [person.id for person in self.persons]
        state.summary.pop("snapshot")
        self.assertEqual(DemographicSource().covered(PERSON, ids, state), set())

    def test_the_versions_read_are_never_sent_as_an_array_to_compare_each_row_with(self):
        _count, state = self.scan()
        Individual.objects.filter(id=self.persons[0].id).update(first_name="Rose")
        from deduplication.sources.snapshot import ReadSnapshot

        since = Watermark(updated_at=state.updated_at, last_id=state.last_id, snapshot=ReadSnapshot.of(state))
        self.assertTrue(ReadSnapshot.of(state).seen)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(touched_ids(Individual, since), {str(self.persons[0].id)})
            DemographicSource().covered(PERSON, [person.id for person in self.persons], state)
        for query in queries.captured_queries:
            sql = " ".join(query["sql"].upper().split())
            self.assertNotIn("ANY(", sql.replace("ANY (", "ANY("))
            self.assertNotIn("<> ALL", sql)

    def test_a_snapshot_leaving_versions_out_logs_how_many_and_the_age_of_the_oldest_transaction(self):
        from deduplication.sources import snapshot as snapshots

        with mock.patch.object(snapshots, "SEEN_LIMIT", 1), \
                self.assertLogs("deduplication.sources.snapshot", "WARNING") as logs:
            taken = snapshots.ReadSnapshot.take(Individual.objects.filter(last_name=LAST_NAME))
        self.assertEqual(len(taken.seen), 1)
        self.assertEqual(len(logs.records), 1)
        message = logs.records[0].getMessage()
        self.assertIn(": 2 row versions written since the oldest running transaction", message)
        self.assertRegex(message, r"started \d+ transaction ids ago")
        for person in self.persons:
            self.assertNotIn(person.first_name, message)
            self.assertNotIn(str(person.id), message)


class TransactionIdRuleTest(TestCase):
    """The unwrap of a 32-bit xmin and the rule that a version counts as old, on literal ids."""

    EPOCH = 2 ** 32

    def unwrapped(self, raw, xmax):
        from deduplication.sources.snapshot import unwrapped

        with connection.cursor() as cursor:
            cursor.execute(f"SELECT {unwrapped('v.raw', 'v.xmax')} "
                           "FROM (VALUES (%s::bigint, %s::bigint)) AS v(raw, xmax)", [raw, xmax])
            return cursor.fetchone()[0]

    def old(self, transaction_id, xmin, xmax, own):
        from deduplication.sources.snapshot import old, unwrapped

        rule = old(unwrapped("v.raw", "v.xmax"), "v.xmin", "v.xmax", "v.own")
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT {rule} FROM (VALUES (%s::bigint, %s::bigint, %s::bigint, %s::bigint)) "
                           "AS v(raw, xmin, xmax, own)", [transaction_id % self.EPOCH, xmin, xmax, own])
            return cursor.fetchone()[0]

    def test_an_id_within_2_31_of_xmax_unwraps_to_itself_across_an_epoch(self):
        xmax = 3 * self.EPOCH + 100
        for transaction_id in (xmax - 5, xmax - 200, xmax + 3, xmax - 2 ** 31 + 1, xmax + 2 ** 31 - 1):
            with self.subTest(transaction_id=transaction_id):
                self.assertEqual(self.unwrapped(transaction_id % self.EPOCH, xmax), transaction_id)

    def test_an_id_more_than_2_31_behind_unwraps_into_the_future(self):
        xmax = 3 * self.EPOCH + 100
        ancient = xmax - 3_000_000_000
        self.assertEqual(self.unwrapped(ancient % self.EPOCH, xmax), ancient + self.EPOCH)

    def test_old_below_xmin_and_not_old_between_xmin_and_xmax(self):
        xmax, xmin = 3 * self.EPOCH + 100, 3 * self.EPOCH + 50
        self.assertTrue(self.old(xmin - 1, xmin, xmax, None))
        self.assertFalse(self.old(xmin, xmin, xmax, None))
        self.assertFalse(self.old(xmax - 1, xmin, xmax, xmin + 10))

    def test_a_frozen_id_unwrapped_into_the_future_is_old_unless_at_or_above_the_own_transaction(self):
        xmax, xmin = 3 * self.EPOCH + 100, 3 * self.EPOCH + 50
        ancient = xmax - 3_000_000_000
        self.assertTrue(self.old(ancient, xmin, xmax, None))
        self.assertTrue(self.old(ancient, xmin, xmax, xmax + 2_000_000_000))
        self.assertFalse(self.old(ancient, xmin, xmax, xmax + 1))

    def test_the_own_transaction_at_or_above_xmax_is_not_old(self):
        xmax, xmin = 3 * self.EPOCH + 100, 3 * self.EPOCH + 50
        own = xmax + 2
        self.assertFalse(self.old(own, xmin, xmax, own))
        self.assertFalse(self.old(own + 5, xmin, xmax, own))
        self.assertTrue(self.old(xmax + 1, xmin, xmax, own))
