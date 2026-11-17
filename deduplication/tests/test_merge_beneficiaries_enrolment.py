import json
import uuid
from unittest import skipUnless

from django.apps import apps
from django.core.exceptions import ValidationError
from django.test import TestCase

from deduplication.services import CreateDeduplicationReviewTasksService
from deduplication.tests.helpers import LogInHelper
from individual.models import Individual
from social_protection import enrolment_hooks
from social_protection.models import Beneficiary, BeneficiaryStatus, BenefitPlan
from tasks_management.models import Task
from tasks_management.services import TaskService


class _RecordingHook:
    """An enrolment hook that records its calls; ``refuse`` makes ``after_write`` raise, ``drop_key`` makes
    ``before_service_write`` remove that json_ext key from the payload."""

    def __init__(self, refuse=False, drop_key=None):
        self.refuse = refuse
        self.drop_key = drop_key
        self.calls = []

    def before_service_write(self, model, obj_data, creating):
        self.calls.append(("service", model, dict(obj_data), creating))
        if self.drop_key and isinstance(obj_data.get("json_ext"), dict):
            obj_data["json_ext"] = {k: v for k, v in obj_data["json_ext"].items() if k != self.drop_key}

    def before_write(self, model, plan_ids):
        self.calls.append(("before", model, list(plan_ids)))
        return "state"

    def after_write(self, model, plan_ids, state):
        self.calls.append(("after", model, list(plan_ids), state))
        if self.refuse:
            raise ValidationError("refused by the hook")


class MergeDuplicateBeneficiariesEnrolmentTest(TestCase):
    """Completing a beneficiary review task merges the beneficiaries through the enrolment hooks."""

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()

    def setUp(self):
        self.plan = BenefitPlan(code="DDMRG", name="DDMRG", type=BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE,
                                beneficiary_data_schema={}, max_beneficiaries=1)
        self.plan.save(username=self.user.username)
        self.oldest = self._beneficiary("Old", BeneficiaryStatus.POTENTIAL, {"k1": "v1", "mark": "m"})
        self.other = self._beneficiary("New", BeneficiaryStatus.POTENTIAL, {"k1": "v1"})

    def _beneficiary(self, name, status, json_ext):
        person = Individual(first_name=name, last_name="Dup", dob="1990-01-01")
        person.save(username=self.user.username)
        beneficiary = Beneficiary(individual=person, benefit_plan=self.plan, status=status, json_ext=json_ext)
        beneficiary.save(username=self.user.username)
        return beneficiary

    def _hook(self, **kwargs):
        hook = _RecordingHook(**kwargs)
        enrolment_hooks.register_enrolment_hook(hook)
        self.addCleanup(enrolment_hooks.unregister_enrolment_hook, hook)
        return hook

    def _complete_review_task(self, values, task=None):
        ids = [str(self.oldest.id), str(self.other.id)]
        if task is None:
            task = Task(source=CreateDeduplicationReviewTasksService.get_class_name(), executor_action_event="x",
                        business_event="", data={"ids": ids})
        task.status = Task.Status.ACCEPTED
        task.json_ext = {"additional_resolve_data": {str(self.user.id): {"values": values, "beneficiaryIds": ids}}}
        task.save(username=self.user.username)
        result = TaskService(self.user).complete_task({"id": str(task.id)})
        self.assertTrue(result["success"], result)
        return Task.objects.get(id=task.id)

    def assertFailedWith(self, task, reason):
        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertIn(reason, task.json_ext["completion_error"])

    def assertCompleted(self, task):
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertNotIn("completion_error", task.json_ext or {})

    def _created_review_task(self):
        result = CreateDeduplicationReviewTasksService(self.user).create_beneficiary_duplication_tasks(
            [{"ids": [str(self.oldest.id), str(self.other.id)], "count": 2,
              "column_values": {"individual__last_name": "Dup"}}])
        self.assertTrue(result["success"], result)
        return Task.objects.get(id=result["data"][0]["data"]["id"])

    def _refresh(self):
        self.oldest.refresh_from_db()
        self.other.refresh_from_db()

    def test_merge_runs_between_the_hooks_of_the_plan_and_writes_through_the_service(self):
        hook = self._hook()
        self._complete_review_task({"status": BeneficiaryStatus.ACTIVE, "k1": "v2"})

        self._refresh()
        self.assertTrue(self.other.is_deleted)
        self.assertEqual(self.oldest.status, BeneficiaryStatus.ACTIVE)
        self.assertEqual(self.oldest.json_ext, {"k1": "v2", "mark": "m"})
        kinds = [call[0] for call in hook.calls]
        self.assertEqual(kinds, ["before", "service", "after"])
        self.assertEqual(hook.calls[0][1:], (Beneficiary, [str(self.plan.id)]))
        _, model, obj_data, creating = hook.calls[1]
        self.assertIs(model, Beneficiary)
        self.assertFalse(creating)
        self.assertEqual(obj_data["id"], str(self.oldest.id))
        self.assertEqual(obj_data["status"], BeneficiaryStatus.ACTIVE)
        self.assertEqual(hook.calls[2][1:], (Beneficiary, [str(self.plan.id)], "state"))

    def test_the_payload_the_service_hook_adjusts_is_the_one_saved(self):
        self._hook(drop_key="mark")
        self._complete_review_task({"status": BeneficiaryStatus.ACTIVE, "k2": "v2"})

        self._refresh()
        self.assertEqual(self.oldest.status, BeneficiaryStatus.ACTIVE)
        self.assertEqual(self.oldest.json_ext, {"k1": "v1", "k2": "v2"})

    def test_a_refusal_of_the_hooks_rolls_the_whole_merge_back(self):
        self._hook(refuse=True)
        self._complete_review_task({"status": BeneficiaryStatus.ACTIVE, "k1": "v2"})

        self._refresh()
        self.assertFalse(self.other.is_deleted)
        self.assertEqual(self.oldest.status, BeneficiaryStatus.POTENTIAL)
        self.assertEqual(self.oldest.json_ext, {"k1": "v1", "mark": "m"})

    def test_unchanged_status_is_not_sent(self):
        hook = self._hook()
        self._complete_review_task({"status": BeneficiaryStatus.POTENTIAL, "k1": "v2"})

        service_calls = [call for call in hook.calls if call[0] == "service"]
        self.assertEqual(len(service_calls), 1)
        self.assertNotIn("status", service_calls[0][2])
        self._refresh()
        self.assertEqual(self.oldest.json_ext, {"k1": "v2", "mark": "m"})

    def test_the_merged_rows_are_deleted_before_the_kept_one_is_activated(self):
        # The plan's cap is 1 and the row to delete is its only ACTIVE one: activating the kept row is
        # accepted once that row is gone.
        self.other.status = BeneficiaryStatus.ACTIVE
        self.other.save(username=self.user.username)
        self._complete_review_task({"status": BeneficiaryStatus.ACTIVE})

        self._refresh()
        self.assertTrue(self.other.is_deleted)
        self.assertEqual(self.oldest.status, BeneficiaryStatus.ACTIVE)

    def test_empty_values_merged_over_the_same_empty_values_change_nothing(self):
        self.oldest.json_ext = {"k1": "v1", "flag": False, "n": 0}
        self.oldest.save(username=self.user.username)
        hook = self._hook()
        self._complete_review_task({"flag": False, "n": 0, "absent": ""})

        self.assertEqual([call for call in hook.calls if call[0] == "service"], [])
        self._refresh()
        self.assertEqual(self.oldest.json_ext, {"k1": "v1", "flag": False, "n": 0})

    def test_a_task_records_the_state_of_its_beneficiaries_and_merges_when_it_holds(self):
        task = self._created_review_task()
        self.assertEqual(task.data["current_state"][str(self.oldest.id)],
                         {"status": BeneficiaryStatus.POTENTIAL, "version": self.oldest.version})
        self._complete_review_task({"status": BeneficiaryStatus.ACTIVE}, task)

        self._refresh()
        self.assertTrue(self.other.is_deleted)
        self.assertEqual(self.oldest.status, BeneficiaryStatus.ACTIVE)

    def test_a_kept_beneficiary_written_since_the_task_was_created_is_not_merged(self):
        task = self._created_review_task()
        self.oldest.status = BeneficiaryStatus.SUSPENDED
        self.oldest.save(username=self.user.username)
        self._complete_review_task({"status": BeneficiaryStatus.ACTIVE}, task)

        self._refresh()
        self.assertFalse(self.other.is_deleted)
        self.assertEqual(self.oldest.status, BeneficiaryStatus.SUSPENDED)

    def test_a_merge_written_completes_its_task(self):
        task = self._complete_review_task({"status": BeneficiaryStatus.ACTIVE}, self._created_review_task())

        self.assertCompleted(task)
        self._refresh()
        self.assertTrue(self.other.is_deleted)

    def test_a_merge_the_hooks_refuse_fails_its_task_with_the_reason(self):
        self._hook(refuse=True)
        task = self._complete_review_task({"status": BeneficiaryStatus.ACTIVE, "k1": "v2"})

        self.assertFailedWith(task, "refused by the hook")
        self._refresh()
        self.assertFalse(self.other.is_deleted)
        self.assertEqual(self.oldest.status, BeneficiaryStatus.POTENTIAL)
        self.assertEqual(self.oldest.json_ext, {"k1": "v1", "mark": "m"})

    def test_a_merge_refused_on_a_stale_kept_row_fails_its_task_and_keeps_the_rows(self):
        task = self._created_review_task()
        self.oldest.status = BeneficiaryStatus.SUSPENDED
        self.oldest.save(username=self.user.username)
        task = self._complete_review_task({"status": BeneficiaryStatus.ACTIVE, "k1": "v2"}, task)

        self.assertFailedWith(task, "changed since the review task was created")
        self._refresh()
        self.assertFalse(self.other.is_deleted)
        self.assertEqual(self.oldest.status, BeneficiaryStatus.SUSPENDED)
        self.assertEqual(self.oldest.json_ext, {"k1": "v1", "mark": "m"})

    def _complete_with_resolve_data(self, resolve_data, task_ids=None):
        """Completes, as ``self.user``, a review task of ``task_ids`` (the two rows by default) whose
        additional_resolve_data is ``resolve_data``."""
        task = Task(source=CreateDeduplicationReviewTasksService.get_class_name(), executor_action_event="x",
                    business_event="", data={"ids": task_ids or self._pair_ids()}, status=Task.Status.ACCEPTED,
                    json_ext={"additional_resolve_data": resolve_data})
        task.save(username=self.user.username)
        result = TaskService(self.user).complete_task({"id": str(task.id)})
        self.assertTrue(result["success"], result)
        return Task.objects.get(id=task.id)

    def _pair_ids(self):
        return [str(self.oldest.id), str(self.other.id)]

    def test_a_reviewer_id_outside_the_task_fails_it_and_nothing_is_merged(self):
        stranger = self._beneficiary("Stranger", BeneficiaryStatus.POTENTIAL, {"k1": "s"})
        task = self._complete_with_resolve_data({str(self.user.id): {
            "values": {"k1": "v2"}, "beneficiaryIds": self._pair_ids() + [str(stranger.id)]}})

        self.assertFailedWith(task, f"not in the review task: {stranger.id}")
        self._refresh()
        stranger.refresh_from_db()
        self.assertFalse(self.other.is_deleted)
        self.assertFalse(stranger.is_deleted)
        self.assertEqual(self.oldest.json_ext, {"k1": "v1", "mark": "m"})

    def test_the_rows_the_reviewer_leaves_out_of_the_merge_are_kept(self):
        third = self._beneficiary("Third", BeneficiaryStatus.POTENTIAL, {"k1": "t"})
        task = self._complete_with_resolve_data(
            {str(self.user.id): {"values": {"k1": "v2"}, "beneficiaryIds": self._pair_ids()}},
            task_ids=self._pair_ids() + [str(third.id)])

        self.assertCompleted(task)
        self._refresh()
        third.refresh_from_db()
        self.assertTrue(self.other.is_deleted)
        self.assertFalse(third.is_deleted)
        self.assertEqual(self.oldest.json_ext, {"k1": "v2", "mark": "m"})

    def test_a_merge_of_no_beneficiary_fails_its_task(self):
        task = self._complete_with_resolve_data(
            {str(self.user.id): {"values": {"k1": "v2"}, "beneficiaryIds": []}})

        self.assertFailedWith(task, "recorded no merge")
        self._refresh()
        self.assertEqual(self.oldest.json_ext, {"k1": "v1", "mark": "m"})

    def test_a_merge_of_a_single_beneficiary_fails_its_task_and_writes_nothing(self):
        task = self._complete_with_resolve_data(
            {str(self.user.id): {"values": {"k1": "v2"}, "beneficiaryIds": [str(self.oldest.id)]}})

        self.assertFailedWith(task, "fewer than two beneficiaries")
        self._refresh()
        self.assertFalse(self.other.is_deleted)
        self.assertEqual(self.oldest.json_ext, {"k1": "v1", "mark": "m"})

    def test_a_merge_whose_other_beneficiary_is_already_deleted_fails_its_task(self):
        self.other.delete(username=self.user.username)
        task = self._complete_with_resolve_data(
            {str(self.user.id): {"values": {"k1": "v2"}, "beneficiaryIds": self._pair_ids()}})

        self.assertFailedWith(task, "fewer than two beneficiaries")
        self._refresh()
        self.assertEqual(self.oldest.json_ext, {"k1": "v1", "mark": "m"})

    def test_a_deleted_beneficiary_is_neither_kept_nor_counted(self):
        third = self._beneficiary("Third", BeneficiaryStatus.POTENTIAL, {"k1": "t"})
        self.oldest.delete(username=self.user.username)
        ids = self._pair_ids() + [str(third.id)]
        task = self._complete_with_resolve_data(
            {str(self.user.id): {"values": {"k1": "v2"}, "beneficiaryIds": ids}}, task_ids=ids)

        self.assertCompleted(task)
        self._refresh()
        third.refresh_from_db()
        self.assertTrue(third.is_deleted)
        self.assertFalse(self.other.is_deleted)
        self.assertEqual(self.other.json_ext, {"k1": "v2"})

    def test_approvers_recording_different_merges_fail_the_task_without_naming_a_value(self):
        task = self._complete_with_resolve_data({
            str(uuid.uuid4()): {"values": {"k1": "first"}, "beneficiaryIds": self._pair_ids()},
            str(self.user.id): {"values": {"k1": "second"}, "beneficiaryIds": self._pair_ids()},
        })

        self.assertFailedWith(task, "recorded different merges")
        self.assertNotIn("first", task.json_ext["completion_error"])
        self.assertNotIn("second", task.json_ext["completion_error"])
        self._refresh()
        self.assertFalse(self.other.is_deleted)
        self.assertEqual(self.oldest.json_ext, {"k1": "v1", "mark": "m"})

    def test_approvers_recording_the_same_merge_in_another_order_merge_it(self):
        task = self._complete_with_resolve_data({
            str(uuid.uuid4()): {"values": {"k1": "v2"}, "beneficiaryIds": self._pair_ids()},
            str(self.user.id): {"values": {"k1": "v2"}, "beneficiaryIds": self._pair_ids()[::-1]},
        })

        self.assertCompleted(task)
        self._refresh()
        self.assertTrue(self.other.is_deleted)
        self.assertEqual(self.oldest.json_ext, {"k1": "v2", "mark": "m"})

    def test_the_merge_another_approver_recorded_is_used_when_the_completing_user_recorded_none(self):
        task = self._complete_with_resolve_data({
            str(uuid.uuid4()): {"values": {"k1": "recorded"}, "beneficiaryIds": self._pair_ids()},
            str(self.user.id): {"values": {}, "beneficiaryIds": []},
        })

        self.assertCompleted(task)
        self._refresh()
        self.assertTrue(self.other.is_deleted)
        self.assertEqual(self.oldest.json_ext, {"k1": "recorded", "mark": "m"})

    def _with_schema(self, schema):
        self.plan.beneficiary_data_schema = schema
        self.plan.save(username=self.user.username)

    def test_merged_values_violating_the_plan_schema_fail_the_task(self):
        self._with_schema({"type": "object", "properties": {"age": {"type": "integer"}}})
        task = self._complete_with_resolve_data({str(self.user.id): {
            "values": {"age": "twelve"}, "beneficiaryIds": self._pair_ids()}})

        self.assertFailedWith(task, "age: type integer")
        self.assertNotIn("twelve", task.json_ext["completion_error"])
        self._refresh()
        self.assertFalse(self.other.is_deleted)
        self.assertEqual(self.oldest.json_ext, {"k1": "v1", "mark": "m"})

    def test_a_merge_removing_a_required_key_fails_the_task(self):
        self._with_schema({"type": "object", "properties": {"k1": {"type": "string"}}, "required": ["k1"]})
        task = self._complete_with_resolve_data({str(self.user.id): {
            "values": {"k1": ""}, "beneficiaryIds": self._pair_ids()}})

        self.assertFailedWith(task, "'k1' is a required property")
        self._refresh()
        self.assertFalse(self.other.is_deleted)
        self.assertEqual(self.oldest.json_ext, {"k1": "v1", "mark": "m"})

    def test_merged_values_matching_the_plan_schema_are_written(self):
        self._with_schema(json.dumps({"type": "object", "properties": {"age": {"type": "integer"}}}))
        task = self._complete_with_resolve_data({str(self.user.id): {
            "values": {"age": 12}, "beneficiaryIds": self._pair_ids()}})

        self.assertCompleted(task)
        self._refresh()
        self.assertTrue(self.other.is_deleted)
        self.assertEqual(self.oldest.json_ext, {"k1": "v1", "mark": "m", "age": 12})


@skipUnless(apps.is_installed("programme_targeting"), "programme_targeting registers the enrolment rules")
class MergeUnderTheEnrolmentRulesTest(TestCase):
    """The merge under programme_targeting's enrolment rules, registered at startup."""

    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()

    def _plan(self, code, cap):
        plan = BenefitPlan(code=code, name=code, type=BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE,
                           beneficiary_data_schema={}, max_beneficiaries=cap)
        plan.save(username=self.user.username)
        return plan

    def _beneficiary(self, plan, name, status, json_ext=None):
        person = Individual(first_name=name, last_name="Rules", dob="1990-01-01")
        person.save(username=self.user.username)
        beneficiary = Beneficiary(individual=person, benefit_plan=plan, status=status, json_ext=json_ext or {})
        beneficiary.save(username=self.user.username)
        return beneficiary

    def _merge(self, kept, other, values):
        ids = [str(kept.id), str(other.id)]
        result = CreateDeduplicationReviewTasksService(self.user).create_beneficiary_duplication_tasks(
            [{"ids": ids, "count": 2, "column_values": {"individual__last_name": "Rules"}}])
        self.assertTrue(result["success"], result)
        task = Task.objects.get(id=result["data"][0]["data"]["id"])
        task.status = Task.Status.ACCEPTED
        task.json_ext = {"additional_resolve_data": {str(self.user.id): {"values": values, "beneficiaryIds": ids}}}
        task.save(username=self.user.username)
        self.assertTrue(TaskService(self.user).complete_task({"id": str(task.id)})["success"])
        kept.refresh_from_db()
        other.refresh_from_db()
        return Task.objects.get(id=task.id)

    def test_a_merge_that_would_raise_the_plan_over_its_cap_is_refused(self):
        plan = self._plan("DDRCAP", 1)
        self._beneficiary(plan, "Active", BeneficiaryStatus.ACTIVE)
        kept = self._beneficiary(plan, "Kept", BeneficiaryStatus.POTENTIAL)
        other = self._beneficiary(plan, "Other", BeneficiaryStatus.POTENTIAL)
        self._merge(kept, other, {"status": BeneficiaryStatus.ACTIVE})

        self.assertEqual(kept.status, BeneficiaryStatus.POTENTIAL)
        self.assertFalse(other.is_deleted)
        self.assertEqual(Beneficiary.objects.filter(
            benefit_plan=plan, is_deleted=False, status=BeneficiaryStatus.ACTIVE).count(), 1)

    def test_a_merge_refused_over_the_cap_fails_its_task_with_the_reason(self):
        plan = self._plan("DDRCAPF", 1)
        self._beneficiary(plan, "Active", BeneficiaryStatus.ACTIVE)
        kept = self._beneficiary(plan, "Kept", BeneficiaryStatus.POTENTIAL, {"k": "v"})
        other = self._beneficiary(plan, "Other", BeneficiaryStatus.POTENTIAL)
        task = self._merge(kept, other, {"status": BeneficiaryStatus.ACTIVE, "k": "w"})

        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertIn("Benefit plan is already at max active beneficiaries", task.json_ext["completion_error"])
        self.assertEqual((kept.status, kept.json_ext), (BeneficiaryStatus.POTENTIAL, {"k": "v"}))
        self.assertFalse(other.is_deleted)

    def test_activating_a_selection_suspended_row_removes_the_suspension_key(self):
        from programme_targeting.enrolment_rules import SUSPENSION_KEY

        plan = self._plan("DDRKEY", None)
        kept = self._beneficiary(plan, "Kept", BeneficiaryStatus.SUSPENDED, {SUSPENSION_KEY: "request-1", "k": "v"})
        other = self._beneficiary(plan, "Other", BeneficiaryStatus.POTENTIAL)
        self._merge(kept, other, {"status": BeneficiaryStatus.ACTIVE})

        self.assertEqual(kept.status, BeneficiaryStatus.ACTIVE)
        self.assertEqual(kept.json_ext, {"k": "v"})
        self.assertTrue(other.is_deleted)

    def _complete(self, task_ids, beneficiary_ids, values):
        task = Task(source=CreateDeduplicationReviewTasksService.get_class_name(), executor_action_event="x",
                    business_event="", data={"ids": task_ids}, status=Task.Status.ACCEPTED,
                    json_ext={"additional_resolve_data": {str(self.user.id): {
                        "values": values, "beneficiaryIds": beneficiary_ids}}})
        task.save(username=self.user.username)
        self.assertTrue(TaskService(self.user).complete_task({"id": str(task.id)})["success"])
        return Task.objects.get(id=task.id)

    def test_an_active_row_outside_the_task_is_neither_deleted_nor_freeing_the_cap(self):
        plan = self._plan("DDROUT", 1)
        kept = self._beneficiary(plan, "Kept", BeneficiaryStatus.POTENTIAL)
        other = self._beneficiary(plan, "Other", BeneficiaryStatus.POTENTIAL)
        active = self._beneficiary(plan, "Active", BeneficiaryStatus.ACTIVE)
        pair = [str(kept.id), str(other.id)]
        task = self._complete(pair, pair + [str(active.id)], {"status": BeneficiaryStatus.ACTIVE})

        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertIn(f"not in the review task: {active.id}", task.json_ext["completion_error"])
        for row in (kept, other, active):
            row.refresh_from_db()
        self.assertEqual((kept.status, other.is_deleted), (BeneficiaryStatus.POTENTIAL, False))
        self.assertEqual((active.status, active.is_deleted), (BeneficiaryStatus.ACTIVE, False))

    def test_an_activation_with_values_violating_the_schema_is_not_written(self):
        plan = self._plan("DDRSCH", None)
        plan.beneficiary_data_schema = {"type": "object", "properties": {"age": {"type": "integer"}}}
        plan.save(username=self.user.username)
        kept = self._beneficiary(plan, "Kept", BeneficiaryStatus.POTENTIAL, {"age": 3})
        other = self._beneficiary(plan, "Other", BeneficiaryStatus.POTENTIAL)
        pair = [str(kept.id), str(other.id)]
        task = self._complete(pair, pair, {"status": BeneficiaryStatus.ACTIVE, "age": "x"})

        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertIn("age: type integer", task.json_ext["completion_error"])
        kept.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((kept.status, kept.json_ext), (BeneficiaryStatus.POTENTIAL, {"age": 3}))
        self.assertFalse(other.is_deleted)
