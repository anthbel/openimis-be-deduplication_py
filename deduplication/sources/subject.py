from typing import Iterable, Optional, Set

from django.apps import apps
from django.db.models import BooleanField, ExpressionWrapper, Q

from deduplication.apps import DeduplicationConfig
from deduplication.sources import Watermark
from deduplication.sources.snapshot import ReadSnapshot


def get_subject_model():
    """Resolve the configured subject model, e.g. individual.Individual."""
    return apps.get_model(DeduplicationConfig.subject_model)


def subject_model_label(model=None) -> str:
    return (model or get_subject_model())._meta.label


def subject_watermark(model=None) -> Watermark:
    """Newest (date_updated, id) across the whole subject table, with the snapshot of the subjects read so far."""
    model = model or get_subject_model()
    snapshot = ReadSnapshot.take(model.objects.all())
    row = model.objects.order_by("-date_updated", "-id").values("date_updated", "id").first()
    if not row:
        return Watermark(snapshot=snapshot)
    return Watermark(updated_at=row["date_updated"], last_id=str(row["id"]), snapshot=snapshot)


def touched_ids(model, since: Optional[Watermark]) -> Optional[Set[str]]:
    """
    Ids of subjects updated after the watermark or whose current version its snapshot did not read, as strings.
    None means "scan everything": no watermark yet, one without a snapshot, or a snapshot of another database
    cluster. A filtered scan skips groups that don't intersect this set, so an unchanged table yields nothing on
    a re-scan.
    """
    if since is None or (since.updated_at is None and since.last_id is None) or since.snapshot is None \
            or not since.snapshot.current():
        return None

    after = Q()
    if since.updated_at is not None:
        after |= Q(date_updated__gt=since.updated_at)
        if since.last_id:
            after |= Q(date_updated=since.updated_at, id__gt=since.last_id)
    elif since.last_id:
        after |= Q(id__gt=since.last_id)

    # One read of the table; the versions of seen are looked up here rather than sent with the query.
    rows = since.snapshot.annotate(model.objects.all()) \
        .annotate(_after=ExpressionWrapper(after, output_field=BooleanField())) \
        .filter(Q(_after=True) | Q(_snapshot_old=False)) \
        .values_list("id", "_after", "_snapshot_old", "_snapshot_version")
    return {str(i) for i, is_after, is_old, version in rows if is_after or not since.snapshot.read(is_old, version)}


def subject_covered(subject_model: str, subject_ids: Iterable, state) -> Set:
    """The ids among ``subject_ids``, as passed, whose subject's ``(date_updated, id)`` is at or before the
    cursor of ``state`` (a ScanState row) and whose current version the snapshot ``state`` keeps read. A state
    without a snapshot, or with one of another database cluster, an empty cursor, or no state, covers nobody."""
    by_key = {str(subject_id): subject_id for subject_id in subject_ids}
    if state is None or state.updated_at is None or not by_key:
        return set()
    snapshot = ReadSnapshot.of(state)
    if snapshot is None or not snapshot.current():
        return set()
    model = apps.get_model(subject_model)
    rows = snapshot.annotate(model.objects.filter(id__in=list(by_key))) \
        .values_list("id", "date_updated", "_snapshot_old", "_snapshot_version")
    return {
        by_key[str(subject_id)]
        for subject_id, updated, is_old, version in rows
        if snapshot.read(is_old, version)
        and (updated < state.updated_at or (updated == state.updated_at and str(subject_id) <= state.last_id))
    }
