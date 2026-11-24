"""Beneficiary and payment deduplication need social_protection, payroll, payment_cycle and invoice; the individual
and group duplicate features must start and run without them. The four apps are reported absent by
``apps.is_installed`` and their modules are made unimportable."""
import importlib.util
import os
import sys
from contextlib import contextmanager
from unittest import mock

from django.apps import apps
from django.test import SimpleTestCase

import deduplication
from deduplication.apps import DeduplicationConfig
from deduplication.gql_mutations import CreateDeduplicationPaymentReviewMutation, CreateDeduplicationReviewMutation
from deduplication.optional_apps import OPTIONAL_APPS_MISSING, OptionalAppsMissing
from deduplication.schema import Query
from deduplication.services import (
    DUPLICATE_CANDIDATE_TASK_SOURCE,
    CreateDeduplicationPaymentReviewTasksService,
    CreateDeduplicationReviewTasksService,
    get_beneficiary_duplication_aggregation,
    get_benefit_consumption_duplication_aggregation,
)

OPTIONAL_APP_LABELS = ("social_protection", "payroll", "payment_cycle", "invoice")
OPTIONAL_MODULES = (
    "invoice.models",
    "payroll.models",
    "payment_cycle.models",
    "social_protection.apps",
    "social_protection.enrolment_hooks",
    "social_protection.models",
    "social_protection.services",
)
MODULE_DIR = os.path.dirname(deduplication.__file__)


def _installed_except(absent):
    real = apps.is_installed
    return lambda label: False if label in absent else real(label)


@contextmanager
def _without_optional_apps(absent=OPTIONAL_APP_LABELS):
    """Reports ``absent`` as not installed; the models of the four apps raise ImportError when imported."""
    with mock.patch.object(apps, "is_installed", side_effect=_installed_except(absent)), \
            mock.patch.dict(sys.modules, {name: None for name in OPTIONAL_MODULES}):
        yield


def _execute_fresh_copy(file_name):
    """Runs the top level of ``deduplication/<file_name>`` once more, under another module name, with the service
    signal registry (which refuses a second registration of a name) out of the way."""
    name = "_probe_" + file_name.replace(os.sep, "_").replace(".", "_")
    spec = importlib.util.spec_from_file_location(name, os.path.join(MODULE_DIR, file_name))
    module = importlib.util.module_from_spec(spec)
    with mock.patch("core.signals.register_service_signal", lambda signal_name: lambda function: function):
        spec.loader.exec_module(module)


def _user():
    return mock.Mock(id=1, has_perms=mock.Mock(return_value=True))


class StartupWithoutPaymentAppsTest(SimpleTestCase):

    def test_services_module_imports_without_the_optional_apps(self):
        with _without_optional_apps():
            _execute_fresh_copy("services.py")

    def test_validations_module_imports_without_the_optional_apps(self):
        with _without_optional_apps():
            _execute_fresh_copy("validations.py")

    def test_signals_module_imports_without_the_optional_apps(self):
        with _without_optional_apps():
            _execute_fresh_copy(os.path.join("signals", "__init__.py"))

    def test_review_task_maskers_skip_the_absent_apps(self):
        with _without_optional_apps(), mock.patch("individual.task_masking.TaskMask.register_source") as register:
            DeduplicationConfig._DeduplicationConfig__register_review_task_maskers()

        self.assertEqual([call.args[0] for call in register.call_args_list], [DUPLICATE_CANDIDATE_TASK_SOURCE])

    def test_review_task_maskers_are_registered_when_the_apps_are_installed(self):
        with mock.patch("individual.task_masking.TaskMask.register_source") as register:
            DeduplicationConfig._DeduplicationConfig__register_review_task_maskers()

        self.assertEqual(
            {call.args[0] for call in register.call_args_list},
            {DUPLICATE_CANDIDATE_TASK_SOURCE,
             CreateDeduplicationReviewTasksService.get_class_name(),
             CreateDeduplicationPaymentReviewTasksService.get_class_name()},
        )

    def test_task_completion_signals_are_not_bound_without_the_apps(self):
        from deduplication.signals import bind_service_signals

        with _without_optional_apps(), mock.patch("deduplication.signals.bind_service_signal") as bind:
            bind_service_signals()

        bind.assert_not_called()

    def test_task_completion_signals_are_bound_for_each_installed_feature(self):
        from deduplication.signals import bind_service_signals

        with _without_optional_apps(("payroll", "payment_cycle", "invoice")), \
                mock.patch("deduplication.signals.bind_service_signal") as bind:
            bind_service_signals()

        self.assertEqual(
            [call.args[1].__name__ for call in bind.call_args_list],
            ["on_deduplication_task_complete_service_handler"],
        )


class RefusalWithoutPaymentAppsTest(SimpleTestCase):

    def assertRefused(self, exc_info, app):
        self.assertIsInstance(exc_info.exception, OptionalAppsMissing)
        self.assertTrue(str(exc_info.exception).startswith(OPTIONAL_APPS_MISSING))
        self.assertIn(app, str(exc_info.exception))

    def test_the_summary_resolvers_refuse(self):
        with _without_optional_apps():
            with self.assertRaises(OptionalAppsMissing) as beneficiary:
                Query().resolve_beneficiary_deduplication_summary(mock.Mock(), columns=["dob"], benefit_plan_id="x")
            with self.assertRaises(OptionalAppsMissing) as payment:
                Query().resolve_benefit_deduplication_summary(mock.Mock(), columns=["dob"], payment_cycle_id="x")

        self.assertRefused(beneficiary, "social_protection")
        self.assertRefused(payment, "payroll")

    def test_the_review_task_mutations_refuse(self):
        with _without_optional_apps():
            with self.assertRaises(OptionalAppsMissing) as beneficiary:
                CreateDeduplicationReviewMutation._validate(_user(), summary=[{"ids": ["x"]}])
            with self.assertRaises(OptionalAppsMissing) as payment:
                CreateDeduplicationPaymentReviewMutation._validate(_user(), summary=[{"ids": ["x"]}])

        self.assertRefused(beneficiary, "social_protection")
        self.assertRefused(payment, "payroll")

    def test_the_aggregations_refuse(self):
        with _without_optional_apps():
            with self.assertRaises(OptionalAppsMissing) as beneficiary:
                get_beneficiary_duplication_aggregation(columns=["dob"], benefit_plan_id="x")
            with self.assertRaises(OptionalAppsMissing) as payment:
                get_benefit_consumption_duplication_aggregation(columns=["dob"], payment_cycle_id="x")

        self.assertRefused(beneficiary, "social_protection")
        self.assertRefused(payment, "payroll")

    def test_the_task_services_return_the_refusal(self):
        with _without_optional_apps():
            beneficiary = CreateDeduplicationReviewTasksService(_user()).create_beneficiary_duplication_tasks([])
            payment = CreateDeduplicationPaymentReviewTasksService(_user()).create_payment_benefit_duplication_tasks(
                [], "x")

        for result in (beneficiary, payment):
            self.assertFalse(result["success"])
            self.assertIn(OPTIONAL_APPS_MISSING, result["detail"])
