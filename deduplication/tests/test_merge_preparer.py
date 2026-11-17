from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from deduplication.models import DuplicateCandidate
from deduplication.services import ResolveRefusal, check_resolve, resolve
from deduplication.sources import MergePlan, MergeRefusal, merge_preparers, register_merge_preparer
from deduplication.tests.helpers import WithoutMergePreparers
from deduplication.tests.test_resolve_check import _PairFixture
from individual.models import Group, GroupIndividual, Individual

WRITING_STATEMENTS = ("INSERT", "UPDATE", "DELETE")


class _RecordingPreparer:
    """A preparer that records its calls and answers as told."""

    def __init__(self, fill=True, lock_refusal=None, prepare_refusal=None, released=(), token="locked"):
        self.calls = []
        self.fill = fill
        self.lock_refusal = lock_refusal
        self.prepare_refusal = prepare_refusal
        self.released = frozenset(released)
        self.token = token

    def lock(self, candidate, keep):
        self.calls.append(("lock", str(candidate.id), keep))
        if self.lock_refusal:
            raise self.lock_refusal
        return self.token

    def prepare(self, candidate, kept, retired, *, actor, check, locked=None, merge_fields=()):
        self.calls.append(("prepare", str(kept.id), str(retired.id), check, locked, tuple(merge_fields)))
        if self.prepare_refusal:
            raise self.prepare_refusal
        return MergePlan(fill=self.fill, released_group_ids=self.released)


class MergePreparerTest(WithoutMergePreparers, _PairFixture, TestCase):
    def _register(self, preparer):
        register_merge_preparer("test", preparer)
        self.addCleanup(register_merge_preparer, "test", None)
        return preparer

    def _pair(self):
        a = Individual(first_name="Ann", last_name="", dob="1990-01-01", json_ext={"phone": "1"})
        a.save(username=self.user.username)
        b = Individual(first_name="Anne", last_name="Smith", dob="1990-01-01", json_ext={"phone": "2", "sex": "F"})
        b.save(username=self.user.username)
        return a, b, self._candidate(a, b)

    def test_no_preparer_registered_leaves_the_merge_as_it_was(self):
        a, b, candidate = self._pair()
        self.assertEqual(merge_preparers(), [])

        resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        a.refresh_from_db()
        self.assertEqual(a.last_name, "Smith")
        self.assertEqual([c["field"] for c in a.json_ext["merge_conflicts"]], ["first_name", "phone"])

    def test_the_lock_phase_runs_before_the_subject_locks_and_hands_its_token_to_prepare(self):
        a, b, candidate = self._pair()
        preparer = self._register(_RecordingPreparer())
        order = []
        original_lock = preparer.lock

        def lock(*args):
            order.append("preparer")
            return original_lock(*args)

        preparer.lock = lock
        with mock.patch("deduplication.services._lock_subjects", side_effect=lambda *args: order.append("subjects")):
            resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        self.assertEqual(order, ["preparer", "subjects"])
        prepare = [call for call in preparer.calls if call[0] == "prepare"]
        self.assertEqual(prepare[0][:5], ("prepare", str(a.id), str(b.id), False, "locked"))
        self.assertEqual(set(prepare[0][5]), {"first_name", "last_name", "phone", "sex"})

    def test_a_dismissal_runs_no_preparer(self):
        a, b, candidate = self._pair()
        preparer = self._register(_RecordingPreparer())

        resolve(candidate, decision="different", actor=self.user)

        self.assertEqual(preparer.calls, [])

    def test_prepare_runs_before_the_enrolment_guard(self):
        a, b, candidate = self._pair()
        group = Group(code="PREP-G")
        group.save(username=self.user.username)
        membership = GroupIndividual(group=group, individual=b)
        membership.save(username=self.user.username)
        username = self.user.username

        class Releasing(_RecordingPreparer):
            def prepare(self, candidate, kept, retired, **kwargs):
                plan = super().prepare(candidate, kept, retired, **kwargs)
                if not kwargs["check"]:
                    membership.delete(username=username)
                return plan

        self._register(Releasing(released={group.id}))

        check_resolve(candidate, decision="same", keep=str(a.id))
        resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, DuplicateCandidate.Status.CONFIRMED)

    def test_a_refusal_of_either_phase_rolls_back_and_is_reported_with_its_code_and_message(self):
        for phase in ("lock", "prepare"):
            with self.subTest(phase):
                a, b, candidate = self._pair()
                refusal = MergeRefusal("registry.merge.refused", "household H-1", message="Refused by the registry.")
                preparer = _RecordingPreparer(**{f"{phase}_refusal": refusal})
                self._register(preparer)

                with self.assertRaises(ResolveRefusal) as raised:
                    resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

                self.assertEqual(raised.exception.code, "registry.merge.refused")
                self.assertEqual(raised.exception.message, "Refused by the registry.")
                self.assertIn("household H-1", str(raised.exception))
                candidate.refresh_from_db()
                b.refresh_from_db()
                self.assertEqual(candidate.status, DuplicateCandidate.Status.OPEN)
                self.assertFalse(b.is_deleted)

    def test_a_refusal_after_a_write_rolls_the_write_back(self):
        a, b, candidate = self._pair()

        class WritingThenRefusing(_RecordingPreparer):
            def prepare(self, candidate, kept, retired, **kwargs):
                Individual.objects.filter(id=kept.id).update(first_name="Written")
                raise MergeRefusal("registry.merge.refused")

        self._register(WritingThenRefusing())

        with self.assertRaises(ResolveRefusal):
            resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        a.refresh_from_db()
        self.assertEqual(a.first_name, "Ann")

    def test_check_resolve_runs_prepare_in_check_mode_without_lock_and_writes_nothing(self):
        a, b, candidate = self._pair()
        preparer = self._register(_RecordingPreparer())

        with CaptureQueriesContext(connection) as ctx:
            check_resolve(candidate, decision="same", keep=str(a.id))

        self.assertEqual([call[0] for call in preparer.calls], ["prepare"])
        self.assertEqual(preparer.calls[0][3:5], (True, None))
        statements = [q["sql"].lstrip().upper() for q in ctx.captured_queries]
        self.assertFalse([sql for sql in statements if sql.startswith(WRITING_STATEMENTS)])
        self.assertFalse([sql for sql in statements if "FOR UPDATE" in sql])

    def test_check_resolve_reports_a_prepare_refusal(self):
        a, b, candidate = self._pair()
        self._register(_RecordingPreparer(prepare_refusal=MergeRefusal("registry.merge.refused", "H-1")))

        with self.assertRaises(ResolveRefusal) as raised:
            check_resolve(candidate, decision="same", keep=str(a.id))

        self.assertEqual(raised.exception.code, "registry.merge.refused")

    def test_check_resolve_counts_no_membership_a_preparer_releases(self):
        a, b, candidate = self._pair()
        group = Group(code="PREP-G2")
        group.save(username=self.user.username)
        GroupIndividual(group=group, individual=b).save(username=self.user.username)
        self._register(_RecordingPreparer(released={group.id}))

        check_resolve(candidate, decision="same", keep=str(a.id))

        register_merge_preparer("test", None)
        with self.assertRaises(ResolveRefusal) as raised:
            check_resolve(candidate, decision="same", keep=str(a.id))
        self.assertEqual(raised.exception.code, "deduplication.resolve.retired_subject_enrolled")

    def test_fill_false_leaves_the_kept_subject_unchanged(self):
        a, b, candidate = self._pair()
        self._register(_RecordingPreparer(fill=False))
        before = Individual.objects.filter(id=a.id).values().get()

        resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        self.assertEqual(Individual.objects.filter(id=a.id).values().get(), before)
        b.refresh_from_db()
        self.assertTrue(b.is_deleted)

    def test_any_preparer_answering_fill_false_skips_the_fill(self):
        a, b, candidate = self._pair()
        self._register(_RecordingPreparer(fill=True))
        register_merge_preparer("other", _RecordingPreparer(fill=False))
        self.addCleanup(register_merge_preparer, "other", None)

        resolve(candidate, decision="same", keep=str(a.id), actor=self.user)

        a.refresh_from_db()
        self.assertEqual(a.last_name, "")
        self.assertNotIn("merge_conflicts", a.json_ext)
