from typing import Iterable, Optional, Set

from django.apps import apps
from django.db.models import Q

from deduplication.apps import DeduplicationConfig
from deduplication.sources import Watermark


def get_subject_model():
    """Resolve the configured subject model, e.g. individual.Individual."""
    return apps.get_model(DeduplicationConfig.subject_model)


def subject_model_label(model=None) -> str:
    return (model or get_subject_model())._meta.label


def subject_watermark(model=None) -> Watermark:
    """Newest (date_updated, id) across the whole subject table."""
    model = model or get_subject_model()
    row = model.objects.order_by("-date_updated", "-id").values("date_updated", "id").first()
    if not row:
        return Watermark()
    return Watermark(updated_at=row["date_updated"], last_id=str(row["id"]))


def touched_ids(model, since: Optional[Watermark]) -> Optional[Set[str]]:
    """
    Ids of subjects updated after the watermark, as strings.
    None means "no watermark yet, scan everything"; a filtered scan skips groups that
    don't intersect this set, so an unchanged table yields nothing on a re-scan.
    """
    if since is None or (since.updated_at is None and since.last_id is None):
        return None

    q = Q()
    if since.updated_at is not None:
        q |= Q(date_updated__gt=since.updated_at)
        if since.last_id:
            q |= Q(date_updated=since.updated_at, id__gt=since.last_id)
    elif since.last_id:
        q |= Q(id__gt=since.last_id)

    return set(str(i) for i in model.objects.filter(q).values_list("id", flat=True))


def subject_covered(subject_model: str, subject_ids: Iterable, state) -> Set:
    """The ids among ``subject_ids``, as passed, whose subject's ``(date_updated, id)`` is at or before the
    cursor of ``state`` (a ScanState row). An empty cursor, or no state, covers nobody."""
    by_key = {str(subject_id): subject_id for subject_id in subject_ids}
    if state is None or state.updated_at is None or not by_key:
        return set()
    rows = apps.get_model(subject_model).objects.filter(id__in=list(by_key)).values_list("id", "date_updated")
    return {
        by_key[str(subject_id)]
        for subject_id, updated in rows
        if updated < state.updated_at or (updated == state.updated_at and str(subject_id) <= state.last_id)
    }
