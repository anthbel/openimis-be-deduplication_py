import graphene
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError, PermissionDenied

from core.schema import OpenIMISMutation
from deduplication.apps import DeduplicationConfig
from deduplication.services import (
    CreateDeduplicationReviewTasksService,
    CreateDeduplicationPaymentReviewTasksService,
)


class SummaryGQLType(graphene.InputObjectType):
    count = graphene.Int()
    ids = graphene.List(graphene.String)
    column_values = graphene.JSONString()


class CreateDeduplicationReviewMutation(OpenIMISMutation):
    _mutation_module = "deduplication"
    _mutation_class = "CreateDeduplicationReviewMutation"

    class Input(OpenIMISMutation.Input):
        summary = graphene.List(SummaryGQLType, required=True)

    @classmethod
    def _validate(cls, user, **data):
        summary = data.get("summary")
        if type(user) is AnonymousUser or not user.id:
            raise ValidationError("mutation.authentication_required")
        if not user.has_perms(DeduplicationConfig.gql_create_deduplication_review_perms):
            raise PermissionDenied("unauthorized")
        if not summary or len(summary) == 0:
            raise ValidationError("mutation.columns_empty_list")

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            cls._validate(user, **data)
            if "client_mutation_id" in data:
                data.pop('client_mutation_id')
            if "client_mutation_label" in data:
                data.pop('client_mutation_label')

            summary = data.get("summary")

            service = CreateDeduplicationReviewTasksService(user)
            res = service.create_beneficiary_duplication_tasks(summary)
            return res if not res['success'] else None
        except Exception as exc:
            return [
                {
                    'message': "deduplication.mutation.failed_to_create_deduplication_review",
                    'detail': str(exc)
                }]


class CreateDeduplicationPaymentReviewMutation(OpenIMISMutation):
    _mutation_module = "deduplication"
    _mutation_class = "CreateDeduplicationPaymentReviewMutation"

    class Input(OpenIMISMutation.Input):
        summary = graphene.List(SummaryGQLType, required=True)
        payment_cycle = graphene.String(required=False)

    @classmethod
    def _validate(cls, user, **data):
        summary = data.get("summary")
        if type(user) is AnonymousUser or not user.id:
            raise ValidationError("mutation.authentication_required")
        # A known discrepancy, left as it stands: this *payment* mutation checks the
        # beneficiary review right (172001), so that
        # gql_create_deduplication_payment_review_perms (172002) is declared but read
        # nowhere. Switching it to 172002 would withdraw access from the roles that
        # only carry 172001; that is a different batch of work from declaring the
        # rights.
        if not user.has_perms(DeduplicationConfig.gql_create_deduplication_review_perms):
            raise PermissionDenied("unauthorized")
        if not summary or len(summary) == 0:
            raise ValidationError("mutation.columns_empty_list")

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            cls._validate(user, **data)
            if "client_mutation_id" in data:
                data.pop('client_mutation_id')
            if "client_mutation_label" in data:
                data.pop('client_mutation_label')

            summary = data.get("summary")
            payment_cycle_id = data.get("payment_cycle")
            service = CreateDeduplicationPaymentReviewTasksService(user)
            res = service.create_payment_benefit_duplication_tasks(summary, payment_cycle_id)
            return res if not res['success'] else None
        except Exception as exc:
            return [
                {
                    'message': "deduplication.mutation.failed_to_create_deduplication_review",
                    'detail': str(exc)
                }]
