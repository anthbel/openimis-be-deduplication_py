from typing import List, Tuple, Type, Union

from django.core.exceptions import FieldDoesNotExist, PermissionDenied
from django.db.models import JSONField
from core.models import ExtendableModel, HistoryModel


def resolve_columns(
    model: Union[Type[ExtendableModel], Type[HistoryModel]], columns: List[str]
) -> Tuple[List[str], List[str]]:
    """Split column names into real model fields and json_ext keys."""
    fields = []
    json_fields = []
    for column in columns:
        if is_model_column(model, column.split('__', 1)[0]):
            fields.append(column)
        else:
            json_fields.append(column)

    return fields, json_fields


def is_model_column(model: Union[Type[ExtendableModel], Type[HistoryModel]], column: str) -> bool:
    try:
        model._meta.get_field(column)
        return True
    except FieldDoesNotExist:
        return False


def _own_column(model, column):
    """Whether ``column`` is one of ``model``'s own columns or a key of one of its JSON columns."""
    concrete = model._meta.concrete_fields
    if column in {name for field in concrete for name in (field.name, field.attname)}:
        return True
    return column.split('__', 1)[0] in {field.name for field in concrete if isinstance(field, JSONField)}


def check_summary_columns(
    model: Union[Type[ExtendableModel], Type[HistoryModel]], columns: List[str], person_columns: List[str],
    person: str = None,
):
    """Refuses a summary column other than ``person_columns``, ``model``'s own columns and the keys of its JSON
    columns, the own columns and JSON keys of the row's person (``<person>__<column>``) and the names that are no
    field of ``model`` (bare ``json_ext`` keys): a column following any other relation reads rows outside the
    reader's scope."""
    person_model = model._meta.get_field(person).related_model if person else None
    for column in columns:
        first, _, rest = column.partition('__')
        if column in person_columns or _own_column(model, column):
            continue
        if person and first == person and rest and _own_column(person_model, rest):
            continue
        if is_model_column(model, first):
            raise PermissionDenied(f"the summary column {column} is not allowed")
