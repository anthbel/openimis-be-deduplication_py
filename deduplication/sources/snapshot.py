"""
Which row versions of a table a scan read, in PostgreSQL transaction ids.

A scan's statements see every row version committed before they run and the versions its own transaction
wrote. ``ReadSnapshot.take`` runs before the scan reads anything and keeps ``xmin``, the lowest transaction id
still running other than the scan's own transaction, or the own transaction's id when lower, and ``seen``, the
versions (``"xmin:ctid"``) already visible that do not count as old (``old``). A version is read by the scan
when it counts as old or it is in ``seen``; any other version is newer than the scan, whatever its
``date_updated`` says.
"""
import logging
from dataclasses import dataclass
from typing import FrozenSet, Optional

from django.db import connection
from django.db.models import BooleanField, TextField
from django.db.models.expressions import RawSQL

logger = logging.getLogger(__name__)

# Versions kept in ``seen`` at most; a version left out counts as not read, so its row is scanned again and
# covers nobody until a later snapshot reads it.
SEEN_LIMIT = 10000

_STATEMENT_XMAX = "pg_snapshot_xmax(pg_current_snapshot())::text::bigint"
_OWN = "pg_current_xact_id_if_assigned()::text::bigint"


def unwrapped(raw: str, xmax: str) -> str:
    """SQL of the 64-bit transaction id whose low 32 bits are ``raw`` (a bigint), taken in the 2^32 window
    centred on ``xmax``. Exact for every version younger than 2^31 transactions."""
    return f"({xmax} - (mod(mod({xmax} - {raw} + 2147483648, 4294967296) + 4294967296, 4294967296) - 2147483648))"


def old(transaction_id: str, xmin: str, xmax: str, own: str) -> str:
    """SQL, true when a visible version written by ``transaction_id`` was committed before a snapshot whose
    ``xmin`` is given. A visible id at or above the statement's ``xmax`` that is not the statement's own
    transaction (``own`` and above; NULL when it has no id) belongs to no running transaction: it is a frozen
    version older than 2^31 transactions, unwrapped into the future."""
    return (f"({transaction_id} < {xmin} OR ({transaction_id} >= {xmax} "
            f"AND ({own} IS NULL OR {transaction_id} < {own})))")


def _version(table: str) -> str:
    return f"({table}.xmin::text || ':' || {table}.ctid::text)"


def _table(model) -> str:
    return connection.ops.quote_name(model._meta.db_table)


def _system() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT system_identifier::text FROM pg_control_system()")
        return cursor.fetchone()[0]


@dataclass(frozen=True)
class ReadSnapshot:
    """The row versions of one table a scan read; ``system`` is the database cluster the ids belong to."""
    system: str
    xmin: int
    seen: FrozenSet[str] = frozenset()

    @classmethod
    def take(cls, queryset) -> "ReadSnapshot":
        """The snapshot of the rows of ``queryset`` visible now, to be taken before the scan reads them. Logs a
        warning when more than ``SEEN_LIMIT`` versions do not count as old."""
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT system_identifier::text, LEAST({_STATEMENT_XMAX}, "
                "(SELECT min(xip::text::bigint) FROM pg_snapshot_xip(pg_current_snapshot()) AS xip), "
                f"{_OWN}), {_STATEMENT_XMAX} FROM pg_control_system()"
            )
            system, xmin, xmax = cursor.fetchone()
        recent = cls(system=system, xmin=int(xmin)).annotate(queryset).filter(_snapshot_old=False)
        versions = list(recent.order_by("_snapshot_version").values_list("_snapshot_version", flat=True)
                        [:SEEN_LIMIT + 1])
        if len(versions) > SEEN_LIMIT:
            logger.warning(
                "Duplicate scan of %s: %d row versions written since the oldest running transaction of the "
                "database server are left out of the scan's snapshot and stay uncovered until a later scan; "
                "that transaction started %d transaction ids ago",
                queryset.model._meta.db_table, recent.count() - SEEN_LIMIT, int(xmax) - int(xmin),
            )
            versions = versions[:SEEN_LIMIT]
        return cls(system=system, xmin=int(xmin), seen=frozenset(versions))

    def annotate(self, queryset):
        """``queryset`` with ``_snapshot_old`` (its current version counts as old for this snapshot) and
        ``_snapshot_version`` (``"xmin:ctid"``); ``read`` tells from both whether the row was read."""
        table = _table(queryset.model)
        transaction_id = unwrapped(f"{table}.xmin::text::bigint", _STATEMENT_XMAX)
        return queryset.annotate(
            _snapshot_old=RawSQL(old(transaction_id, "%s", _STATEMENT_XMAX, _OWN), [self.xmin],
                                 output_field=BooleanField()),
            _snapshot_version=RawSQL(_version(table), [], output_field=TextField()),
        )

    def read(self, is_old: bool, version: str) -> bool:
        """Whether the scan read a row whose ``annotate`` values are ``is_old`` and ``version``."""
        return is_old or version in self.seen

    def current(self) -> bool:
        """Whether the snapshot belongs to the database cluster in use; one of another cluster reads nothing."""
        return self.system == _system()

    def as_json(self) -> dict:
        return {"system": self.system, "xmin": self.xmin, "seen": sorted(self.seen)}

    @classmethod
    def of(cls, state) -> Optional["ReadSnapshot"]:
        """The snapshot a ScanState row keeps under ``summary["snapshot"]``, or None when it keeps none."""
        summary = getattr(state, "summary", None)
        stored = summary.get("snapshot") if isinstance(summary, dict) else None
        if not isinstance(stored, dict):
            return None
        try:
            return cls(system=str(stored["system"]), xmin=int(stored["xmin"]),
                       seen=frozenset(str(version) for version in stored.get("seen") or ()))
        except (KeyError, TypeError, ValueError):
            return None
