"""A deduplication summary column is a person column (``first_name``, ``last_name``, ``dob``), one of the model's
own columns or a ``json_ext`` key; a column following a relation is refused before any query, naming it."""
import json
import uuid

from django.core.cache import caches
from django.test import override_settings

from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_right_only_user
from individual.models import Group, GroupIndividual, Individual
from location.models import Location
from social_protection.models import Beneficiary, BenefitPlan

PERMS = ["gql_beneficiary_search_perms", "gql_individual_search_perms", "gql_group_search_perms"]
SUMMARY = ('{ beneficiaryDeduplicationSummary(columns: %s, benefitPlanId: "%s") '
           '{ rows { count ids columnValues } } }')
BENEFIT_SUMMARY = ('{ benefitDeduplicationSummary(columns: %s, paymentCycleId: "%s") '
                   '{ rows { count ids columnValues } } }')


def clear_location_cache():
    caches["location"].clear()
    caches["default"].clear()


def make_location(code, loc_type, parent=None):
    return Location.objects.create(code=code, name=code, type=loc_type, parent=parent, validity_from="2019-06-01",
                                   audit_user_id=-1)


@override_settings(ROW_SECURITY=True)
class SummaryColumnsTest(openIMISGraphQLTestCase):
    """P heads G1 in district D; the beneficiaries K1 and K2 live in D2 and belong to G2 (D2) and G1. The reader
    is limited to D2."""

    def setUp(self):
        super().setUp()
        clear_location_cache()
        self.addCleanup(clear_location_cache)
        region = make_location("DSC-R", "R")
        village = make_location("DSC-V", "V", make_location("DSC-W", "W", make_location("DSC-D", "D", region)))
        village2 = make_location("DSC-V2", "V", make_location("DSC-W2", "W", make_location("DSC-D2", "D", region)))
        clear_location_cache()
        self.admin = create_right_only_user("dsc_admin", PERMS, ["DSC-D", "DSC-D2"])
        self.reader = create_right_only_user("dsc_reader", PERMS, ["DSC-D2"])
        g1 = self.group("DSC-G1", village)
        g2 = self.group("DSC-G2", village2)
        GroupIndividual(group=g1, individual=self.person("Dscpind", village), role=GroupIndividual.Role.HEAD).save(
            user=self.admin)
        self.plan = BenefitPlan(code="DSC-I", name="DSC-I", type=BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE,
                                max_beneficiaries=0, ceiling_per_beneficiary="0.00", beneficiary_data_schema={},
                                date_valid_from="2023-01-01", date_valid_to="2099-12-31")
        self.plan.save(user=self.admin)
        for last_name in ("Dsck1", "Dsck2"):
            person = self.person(last_name, village2)
            GroupIndividual(group=g2, individual=person, role=GroupIndividual.Role.SON).save(user=self.admin)
            GroupIndividual(group=g1, individual=person, role=GroupIndividual.Role.SON).save(user=self.admin)
            Beneficiary(individual=Individual.objects.get(id=person.id), benefit_plan=self.plan, status="ACTIVE",
                        json_ext={"phone": "+257 5"}).save(user=self.admin)
        clear_location_cache()

    def group(self, code, location):
        group = Group(code=code, location=location)
        group.save(user=self.admin)
        return group

    def person(self, last_name, location):
        person = Individual(first_name="Dsc", last_name=last_name, dob="1990-01-01", location=location)
        person.save(user=self.admin)
        return person

    def gql(self, user, query):
        token = BaseTestContext(user=user).get_jwt()
        return json.loads(self.query(query, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"}).content)

    def summary(self, user, columns):
        return self.gql(user, SUMMARY % (json.dumps(columns), self.plan.id))

    RELATION_COLUMNS = (
        "individual__groupindividuals__group__groupindividuals__individual__last_name",
        "individual__groupindividuals__group__code",
        "individual__location__code",
        "individual__location__individuals__last_name",
        "benefit_plan__beneficiary__individual__last_name",
        "user_created__individual_user_created__last_name",
    )

    def test_a_summary_column_following_a_relation_is_refused_naming_the_column(self):
        for column in self.RELATION_COLUMNS:
            for user in (self.reader, self.admin):
                with self.subTest(column=column, user=user.username):
                    content = self.summary(user, [column])
                    self.assertIn(f"the summary column {column} is not allowed", json.dumps(content["errors"]))

    def test_the_reader_gets_no_value_of_another_district_through_a_summary_column(self):
        for column in self.RELATION_COLUMNS[:4]:
            with self.subTest(column=column):
                text = json.dumps(self.summary(self.reader, [column]))
                self.assertNotIn("Dscpind", text)
                self.assertNotIn("DSC-G1", text)
                self.assertNotIn('DSC-V\\"', text)

    def test_the_benefit_summary_refuses_a_column_following_a_relation(self):
        for column in ("user_created__individual_user_created__last_name", "groupindividuals__group__code"):
            with self.subTest(column=column):
                content = self.gql(self.reader, BENEFIT_SUMMARY % (json.dumps([column]), uuid.uuid4()))
                self.assertIn(f"the summary column {column} is not allowed", json.dumps(content["errors"]))

    def test_the_person_columns_json_keys_and_own_columns_still_summarise(self):
        content = self.summary(self.reader, ["first_name", "last_name", "dob"])
        self.assertNotIn("errors", content)
        self.assertEqual(content["data"]["beneficiaryDeduplicationSummary"]["rows"], [])
        for columns in (["first_name"], ["phone"], ["json_ext__phone"], ["status"], ["first_name", "phone", "status"]):
            with self.subTest(columns=columns):
                content = self.summary(self.reader, columns)
                self.assertNotIn("errors", content)
                [row] = content["data"]["beneficiaryDeduplicationSummary"]["rows"]
                self.assertEqual(row["count"], 2)
