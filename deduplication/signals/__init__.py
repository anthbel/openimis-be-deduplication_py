from core.service_signals import ServiceSignalBindType
from core.signals import bind_service_signal
from deduplication.optional_apps import beneficiary_deduplication_available, payment_deduplication_available
from deduplication.services import (
    on_deduplication_task_complete_service_handler,
    on_payment_benefit_deduplication_task_complete_service_handler,
)


def bind_service_signals():
    if beneficiary_deduplication_available():
        bind_service_signal(
            'task_service.complete_task',
            on_deduplication_task_complete_service_handler,
            bind_type=ServiceSignalBindType.AFTER
        )
    if payment_deduplication_available():
        bind_service_signal(
            'task_service.complete_task',
            on_payment_benefit_deduplication_task_complete_service_handler,
            bind_type=ServiceSignalBindType.AFTER
        )
