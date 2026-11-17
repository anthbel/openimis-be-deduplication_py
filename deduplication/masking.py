"""individual's masks on the person values deduplication gives: the columns of a candidate's evidence and of a
duplication summary. Each column is a field lookup or a json_ext key of the model it is read on, as
``column_resolution.resolve_columns`` splits them; both readings are checked."""
import copy
import logging

from django.apps import apps

from core.data_masking import DataMaskAbs, MaskingClassStorage
from individual.data_masking import MASK_PLACEHOLDER, IndividualMask, may_see_masked, order_lookups, reads_masked_field

logger = logging.getLogger(__name__)

JSON_EXT = "json_ext"
JSON_LOOKUP = JSON_EXT + "__"


def reads_masked_column(model, column, person=None):
    """Whether ``column`` of ``model`` reads a field a registered mask masks. ``person`` names the relation to
    the person whose json_ext keys ``model``'s own json_ext may copy: a column reading ``model``'s json_ext
    (``json_ext``, ``json_ext__<key>`` or a bare key) is also checked on the person's json_ext."""
    lookups = [column, JSON_LOOKUP + column]
    if person:
        lookups += [f"{person}__{lookup}" for lookup in lookups
                    if lookup == JSON_EXT or lookup.startswith(JSON_LOOKUP)]
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


def mask_review_task(task):
    """Masks in place the candidate evidence a review task copies into its data, as ``masked_evidence`` masks
    it. Registered with individual's task mask, which applies it for a reader without the masked-data right."""
    data = getattr(task, "data", None)
    if isinstance(data, dict) and "evidence" in data:
        data["evidence"] = masked_evidence(data.get("subject_model"), data["evidence"])
    return task


def mask_summary_values(model, column_values, person):
    """Replaces in place the values of ``column_values`` ({column: value as text}, one summary row read on
    ``model``) whose column reads a masked field by the string mask."""
    for column in column_values:
        if reads_masked_column(model, column, person=person):
            column_values[column] = MASK_PLACEHOLDER
    return column_values


def mask_representation(model, values, person=None):
    """Replaces in place, by the value individual's masks give, each value of ``values`` (a
    ``model_representation`` of a ``model`` row) whose field reads a masked field; a ``json_ext`` dict is
    masked key by key. ``person`` as in ``reads_masked_column``."""
    mask = IndividualMask()
    for key, value in values.items():
        if key == JSON_EXT and isinstance(value, dict):
            for json_key in value:
                if reads_masked_column(model, JSON_LOOKUP + json_key, person=person):
                    value[json_key] = mask.anonymize(value[json_key])
        elif reads_masked_column(model, key, person=person):
            values[key] = mask.anonymize(value)
    return values


def mask_review_task_data(model, serialized_data):
    """Masks in place the business data of a duplicate review task of ``model`` rows (beneficiaries or
    benefits): each row of ``ids`` with its person, and the summary row ``column_values`` it was made from."""
    from individual.models import Individual

    for row in serialized_data.get("ids") or []:
        if not isinstance(row, dict):
            continue
        if isinstance(row.get("individual"), dict):
            mask_representation(Individual, row["individual"])
        mask_representation(model, row, person="individual")
    column_values = serialized_data.get("column_values")
    if isinstance(column_values, dict):
        mask_summary_values(model, column_values, person="individual")
    return serialized_data


def summary_review_task_masker(model):
    """A task masker for the review tasks made from a duplication summary row of ``model`` rows (beneficiaries
    or benefits): the stored row's ``column_values`` masked as the summaries mask them. Registered with
    individual's task mask, which applies it for a reader without the masked-data right."""

    def mask(task):
        data = getattr(task, "data", None)
        column_values = data.get("column_values") if isinstance(data, dict) else None
        if isinstance(column_values, dict):
            mask_summary_values(model, column_values, person="individual")
        return task

    return mask


def unmasked_candidate_order_by(user, model, order_by):
    """``order_by`` less its terms ordering by a candidate's evidence, for a user without the masked-data right:
    the evidence holds person values, so such an ordering would tell the masked ones. The primary key orders
    last, so the order stays total and pages neither repeat nor skip rows."""
    if not order_by:
        return order_by
    terms = [order_by] if isinstance(order_by, str) else list(order_by)
    kept = terms if may_see_masked(user) else [
        term for term in terms if not any(lookup.split("__")[0] == "evidence" for lookup in order_lookups([term]))]
    if len(kept) < len(terms):
        logger.info("orderBy %s of %s left out for a user without the masked-data right",
                    [term for term in terms if term not in kept], model.__name__)
    primary_key = model._meta.pk.name
    if primary_key not in order_lookups(kept):
        kept = [*kept, primary_key]
    return kept
