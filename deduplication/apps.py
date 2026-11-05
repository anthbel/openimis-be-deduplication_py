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
# The candidate workflow is the third entity: `resolve` decides a candidate, `scan` runs
# the sources, `view` reads candidates.
#
# The django names stay purely declarative: nothing grants them, and checks read the
# numeric rights.
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
    "duplicateCandidate": {
        "resolve": ("deduplication.change_duplicatecandidate", 172003),
        "scan": ("deduplication.add_duplicatecandidate", 172004),
        "view": ("deduplication.view_duplicatecandidate", 172005),
    },
}

_PERM_CFG = {
    "gql_create_deduplication_review_perms": ("deduplicationReview", "create"),
    "gql_create_deduplication_payment_review_perms": ("paymentDeduplicationReview", "create"),
    "gql_resolve_duplicate_perms": ("duplicateCandidate", "resolve"),
    "gql_run_scan_perms": ("duplicateCandidate", "scan"),
    "gql_query_duplicates_perms": ("duplicateCandidate", "view"),
}

RIGHTS = RightsDeclaration(MODULE_NAME, DJANGO_PERMS, _PERM_CFG)

perms = RIGHTS.perms
django_perms = RIGHTS.django_perm_names
configured_perms = RIGHTS.configured
require = RIGHTS.require


DEFAULT_CONFIG = {
    "subject_model": "individual.Individual",
    "demographic_columns": ["first_name", "last_name", "dob"],
    "identifier_keys": [],
    "identifier_match": "each",
    "merge_policy": "delete",
}


class DeduplicationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = MODULE_NAME

    # Rights: constants, no longer overridable. They go neither through DEFAULT_CFG
    # nor through ready(): `ModuleConfiguration.get_or_default` now ignores any
    # `_perms` key stored in the database.
    gql_create_deduplication_review_perms = RIGHTS.perms("deduplicationReview", "create")
    gql_create_deduplication_payment_review_perms = RIGHTS.perms("paymentDeduplicationReview", "create")
    gql_resolve_duplicate_perms = RIGHTS.perms("duplicateCandidate", "resolve")
    gql_run_scan_perms = RIGHTS.perms("duplicateCandidate", "scan")
    gql_query_duplicates_perms = RIGHTS.perms("duplicateCandidate", "view")

    subject_model = None
    demographic_columns = None
    identifier_keys = None
    identifier_match = None
    merge_policy = None

    def ready(self):
        from core.models import ModuleConfiguration

        cfg = ModuleConfiguration.get_or_default(self.name, DEFAULT_CONFIG)
        self.__load_config(cfg)
        self.__register_builtin_sources()
        self.__connect_task_bridge()

    @classmethod
    def __load_config(cls, cfg):
        """
        Load all config fields that match current AppConfig class fields, all custom fields have to be loaded separately
        """
        for field in cfg:
            if hasattr(DeduplicationConfig, field):
                setattr(DeduplicationConfig, field, cfg[field])

    @classmethod
    def __register_builtin_sources(cls):
        from deduplication.sources import register
        from deduplication.sources.demographic import DemographicSource
        from deduplication.sources.identifier import IdentifierSource

        register(DemographicSource())
        register(IdentifierSource())

    @classmethod
    def __connect_task_bridge(cls):
        from django.db.models.signals import pre_save
        from deduplication.services import resolve_candidate_of_completing_task

        pre_save.connect(
            resolve_candidate_of_completing_task,
            sender='tasks_management.Task',
            dispatch_uid='deduplication.resolve_candidate_of_completing_task',
        )
