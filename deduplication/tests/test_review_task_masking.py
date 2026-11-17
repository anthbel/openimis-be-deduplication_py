"""The evidence a review task copies from its candidate is masked by individual's task mask for a reader without
the masked-data right, on the task queries and from the candidate."""
import json
from unittest import mock

from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from deduplication.models import DuplicateCandidate
from deduplication.services import DUPLICATE_CANDIDATE_TASK_SOURCE, create_review_tasks
from deduplication.sources import order_pair
from deduplication.tests.helpers import LogInHelper
from individual.apps import IndividualConfig
from individual.data_masking import IndividualMask
from individual.models import Individual

MASKED = "***************"
UNMASK = "gql_query_enable_viewing_masked_data_perms"
TRIAGE = ["gql_task_search_perms", "gql_task_group_search_perms", "gql_task_group_create_perms",
          "gql_task_group_update_perms", "gql_task_group_delete_perms"]

TASK_QUERY = """
query { task(source: "%s") { edges { node { data businessData } } } }
""" % DUPLICATE_CANDIDATE_TASK_SOURCE
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

    def test_a_reader_with_the_masked_data_right_reads_the_evidence(self):
        node, = self._task_nodes("triage_unmasked")
        self.assertEqual(json.loads(node["data"])["evidence"]["columns"]["first_name"], "Taskfirst")
        data, = self._candidate_task_data("dup_unmasked")
        self.assertEqual(data["evidence"]["columns"]["dob"], "1990-01-01")
