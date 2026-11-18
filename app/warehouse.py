"""Trusted request-local warehouse selection for storage operations."""

import re
from contextvars import ContextVar

warehouse_context: ContextVar[str] = ContextVar("warehouse", default="bgd")


def current_warehouse() -> str:
    warehouse = warehouse_context.get()
    if warehouse not in ("bgd", "cas"):
        raise ValueError("invalid warehouse")
    return warehouse


_NAMES = r"parcel_identity|parcel_embedding|review_audit|session|awb|label_print"
_TABLES = re.compile(
    rf"(?i)(\b(?:FROM|JOIN|UPDATE|INTO|TABLE)\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:cv\.)?)({_NAMES})\b"
)
_QUALIFIED = re.compile(rf"(?i)(\bcv\.)({_NAMES})(?=\.)")
_LITERALS = re.compile(r"('(?:''|[^'])*'|--[^\n]*|/\*[\s\S]*?\*/)")


def warehouse_sql(sql: str, warehouse: str | None = None) -> str:
    warehouse = current_warehouse() if warehouse is None else warehouse
    if warehouse not in ("bgd", "cas"):
        raise ValueError("invalid warehouse")

    def table(match: re.Match) -> str:
        name = match.group(2)
        name = f"{name}_{warehouse}"
        return match.group(1) + name

    parts = _LITERALS.split(sql)
    for i in range(0, len(parts), 2):
        parts[i] = _QUALIFIED.sub(table, _TABLES.sub(table, parts[i]))
    return "".join(parts)
