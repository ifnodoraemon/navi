"""Round-7 security-hardening regressions.

SQL identifiers cannot be parameterized, so table names are f-string
interpolated into DDL/PRAGMA/DROP. They are schema-as-code today, but the
``Table`` constructor now validates the identifier shape so no future table
declaration can smuggle SQL through ``name`` (defense in depth, principle 16).
"""

from __future__ import annotations

import pytest

from navi.schema import Column, Table


def test_table_rejects_non_identifier_name() -> None:
    with pytest.raises(ValueError):
        Table("runs; DROP TABLE runs", [Column("id", "TEXT", primary_key=True)])
    with pytest.raises(ValueError):
        Table("bad-name", [Column("id", "TEXT")])
    with pytest.raises(ValueError):
        Table("", [Column("id", "TEXT")])


def test_table_accepts_valid_identifier() -> None:
    table = Table("valid_table_1", [Column("id", "TEXT", primary_key=True)])
    assert table.name == "valid_table_1"
