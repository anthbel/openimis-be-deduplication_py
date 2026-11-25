"""Beneficiary and payment deduplication need social_protection, payroll, payment_cycle and invoice; the individual
and group duplicate features must start and run without them. The four apps are reported absent by
``apps.is_installed`` and their modules are made unimportable."""
import copy
import importlib
import importlib.abc
import importlib.util
import os
import sys
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest import mock

from django.apps import apps
from django.test import SimpleTestCase

import deduplication
from deduplication.apps import DeduplicationConfig
from deduplication.gql_mutations import CreateDeduplicationPaymentReviewMutation, CreateDeduplicationReviewMutation
from deduplication.masking import summary_review_task_masker
from deduplication.optional_apps import OPTIONAL_APPS_MISSING, OptionalAppsMissing
from deduplication.schema import Query
from deduplication.services import (
    DUPLICATE_CANDIDATE_TASK_SOURCE,
    CreateDeduplicationPaymentReviewTasksService,
    CreateDeduplicationReviewTasksService,
    get_beneficiary_duplication_aggregation,
    get_benefit_consumption_duplication_aggregation,
)
from individual.apps import IndividualConfig
from individual.data_masking import MASK_PLACEHOLDER, IndividualMask
from individual.task_masking import TaskMask
from tasks_management.data_masking import mask_task

OPTIONAL_APP_LABELS = ("social_protection", "payroll", "payment_cycle", "invoice")
MODULE_DIR = os.path.dirname(deduplication.__file__)


def _installed_except(absent):
    real = apps.is_installed
    return lambda label: False if label in absent else real(label)


def _under(roots, name):
    return name.split(".")[0] in roots


class _BlockedRoots(importlib.abc.MetaPathFinder):
    """Refuses the import of every module under ``roots``."""

    def __init__(self, roots):
        self.roots = roots

    def find_spec(self, name, path=None, target=None):
        if _under(self.roots, name):
            raise ModuleNotFoundError(f"{name} is not importable in this test", name=name)
        return None


@contextmanager
def _without_optional_apps(absent=OPTIONAL_APP_LABELS, blocked=OPTIONAL_APP_LABELS):
    """Reports ``absent`` as not installed; every module under ``blocked`` raises ImportError when imported."""
    with mock.patch.object(apps, "is_installed", side_effect=_installed_except(absent)), \
            mock.patch.dict(sys.modules), mock.patch.object(sys, "meta_path", [_BlockedRoots(blocked), *sys.meta_path]):
        for name in [name for name in sys.modules if _under(blocked, name)]:
            del sys.modules[name]
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

    def test_review_task_maskers_cover_every_source_without_the_apps(self):
        with _without_optional_apps(), mock.patch("individual.task_masking.TaskMask.register_source") as register:
            DeduplicationConfig._DeduplicationConfig__register_review_task_maskers()

        self.assertEqual(
            [call.args[0] for call in register.call_args_list],
            [DUPLICATE_CANDIDATE_TASK_SOURCE,
             CreateDeduplicationReviewTasksService.get_class_name(),
             CreateDeduplicationPaymentReviewTasksService.get_class_name()],
        )

    def test_every_module_of_the_absent_apps_is_unimportable(self):
        with _without_optional_apps():
            self.assertFalse([name for name in sys.modules if _under(OPTIONAL_APP_LABELS, name)])
            for name in (*OPTIONAL_APP_LABELS, "social_protection.gql_queries", "payroll.apps",
                         "payment_cycle.services", "invoice.apps"):
                with self.subTest(module=name), self.assertRaises(ImportError):
                    importlib.import_module(name)

        self.assertIn("social_protection.models", sys.modules)

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


BENEFICIARY_SOURCE = CreateDeduplicationReviewTasksService.get_class_name()
PAYMENT_SOURCE = CreateDeduplicationPaymentReviewTasksService.get_class_name()
SUMMARY_ROW = {"individual__first_name": "Sumfirst", "phone": "+25779000002", "k1": "v1"}
ROW_IDS = ["7b0e8f9e-0000-4000-8000-000000000001", "7b0e8f9e-0000-4000-8000-000000000002"]
ROW_STATE = {row_id: {"status": "ACTIVE", "version": 1} for row_id in ROW_IDS}


def _summary_task(source, ids=ROW_IDS):
    return SimpleNamespace(source=source, data={
        "ids": copy.deepcopy(ids), "count": 2, "column_values": dict(SUMMARY_ROW),
        "current_state": copy.deepcopy(ROW_STATE),
    })


def _registered_maskers(absent=None):
    """The task maskers the app registers, by source; ``absent`` apps reported not installed and unimportable."""
    apps_context = nullcontext() if absent is None else _without_optional_apps(absent, blocked=absent)
    with apps_context, mock.patch("individual.task_masking.TaskMask.register_source") as register:
        DeduplicationConfig._DeduplicationConfig__register_review_task_maskers()
    return {call.args[0]: call.args[1] for call in register.call_args_list}


def _reader(may_see_masked):
    user = mock.Mock(id=1, has_perms=mock.Mock(return_value=may_see_masked))
    return SimpleNamespace(context=SimpleNamespace(user=user))


class SummaryReviewTaskMaskersTest(SimpleTestCase):
    """The task maskers of the beneficiary and payment review tasks, a person's first_name, dob and json_ext.phone
    being masked."""

    MODEL_MASKED_ROW = {"individual__first_name": MASK_PLACEHOLDER, "phone": MASK_PLACEHOLDER, "k1": "v1"}
    WHOLLY_MASKED_ROW = {column: MASK_PLACEHOLDER for column in SUMMARY_ROW}

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(IndividualMask._sources, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        IndividualMask.register_fields("test", lambda: ["first_name", "dob", "json_ext.phone"])
        own = mock.patch.object(IndividualConfig, "individual_masking_enabled", False)
        own.start()
        self.addCleanup(own.stop)

    def assertMasked(self, task, row):
        self.assertEqual(task.data["column_values"], row)
        self.assertEqual(task.data["ids"], ROW_IDS)
        self.assertEqual(task.data["count"], 2)
        self.assertEqual(task.data["current_state"], ROW_STATE)

    def test_with_the_apps_a_summary_task_is_masked_by_its_model_masks(self):
        from payroll.models import BenefitConsumption
        from social_protection.models import Beneficiary

        maskers = _registered_maskers()
        for source, model in ((BENEFICIARY_SOURCE, Beneficiary), (PAYMENT_SOURCE, BenefitConsumption)):
            with self.subTest(source=source):
                expected = summary_review_task_masker(model)(_summary_task(source))
                task = maskers[source](_summary_task(source))
                self.assertEqual(task.data, expected.data)
                self.assertMasked(task, self.MODEL_MASKED_ROW)

    def test_without_the_apps_every_value_of_a_summary_task_row_is_masked(self):
        maskers = _registered_maskers(OPTIONAL_APP_LABELS)
        for source in (BENEFICIARY_SOURCE, PAYMENT_SOURCE):
            with self.subTest(source=source):
                self.assertMasked(maskers[source](_summary_task(source)), self.WHOLLY_MASKED_ROW)

    def test_without_the_payment_apps_only_the_payment_task_is_wholly_masked(self):
        maskers = _registered_maskers(("payroll", "payment_cycle", "invoice"))
        self.assertMasked(maskers[BENEFICIARY_SOURCE](_summary_task(BENEFICIARY_SOURCE)), self.MODEL_MASKED_ROW)
        self.assertMasked(maskers[PAYMENT_SOURCE](_summary_task(PAYMENT_SOURCE)), self.WHOLLY_MASKED_ROW)

    def test_without_the_apps_a_summary_row_that_is_not_a_dict_is_masked_whole(self):
        maskers = _registered_maskers(OPTIONAL_APP_LABELS)
        for source in (BENEFICIARY_SOURCE, PAYMENT_SOURCE):
            for column_values in (["Sumfirst", "+25779000002"], "Sumfirst"):
                with self.subTest(source=source, column_values=column_values):
                    task = _summary_task(source)
                    task.data["column_values"] = column_values
                    self.assertMasked(maskers[source](task), MASK_PLACEHOLDER)

    def test_without_the_apps_the_task_mask_masks_a_summary_task_for_a_reader_without_the_masked_data_right(self):
        with mock.patch.dict(TaskMask._maskers, clear=True), _without_optional_apps():
            DeduplicationConfig._DeduplicationConfig__register_review_task_maskers()
            for source in (BENEFICIARY_SOURCE, PAYMENT_SOURCE):
                with self.subTest(source=source):
                    masked = mask_task(_reader(False), _summary_task(source), "Task", lambda task: "")
                    clear = mask_task(_reader(True), _summary_task(source), "Task", lambda task: "")
                    self.assertMasked(masked, self.WHOLLY_MASKED_ROW)
                    self.assertMasked(clear, SUMMARY_ROW)


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

    def test_the_summary_resolvers_check_the_permission_before_the_apps(self):
        info = _reader(False)
        with _without_optional_apps():
            with self.assertRaises(PermissionError) as beneficiary:
                Query().resolve_beneficiary_deduplication_summary(info, columns=["dob"], benefit_plan_id="x")
            with self.assertRaises(PermissionError) as payment:
                Query().resolve_benefit_deduplication_summary(info, columns=["dob"], payment_cycle_id="x")

        for refusal in (beneficiary, payment):
            self.assertNotIsInstance(refusal.exception, OptionalAppsMissing)
            self.assertEqual(str(refusal.exception), "Unauthorized")
        info.context.user.has_perms.assert_called_with(["170001"])

    def test_the_beneficiary_search_right_is_social_protections(self):
        from deduplication.optional_apps import BENEFICIARY_SEARCH_PERMS, beneficiary_search_perms
        from social_protection.apps import SocialProtectionConfig

        self.assertEqual(BENEFICIARY_SEARCH_PERMS, SocialProtectionConfig.gql_beneficiary_search_perms)
        self.assertEqual(beneficiary_search_perms(), SocialProtectionConfig.gql_beneficiary_search_perms)
        with _without_optional_apps():
            self.assertEqual(beneficiary_search_perms(), BENEFICIARY_SEARCH_PERMS)

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
