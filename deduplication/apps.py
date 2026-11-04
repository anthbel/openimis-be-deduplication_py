from django.apps import AppConfig

from core.rights_declaration import RightsDeclaration

MODULE_NAME = "deduplication"

# Rights, by entity then by action.
#
# Two entities rather than a single one with two actions: the two rights do not bear on
# the same business object. 172001 creates review tasks for *beneficiary* duplicates
# (social_protection.Beneficiary), 172002 for *payment* duplicates
# (payroll.BenefitConsumption); mutations, services and validations are distinct end to
# end. Each therefore has a single action, `create`.
#
# The django names stay purely declarative here: this module has no model at all
# (models.py is empty), the review being materialised by a tasks_management.Task. The
# prefix stays `deduplication` because this module is the one that declares and
# enforces the right, even though no django permission will be created at
# post_migrate.
DJANGO_PERMS = {
    "deduplicationReview": {
        "create": ("deduplication.add_deduplicationreview", 172001),
    },
    # Dormant right: declared and exposed in the roles screen, but no check reads it.
    # `CreateDeduplicationPaymentReviewMutation._validate` (gql_mutations.py) checks
    # 172001. The discrepancy is reported, not fixed: fixing it would withdraw access
    # from the roles that only have 172001.
    "paymentDeduplicationReview": {
        "create": ("deduplication.add_paymentdeduplicationreview", 172002),
    },
}

_PERM_CFG = {
    "gql_create_deduplication_review_perms": ("deduplicationReview", "create"),
    "gql_create_deduplication_payment_review_perms": ("paymentDeduplicationReview", "create"),
}

RIGHTS = RightsDeclaration(MODULE_NAME, DJANGO_PERMS, _PERM_CFG)

perms = RIGHTS.perms
django_perms = RIGHTS.django_perm_names
configured_perms = RIGHTS.configured
require = RIGHTS.require


DEFAULT_CONFIG = {
}


class DeduplicationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = MODULE_NAME

    # Rights: constants, no longer overridable. They go neither through DEFAULT_CFG
    # nor through ready(): `ModuleConfiguration.get_or_default` now ignores any
    # `_perms` key stored in the database.
    gql_create_deduplication_review_perms = RIGHTS.perms("deduplicationReview", "create")
    gql_create_deduplication_payment_review_perms = RIGHTS.perms("paymentDeduplicationReview", "create")

    def ready(self):
        from core.models import ModuleConfiguration

        cfg = ModuleConfiguration.get_or_default(self.name, DEFAULT_CONFIG)
        self.__load_config(cfg)

    @classmethod
    def __load_config(cls, cfg):
        """
        Load all config fields that match current AppConfig class fields, all custom fields have to be loaded separately
        """
        for field in cfg:
            if hasattr(DeduplicationConfig, field):
                setattr(DeduplicationConfig, field, cfg[field])
