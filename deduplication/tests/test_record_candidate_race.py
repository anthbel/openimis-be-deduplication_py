import threading
import time
from unittest import mock

from django.db import connection, connections, transaction
from django.test import TransactionTestCase

from deduplication import sources as candidate_sources
from deduplication.models import DuplicateCandidate, ScanState
from deduplication.services import record_candidate, resolve, run_scan
from deduplication.sources import Candidate, CandidateSource, Watermark, order_pair
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


WAIT_SECONDS = 10
WAITING_ON_A_LOCK = (
    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
    "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
)


class ScanAgainstMergeTest(TransactionTestCase):
    """A scan rescoring a pair on the retired person, then the merged pair, while a merge waits for the first: the
    scan skips the row the merge holds, so neither waits on the other."""

    def setUp(self):
        self.user = LogInHelper().get_or_create_user_api()
        self.kept, self.retired, self.other = (self.person(name) for name in ("Kept", "Retired", "Other"))
        self.merged = self.candidate(self.kept, self.retired)
        self.stale = self.candidate(self.retired, self.other)

    def person(self, name):
        person = Individual(first_name=f"{PREFIX}{name}", last_name="Scan", dob="1990-01-01")
        person.save(username=self.user.username)
        return person

    def candidate(self, first, second):
        subject_a, subject_b = order_pair(str(first.id), str(second.id))
        return DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=subject_a, subject_b=subject_b, kind="biometric",
            source="TestSource", score=0.5, evidence={"template_ids": ["t1"]},
        )

    def _fixture_teardown(self):
        persons = Individual.objects.filter(first_name__startswith=PREFIX)
        ids = [str(person_id) for person_id in persons.values_list("id", flat=True)]
        DuplicateCandidate.objects.filter(subject_a__in=ids).delete()
        DuplicateCandidate.objects.filter(subject_b__in=ids).delete()
        Individual.history.filter(first_name__startswith=PREFIX).delete()
        persons.delete()

    @staticmethod
    def wait_for_a_lock_waiter():
        deadline = time.monotonic() + WAIT_SECONDS
        with connection.cursor() as cursor:
            while time.monotonic() < deadline:
                cursor.execute("SELECT pg_stat_clear_snapshot()")
                cursor.execute(WAITING_ON_A_LOCK)
                if cursor.fetchone()[0]:
                    return True
                time.sleep(0.05)
        return False

    @staticmethod
    def rescored(row):
        return Candidate(subject_model="individual.Individual", subject_a=row.subject_a, subject_b=row.subject_b,
                         kind="biometric", score=0.9, evidence={"template_ids": ["t2"]})

    def run_in_thread(self, name, body, results, start=None):
        def target():
            try:
                if start is not None:
                    start.wait(WAIT_SECONDS)
                results[name] = body()
            except Exception as exc:
                results[name] = exc
            finally:
                connections.close_all()

        return threading.Thread(target=target, name=name)

    def test_a_scan_and_a_merge_rescoring_and_moving_the_same_pairs_do_not_deadlock(self):
        holding, results = threading.Event(), {}

        def scan():
            with transaction.atomic():
                record_candidate(self.rescored(self.stale), source="TestSource")
                holding.set()
                waited = self.wait_for_a_lock_waiter()
                record_candidate(self.rescored(self.merged), source="TestSource")
            return waited

        def merge():
            return resolve(DuplicateCandidate.objects.get(id=self.merged.id), decision="same",
                           keep=str(self.kept.id), actor=self.user)

        threads = [self.run_in_thread("scan", scan, results),
                   self.run_in_thread("merge", merge, results, start=holding)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4 * WAIT_SECONDS)
            self.assertFalse(thread.is_alive(), thread.name)

        self.assertIs(results["scan"], True, results)
        self.assertIsInstance(results["merge"], DuplicateCandidate, results)
        self.assertEqual(DuplicateCandidate.objects.get(id=self.merged.id).status, DuplicateCandidate.Status.CONFIRMED)
        stale = DuplicateCandidate.objects.get(id=self.stale.id)
        self.assertEqual((stale.subject_a, stale.subject_b), order_pair(str(self.kept.id), str(self.other.id)))
        self.assertEqual(stale.score, 0.9)


class OneRescoringSource(CandidateSource):
    kind = "test_deferred"

    def __init__(self, candidate):
        self.candidate = candidate

    def scan(self, since):
        yield self.candidate

    def watermark(self):
        return Watermark()


class DeferredRescoringTest(TransactionTestCase):
    """A rescoring the scan skips because a resolve holds the row is written once the scan has committed and the
    row is free, although the scan's cursor has moved past the pair."""

    def setUp(self):
        self.user = LogInHelper().get_or_create_user_api()
        self.first, self.second = (ScanAgainstMergeTest.person(self, name) for name in ("First", "Second"))
        subject_a, subject_b = order_pair(str(self.first.id), str(self.second.id))
        self.candidate = DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=subject_a, subject_b=subject_b,
            kind=OneRescoringSource.kind, source="TestSource", score=0.5, evidence={"columns": {"dob": "1990"}})
        self.source = OneRescoringSource(Candidate(
            subject_model="individual.Individual", subject_a=subject_a, subject_b=subject_b,
            kind=OneRescoringSource.kind, score=0.9, evidence={"columns": {"last_name": "Scan"}}))
        candidate_sources.register(self.source)
        self.addCleanup(candidate_sources._REGISTRY.remove, self.source)

    def _fixture_teardown(self):
        ScanAgainstMergeTest._fixture_teardown(self)
        ScanState.objects.filter(kind=OneRescoringSource.kind).delete()

    def test_a_rescoring_skipped_while_a_resolve_holds_the_row_is_written_once_it_is_free(self):
        holding, waited, results = threading.Event(), [], {}

        def resolve_holding_the_row():
            with transaction.atomic():
                DuplicateCandidate.objects.select_for_update().get(id=self.candidate.id)
                holding.set()
                waited.append(ScanAgainstMergeTest.wait_for_a_lock_waiter())
            return True

        def scan():
            return run_scan(kinds=[OneRescoringSource.kind], actor=self.user)

        threads = [ScanAgainstMergeTest.run_in_thread(self, "resolve", resolve_holding_the_row, results),
                   ScanAgainstMergeTest.run_in_thread(self, "scan", scan, results, start=holding)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4 * WAIT_SECONDS)
            self.assertFalse(thread.is_alive(), thread.name)

        self.assertEqual(waited, [True], results)
        self.assertEqual(results["scan"], {OneRescoringSource.kind: 1}, results)
        self.assertIsNotNone(ScanState.objects.get(kind=OneRescoringSource.kind).last_scan_at)
        candidate = DuplicateCandidate.objects.get(id=self.candidate.id)
        self.assertEqual(candidate.score, 0.9)
        self.assertEqual(candidate.evidence["columns"], {"dob": "1990", "last_name": "Scan"})
