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

    def _complete_review_task(self, values):
        task = Task(
            source=CreateDeduplicationReviewTasksService.get_class_name(), executor_action_event="x",
            business_event="", data={"ids": [str(self.oldest.id), str(self.other.id)]},
            status=Task.Status.ACCEPTED,
            json_ext={"additional_resolve_data": {str(self.user.id): {
                "values": values, "beneficiaryIds": [str(self.oldest.id), str(self.other.id)]}}},
        )
        task.save(username=self.user.username)
        result = TaskService(self.user).complete_task({"id": str(task.id)})
        self.assertTrue(result["success"], result)

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
