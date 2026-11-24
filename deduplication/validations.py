import json

from django.db.models import Q
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from tasks_management.models import Task


class CreateDeduplicationReviewTasksValidation:
    @classmethod
    def validate_create_deduplication_task(cls, task_data, service_name):
        errors = cls.get_errors_deduplication_task_data(task_data, service_name)
        if errors:
            raise ValidationError(errors)

    @classmethod
    def get_errors_deduplication_task_data(cls, task_data, service_name):
        count = task_data.get("count")
        ids = task_data.get("ids")
        column_values = task_data.get("column_values")

        errors = [
            *cls._validate_arg_existence(
                count,
                "deduplication.validations.CreateDeduplicationReviewTasksValidation.count_missing"),
            *cls._validate_arg_existence(
                ids,
                "deduplication.validations.CreateDeduplicationReviewTasksValidation.ids_missing"),
            *cls._validate_arg_existence(
                column_values,
                "deduplication.validations.CreateDeduplicationReviewTasksValidation.column_values_missing")
        ]

        not_existing_ids = cls._validate_existence_of_beneficiary(ids)
        beneficiary_in_task = cls._validate_beneficiary_already_in_task(ids, service_name)

        errors.extend(
            [
                *cls._validate_no_errors(
                    not_existing_ids,
                    "deduplication.validations.CreateDeduplicationReviewTasksValidation.not_existing_ids",
                    "ids"
                ),
                *cls._validate_no_errors(
                    beneficiary_in_task,
                    "deduplication.validations.CreateDeduplicationReviewTasksValidation.beneficiary_in_task",
                    "beneficiaries"
                )
            ]
        )

        return errors

    @classmethod
    def _validate_arg_existence(cls, kwarg, error_message_id):
        if not kwarg:
            return [{"message": _(error_message_id)}]
        return []

    @classmethod
    def _validate_no_errors(cls, errors, error_message_id, dict_key):
        if errors:
            return [{"message": _(error_message_id) % {dict_key: json.dumps(errors)}}]
        return []

    @classmethod
    def _validate_existence_of_beneficiary(cls, ids):
        from social_protection.models import Beneficiary

        not_existing_ids = []
        for beneficiary_id in ids:
            if not Beneficiary.objects.filter(id=beneficiary_id).exists():
                not_existing_ids.append(beneficiary_id)
        return not_existing_ids

    @classmethod
    def _validate_beneficiary_already_in_task(cls, ids, service_name):
        from social_protection.models import Beneficiary

        beneficiary_in_task = {}
        status_accepted = Task.Status.ACCEPTED
        status_received = Task.Status.RECEIVED

        filters = [
            Q(is_deleted=False),
            Q(status=status_accepted) | Q(status=status_received),
            Q(source=service_name),
        ]
        task_queryset = Task.objects.filter(*filters)

        for beneficiary_id in ids:
            tmp_task_queryset = task_queryset
            if tmp_task_queryset.filter(data__ids__contains=beneficiary_id).exists():
                beneficiary = Beneficiary.objects.get(id=beneficiary_id)
                beneficiary_in_task[beneficiary_id] = beneficiary.individual.__str__()

        return beneficiary_in_task


class CreateDeduplicationPaymentReviewTasksValidation:
    @classmethod
    def validate_create_deduplication_task(cls, task_data, service_name):
        errors = cls.get_errors_deduplication_task_data(task_data, service_name)
        if errors:
            raise ValidationError(errors)

    @classmethod
    def get_errors_deduplication_task_data(cls, task_data, service_name):
        count = task_data.get("count")
        ids = task_data.get("ids")
        column_values = task_data.get("column_values")

        errors = [
            *cls._validate_arg_existence(
                count,
                "deduplication.validations.CreateDeduplicationPaymentReviewTasksValidation.count_missing"),
            *cls._validate_arg_existence(
                ids,
                "deduplication.validations.CreateDeduplicationPaymentReviewTasksValidation.ids_missing"),
            *cls._validate_arg_existence(
                column_values,
                "deduplication.validations.CreateDeduplicationPaymentReviewTasksValidation.column_values_missing")
        ]

        not_existing_ids = cls._validate_existence_of_benefits(ids)
        benefits_in_task = cls._validate_benefits_already_in_task(ids, service_name)

        errors.extend(
            [
                *cls._validate_no_errors(
                    not_existing_ids,
                    "deduplication.validations.CreateDeduplicationPaymentReviewTasksValidation.not_existing_ids",
                    "ids"
                ),
                *cls._validate_no_errors(
                    benefits_in_task,
                    "deduplication.please_solve_the_created_tasks_first",
                    "benefits"
                )
            ]
        )

        return errors

    @classmethod
    def _validate_arg_existence(cls, kwarg, error_message_id):
        if not kwarg:
            return [{"message": _(error_message_id)}]
        return []

    @classmethod
    def _validate_no_errors(cls, errors, error_message_id, dict_key):
        if errors:
            return [{"message": _(error_message_id) % {dict_key: json.dumps(errors)}}]
        return []

    @classmethod
    def _validate_existence_of_benefits(cls, ids):
        from payroll.models import BenefitConsumption

        not_existing_ids = []
        for benefits_id in ids:
            if not BenefitConsumption.objects.filter(id=benefits_id).exists():
                not_existing_ids.append(benefits_id)
        return not_existing_ids

    @classmethod
    def _validate_benefits_already_in_task(cls, ids, service_name):
        from payroll.models import BenefitConsumption

        benefit_in_task = {}
        status_accepted = Task.Status.ACCEPTED
        status_received = Task.Status.RECEIVED

        filters = [
            Q(is_deleted=False),
            Q(status=status_accepted) | Q(status=status_received),
            Q(source=service_name),
        ]
        task_queryset = Task.objects.filter(*filters)

        for benefit_id in ids:
            tmp_task_queryset = task_queryset
            if tmp_task_queryset.filter(data__ids__contains=benefit_id).exists():
                benefit = BenefitConsumption.objects.get(id=benefit_id)
                benefit_in_task[benefit_id] = benefit.individual.__str__()

        return benefit_in_task
