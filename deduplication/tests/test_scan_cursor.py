"""Two connections: a subject committed after a scan's cursor was taken, but stamped before it, is compared by
the next scan and is not covered until then."""
import threading
from datetime import datetime, timedelta

from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase

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
        self.assertTrue({str(person.id) for person in self.persons} <= touched_ids(Individual, since))

    def test_a_cursor_without_a_snapshot_scans_everything(self):
        later = datetime.now() + timedelta(days=1)
        self.assertIsNone(touched_ids(Individual, Watermark(updated_at=later, last_id="")))
        ScanState.objects.create(kind="demographic", updated_at=later, last_id="", last_scan_at=datetime.now())
        self.assertEqual(self.scan()[0], 1)
