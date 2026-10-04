"""Helpers for schema/expected_schema.yaml: what Layer 2 expects in Layer 1's c360.duckdb."""
from dataclasses import dataclass

from c360.layer2 import config

PARTS = ("confirmed", "protected", "inferred")


@dataclass(frozen=True)
class ExpectedColumn:
    schema: str
    table: str
    column: str
    type: str            # string | int | float | bool | date | timestamp | list
    confidence: str      # confirmed | protected | inferred


def load_expected(path=None) -> dict:
    return config.load_yaml(path or config.EXPECTED_SCHEMA_YAML)


def expected_columns(expected=None) -> list[ExpectedColumn]:
    expected = expected or load_expected()
    out = []
    for schema, tables in expected.items():
        for table, spec in tables.items():
            for part in PARTS:
                for column, typ in (spec.get(part) or {}).items():
                    out.append(ExpectedColumn(schema, table, column, typ, part))
    return out


def type_class(duck_type: str) -> str:
    """Collapse a DuckDB type name into the coarse classes used in expected_schema.yaml."""
    t = duck_type.upper()
    if t.endswith("[]") or t.startswith("LIST"):
        return "list"
    if t.startswith(("VARCHAR", "TEXT", "STRING", "CHAR", "UUID", "JSON")):
        return "string"
    if t in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT"):
        return "int"
    if t.startswith(("DOUBLE", "FLOAT", "REAL", "DECIMAL", "NUMERIC")):
        return "float"
    if t == "BOOLEAN":
        return "bool"
    if t == "DATE":
        return "date"
    if t.startswith("TIMESTAMP") or t == "DATETIME":
        return "timestamp"
    return "other"


def compatible(expected_type: str, duck_type: str) -> bool:
    actual = type_class(duck_type)
    if expected_type == actual:
        return True
    numeric = {"int", "float"}
    temporal = {"date", "timestamp"}
    return ({expected_type, actual} <= numeric) or ({expected_type, actual} <= temporal)
