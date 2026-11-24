import graphene
from django.contrib.auth.models import AnonymousUser
from django.db.models import Q

from core.schema import OrderedDjangoFilterConnectionField
from deduplication.apps import DeduplicationConfig
from deduplication.column_resolution import check_summary_columns
from deduplication.gql_mutations import (
    CreateDeduplicationReviewMutation,
    CreateDeduplicationPaymentReviewMutation,
    RunDuplicateScanMutation,
    ResolveDuplicateCandidateMutation,
    CreateDuplicateReviewTasksMutation,
)
from deduplication.gql_queries import (
    DeduplicationSummaryGQLType,
    DeduplicationSummaryRowGQLType,
    DuplicateCandidateGQLType,
    DuplicateCandidateResolveCheckGQLType,
)
from deduplication.masking import mask_summary_values, unmasked_candidate_order_by
from deduplication.models import DuplicateCandidate
from deduplication.optional_apps import require_beneficiary_deduplication, require_payment_deduplication
from deduplication.services import ResolveRefusal, check_resolve
from individual.data_masking import may_see_masked


def _rows_in_scope(rows, visible):
    """The summary rows all of whose ``ids`` are among the ids of ``visible(ids)``, the queryset of the rows
    the user may see among ``ids``: a row with any record out of the user's reach is left out."""
    rows = list(rows)
    ids = {row_id for row in rows for row_id in row["ids"]}
    if not ids:
        return rows
    seen = set(visible(ids).values_list("id", flat=True))
    return [row for row in rows if all(row_id in seen for row_id in row["ids"])]


class MaskedCandidateConnectionField(OrderedDjangoFilterConnectionField):
    """Core's ordered connection leaving out, for a user without the masked-data right, the ``orderBy`` terms
    that order by the evidence (``unmasked_candidate_order_by``)."""

    @classmethod
    def resolve_queryset(cls, connection, iterable, info, args, filtering_args, filterset_class):
        if args.get("orderBy"):
            args = {**args, "orderBy": unmasked_candidate_order_by(
                info.context.user, filterset_class._meta.model, args["orderBy"])}
        return super().resolve_queryset(connection, iterable, info, args, filtering_args, filterset_class)


class Query(graphene.ObjectType):
    # Another module's name, and without effect here: `module_name` is only read by
    # `core.gql.export_mixin.ExportableQueryMixin.get_module_name` (customFilters
    # routing), which this Query does not inherit from. The rights, for their part, are
    # anchored per AppConfig (`core.utils.collect_all_gql_permissions` walks the app
    # configs), so this module's rights stay announced under "deduplication". Left as
    # it is: a dead attribute, but removing it gains nothing and falls outside this
    # batch of work.
    module_name = "tasks_management"

    duplicate_candidates = MaskedCandidateConnectionField(
        DuplicateCandidateGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        status=graphene.String(),
        kind=graphene.String(),
        subjectId=graphene.String(),
    )

    duplicate_candidate_resolve_check = graphene.Field(
        DuplicateCandidateResolveCheckGQLType,
        candidate_id=graphene.UUID(required=True),
        decision=graphene.String(required=True),
        keep=graphene.String(required=False),
        description="Runs the checks resolveDuplicateCandidate would run for this decision, and writes nothing.",
    )

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
        require_beneficiary_deduplication()
        from social_protection.apps import SocialProtectionConfig
        from social_protection.models import Beneficiary
        from deduplication.services import get_beneficiary_duplication_aggregation

        # A borrowing of another module's right, unjustified from the start and left
        # as it stands: reading the duplicates summary is guarded by
        # social_protection's beneficiary search right (170001), not by a deduplication
        # right. Reported by the audit, to be dealt with in the authorisation batch.
        Query._check_permissions(info.context.user, SocialProtectionConfig.gql_beneficiary_search_perms)

        if not columns:
            return ["deduplication.validation.no_columns_provided"]

        individual_columns = ['first_name', 'last_name', 'dob']
        check_summary_columns(Beneficiary, columns, individual_columns, person='individual')
        columns = [f'individual__{column}' if column in individual_columns else column for column in columns]
        aggr = get_beneficiary_duplication_aggregation(columns=columns, benefit_plan_id=benefit_plan_id)
        aggr = _rows_in_scope(aggr, lambda ids: Beneficiary.get_queryset(
            Beneficiary.objects.filter(id__in=ids), info.context.user))
        masked = not may_see_masked(info.context.user)
        rows = list()
        for row in aggr:
            individual_columns = [f'individual__{column}' for column in individual_columns]
            count = row.pop('id_count')
            ids = row.pop('ids')
            row_column_values = {column: str(row[column])
                                 for
                                 column in row}
            if masked:
                mask_summary_values(Beneficiary, row_column_values, person='individual')
            rows.append(DeduplicationSummaryRowGQLType(column_values=row_column_values, count=count, ids=ids))

        return DeduplicationSummaryGQLType(rows=rows)

    def resolve_benefit_deduplication_summary(self, info, columns=None, payment_cycle_id=None, **kwargs):
        require_payment_deduplication()
        from social_protection.apps import SocialProtectionConfig
        from individual.models import Individual
        from payroll.models import BenefitConsumption
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
        check_summary_columns(Individual, columns, individual_columns)
        columns = [f'{column}' if column in individual_columns else column for column in columns]

        # Fetch the aggregation data
        aggr = get_benefit_consumption_duplication_aggregation(columns=columns, payment_cycle_id=payment_cycle_id)
        aggr = _rows_in_scope(aggr, lambda ids: BenefitConsumption.objects.filter(
            id__in=ids, individual__in=Individual.get_queryset(None, info.context.user)))
        masked = not may_see_masked(info.context.user)
        rows = []
        for row in aggr:
            count = row.pop('id_count')
            ids = row.pop('ids')
            row_column_values = {column: str(row[column]) for column in row}
            if masked:
                mask_summary_values(BenefitConsumption, row_column_values, person='individual')
            rows.append(DeduplicationSummaryRowGQLType(column_values=row_column_values, count=count, ids=ids))

        return DeduplicationSummaryGQLType(rows=rows)

    def resolve_duplicate_candidates(self, info, **kwargs):
        Query._check_permissions(info.context.user, DeduplicationConfig.gql_query_duplicates_perms)

        filters = []
        status = kwargs.get("status")
        if status:
            filters.append(Q(status=status))
        kind = kwargs.get("kind")
        if kind:
            filters.append(Q(kind=kind))
        subject_id = kwargs.get("subjectId")
        if subject_id:
            filters.append(Q(subject_a=subject_id) | Q(subject_b=subject_id))

        return DuplicateCandidate.objects.filter(*filters)

    def resolve_duplicate_candidate_resolve_check(self, info, candidate_id, decision, keep=None, **kwargs):
        Query._check_permissions(info.context.user, DeduplicationConfig.gql_query_duplicates_perms)

        if decision not in ('same', 'different'):
            raise ValueError("deduplication.mutation.invalid_decision")
        candidate = DuplicateCandidate.get_queryset(None, info.context.user).get(id=candidate_id)
        try:
            check_resolve(candidate, decision=decision, keep=keep)
        except ResolveRefusal as refusal:
            return DuplicateCandidateResolveCheckGQLType(ok=False, code=refusal.code, message=refusal.message)
        return DuplicateCandidateResolveCheckGQLType(ok=True)

    @staticmethod
    def _check_permissions(user, perms):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(perms):
            raise PermissionError("Unauthorized")


class Mutation(graphene.ObjectType):
    create_deduplication_tasks = CreateDeduplicationReviewMutation.Field()
    create_deduplication_payment_tasks = CreateDeduplicationPaymentReviewMutation.Field()
    run_duplicate_scan = RunDuplicateScanMutation.Field()
    resolve_duplicate_candidate = ResolveDuplicateCandidateMutation.Field()
    create_duplicate_review_tasks = CreateDuplicateReviewTasksMutation.Field()
