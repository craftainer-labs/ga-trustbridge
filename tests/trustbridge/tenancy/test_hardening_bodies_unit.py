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


def test_the_tombstone_migration_restores_the_bodies_it_replaced() -> None:
    """NG6h-3c-1: the copies are byte for byte what 5d7a2c8e1b94 and 3b8d6f2a4c17 install."""
    finish = _load("5d7a2c8e1b94")
    upsert = _load("3b8d6f2a4c17")
    after = _load("4e9a1c7b5d23")
    assert _body(after._PREVIOUS_FINISH_FN) == _body(finish._FINISH_FN)
    assert _body(after._PREVIOUS_UPSERT_FN) == _body(upsert._UPSERT_FN)
    # the restored upsert keeps pg_temp (6f3b9d2e4a85 pinned it on the live function)
    assert "SET search_path = pg_catalog, public, pg_temp\n" in after._PINNED_PREVIOUS_UPSERT_FN


def test_the_tombstone_bodies_differ_and_the_guards_hash_the_right_ones() -> None:
    """The finish function writes the tombstone, the upsert reads it, and both hashes differ."""
    after = _load("4e9a1c7b5d23")
    assert "INSERT INTO public.tenant_tombstones" in after._FINISH_FN
    assert "tenant_tombstones" not in after._PREVIOUS_FINISH_FN
    assert "FROM public.tenant_tombstones s" in after._UPSERT_FN
    assert "d.state = 'done'" not in after._UPSERT_FN  # the `done` rows no longer decide
    assert "d.state = 'database'" in after._UPSERT_FN  # the in-flight rule stays
    for old, new in (
        (after._PREVIOUS_FINISH_FN, after._FINISH_FN),
        (after._PREVIOUS_UPSERT_FN, after._UPSERT_FN),
    ):
        assert _body(old) != _body(new)
        assert after._body_md5(old) in "".join(after._ASSERT_PREVIOUS)
        assert after._body_md5(new) in "".join(after._ASSERT_NEW)


def test_the_environments_migration_restores_the_delete_body_it_replaced() -> None:
    """NG6h-6a: the copy is the body 6f3b9d2e4a85 installs; the new one lists the new table."""
    hardening = _load("6f3b9d2e4a85")
    tracking = _load("9a4d6c2e8b17")
    assert _body(tracking._PREVIOUS_DELETE_FN) == _body(hardening._DELETE_FN)
    assert _body(tracking._DELETE_FN) != _body(tracking._PREVIOUS_DELETE_FN)
    assert _body(tracking._DELETE_FN).count("'github_environments'") == 2
    assert "pg_temp" in tracking._DELETE_FN.split("AS $$")[0]
