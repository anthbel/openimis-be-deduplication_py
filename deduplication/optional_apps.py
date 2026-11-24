"""The apps whose models beneficiary and payment deduplication read. Outside INSTALLED_APPS their models cannot be
imported, so every use is gated on ``apps.is_installed`` and imports them inside the function that needs them."""
from django.apps import apps

BENEFICIARY_APPS = ("social_protection",)
PAYMENT_APPS = ("social_protection", "payroll", "payment_cycle", "invoice")

OPTIONAL_APPS_MISSING = "deduplication.optional_apps_missing"


class OptionalAppsMissing(ValueError):
    """A feature was used while an app it needs is not installed; str() starts with the code, then the details."""

    def __init__(self, feature, missing):
        self.code = OPTIONAL_APPS_MISSING
        super().__init__(f"{OPTIONAL_APPS_MISSING}: {feature} needs {', '.join(missing)}, not installed")


def missing_apps(required):
    return [app for app in required if not apps.is_installed(app)]


def beneficiary_deduplication_available():
    return not missing_apps(BENEFICIARY_APPS)


def payment_deduplication_available():
    return not missing_apps(PAYMENT_APPS)


def require_beneficiary_deduplication():
    """Raises OptionalAppsMissing unless every app of beneficiary deduplication is installed."""
    missing = missing_apps(BENEFICIARY_APPS)
    if missing:
        raise OptionalAppsMissing("beneficiary deduplication", missing)


def require_payment_deduplication():
    """Raises OptionalAppsMissing unless every app of payment deduplication is installed."""
    missing = missing_apps(PAYMENT_APPS)
    if missing:
        raise OptionalAppsMissing("payment deduplication", missing)
