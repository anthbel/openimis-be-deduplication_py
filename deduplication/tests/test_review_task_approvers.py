from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from core.test_helpers import create_admin_role, create_test_interactive_user
from deduplication.services import CreateDeduplicationPaymentReviewTasksService, CreateDeduplicationReviewTasksService
from deduplication.tests.helpers import LogInHelper
from individual.models import Individual
from invoice.models import Bill, BillItem
from payment_cycle.models import PaymentCycle
from payroll.models import BenefitAttachment, BenefitConsumption, Payroll, PayrollBenefitConsumption, PayrollStatus
from social_protection.models import Beneficiary, BeneficiaryStatus, BenefitPlan
from tasks_management.models import Task, TaskExecutor, TaskGroup
from tasks_management.services import TaskService


class ReviewTaskApproversTest(TestCase):
    """Review tasks resolved by two approvers under policy ALL, as the frontend resolves them: the first
    approver records the selection, the second, shown the task as already resolved, sends an empty one."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = LogInHelper().get_or_create_user_api()
        cls.second = create_test_interactive_user(username="DDApproverTwo", roles=[create_admin_role().id])

    def _person(self, name):
        person = Individual(first_name=name, last_name="Approvers", dob="1990-01-01")
        person.save(user=self.admin)
        return person

    def _beneficiaries(self, code):
        plan = BenefitPlan(code=code, name=code, type=BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE,
                           beneficiary_data_schema={})
        plan.save(user=self.admin)
        rows = []
        for name, json_ext in (("Kept", {"k": "v"}), ("Other", {"k": "w"})):
            row = Beneficiary(individual=self._person(name), benefit_plan=plan, status=BeneficiaryStatus.POTENTIAL,
                              json_ext=json_ext)
            row.save(user=self.admin)
            rows.append(row)
        return rows

    def _under_policy_all(self, result):
        self.assertTrue(result["success"], result)
        group = TaskGroup(code=f"dd-all-{Task.objects.count()}",
                          completion_policy=TaskGroup.TaskGroupCompletionPolicy.ALL)
        group.save(user=self.admin)
        for approver in (self.admin, self.second):
            TaskExecutor(user=approver, task_group=group).save(user=self.admin)
        task = Task.objects.get(id=result["data"][0]["data"]["id"])
        task.task_group = group
        task.status = Task.Status.ACCEPTED
        task.save(user=self.admin)
        return task

    @staticmethod
    def _resolve(task, approver, selection):
        result = TaskService(approver).resolve_task({
            "id": str(task.id), "business_status": {str(approver.id): "APPROVED"}, "additional_data": selection})
        assert result["success"], result

    def _merge_in_order(self, code, first, second):
        kept, other = self._beneficiaries(code)
        ids = [str(kept.id), str(other.id)]
        service = CreateDeduplicationReviewTasksService(self.admin)
        task = self._under_policy_all(service.create_beneficiary_duplication_tasks(
            [{"ids": ids, "count": 2, "column_values": {"individual__last_name": "Approvers"}}]))
        self._resolve(task, first, {"values": {"k": "merged"}, "beneficiaryIds": ids})
        self._resolve(task, second, {"values": {}, "beneficiaryIds": []})
        task.refresh_from_db()
        kept.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED, task.json_ext)
        self.assertTrue(other.is_deleted)
        self.assertEqual(kept.json_ext, {"k": "merged"})

    def test_a_merge_recorded_by_the_first_of_two_approvers_is_merged(self):
        self._merge_in_order("DDAPP1", self.admin, self.second)

    def test_a_merge_recorded_by_the_first_of_two_approvers_is_merged_in_the_other_order(self):
        self._merge_in_order("DDAPP2", self.second, self.admin)

    def _payment_cycle(self):
        cycle = PaymentCycle(code="DDAPP-PC", start_date="2026-01-01", end_date="2026-01-31")
        cycle.save(user=self.admin)
        return cycle

    def _billed_benefit(self, name, payroll):
        """A benefit of a new person, attached to a bill with one line, on ``payroll``."""
        person = self._person(name)
        benefit = BenefitConsumption(individual=person, code=f"DDAPP-{name}")
        benefit.save(user=self.admin)
        person_type = ContentType.objects.get_for_model(person)
        bill = Bill(code=f"DDAPP-B-{name}", subject_type=person_type, subject_id=person.id,
                    thirdparty_type=person_type, thirdparty_id=person.id, date_bill="2026-01-15",
                    amount_net=10, amount_total=10, status=Bill.Status.VALIDATED)
        bill.save(user=self.admin)
        line = BillItem(code=f"DDAPP-L-{name}", bill=bill, line_type=person_type, line_id=person.id,
                        quantity=1, unit_price=10, amount_net=10, amount_total=10)
        line.save(user=self.admin)
        attachment = BenefitAttachment(benefit=benefit, bill=bill)
        attachment.save(user=self.admin)
        link = PayrollBenefitConsumption(payroll=payroll, benefit=benefit)
        link.save(user=self.admin)
        return benefit, [attachment, bill, line, link]

    def _payroll(self, cycle):
        payroll = Payroll(name="DDAPP-P", payment_cycle=cycle, status=PayrollStatus.PENDING_APPROVAL)
        payroll.save(user=self.admin)
        return payroll

    def _payment_task(self, benefits, cycle):
        return self._under_policy_all(
            CreateDeduplicationPaymentReviewTasksService(self.admin).create_payment_benefit_duplication_tasks(
                [{"ids": [str(benefit.id) for benefit in benefits], "count": 2,
                  "column_values": {"individual__first_name": "Twin"}}], str(cycle.id)))

    @staticmethod
    def _live(record):
        return type(record).objects.filter(id=record.id, is_deleted=False).exists()

    def _removal_in_order(self, suffix, first, second):
        cycle = self._payment_cycle()
        payroll = self._payroll(cycle)
        kept, _ = self._billed_benefit(f"Kept{suffix}", payroll)
        removed, removed_records = self._billed_benefit(f"Removed{suffix}", payroll)
        task = self._payment_task([kept, removed], cycle)
        self._resolve(task, first, {"benefitIds": [str(removed.id)]})
        self._resolve(task, second, {"benefitIds": []})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED, task.json_ext)
        self.assertTrue(self._live(kept))
        self.assertFalse(any(self._live(record) for record in [removed, *removed_records]))

    def test_a_removal_recorded_by_the_first_of_two_approvers_is_removed(self):
        self._removal_in_order("A", self.admin, self.second)

    def test_a_removal_recorded_by_the_first_of_two_approvers_is_removed_in_the_other_order(self):
        self._removal_in_order("B", self.second, self.admin)

    def test_a_removal_of_a_benefit_outside_the_task_fails_it_and_removes_nothing(self):
        cycle = self._payment_cycle()
        payroll = self._payroll(cycle)
        first, _ = self._billed_benefit("First", payroll)
        second, _ = self._billed_benefit("Second", payroll)
        stranger, stranger_records = self._billed_benefit("Stranger", payroll)
        task = self._payment_task([first, second], cycle)
        self._resolve(task, self.admin, {"benefitIds": [str(second.id), str(stranger.id)]})
        self._resolve(task, self.second, {"benefitIds": []})

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertIn(f"Benefits not in the review task: {stranger.id}", task.json_ext["completion_error"])
        self.assertTrue(all(self._live(record) for record in [second, stranger, *stranger_records]))

    def test_a_payment_task_approved_with_no_selection_fails_with_a_readable_reason(self):
        cycle = self._payment_cycle()
        payroll = self._payroll(cycle)
        benefits = [self._billed_benefit(name, payroll)[0] for name in ("NoneA", "NoneB")]
        task = self._payment_task(benefits, cycle)
        self._resolve(task, self.admin, None)
        self._resolve(task, self.second, None)

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertEqual(task.json_ext["completion_error"], "The approvers of the review task recorded no removal")
        self.assertTrue(all(self._live(benefit) for benefit in benefits))
