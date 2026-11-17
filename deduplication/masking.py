"""individual's masks on the person values deduplication gives: the columns of a candidate's evidence and of a
duplication summary. Each column is a field lookup or a json_ext key of the model it is read on, as
``column_resolution.resolve_columns`` splits them; both readings are checked."""
import copy

from django.apps import apps

from core.data_masking import DataMaskAbs, MaskingClassStorage
from individual.data_masking import MASK_PLACEHOLDER, reads_masked_field

JSON_LOOKUP = "json_ext__"


def reads_masked_column(model, column, person=None):
    """Whether ``column`` of ``model`` reads a field a registered mask masks. ``person`` names the relation to
    the person whose json_ext keys ``model``'s own json_ext may copy, so its keys are also checked there."""
    lookups = [column, JSON_LOOKUP + column]
    if person:
        lookups.append(f"{person}__{JSON_LOOKUP}{column}")
    return any(reads_masked_field(model, lookup) for lookup in lookups)


def masked_evidence(subject_model, evidence):
    """A copy of ``evidence`` whose ``columns`` values read from a masked field of ``subject_model`` (an
    ``app_label.Model`` label) are masked by that model's mask; all of them when the label names no model."""
    columns = evidence.get("columns") if isinstance(evidence, dict) else None
    if not isinstance(columns, dict):
        return evidence
    try:
        model = apps.get_model(subject_model)
    except (LookupError, ValueError):
        model = None
    mask = (MaskingClassStorage.get_masking_class(model.__name__) if model else None) or DataMaskAbs()
    masked = copy.deepcopy(evidence)
    for column, value in columns.items():
        if model is None or reads_masked_column(model, column):
            masked["columns"][column] = mask.anonymize(value)
    return masked


def mask_summary_values(model, column_values, person):
    """Replaces in place the values of ``column_values`` ({column: value as text}, one summary row read on
    ``model``) whose column reads a masked field by the string mask."""
    for column in column_values:
        if reads_masked_column(model, column, person=person):
            column_values[column] = MASK_PLACEHOLDER
    return column_values
