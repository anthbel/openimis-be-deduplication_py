"""The evidence a review task copies from its candidate is masked by individual's task mask for a reader without
the masked-data right, on the task queries and from the candidate."""
import json
from unittest import mock

import graphene

from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from deduplication.models import DuplicateCandidate
from deduplication.services import (
    DUPLICATE_CANDIDATE_TASK_SOURCE, CreateDeduplicationPaymentReviewTasksService,
    CreateDeduplicationReviewTasksService, create_review_tasks,
)
from deduplication.sources import order_pair
from deduplication.tests.data.dedup_beneficiary import benefit_plan_data
from deduplication.tests.helpers import LogInHelper
from deduplication.tests.test_masking import MaskedFieldsMixin
from individual.apps import IndividualConfig
from individual.data_masking import IndividualMask
from individual.models import Individual
from payroll.models import BenefitConsumption
from social_protection.models import Beneficiary, BeneficiaryStatus, BenefitPlan
from tasks_management.models import Task, TaskExecutor, TaskGroup

MASKED = "***************"
UNMASK = "gql_query_enable_viewing_masked_data_perms"
TRIAGE = ["gql_task_search_perms", "gql_task_group_search_perms", "gql_task_group_create_perms",
          "gql_task_group_update_perms", "gql_task_group_delete_perms"]

TASK_QUERY = """
query { task(source: "%s") { edges { node { data businessData } } } }
""" % DUPLICATE_CANDIDATE_TASK_SOURCE
TASK_HISTORY_QUERY = """
query { taskHistory(source: "%s") { edges { node { data } } } }
"""
CANDIDATE_TASK_QUERY = """
query Candidates($subject: String) {
  duplicateCandidates(subjectId: $subject, first: 10) { edges { node { task { data } } } }
}
"""


class ReviewTaskMaskingTest(openIMISGraphQLTestCase):
    @classmethod
    def setUpTestData(cls):
        admin = LogInHelper().get_or_create_user_api()
        cls.tokens = {
            name: BaseTestContext(user=create_right_only_user(f"ddt_{name}", perms)).get_jwt()
            for name, perms in (("triage", TRIAGE), ("triage_unmasked", TRIAGE + [UNMASK]),
                                ("duplicates", ["gql_query_duplicates_perms"]),
                                ("dup_unmasked", ["gql_query_duplicates_perms", UNMASK]))}
        a = Individual(first_name="Taskfirst", last_name="Clearlast", dob="1990-01-01")
        a.save(username=admin.username)
        b = Individual(first_name="Taskfirst", last_name="Clearlast", dob="1990-01-01")
        b.save(username=admin.username)
        cls.subject_a, subject_b = order_pair(str(a.id), str(b.id))
        candidate = DuplicateCandidate.objects.create(
            subject_model="individual.Individual", subject_a=cls.subject_a, subject_b=subject_b,
            kind="demographic", source="TestSource",
            evidence={"columns": {"first_name": "Taskfirst", "last_name": "Clearlast", "dob": "1990-01-01"}},
        )
        result = create_review_tasks([candidate.id], admin)
        assert result["success"] and result["data"][0]["success"], result

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(IndividualMask._sources, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        IndividualMask.register_fields("test", lambda: ["first_name", "dob"])
        own = mock.patch.object(IndividualConfig, "individual_masking_enabled", False)
        own.start()
        self.addCleanup(own.stop)

    def _query(self, token, query, variables=None):
        response = self.query(query, variables=variables, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
        self.assertResponseNoErrors(response)
        return json.loads(response.content)["data"]

    def _task_nodes(self, name):
        return [edge["node"] for edge in self._query(self.tokens[name], TASK_QUERY)["task"]["edges"]]

    def _candidate_task_data(self, name):
        data = self._query(self.tokens[name], CANDIDATE_TASK_QUERY, {"subject": self.subject_a})
        return [json.loads(edge["node"]["task"]["data"]) for edge in data["duplicateCandidates"]["edges"]]

    def test_a_review_task_shows_the_evidence_masked_without_the_masked_data_right(self):
        node, = self._task_nodes("triage")
        for value in (node["data"], node["businessData"]):
            self.assertEqual(json.loads(value)["evidence"]["columns"],
                             {"first_name": MASKED, "last_name": "Clearlast", "dob": MASKED})

    def test_the_task_of_a_candidate_shows_the_evidence_masked_without_the_masked_data_right(self):
        data, = self._candidate_task_data("duplicates")
        self.assertEqual(data["evidence"]["columns"],
                         {"first_name": MASKED, "last_name": "Clearlast", "dob": MASKED})

    def test_the_history_of_a_review_task_shows_the_evidence_masked_without_the_masked_data_right(self):
        query = TASK_HISTORY_QUERY % DUPLICATE_CANDIDATE_TASK_SOURCE
        rows = [json.loads(edge["node"]["data"])
                for edge in self._query(self.tokens["triage"], query)["taskHistory"]["edges"]]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["evidence"]["columns"],
                             {"first_name": MASKED, "last_name": "Clearlast", "dob": MASKED})
        rows = self._query(self.tokens["triage_unmasked"], query)["taskHistory"]["edges"]
        self.assertEqual({json.loads(edge["node"]["data"])["evidence"]["columns"]["first_name"] for edge in rows},
                         {"Taskfirst"})

    def test_a_reader_with_the_masked_data_right_reads_the_evidence(self):
        node, = self._task_nodes("triage_unmasked")
        self.assertEqual(json.loads(node["data"])["evidence"]["columns"]["first_name"], "Taskfirst")
        data, = self._candidate_task_data("dup_unmasked")
        self.assertEqual(data["evidence"]["columns"]["dob"], "1990-01-01")


TASK_NODE_QUERY = """
query Task($id: ID!) { node(id: $id) { ... on TaskGQLType { data businessData } } }
"""


class SummaryReviewTaskDataMaskingTest(MaskedFieldsMixin, openIMISGraphQLTestCase):
    """The summary row a beneficiary or payment review task stores in its data, as a task reader may see it, on
    the task, its node and its history."""

    @classmethod
    def setUpTestData(cls):
        admin = LogInHelper().get_or_create_user_api()
        reader = create_right_only_user("ddt_sum_reader", ["gql_task_search_perms"])
        unmasked = create_right_only_user("ddt_sum_unmasked", ["gql_task_search_perms", UNMASK])
        cls.tokens = {"reader": BaseTestContext(user=reader).get_jwt(),
                      "unmasked": BaseTestContext(user=unmasked).get_jwt()}
        group = TaskGroup(code="ddt-sum-tasks", completion_policy=TaskGroup.TaskGroupCompletionPolicy.ANY)
        group.save(username=admin.username)
        for user in (reader, unmasked):
            TaskExecutor(user=user, task_group=group).save(username=admin.username)
        plan = BenefitPlan(**{**benefit_plan_data, "code": "DDTSUMBP"})
        plan.save(username=admin.username)
        beneficiaries, benefits = [], []
        for _ in range(2):
            person = Individual(first_name="Sumfirst", last_name="Clearlast", dob="1990-01-01",
                                json_ext={"phone": "+25779000002"})
            person.save(username=admin.username)
            beneficiary = Beneficiary(individual=person, benefit_plan=plan, status=BeneficiaryStatus.ACTIVE,
                                      json_ext={"phone": "+25779000002", "k1": "v1"})
            beneficiary.save(username=admin.username)
            beneficiaries.append(str(beneficiary.id))
            benefit = BenefitConsumption(individual=person, code="DDTSUMB", json_ext={"phone": "+25779000002"})
            benefit.save(username=admin.username)
            benefits.append(str(benefit.id))

        def task(service, method, ids):
            task = Task(
                source=service.get_class_name(), executor_action_event="x", business_event="",
                business_data_serializer=f"deduplication.services.{service.__name__}.{method}",
                data={"ids": ids, "count": 2,
                      "column_values": {"individual__first_name": "Sumfirst", "phone": "+25779000002", "k1": "v1"}},
                status=Task.Status.ACCEPTED, task_group=group,
            )
            task.save(username=admin.username)
            return graphene.relay.Node.to_global_id("TaskGQLType", str(task.id))

        cls.tasks = {
            CreateDeduplicationReviewTasksService.get_class_name(): task(
                CreateDeduplicationReviewTasksService, "create_beneficiary_duplication_task_serializer",
                beneficiaries),
            CreateDeduplicationPaymentReviewTasksService.get_class_name(): task(
                CreateDeduplicationPaymentReviewTasksService, "create_payment_benefit_duplication_task_serializer",
                benefits),
        }

    def _query(self, token, query, variables=None):
        response = self.query(query, variables=variables, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
        self.assertResponseNoErrors(response)
        return json.loads(response.content)["data"]

    def _column_values(self, who, source):
        """The stored summary row as ``who`` reads it on the task node, the task list (data and businessData)
        and the task history."""
        token = self.tokens[who]
        node = self._query(token, TASK_NODE_QUERY, {"id": self.tasks[source]})["node"]
        values = [json.loads(node["data"])["column_values"], json.loads(node["businessData"])["column_values"]]
        for edge in self._query(token, TASK_QUERY.replace(DUPLICATE_CANDIDATE_TASK_SOURCE, source))["task"]["edges"]:
            values += [json.loads(edge["node"]["data"])["column_values"],
                       json.loads(edge["node"]["businessData"])["column_values"]]
        history = self._query(token, TASK_HISTORY_QUERY % source)["taskHistory"]["edges"]
        self.assertTrue(history)
        values += [json.loads(edge["node"]["data"])["column_values"] for edge in history]
        return values

    def test_the_summary_row_is_masked_without_the_masked_data_right(self):
        for source in self.tasks:
            with self.subTest(source=source):
                for values in self._column_values("reader", source):
                    self.assertEqual(values, {"individual__first_name": MASKED, "phone": MASKED, "k1": "v1"})

    def test_the_summary_row_is_clear_with_the_masked_data_right(self):
        for source in self.tasks:
            with self.subTest(source=source):
                for values in self._column_values("unmasked", source):
                    self.assertEqual(values,
                                     {"individual__first_name": "Sumfirst", "phone": "+25779000002", "k1": "v1"})


CANDIDATE_ORDER_QUERY = """
query { duplicateCandidates(first: 10, kind: "ordering", orderBy: ["%s"]) { edges { node { id evidence } } } }
"""


class CandidateOrderingTest(MaskedFieldsMixin, openIMISGraphQLTestCase):
    """An ordering on the candidates' evidence is left out for a user without the masked-data right."""

    @classmethod
    def setUpTestData(cls):
        admin = LogInHelper().get_or_create_user_api()
        cls.tokens = {
            name: BaseTestContext(user=create_right_only_user(f"ddo_{name}", perms)).get_jwt()
            for name, perms in (("duplicates", ["gql_query_duplicates_perms"]),
                                ("dup_unmasked", ["gql_query_duplicates_perms", UNMASK]))}
        candidates = []
        for _ in range(2):
            a = Individual(first_name="Order", last_name="Clearlast", dob="1990-01-01")
            a.save(username=admin.username)
            b = Individual(first_name="Order", last_name="Clearlast", dob="1990-01-01")
            b.save(username=admin.username)
            subject_a, subject_b = order_pair(str(a.id), str(b.id))
            candidates.append(DuplicateCandidate.objects.create(
                subject_model="individual.Individual", subject_a=subject_a, subject_b=subject_b,
                kind="ordering", source="TestSource", evidence={}))
        cls.by_id = sorted(candidates, key=lambda candidate: str(candidate.id))
        for candidate, name in zip(cls.by_id, ("Zelda", "Abel")):
            candidate.evidence = {"columns": {"first_name": name}}
            candidate.save()
        result = create_review_tasks([candidate.id for candidate in cls.by_id], admin)
        assert result["success"] and all(task["success"] for task in result["data"]), result

    def _ids(self, name, order):
        response = self.query(CANDIDATE_ORDER_QUERY % order,
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.tokens[name]}"})
        self.assertResponseNoErrors(response)
        edges = json.loads(response.content)["data"]["duplicateCandidates"]["edges"]
        return [graphene.relay.Node.from_global_id(edge["node"]["id"])[1] for edge in edges]

    ORDERS = ("evidence__columns__first_name", "evidence_Columns_FirstName",
              "task__data__evidence__columns__first_name", "task_Data_Evidence_Columns_FirstName")

    def test_an_ordering_on_the_evidence_is_left_out_without_the_masked_data_right(self):
        for order in self.ORDERS:
            with self.subTest(order=order):
                self.assertEqual(self._ids("duplicates", order), [str(candidate.id) for candidate in self.by_id])

    def test_an_ordering_on_the_evidence_applies_with_the_masked_data_right(self):
        for order in self.ORDERS:
            with self.subTest(order=order):
                self.assertEqual(self._ids("dup_unmasked", order),
                                 [str(candidate.id) for candidate in reversed(self.by_id)])
