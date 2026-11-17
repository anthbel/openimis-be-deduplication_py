import uuid

from django.apps import apps
from django.conf import settings
from django.db import models
from django.db.models import CharField, F, Q
from django.db.models.functions import Cast, Lower
from django.utils.translation import gettext_lazy as _


class DuplicateCandidate(models.Model):
    """A suspected duplicate pair of subjects, detected by a CandidateSource."""

    class Status(models.TextChoices):
        OPEN = "OPEN", _("Open")
        CONFIRMED = "CONFIRMED", _("Confirmed")
        DISMISSED = "DISMISSED", _("Dismissed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subject_model = models.CharField(max_length=255)
    subject_a = models.CharField(max_length=64)
    subject_b = models.CharField(max_length=64)
    kind = models.CharField(max_length=32)
    source = models.CharField(max_length=64)
    score = models.FloatField(null=True, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    task = models.ForeignKey(
        'tasks_management.Task', models.DO_NOTHING, null=True, blank=True,
        related_name='duplicate_candidates',
    )
    reviewed_by = models.CharField(max_length=64, blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "deduplication_candidate"
        constraints = [
            models.UniqueConstraint(
                fields=["subject_model", "subject_a", "subject_b", "kind"],
                name="dedup_candidate_unique_pair",
            ),
            models.CheckConstraint(
                check=Q(subject_a__lt=F("subject_b")),
                name="dedup_candidate_subject_order",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "kind"], name="dedup_candidate_status_kind"),
        ]

    def __str__(self):
        return f"{self.subject_model} {self.subject_a}/{self.subject_b} ({self.kind})"

    @classmethod
    def get_queryset(cls, queryset, user):
        """``queryset`` (every candidate when None) narrowed to the pairs whose two subjects are both rows the
        subject model's own ``get_queryset(None, user)`` gives ``user`` (``Individual``: the person's or its
        household's location; ``Group``: the group's location). A pair with a subject out of reach, or whose
        subject model has no such method, is left out."""
        if queryset is None:
            queryset = cls.objects.all()
        if not settings.ROW_SECURITY:
            return queryset
        if user.is_anonymous:
            return queryset.none()
        if user.is_imis_admin:
            return queryset
        in_scope = Q(pk__in=[])
        for label in queryset.order_by().values_list("subject_model", flat=True).distinct():
            subject_ids = _visible_subject_ids(label, user)
            if subject_ids is not None:
                in_scope |= Q(subject_model=label, subject_a__in=subject_ids, subject_b__in=subject_ids)
        return queryset.filter(in_scope)


def _visible_subject_ids(subject_model, user):
    """Subquery of the ids, as the lowercase text the candidates store, of the ``subject_model`` rows ``user``
    may see; None when the label names no model with a ``get_queryset(queryset, user)``."""
    try:
        model = apps.get_model(subject_model)
    except (LookupError, ValueError):
        return None
    if not callable(getattr(model, "get_queryset", None)):
        return None
    return (model.get_queryset(None, user).order_by()
            .annotate(_subject_id=Lower(Cast("id", CharField()))).values("_subject_id"))


class ScanState(models.Model):
    """Watermark bookkeeping for a candidate source, one row per source kind."""

    kind = models.CharField(max_length=32, primary_key=True)
    updated_at = models.DateTimeField(null=True, blank=True)
    last_id = models.CharField(max_length=64, blank=True, default="")
    last_scan_at = models.DateTimeField(null=True, blank=True)
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "deduplication_scan_state"

    def __str__(self):
        return f"ScanState({self.kind})"
