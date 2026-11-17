from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from deduplication.models import DuplicateCandidate
from deduplication.services import merge_subjects, resolve
from deduplication.sources import order_pair
from deduplication.tests.helpers import LogInHelper, WithoutMergePreparers
from individual.models import Individual


class ResolveTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def _make_pair(self):
        a = Individual(first_name="A", last_name="One", dob="1990-01-01")
        a.save(username=self.user.username)
        b = Individual(first_name="B", last_name="Two", dob="1990-01-01")
        b.save(username=self.user.username)
        subject_a, subject_b = order_pair(str(a.id), str(b.id))
        candidate = DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=subject_a, subject_b=subject_b,
            kind="demographic", source="TestSource", evidence={},
        )
        return a, b, candidate

    def test_resolve_different_dismisses_without_merging(self):
        a, b, candidate = self._make_pair()

        resolve(candidate, decision="different", actor=self.user, note="not a duplicate")

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.DISMISSED)
        self.assertEqual(candidate.decision_note, "not a duplicate")
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertFalse(b.is_deleted)

    def test_resolve_same_confirms_and_merges(self):
        a, b, candidate = self._make_pair()

        resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.CONFIRMED)
        self.assertEqual(candidate.reviewed_by, self.user.username)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertTrue(b.is_deleted)

    def test_resolve_never_reopens_a_dismissed_candidate(self):
        a, b, candidate = self._make_pair()
        candidate.status = DuplicateCandidate.Status.DISMISSED
        candidate.save()

        resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.DISMISSED)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertFalse(b.is_deleted)

    def test_resolve_leaves_a_confirmed_candidate_unchanged(self):
        a, b, candidate = self._make_pair()
        resolve(candidate, decision="same", keep=str(a.id), actor=self.user, note="first decision")

        resolve(candidate, decision="same", keep=str(b.id), actor=self.user, note="stale decision")

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.CONFIRMED)
        self.assertEqual(candidate.decision_note, "first decision")
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertTrue(b.is_deleted)

    def test_resolve_never_dismisses_a_confirmed_candidate(self):
        a, b, candidate = self._make_pair()
        resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        resolve(candidate, decision="different", actor=self.user, note="stale decision")

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.CONFIRMED)
        self.assertEqual(candidate.decision_note, "")

    def test_resolve_reads_the_current_status_not_the_passed_instance(self):
        a, b, candidate = self._make_pair()
        stale = DuplicateCandidate.objects.get(id=candidate.id)
        resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        resolve(stale, decision="same", keep=str(b.id), actor=self.user)

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertTrue(b.is_deleted)

    def _make_outsider(self):
        x = Individual(first_name="X", last_name="Outside", dob="1985-05-05")
        x.save(username=self.user.username)
        return x

    def test_resolve_refuses_a_keep_outside_the_pair(self):
        a, b, candidate = self._make_pair()
        x = self._make_outsider()

        with self.assertRaisesMessage(ValueError, "deduplication.resolve.keep_not_in_pair"):
            resolve(candidate, decision="same", keep=str(x.id), actor=self.user)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.OPEN)
        self.assertEqual(candidate.reviewed_by, "")
        for subject in (a, b, x):
            subject.refresh_from_db()
            self.assertFalse(subject.is_deleted)
        self.assertNotIn('merge_conflicts', x.json_ext or {})

    def test_resolve_refuses_a_soft_deleted_kept_subject(self):
        a, b, candidate = self._make_pair()
        a.delete(user=self.user)

        with self.assertRaisesMessage(ValueError, "deduplication.resolve.subject_deleted"):
            resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.OPEN)
        b.refresh_from_db()
        self.assertFalse(b.is_deleted)

    def test_resolve_refuses_a_soft_deleted_retired_subject(self):
        a, b, candidate = self._make_pair()
        b.delete(user=self.user)

        with self.assertRaisesMessage(ValueError, "deduplication.resolve.subject_deleted"):
            resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.OPEN)
        a.refresh_from_db()
        self.assertFalse(a.is_deleted)


class ResolveSameSubjectsAcrossKindsTest(TestCase):
    """One pair proposed by several sources yields one candidate per kind; a merge settles them all."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def _make_subjects(self):
        a = Individual(first_name="A", last_name="One", dob="1990-01-01")
        a.save(username=self.user.username)
        b = Individual(first_name="B", last_name="Two", dob="1990-01-01")
        b.save(username=self.user.username)
        return a, b

    def _candidate(self, a, b, kind):
        subject_a, subject_b = order_pair(str(a.id), str(b.id))
        return DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=subject_a, subject_b=subject_b,
            kind=kind, source="TestSource", evidence={},
        )

    def test_confirming_one_kind_confirms_the_other_open_kinds_of_the_pair(self):
        a, b = self._make_subjects()
        demographic = self._candidate(a, b, "demographic")
        identifier = self._candidate(a, b, "identifier")
        other_pair = self._candidate(a, self._make_subjects()[1], "demographic")

        with mock.patch("deduplication.services.merge_subjects", wraps=merge_subjects) as merge:
            resolve(demographic, decision="same", keep=str(a.id), actor=self.user, note="same person")

        self.assertEqual(merge.call_count, 1)
        identifier.refresh_from_db()
        self.assertEqual(identifier.status, DuplicateCandidate.Status.CONFIRMED)
        self.assertEqual(identifier.reviewed_by, self.user.username)
        self.assertIsNotNone(identifier.reviewed_at)
        other_pair.refresh_from_db()
        self.assertEqual(other_pair.status, DuplicateCandidate.Status.OPEN)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertTrue(b.is_deleted)

    def test_confirming_leaves_a_dismissed_kind_of_the_pair_dismissed(self):
        a, b = self._make_subjects()
        demographic = self._candidate(a, b, "demographic")
        identifier = self._candidate(a, b, "identifier")
        identifier.status = DuplicateCandidate.Status.DISMISSED
        identifier.save()

        resolve(demographic, decision="same", keep=str(a.id), actor=self.user)

        identifier.refresh_from_db()
        self.assertEqual(identifier.status, DuplicateCandidate.Status.DISMISSED)

    def test_same_on_a_kind_whose_pair_is_already_merged_confirms_without_merging(self):
        a, b = self._make_subjects()
        resolve(self._candidate(a, b, "demographic"), decision="same", keep=str(a.id), actor=self.user)
        late = self._candidate(a, b, "biometric")

        with mock.patch("deduplication.services.merge_subjects") as merge:
            resolve(late, decision="same", keep=str(a.id), actor=self.user, note="second source")

        merge.assert_not_called()
        late.refresh_from_db()
        self.assertEqual(late.status, DuplicateCandidate.Status.CONFIRMED)
        self.assertEqual(late.decision_note, "second source")
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_deleted)
        self.assertTrue(b.is_deleted)

    def test_same_on_a_merged_pair_with_the_other_keep_is_refused(self):
        a, b = self._make_subjects()
        resolve(self._candidate(a, b, "demographic"), decision="same", keep=str(a.id), actor=self.user)
        late = self._candidate(a, b, "biometric")

        with self.assertRaisesMessage(ValueError, "deduplication.resolve.keep_contradicts_merge"):
            resolve(late, decision="same", keep=str(b.id), actor=self.user)

        late.refresh_from_db()
        self.assertEqual(late.status, DuplicateCandidate.Status.OPEN)
        a.refresh_from_db()
        self.assertFalse(a.is_deleted)

    def test_same_on_a_merged_pair_whose_kept_subject_is_gone_is_refused(self):
        a, b = self._make_subjects()
        resolve(self._candidate(a, b, "demographic"), decision="same", keep=str(a.id), actor=self.user)
        a.delete(user=self.user)
        late = self._candidate(a, b, "biometric")

        with self.assertRaisesMessage(ValueError, "deduplication.resolve.subject_deleted"):
            resolve(late, decision="same", keep=str(a.id), actor=self.user)

        late.refresh_from_db()
        self.assertEqual(late.status, DuplicateCandidate.Status.OPEN)

    def test_different_on_a_kind_whose_pair_is_already_merged_is_refused(self):
        a, b = self._make_subjects()
        resolve(self._candidate(a, b, "demographic"), decision="same", keep=str(a.id), actor=self.user)
        late = self._candidate(a, b, "biometric")

        with self.assertRaisesMessage(ValueError, "deduplication.resolve.pair_already_merged"):
            resolve(late, decision="different", actor=self.user, note="late dismissal")

        late.refresh_from_db()
        self.assertEqual(late.status, DuplicateCandidate.Status.OPEN)
        self.assertEqual(late.reviewed_by, "")
        self.assertEqual(late.decision_note, "")

    def test_different_is_still_allowed_next_to_a_dismissed_kind(self):
        a, b = self._make_subjects()
        resolve(self._candidate(a, b, "demographic"), decision="different", actor=self.user)
        other = self._candidate(a, b, "identifier")

        resolve(other, decision="different", actor=self.user)

        other.refresh_from_db()
        self.assertEqual(other.status, DuplicateCandidate.Status.DISMISSED)


class ResolveLocksSubjectsTest(WithoutMergePreparers, TestCase):
    """Both subject rows are locked, in sorted id order, before the candidate row and any check."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def _pair(self):
        a = Individual(first_name="Lock", last_name="One", dob="1990-01-01")
        a.save(username=self.user.username)
        b = Individual(first_name="Lock", last_name="Two", dob="1990-01-01")
        b.save(username=self.user.username)
        low, high = order_pair(str(a.id), str(b.id))
        candidate = DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=low, subject_b=high,
            kind="demographic", source="TestSource", evidence={},
        )
        return low, high, candidate

    def _locks(self, queries):
        subject_table = Individual._meta.db_table
        candidate_table = DuplicateCandidate._meta.db_table
        locks = []
        for query in queries:
            sql = query["sql"]
            if "FOR UPDATE" not in sql:
                continue
            if f'FROM "{subject_table}"' in sql:
                locks.append(("subject", sql))
            elif f'FROM "{candidate_table}"' in sql:
                locks.append(("candidate", sql))
        return locks

    def _assert_subjects_locked_first_in_order(self, low, high, queries):
        locks = self._locks(queries)
        self.assertGreaterEqual(len(locks), 3, locks)
        self.assertEqual([kind for kind, _sql in locks[:3]], ["subject", "subject", "candidate"])
        # The database driver may render a UUID parameter with or without its hyphens.
        self.assertIn(low.replace("-", ""), locks[0][1].replace("-", ""))
        self.assertIn(high.replace("-", ""), locks[1][1].replace("-", ""))
        statements = [q["sql"] for q in queries if not q["sql"].startswith(("SAVEPOINT", "RELEASE SAVEPOINT"))]
        self.assertIn("FOR UPDATE", statements[0], "a query ran before the first subject lock")

    def test_same_locks_both_subjects_in_sorted_order_first(self):
        low, high, candidate = self._pair()

        with CaptureQueriesContext(connection) as ctx:
            resolve(candidate, decision="same", keep=high, actor=self.user)

        self._assert_subjects_locked_first_in_order(low, high, ctx.captured_queries)

    def test_different_locks_both_subjects_in_sorted_order_first(self):
        low, high, candidate = self._pair()

        with CaptureQueriesContext(connection) as ctx:
            resolve(candidate, decision="different", actor=self.user)

        self._assert_subjects_locked_first_in_order(low, high, ctx.captured_queries)


class ResolveMovesOpenPairsOfTheRetiredSubjectTest(TestCase):
    """A merge of (kept, retired) moves the other OPEN candidates naming retired onto kept."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def _person(self, name):
        person = Individual(first_name=name, last_name="Moved", dob="1990-01-01")
        person.save(username=self.user.username)
        return person

    def _candidate(self, first, second, kind="demographic", **fields):
        subject_a, subject_b = order_pair(str(first.id), str(second.id))
        return DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=subject_a, subject_b=subject_b,
            kind=kind, source="TestSource", **{"evidence": {}, **fields},
        )

    def setUp(self):
        self.kept, self.retired, self.other = self._person("Kept"), self._person("Retired"), self._person("Other")
        self.merged = self._candidate(self.kept, self.retired)

    def _merge(self):
        resolve(self.merged, decision="same", keep=str(self.kept.id), actor=self.user)

    def test_an_open_pair_on_the_retired_subject_moves_to_the_kept_subject(self):
        stale = self._candidate(self.retired, self.other, score=0.9, evidence={"columns": {"last_name": "Moved"}})

        self._merge()

        stale.refresh_from_db()
        self.assertEqual((stale.subject_a, stale.subject_b), order_pair(str(self.kept.id), str(self.other.id)))
        self.assertEqual((stale.status, stale.kind, stale.score), (DuplicateCandidate.Status.OPEN, "demographic", 0.9))
        self.assertEqual(stale.evidence["columns"], {"last_name": "Moved"})
        self.assertEqual(len(stale.evidence["moved_from"]), 1)
        move = stale.evidence["moved_from"][0]
        self.assertEqual((move["subject"], move["kept"], move["merged_by"]),
                         (str(self.retired.id), str(self.kept.id), str(self.merged.id)))

    def test_the_moved_pair_can_be_merged(self):
        stale = self._candidate(self.retired, self.other)
        self._merge()
        stale.refresh_from_db()

        resolve(stale, decision="same", keep=str(self.kept.id), actor=self.user)

        stale.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(stale.status, DuplicateCandidate.Status.CONFIRMED)
        self.assertTrue(self.other.is_deleted)

    def test_a_pair_already_open_on_the_kept_subject_takes_the_move_and_the_stale_pair_is_deleted(self):
        stale = self._candidate(self.retired, self.other, score=0.7)
        existing = self._candidate(self.kept, self.other, score=0.8, evidence={"columns": {"dob": "1990-01-01"}})

        self._merge()

        self.assertFalse(DuplicateCandidate.objects.filter(id=stale.id).exists())
        existing.refresh_from_db()
        self.assertEqual((existing.status, existing.score), (DuplicateCandidate.Status.OPEN, 0.8))
        self.assertEqual(existing.evidence["columns"], {"dob": "1990-01-01"})
        move = existing.evidence["moved_from"][0]
        self.assertEqual((move["candidate_id"], move["score"], move["subject"]),
                         (str(stale.id), 0.7, str(self.retired.id)))

    def test_a_pair_dismissed_on_the_kept_subject_dismisses_the_stale_pair(self):
        stale = self._candidate(self.retired, self.other)
        dismissed = self._candidate(self.kept, self.other, status=DuplicateCandidate.Status.DISMISSED)

        self._merge()

        stale.refresh_from_db()
        self.assertEqual(stale.status, DuplicateCandidate.Status.DISMISSED)
        self.assertEqual(stale.reviewed_by, self.user.username)
        self.assertIsNotNone(stale.reviewed_at)
        self.assertIn(str(dismissed.id), stale.decision_note)
        self.assertEqual((stale.subject_a, stale.subject_b), order_pair(str(self.retired.id), str(self.other.id)))

    def test_only_a_candidate_of_the_same_kind_takes_the_move(self):
        stale = self._candidate(self.retired, self.other, kind="biometric_face")
        other_kind = self._candidate(self.kept, self.other, status=DuplicateCandidate.Status.DISMISSED)

        self._merge()

        stale.refresh_from_db()
        self.assertEqual((stale.subject_a, stale.subject_b), order_pair(str(self.kept.id), str(self.other.id)))
        self.assertEqual(stale.status, DuplicateCandidate.Status.OPEN)
        other_kind.refresh_from_db()
        self.assertEqual(other_kind.status, DuplicateCandidate.Status.DISMISSED)

    def test_a_pair_whose_other_subject_is_deleted_is_left_as_it_is(self):
        stale = self._candidate(self.retired, self.other)
        self.other.delete(user=self.user)

        self._merge()

        stale.refresh_from_db()
        self.assertEqual((stale.subject_a, stale.subject_b), order_pair(str(self.retired.id), str(self.other.id)))
        self.assertEqual(stale.status, DuplicateCandidate.Status.OPEN)
        self.assertNotIn("moved_from", stale.evidence)

    def test_a_dismissal_moves_nothing(self):
        stale = self._candidate(self.retired, self.other)

        resolve(self.merged, decision="different", actor=self.user)

        stale.refresh_from_db()
        self.assertEqual((stale.subject_a, stale.subject_b), order_pair(str(self.retired.id), str(self.other.id)))

    def test_a_candidate_moved_after_it_was_read_is_refused(self):
        stale = self._candidate(self.retired, self.other)
        read = DuplicateCandidate.objects.get(id=stale.id)
        self._merge()

        with self.assertRaisesMessage(ValueError, "deduplication.resolve.pair_changed"):
            resolve(read, decision="same", keep=str(self.other.id), actor=self.user)

        stale.refresh_from_db()
        self.kept.refresh_from_db()
        self.assertEqual(stale.status, DuplicateCandidate.Status.OPEN)
        self.assertFalse(self.kept.is_deleted)

    def test_a_candidate_deleted_by_the_move_after_it_was_read_is_refused(self):
        stale = self._candidate(self.retired, self.other)
        self._candidate(self.kept, self.other)
        read = DuplicateCandidate.objects.get(id=stale.id)
        self._merge()

        with self.assertRaisesMessage(ValueError, "deduplication.resolve.pair_changed"):
            resolve(read, decision="different", actor=self.user)

    def test_the_moved_candidates_are_locked_after_the_merged_one_in_id_order(self):
        self._candidate(self.retired, self.other)
        self._candidate(self.kept, self.other)
        table = DuplicateCandidate._meta.db_table

        with CaptureQueriesContext(connection) as ctx:
            self._merge()

        locks = [query["sql"] for query in ctx.captured_queries
                 if "FOR UPDATE" in query["sql"] and f'FROM "{table}"' in query["sql"]]
        self.assertEqual(len(locks), 3, locks)
        self.assertIn('ORDER BY "deduplication_candidate"."id" ASC', locks[2])
