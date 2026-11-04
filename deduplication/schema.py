import graphene
from django.contrib.auth.models import AnonymousUser

from deduplication.gql_mutations import CreateDeduplicationReviewMutation, CreateDeduplicationPaymentReviewMutation
from deduplication.gql_queries import DeduplicationSummaryGQLType, DeduplicationSummaryRowGQLType


class Query(graphene.ObjectType):
    # Another module's name, and without effect here: `module_name` is only read by
    # `core.gql.export_mixin.ExportableQueryMixin.get_module_name` (customFilters
    # routing), which this Query does not inherit from. The rights, for their part, are
    # anchored per AppConfig (`core.utils.collect_all_gql_permissions` walks the app
    # configs), so this module's rights stay announced under "deduplication". Left as
    # it is: a dead attribute, but removing it gains nothing and falls outside this
    # batch of work.
    module_name = "tasks_management"

    beneficiary_deduplication_summary = graphene.Field(
        DeduplicationSummaryGQLType,
        columns=graphene.List(graphene.String, required=True),
        benefit_plan_id=graphene.UUID(required=True),
    )

    benefit_deduplication_summary = graphene.Field(
        DeduplicationSummaryGQLType,
        columns=graphene.List(graphene.String, required=True),
        payment_cycle_id=graphene.ID(required=False)
    )

    def resolve_beneficiary_deduplication_summary(self, info, columns=None, benefit_plan_id=None, **kwargs):
        from social_protection.apps import SocialProtectionConfig
        from deduplication.services import get_beneficiary_duplication_aggregation

        # A borrowing of another module's right, unjustified from the start and left
        # as it stands: reading the duplicates summary is guarded by
        # social_protection's beneficiary search right (170001), not by a deduplication
        # right. Reported by the audit, to be dealt with in the authorisation batch.
        Query._check_permissions(info.context.user, SocialProtectionConfig.gql_beneficiary_search_perms)

        if not columns:
            return ["deduplication.validation.no_columns_provided"]

        individual_columns = ['first_name', 'last_name', 'dob']
        columns = [f'individual__{column}' if column in individual_columns else column for column in columns]
        aggr = get_beneficiary_duplication_aggregation(columns=columns, benefit_plan_id=benefit_plan_id)
        rows = list()
        for row in aggr:
            individual_columns = [f'individual__{column}' for column in individual_columns]
            count = row.pop('id_count')
            ids = row.pop('ids')
            row_column_values = {column: str(row[column])
                                 for
                                 column in row}
            rows.append(DeduplicationSummaryRowGQLType(column_values=row_column_values, count=count, ids=ids))

        return DeduplicationSummaryGQLType(rows=rows)

    def resolve_benefit_deduplication_summary(self, info, columns=None, payment_cycle_id=None, **kwargs):
        from social_protection.apps import SocialProtectionConfig
        from deduplication.services import get_benefit_consumption_duplication_aggregation

        # Check permissions
        # The same borrowing as above, and further removed still: this summary bears
        # on BenefitConsumption rows (payroll) but is guarded by social_protection's
        # beneficiary search right (170001). Not fixed here.
        Query._check_permissions(info.context.user, SocialProtectionConfig.gql_beneficiary_search_perms)

        if not columns:
            return ["deduplication.validation.no_columns_provided"]

        # Add prefix to individual columns
        individual_columns = ['first_name', 'last_name', 'dob']
        columns = [f'{column}' if column in individual_columns else column for column in columns]

        # Fetch the aggregation data
        aggr = get_benefit_consumption_duplication_aggregation(columns=columns, payment_cycle_id=payment_cycle_id)
        rows = []
        for row in aggr:
            count = row.pop('id_count')
            ids = row.pop('ids')
            row_column_values = {column: str(row[column]) for column in row}
            rows.append(DeduplicationSummaryRowGQLType(column_values=row_column_values, count=count, ids=ids))

        return DeduplicationSummaryGQLType(rows=rows)

    @staticmethod
    def _check_permissions(user, perms):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(perms):
            raise PermissionError("Unauthorized")


class Mutation(graphene.ObjectType):
    create_deduplication_tasks = CreateDeduplicationReviewMutation.Field()
    create_deduplication_payment_tasks = CreateDeduplicationPaymentReviewMutation.Field()
