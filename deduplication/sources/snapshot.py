"""
Which row versions of a table a scan read, in PostgreSQL transaction ids.

A scan's statements see every row version committed before they run and the versions its own transaction
wrote. ``ReadSnapshot.take`` runs before the scan reads anything and keeps ``xmin``, the lowest transaction id
still running other than the scan's own transaction, or the own transaction's id when lower, and ``seen``, the
versions (``"xmin:ctid"``) already visible whose id is at or above ``xmin``. A version is read by the scan when
its transaction id is below ``xmin`` or it is in ``seen``; any other version is newer than the scan, whatever
its ``date_updated`` says.
"""
from dataclasses import dataclass
from typing import FrozenSet, Optional

from django.db import connection
from django.db.models import BigIntegerField, BooleanField, TextField
from django.db.models.expressions import RawSQL

# Versions kept in ``seen`` at most; a version left out counts as not read, so its row is scanned again and
# covers nobody until a later snapshot reads it.
SEEN_LIMIT = 10000

_STATEMENT_XMAX = "pg_snapshot_xmax(pg_current_snapshot())::text::bigint"
_SYSTEM = "(SELECT system_identifier FROM pg_control_system())::text"


def _transaction_id(table: str) -> str:
    """SQL of the 64-bit id of the transaction that wrote a row's current version: its 32-bit ``xmin`` taken
    in the 2^32 window around the statement snapshot's xmax. Exact for every version younger than 2^31
    transactions; an older (frozen) version can read as recent, and then counts as read only through ``seen``."""
    return (f"({_STATEMENT_XMAX} - (mod(mod({_STATEMENT_XMAX} - {table}.xmin::text::bigint + 2147483648, "
            f"4294967296) + 4294967296, 4294967296) - 2147483648))")


def _version(table: str) -> str:
    return f"({table}.xmin::text || ':' || {table}.ctid::text)"


def _table(model) -> str:
    return connection.ops.quote_name(model._meta.db_table)


@dataclass(frozen=True)
class ReadSnapshot:
    """The row versions of one table a scan read; ``system`` is the database cluster the ids belong to."""
    system: str
    xmin: int
    seen: FrozenSet[str] = frozenset()

    @classmethod
    def take(cls, queryset) -> "ReadSnapshot":
        """The snapshot of the rows of ``queryset`` visible now, to be taken before the scan reads them."""
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_SYSTEM}, LEAST({_STATEMENT_XMAX}, "
                "(SELECT min(xip::text::bigint) FROM pg_snapshot_xip(pg_current_snapshot()) AS xip), "
                "pg_current_xact_id_if_assigned()::text::bigint)"
            )
            system, xmin = cursor.fetchone()
        table = _table(queryset.model)
        seen = queryset.annotate(
            _transaction_id=RawSQL(_transaction_id(table), [], output_field=BigIntegerField()),
            _version=RawSQL(_version(table), [], output_field=TextField()),
        ).filter(_transaction_id__gte=xmin).order_by("_transaction_id", "_version") \
            .values_list("_version", flat=True)[:SEEN_LIMIT]
        return cls(system=system, xmin=int(xmin), seen=frozenset(seen))

    def read(self, model) -> RawSQL:
        """Boolean expression on the rows of ``model``: their current version was read by this snapshot's scan.
        False on every row in another database cluster."""
        table = _table(model)
        return RawSQL(
            f"({_SYSTEM} = %s AND ({_transaction_id(table)} < %s OR {_version(table)} = ANY(%s::text[])))",
            [self.system, self.xmin, sorted(self.seen)], output_field=BooleanField(),
        )

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
