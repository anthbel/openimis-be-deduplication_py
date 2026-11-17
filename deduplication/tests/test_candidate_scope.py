import json
from unittest import mock

import graphene
from django.core.cache import caches
from django.test import override_settings

from core.models.openimis_graphql_test_case import openIMISGraphQLTestCase, BaseTestContext
from core.test_helpers import create_right_only_user
from deduplication.models import DuplicateCandidate
from deduplication.sources import order_pair
from deduplication.tests.helpers import LogInHelper
from individual.models import Group, GroupIndividual, Individual
from location.models import Location
from payroll.models import BenefitConsumption
from social_protection.models import Beneficiary, BeneficiaryStatus, BenefitPlan
from tasks_management.models import Task, TaskExecutor, TaskGroup

RIGHTS = ["gql_query_duplicates_perms", "gql_beneficiary_search_perms"]

CANDIDATES_QUERY = """
query { duplicateCandidates(first: 50) { totalCount edges { node { id } } } }
"""
NODE_QUERY = """
query Node($id: ID!) { node(id: $id) { ... on DuplicateCandidateGQLType { id } } }
"""
TASK_CANDIDATES_QUERY = """
query TaskNode($id: ID!) {
  node(id: $id) { ... on TaskGQLType { duplicateCandidates { edges { node { id } } } } }
}
"""
RESOLVE_CHECK_QUERY = """
query Check($id: UUID!) { duplicateCandidateResolveCheck(candidateId: $id, decision: "different") { ok } }
"""
BENEFICIARY_SUMMARY_QUERY = """
query Summary($plan: UUID!) {
  beneficiaryDeduplicationSummary(columns: ["first_name"], benefitPlanId: $plan) { rows { count ids } }
}
"""
BENEFIT_SUMMARY_QUERY = """
query { benefitDeduplicationSummary(columns: ["first_name"], paymentCycleId: "1") { rows { count ids } } }
"""


def clear_location_cache():
    caches["location"].clear()
    caches["default"].clear()


def make_location(code, loc_type, parent=None):
    return Location.objects.create(
        code=code, name=code, type=loc_type, parent=parent, validity_from="2019-06-01", audit_user_id=-1,
    )


@override_settings(ROW_SECURITY=True)
class DuplicateCandidateScopeTest(openIMISGraphQLTestCase):
    """Candidates and duplicate summaries are given only when every record they name is within the user's
    locations: a person's own location or its household's, a group's location."""

    def setUp(self):
        super().setUp()
        clear_location_cache()
        self.addCleanup(clear_location_cache)
        self.admin = LogInHelper().get_or_create_user_api()
        region = make_location("DDS-R", "R")
        self.village = self._village("DDS-D1", region)
        self.other_village = self._village("DDS-D2", region)
        clear_location_cache()
        self.user = create_right_only_user("dd_scope_user", RIGHTS, ["DDS-D1"])
        clear_location_cache()
        self.user_token = BaseTestContext(user=self.user).get_jwt()
        self.admin_token = BaseTestContext(user=self.admin).get_jwt()

        near, near_too, far, far_too = (self._person(n, loc) for n, loc in (
            ("Near", self.village), ("NearToo", self.village), ("Far", self.other_village),
            ("FarToo", self.other_village)))
        housed = self._person("Housed", None)
        GroupIndividual(group=self._group("DDS-G1", self.village), individual=housed,
                        role=GroupIndividual.Role.HEAD).save(user=self.admin)
        retired = self._person("Retired", self.village)
        retired.delete(user=self.admin)
        group, group_too, far_group = (self._group(code, loc) for code, loc in (
            ("DDS-G2", self.village), ("DDS-G3", self.village), ("DDS-G4", self.other_village)))

        self.in_scope = self._candidate(near, near_too)
        self.housed_pair = self._candidate(near, housed)
        self.merged_pair = self._candidate(near, retired, status=DuplicateCandidate.Status.CONFIRMED)
        self.group_pair = self._candidate(group, group_too, model="individual.Group")
        self.visible = {self.in_scope, self.housed_pair, self.merged_pair, self.group_pair}
        self.cross = self._candidate(near, far)
        self.out_of_scope = self._candidate(far, far_too)
        self.cross_group = self._candidate(group, far_group, model="individual.Group")
        self.hidden = {self.cross, self.out_of_scope, self.cross_group}

    def _village(self, district_code, region):
        district = make_location(district_code, "D", region)
        return make_location(f"{district_code}-V", "V", make_location(f"{district_code}-W", "W", district))

    def _person(self, name, location):
        person = Individual(first_name=name, last_name="Scope", dob="1990-01-01", location=location)
        person.save(user=self.admin)
        return person

    def _group(self, code, location):
        group = Group(code=code, location=location)
        group.save(user=self.admin)
        return group

    def _candidate(self, a, b, model="individual.Individual", status=DuplicateCandidate.Status.OPEN):
        subject_a, subject_b = order_pair(str(a.id), str(b.id))
        return str(DuplicateCandidate.objects.create(
            subject_model=model, subject_a=subject_a, subject_b=subject_b, kind="demographic",
            source="TestSource", evidence={}, status=status,
        ).id)

    def _run(self, query, token, variables=None):
        response = self.query(query, variables=variables, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
        body = json.loads(response.content)
        self.assertFalse(body.get("errors"), body)
        return body["data"]

    @staticmethod
    def _ids(connection):
        return {graphene.relay.Node.from_global_id(edge["node"]["id"])[1] for edge in connection["edges"]}

    def _gid(self, candidate_id):
        return graphene.relay.Node.to_global_id("DuplicateCandidateGQLType", candidate_id)

    def test_connection_lists_the_pairs_whose_two_subjects_are_in_scope(self):
        data = self._run(CANDIDATES_QUERY, self.user_token)["duplicateCandidates"]
        self.assertEqual(self._ids(data), self.visible)
        self.assertEqual(data["totalCount"], len(self.visible))

    def test_connection_lists_every_pair_to_an_admin(self):
        data = self._run(CANDIDATES_QUERY, self.admin_token)["duplicateCandidates"]
        self.assertTrue(self.visible | self.hidden <= self._ids(data))

    def test_node_gives_a_pair_in_scope_and_hides_the_others(self):
        self.assertIsNotNone(self._run(NODE_QUERY, self.user_token, {"id": self._gid(self.in_scope)})["node"])
        for candidate_id in self.hidden:
            self.assertIsNone(self._run(NODE_QUERY, self.user_token, {"id": self._gid(candidate_id)})["node"])

    def test_task_reverse_connection_lists_only_the_pairs_in_scope(self):
        group = TaskGroup(code="dds-scope", completion_policy=TaskGroup.TaskGroupCompletionPolicy.ANY)
        group.save(user=self.admin)
        TaskExecutor(user=self.user, task_group=group).save(user=self.admin)
        task = Task(source="deduplication_candidate", executor_action_event="x", business_event="", data={},
                    status=Task.Status.ACCEPTED, task_group=group)
        task.save(user=self.admin)
        DuplicateCandidate.objects.filter(id__in=[self.in_scope, self.cross]).update(task=task)

        data = self._run(TASK_CANDIDATES_QUERY, self.user_token,
                         {"id": graphene.relay.Node.to_global_id("TaskGQLType", str(task.id))})
        self.assertEqual(self._ids(data["node"]["duplicateCandidates"]), {self.in_scope})

    def test_resolve_check_finds_no_pair_out_of_scope(self):
        data = self._run(RESOLVE_CHECK_QUERY, self.user_token, {"id": self.in_scope})
        self.assertTrue(data["duplicateCandidateResolveCheck"]["ok"])
        response = self.query(RESOLVE_CHECK_QUERY, variables={"id": self.cross},
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.user_token}"})
        body = json.loads(response.content)
        self.assertTrue(body.get("errors"), body)
        self.assertIsNone((body.get("data") or {}).get("duplicateCandidateResolveCheck"))

    def _summary_people(self, names_and_locations):
        return [self._person(name, location) for name, location in names_and_locations]

    def test_beneficiary_summary_leaves_out_the_rows_with_a_beneficiary_out_of_scope(self):
        plan = BenefitPlan(code="DDS-P", name="DDS-P", type=BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE,
                           beneficiary_data_schema={})
        plan.save(user=self.admin)
        people = self._summary_people([("Twin", self.village), ("Twin", self.village),
                                       ("Split", self.village), ("Split", self.other_village)])
        beneficiaries = []
        for person in people:
            beneficiary = Beneficiary(individual=person, benefit_plan=plan, status=BeneficiaryStatus.POTENTIAL)
            beneficiary.save(user=self.admin)
            beneficiaries.append(str(beneficiary.id))

        rows = self._run(BENEFICIARY_SUMMARY_QUERY, self.user_token, {"plan": str(plan.id)})[
            "beneficiaryDeduplicationSummary"]["rows"]
        self.assertEqual([set(row["ids"]) for row in rows], [set(beneficiaries[:2])])
        admin_rows = self._run(BENEFICIARY_SUMMARY_QUERY, self.admin_token, {"plan": str(plan.id)})[
            "beneficiaryDeduplicationSummary"]["rows"]
        self.assertEqual(len(admin_rows), 2)

    def test_benefit_summary_leaves_out_the_rows_with_a_benefit_out_of_scope(self):
        people = self._summary_people([("Twin", self.village), ("Twin", self.village),
                                       ("Split", self.village), ("Split", self.other_village)])
        benefits = []
        for person in people:
            benefit = BenefitConsumption(individual=person, code="DDS-B")
            benefit.save(user=self.admin)
            benefits.append(benefit.id)
        aggregation = [
            {"individual__first_name": "Twin", "id_count": 2, "ids": benefits[:2]},
            {"individual__first_name": "Split", "id_count": 2, "ids": benefits[2:]},
        ]
        with mock.patch("deduplication.services.get_benefit_consumption_duplication_aggregation",
                        side_effect=lambda **kwargs: [dict(row) for row in aggregation]):
            rows = self._run(BENEFIT_SUMMARY_QUERY, self.user_token)["benefitDeduplicationSummary"]["rows"]
            admin_rows = self._run(BENEFIT_SUMMARY_QUERY, self.admin_token)["benefitDeduplicationSummary"]["rows"]
        self.assertEqual([set(row["ids"]) for row in rows], [{str(benefit_id) for benefit_id in benefits[:2]}])
        self.assertEqual(len(admin_rows), 2)
