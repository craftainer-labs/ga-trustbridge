"""The bodies migration 6f3b9d2e4a85 restores and hashes are what 5d7a2c8e1b94 installs.

The hardening migration compares `md5(prosrc)` of the live functions with a copy of the
`5d7a2c8e1b94` bodies; a byte of drift between the copy and what that migration builds would fail
every upgrade. This pins the copy to the other migration's own output, with no database.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[3]


def _load(prefix: str) -> ModuleType:
    path = next((ROOT / "alembic" / "versions").glob(f"{prefix}_*.py"))
    spec = importlib.util.spec_from_file_location(f"_mig_{prefix}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _body(sql: str) -> str:
    return sql[sql.index("$$") + 2 : sql.rindex("$$")]


def test_the_restored_bodies_are_the_ones_the_previous_migration_installs() -> None:
    """Byte for byte, so the upgrade's md5 guard accepts a database migrated by 5d7a2c8e1b94."""
    before = _load("5d7a2c8e1b94")
    after = _load("6f3b9d2e4a85")
    assert _body(after._PREVIOUS_ADVANCE_FN) == _body(before._ADVANCE_FN)
    assert _body(after._PREVIOUS_DELETE_FN) == _body(before._DELETE_FN)


def test_the_new_bodies_differ_and_the_guards_hash_the_right_ones() -> None:
    """The two directions hash different bodies: exactly the text between the outer `$$`."""
    after = _load("6f3b9d2e4a85")
    assert _body(after._ADVANCE_FN) != _body(after._PREVIOUS_ADVANCE_FN)
    assert _body(after._DELETE_FN) != _body(after._PREVIOUS_DELETE_FN)
    for sql in (after._PREVIOUS_ADVANCE_FN, after._PREVIOUS_DELETE_FN):
        assert after._body_md5(sql) in "".join(after._ASSERT_PREVIOUS)
    for sql in (after._ADVANCE_FN, after._DELETE_FN):
        assert after._body_md5(sql) in "".join(after._ASSERT_NEW)
