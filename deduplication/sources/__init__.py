"""
Seam owned by the deduplication module: the shapes and registry other modules
(e.g. biometric_verification) use to feed duplicate-subject candidates into
deduplication, without deduplication importing them back.
"""
import abc
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, FrozenSet, Iterable, Iterator, List, Optional, Set


@dataclass(frozen=True)
class Watermark:
    """Cursor a source resumes scanning from; both fields None means 'from the start'."""
    updated_at: Optional[datetime] = None
    last_id: Optional[str] = None


@dataclass(frozen=True)
class Candidate:
    """One suspected duplicate pair, as reported by a source."""
    subject_model: str
    subject_a: str          # ordered: subject_a < subject_b as strings
    subject_b: str
    kind: str                # "demographic" | "identifier" | "biometric" | any registered kind
    score: Optional[float]   # higher = more likely the same person; None for exact matches
    evidence: dict           # JSON-serialisable; what a reviewer needs to see


class CandidateSource(abc.ABC):
    """A pluggable producer of duplicate-subject candidates."""
    kind: str

    @abc.abstractmethod
    def scan(self, since: Optional[Watermark]) -> Iterable[Candidate]:
        """Yield candidates found since the given watermark (None: scan everything)."""
        raise NotImplementedError

    @abc.abstractmethod
    def watermark(self) -> Watermark:
        """Cursor where the next scan should start from."""
        raise NotImplementedError


_REGISTRY: List[CandidateSource] = []


def register(source: CandidateSource) -> None:
    """Add a source to the registry, in call order."""
    _REGISTRY.append(source)


def sources() -> List[CandidateSource]:
    """Registered sources, in registration order."""
    return list(_REGISTRY)


class MergeRefusal(Exception):
    """A merge a preparer refuses; resolve() reports it as a ResolveRefusal with this code and message."""

    def __init__(self, code: str, details: str = "", message: Optional[str] = None):
        self.code = code
        self.details = details
        self.message = message or code
        super().__init__(f"{code}: {details}" if details else code)


@dataclass(frozen=True)
class MergePlan:
    """A preparer's answer: ``fill`` False skips the fill of the kept subject and its conflict journal;
    ``released_group_ids`` are the groups whose membership of the retired subject the preparer ends before the
    enrolment guard, which a check (no write) then does not count."""
    fill: bool = True
    released_group_ids: FrozenSet = frozenset()


class MergePreparer(abc.ABC):
    """Prepares the merges of resolve(), inside its transaction.

    ``lock`` runs before the subject locks, for a "same" decision; its return value is handed to ``prepare`` as
    ``locked``. ``prepare`` runs after the subject and candidate locks and before the enrolment guard, with
    ``merge_fields`` naming the fields the fill would copy or journal. check_resolve() calls ``prepare`` alone,
    with ``check`` True, ``actor`` and ``locked`` None: it must then write nothing. Both raise MergeRefusal to
    refuse.
    """

    def lock(self, candidate, keep: Optional[str]):
        return None

    @abc.abstractmethod
    def prepare(self, candidate, kept, retired, *, actor, check: bool, locked=None,
                merge_fields: Iterable[str] = ()) -> MergePlan:
        raise NotImplementedError


_MERGE_PREPARERS: Dict[str, MergePreparer] = {}


def register_merge_preparer(key: str, preparer: Optional[MergePreparer]) -> None:
    """Registers a merge preparer under ``key``; None removes it. Preparers run in key order."""
    if preparer is None:
        _MERGE_PREPARERS.pop(key, None)
    else:
        _MERGE_PREPARERS[key] = preparer


def merge_preparers() -> List[MergePreparer]:
    """The registered merge preparers, in key order."""
    return [_MERGE_PREPARERS[key] for key in sorted(_MERGE_PREPARERS)]


def order_pair(a: str, b: str):
    """Return (a, b) sorted so the first element is strictly the smaller string."""
    return (a, b) if a < b else (b, a)


# subject_filter(subject_model, subject_ids) -> the ids among subject_ids that scans leave out.
SubjectFilter = Callable[[str, Set[str]], Iterable[str]]

_SUBJECT_FILTERS: Dict[str, SubjectFilter] = {}
FILTER_BATCH_SIZE = 500


def register_subject_filter(key: str, subject_filter: Optional[SubjectFilter]) -> None:
    """Registers a filter under ``key``; None removes it. A scan records no candidate with a subject that a
    registered filter leaves out."""
    if subject_filter is None:
        _SUBJECT_FILTERS.pop(key, None)
    else:
        _SUBJECT_FILTERS[key] = subject_filter


def left_out(subject_model: str, subject_ids: Iterable[str]) -> Set[str]:
    """The ids among ``subject_ids`` that a registered filter leaves out of scans."""
    subject_ids = set(subject_ids)
    found = set()
    if subject_ids:
        for subject_filter in list(_SUBJECT_FILTERS.values()):
            found.update(str(subject_id) for subject_id in subject_filter(subject_model, subject_ids) or ())
    return found


def without_left_out(candidates: Iterable[Candidate]) -> Iterator[Candidate]:
    """The candidates neither of whose subjects is left out, the filters being asked once per batch."""
    batch = []
    for candidate in candidates:
        batch.append(candidate)
        if len(batch) >= FILTER_BATCH_SIZE:
            yield from _kept(batch)
            batch = []
    yield from _kept(batch)


def _kept(batch):
    if not batch or not _SUBJECT_FILTERS:
        return batch
    by_model = {}
    for candidate in batch:
        by_model.setdefault(candidate.subject_model, set()).update((candidate.subject_a, candidate.subject_b))
    out = {model: left_out(model, ids) for model, ids in by_model.items()}
    return [c for c in batch if c.subject_a not in out[c.subject_model] and c.subject_b not in out[c.subject_model]]


def rescan(subject_model: str, subject_ids: Iterable[str]) -> None:
    """Moves back, before the creation of the earliest of ``subject_ids``, every source cursor that is past it,
    so the next scan of each source revisits those subjects; a source never scanned keeps no cursor.

    A source's cursor must order rows written no earlier than their subject's ``date_created``. Every cursor
    row is locked before any is compared, so a scan running meanwhile finishes first and its new cursor is
    the one compared and moved back.
    """
    from django.apps import apps
    from django.db import transaction
    from django.db.models import Min

    from deduplication.models import ScanState

    subject_ids = [str(subject_id) for subject_id in subject_ids]
    if not subject_ids:
        return
    created = apps.get_model(subject_model).objects.filter(id__in=subject_ids).aggregate(
        earliest=Min("date_created"))["earliest"]
    if created is None:
        return
    before = created - timedelta(microseconds=1)
    with transaction.atomic():
        # A WHERE on updated_at would be checked against the cursor committed before a running scan's.
        for state in ScanState.objects.select_for_update().order_by("kind"):
            if state.updated_at is None or state.updated_at <= before:
                continue
            state.updated_at = before
            state.last_id = ""
            state.save(update_fields=["updated_at", "last_id"])
