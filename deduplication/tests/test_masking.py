"""Candidate evidence and duplication summaries apply individual's masks to a user without the masked-data right."""
import json
from unittest import mock

from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from deduplication.models import DuplicateCandidate
from deduplication.sources import order_pair
from deduplication.tests.data.dedup_beneficiary import benefit_plan_data
from deduplication.tests.helpers import LogInHelper
from individual.apps import IndividualConfig
from individual.data_masking import GroupMask, IndividualMask
from individual.models import Group, Individual
from social_protection.models import Beneficiary, BeneficiaryStatus, BenefitPlan

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
