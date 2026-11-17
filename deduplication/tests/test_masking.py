"""Candidate evidence and duplication summaries apply individual's masks to a user without the masked-data right."""
import json
from unittest import mock

import graphene

from core.data_masking import MaskingClassStorage
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from deduplication.models import DuplicateCandidate
from deduplication.sources import order_pair
from deduplication.tests.data.dedup_beneficiary import benefit_plan_data
from deduplication.tests.helpers import LogInHelper
from individual.apps import IndividualConfig
from individual.data_masking import GroupMask, IndividualMask
from individual.models import Group, Individual
from deduplication.services import CreateDeduplicationPaymentReviewTasksService, CreateDeduplicationReviewTasksService
from payroll.models import BenefitConsumption
from social_protection.models import Beneficiary, BeneficiaryStatus, BenefitPlan
from tasks_management.models import Task, TaskExecutor, TaskGroup

MASKED = "***************"
UNMASK = "gql_query_enable_viewing_masked_data_perms"

CANDIDATES_QUERY = """
query Candidates($subject: String) {
  duplicateCandidates(subjectId: $subject, first: 10) { edges { node { kind evidence } } }
}
"""
BENEFICIARY_SUMMARY_QUERY = """
query Summary($plan: UUID!) {
  beneficiaryDeduplicationSummary(columns: ["first_name", "last_name", "phone", "k1"], benefitPlanId: $plan) {
    rows { count columnValues }
  }
}
"""
BENEFIT_SUMMARY_QUERY = """
query { benefitDeduplicationSummary(columns: ["first_name", "last_name", "phone"], paymentCycleId: "1") {
  rows { count columnValues }
} }
"""

COLUMN_SUMMARY_QUERY = """
query Summary($plan: UUID!, $columns: [String]!) {
  beneficiaryDeduplicationSummary(columns: $columns, benefitPlanId: $plan) { rows { count columnValues } }
}
"""
TASK_QUERY = """
query Task($id: ID!) { node(id: $id) { ... on TaskGQLType { businessData } } }
"""
# Each form a summary column takes to read the person's json_ext.phone or first_name, or a whole json_ext
# holding the person's phone.
PERSON_COLUMN_FORMS = ["phone", "json_ext__phone", "json_ext", "first_name", "individual__first_name",
                       "individual__json_ext__phone", "individual__json_ext"]


def beneficiary_mask_off(test):
    """Turns social_protection's own beneficiary mask off, so only the person's mask applies."""
    patcher = mock.patch.object(MaskingClassStorage.get_masking_class("Beneficiary"), "masking_enabled", False)
    patcher.start()
    test.addCleanup(patcher.stop)


class MaskedFieldsMixin:
    """Masks first_name, dob and json_ext.phone of a person and json_ext.income of a group, and nothing else."""

    def setUp(self):
        super().setUp()
        for mask, fields in ((IndividualMask, ["first_name", "dob", "json_ext.phone"]),
                             (GroupMask, ["json_ext.income"])):
            patcher = mock.patch.dict(mask._sources, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
            mask.register_fields("test", lambda fields=fields: list(fields))
        own = mock.patch.object(IndividualConfig, "individual_masking_enabled", False)
        own.start()
        self.addCleanup(own.stop)


class CandidateEvidenceMaskingTest(MaskedFieldsMixin, openIMISGraphQLTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = LogInHelper().get_or_create_user_api()
        masked = create_right_only_user("dedup_mask_masked", ["gql_query_duplicates_perms"])
        unmasked = create_right_only_user("dedup_mask_unmasked", ["gql_query_duplicates_perms", UNMASK])
        cls.masked_token = BaseTestContext(user=masked).get_jwt()
        cls.unmasked_token = BaseTestContext(user=unmasked).get_jwt()

        a = Individual(first_name="Maskfirst", last_name="Clearlast", dob="1990-01-01")
        a.save(username=cls.admin.username)
        b = Individual(first_name="Maskfirst", last_name="Clearlast", dob="1990-01-01")
        b.save(username=cls.admin.username)
        cls.subject_a, subject_b = order_pair(str(a.id), str(b.id))
        DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=cls.subject_a, subject_b=subject_b,
            kind="demographic", source="TestSource",
            evidence={"columns": {"first_name": "Maskfirst", "last_name": "Clearlast", "dob": "1990-01-01"}},
        )
        DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=cls.subject_a, subject_b=subject_b,
            kind="identifier", source="TestSource",
            evidence={"columns": {"phone": "+25779000001", "nin": "NIN-1"}},
        )
        g1 = Group(code="MASKG1")
        g1.save(username=cls.admin.username)
        g2 = Group(code="MASKG2")
        g2.save(username=cls.admin.username)
        cls.group_a, group_b = order_pair(str(g1.id), str(g2.id))
        DuplicateCandidate.objects.create(
            subject_model="individual.Group", subject_a=cls.group_a, subject_b=group_b,
            kind="identifier", source="TestSource",
            evidence={"columns": {"income": "1200", "code": "MASKG"}},
        )

    def _evidence(self, token, subject):
        response = self.query(CANDIDATES_QUERY, variables={"subject": subject},
                              headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
        self.assertResponseNoErrors(response)
        edges = json.loads(response.content)["data"]["duplicateCandidates"]["edges"]
        return {edge["node"]["kind"]: json.loads(edge["node"]["evidence"]) for edge in edges}

    def test_person_evidence_is_masked_without_the_masked_data_right(self):
        evidence = self._evidence(self.masked_token, self.subject_a)
        self.assertEqual(evidence["demographic"]["columns"],
                         {"first_name": MASKED, "last_name": "Clearlast", "dob": MASKED})
        self.assertEqual(evidence["identifier"]["columns"], {"phone": MASKED, "nin": "NIN-1"})

    def test_person_evidence_is_clear_with_the_masked_data_right(self):
        evidence = self._evidence(self.unmasked_token, self.subject_a)
        self.assertEqual(evidence["demographic"]["columns"],
                         {"first_name": "Maskfirst", "last_name": "Clearlast", "dob": "1990-01-01"})
        self.assertEqual(evidence["identifier"]["columns"], {"phone": "+25779000001", "nin": "NIN-1"})

    def test_group_evidence_takes_the_group_mask(self):
        evidence = self._evidence(self.masked_token, self.group_a)
        self.assertEqual(evidence["identifier"]["columns"], {"income": MASKED, "code": "MASKG"})

    def test_the_stored_evidence_is_unchanged(self):
        self._evidence(self.masked_token, self.subject_a)
        stored = DuplicateCandidate.objects.get(subject_a=self.subject_a, kind="demographic")
        self.assertEqual(stored.evidence["columns"]["first_name"], "Maskfirst")


class SummaryMaskingTest(MaskedFieldsMixin, openIMISGraphQLTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = LogInHelper().get_or_create_user_api()
        masked = create_right_only_user("dedup_sum_masked", ["gql_beneficiary_search_perms"])
        unmasked = create_right_only_user("dedup_sum_unmasked", ["gql_beneficiary_search_perms", UNMASK])
        cls.masked_token = BaseTestContext(user=masked).get_jwt()
        cls.unmasked_token = BaseTestContext(user=unmasked).get_jwt()

        cls.plan = BenefitPlan(**{**benefit_plan_data, "code": "MASKBP"})
        cls.plan.save(username=cls.admin.username)
        for _ in range(2):
            person = Individual(first_name="Maskfirst", last_name="Clearlast", dob="1990-01-01",
                                json_ext={"phone": "+25779000001", "k1": "v1"})
            person.save(username=cls.admin.username)
            Beneficiary(individual=person, benefit_plan=cls.plan, status=BeneficiaryStatus.ACTIVE,
                        json_ext={"phone": "+25779000001", "k1": "v1"}).save(username=cls.admin.username)

    def _rows(self, token, query, variables=None):
        response = self.query(query, variables=variables, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
        self.assertResponseNoErrors(response)
        data = json.loads(response.content)["data"]
        rows = next(iter(data.values()))["rows"]
        return [(row["count"], json.loads(row["columnValues"])) for row in rows]

    def test_beneficiary_summary_is_masked_without_the_masked_data_right(self):
        rows = self._rows(self.masked_token, BENEFICIARY_SUMMARY_QUERY, {"plan": str(self.plan.id)})
        self.assertEqual(rows, [(2, {"individual__first_name": MASKED, "individual__last_name": "Clearlast",
                                     "phone": MASKED, "k1": "v1"})])

    def test_beneficiary_summary_is_clear_with_the_masked_data_right(self):
        rows = self._rows(self.unmasked_token, BENEFICIARY_SUMMARY_QUERY, {"plan": str(self.plan.id)})
        self.assertEqual(rows, [(2, {"individual__first_name": "Maskfirst", "individual__last_name": "Clearlast",
                                     "phone": "+25779000001", "k1": "v1"})])

    def _benefit_rows(self, token):
        aggregation = [{"individual__first_name": "Maskfirst", "individual__last_name": "Clearlast",
                        "phone": "+25779000001", "id_count": 2, "ids": []}]
        with mock.patch("deduplication.services.get_benefit_consumption_duplication_aggregation",
                        return_value=aggregation):
            return self._rows(token, BENEFIT_SUMMARY_QUERY)

    def test_benefit_summary_is_masked_without_the_masked_data_right(self):
        self.assertEqual(self._benefit_rows(self.masked_token), [(2, {
            "individual__first_name": MASKED, "individual__last_name": "Clearlast", "phone": MASKED})])

    def test_benefit_summary_is_clear_with_the_masked_data_right(self):
        self.assertEqual(self._benefit_rows(self.unmasked_token), [(2, {
            "individual__first_name": "Maskfirst", "individual__last_name": "Clearlast", "phone": "+25779000001"})])

    def test_every_column_form_reading_a_masked_person_field_is_masked_in_the_beneficiary_summary(self):
        beneficiary_mask_off(self)
        for column in PERSON_COLUMN_FORMS:
            with self.subTest(column=column):
                rows = self._rows(self.masked_token, COLUMN_SUMMARY_QUERY,
                                  {"plan": str(self.plan.id), "columns": [column]})
                self.assertEqual(len(rows), 1, rows)
                self.assertEqual(set(rows[0][1].values()), {MASKED}, rows)

    def test_every_column_form_is_clear_in_the_beneficiary_summary_with_the_masked_data_right(self):
        beneficiary_mask_off(self)
        for column in PERSON_COLUMN_FORMS:
            with self.subTest(column=column):
                rows = self._rows(self.unmasked_token, COLUMN_SUMMARY_QUERY,
                                  {"plan": str(self.plan.id), "columns": [column]})
                self.assertNotIn(MASKED, rows[0][1].values())

    def test_every_column_form_reading_a_masked_person_field_is_masked_in_the_benefit_summary(self):
        row = {"individual__first_name": "Maskfirst", "individual__json_ext__phone": "+25779000001",
               "individual__json_ext": "{'phone': '+25779000001'}", "json_ext__phone": "+25779000001",
               "json_ext": "{'phone': '+25779000001'}", "phone": "+25779000001", "k1": "v1"}
        with mock.patch("deduplication.services.get_benefit_consumption_duplication_aggregation",
                        return_value=[{**row, "id_count": 2, "ids": []}]):
            rows = self._rows(self.masked_token, BENEFIT_SUMMARY_QUERY)
        self.assertEqual(rows, [(2, {**{column: MASKED for column in row}, "k1": "v1"})])


class LegacyReviewTaskMaskingTest(MaskedFieldsMixin, openIMISGraphQLTestCase):
    """The business data of the beneficiary and payment duplicate review tasks, as each reader may see it."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = LogInHelper().get_or_create_user_api()
        masked = create_right_only_user("dedup_task_masked", ["gql_task_search_perms"])
        unmasked = create_right_only_user("dedup_task_unmasked", ["gql_task_search_perms", UNMASK])
        cls.masked_token = BaseTestContext(user=masked).get_jwt()
        cls.unmasked_token = BaseTestContext(user=unmasked).get_jwt()
        group = TaskGroup(code="dedup-mask-tasks", completion_policy=TaskGroup.TaskGroupCompletionPolicy.ANY)
        group.save(username=cls.admin.username)
        for user in (masked, unmasked):
            TaskExecutor(user=user, task_group=group).save(username=cls.admin.username)

        plan = BenefitPlan(**{**benefit_plan_data, "code": "MASKTBP"})
        plan.save(username=cls.admin.username)
        beneficiaries, benefits = [], []
        for _ in range(2):
            person = Individual(first_name="Maskfirst", last_name="Clearlast", dob="1990-01-01",
                                json_ext={"phone": "+25779000001"})
            person.save(username=cls.admin.username)
            beneficiary = Beneficiary(individual=person, benefit_plan=plan, status=BeneficiaryStatus.ACTIVE,
                                      json_ext={"phone": "+25779000001", "k1": "v1"})
            beneficiary.save(username=cls.admin.username)
            beneficiaries.append(str(beneficiary.id))
            benefit = BenefitConsumption(individual=person, code="MASKB", json_ext={"phone": "+25779000001"})
            benefit.save(username=cls.admin.username)
            benefits.append(str(benefit.id))

        def task(service, method, ids, column_values):
            task = Task(
                source=service.__name__, executor_action_event="x", business_event="",
                business_data_serializer=f"deduplication.services.{service.__name__}.{method}",
                data={"ids": ids, "count": 2, "column_values": column_values},
                status=Task.Status.ACCEPTED, task_group=group,
            )
            task.save(username=cls.admin.username)
            return graphene.relay.Node.to_global_id("TaskGQLType", str(task.id))

        cls.beneficiary_task = task(
            CreateDeduplicationReviewTasksService, "create_beneficiary_duplication_task_serializer", beneficiaries,
            {"individual__first_name": "Maskfirst", "phone": "+25779000001", "k1": "v1"})
        cls.payment_task = task(
            CreateDeduplicationPaymentReviewTasksService, "create_payment_benefit_duplication_task_serializer",
            benefits, {"individual__first_name": "Maskfirst", "phone": "+25779000001"})

    def _business_data(self, token, gid):
        response = self.query(TASK_QUERY, variables={"id": gid}, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
        self.assertResponseNoErrors(response)
        return json.loads(json.loads(response.content)["data"]["node"]["businessData"])

    def test_beneficiary_review_task_is_masked_without_the_masked_data_right(self):
        data = self._business_data(self.masked_token, self.beneficiary_task)
        self.assertEqual(data["column_values"], {"individual__first_name": MASKED, "phone": MASKED, "k1": "v1"})
        for beneficiary in data["ids"]:
            self.assertEqual(beneficiary["individual"]["first_name"], MASKED)
            self.assertEqual(beneficiary["individual"]["dob"], MASKED)
            self.assertEqual(beneficiary["individual"]["last_name"], "Clearlast")
            self.assertEqual(beneficiary["json_ext"], {"phone": MASKED, "k1": "v1"})

    def test_beneficiary_review_task_is_clear_with_the_masked_data_right(self):
        data = self._business_data(self.unmasked_token, self.beneficiary_task)
        self.assertEqual(data["column_values"]["individual__first_name"], "Maskfirst")
        for beneficiary in data["ids"]:
            self.assertEqual(beneficiary["individual"]["first_name"], "Maskfirst")
            self.assertEqual(beneficiary["individual"]["dob"], "1990-01-01")
            self.assertEqual(beneficiary["json_ext"], {"phone": "+25779000001", "k1": "v1"})

    def test_payment_review_task_is_masked_without_the_masked_data_right(self):
        data = self._business_data(self.masked_token, self.payment_task)
        self.assertEqual(data["column_values"], {"individual__first_name": MASKED, "phone": MASKED})
        for benefit in data["ids"]:
            self.assertEqual(benefit["individual"]["first_name"], MASKED)
            self.assertEqual(benefit["individual"]["dob"], MASKED)
            self.assertEqual(benefit["individual"]["last_name"], "Clearlast")
            self.assertEqual(benefit["json_ext"], {"phone": MASKED})

    def test_payment_review_task_is_clear_with_the_masked_data_right(self):
        data = self._business_data(self.unmasked_token, self.payment_task)
        for benefit in data["ids"]:
            self.assertEqual(benefit["individual"]["first_name"], "Maskfirst")
            self.assertEqual(benefit["json_ext"], {"phone": "+25779000001"})
