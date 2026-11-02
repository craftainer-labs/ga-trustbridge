"""The local secret store: `file:` / `env:` reads, `store:` reads and writes, fail closed."""

import stat
from pathlib import Path

import pytest
from pydantic import SecretStr

from trustbridge.worker.secrets import LocalSecrets, MemorySecrets, SecretUnavailable


async def test_env_and_file_references_are_read_with_the_trailing_newline_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env and file references are read with the trailing newline stripped."""
    secret_file = tmp_path / "s"
    secret_file.write_text("from-file\n")
    monkeypatch.setenv("TB_TEST_SECRET", "from-env")
    secrets = LocalSecrets()
    assert (await secrets.read(f"file:{secret_file}")).get_secret_value() == "from-file"
    assert (await secrets.read("env:TB_TEST_SECRET")).get_secret_value() == "from-env"


@pytest.mark.parametrize(
    "ref", ["env:TB_TEST_MISSING", "file:/nonexistent/x", "vault:x", "store:x"]
)
async def test_unreadable_or_unknown_references_are_unavailable(ref: str) -> None:
    """Unreadable or unknown references are unavailable."""
    with pytest.raises(SecretUnavailable):
        await LocalSecrets().read(ref)


async def test_an_empty_secret_is_unavailable(tmp_path: Path) -> None:
    """An empty secret is unavailable."""
    empty = tmp_path / "e"
    empty.write_text("\n")
    with pytest.raises(SecretUnavailable, match="empty"):
        await LocalSecrets().read(f"file:{empty}")


async def test_store_references_round_trip_with_mode_0600_delete_and_list(tmp_path: Path) -> None:
    """Store references round trip with mode 0600 delete and list."""
    secrets = LocalSecrets(tmp_path)
    await secrets.write("store:parked/a", SecretStr("one"))
    await secrets.write("store:parked/b", SecretStr("two"))
    await secrets.write("store:parked/a", SecretStr("one-again"))  # replaced
    await secrets.write("store:other/c", SecretStr("three"))
    assert (await secrets.read("store:parked/a")).get_secret_value() == "one-again"
    assert stat.S_IMODE((tmp_path / "parked" / "a").stat().st_mode) == 0o600
    assert await secrets.list_refs("parked/") == ["store:parked/a", "store:parked/b"]
    assert await secrets.list_refs("missing") == []
    assert len(await secrets.list_refs("")) == 3
    await secrets.delete("store:parked/a")
    await secrets.delete("store:parked/a")  # absent is fine
    with pytest.raises(SecretUnavailable):
        await secrets.read("store:parked/a")


@pytest.mark.parametrize(
    "ref", ["store:../escape", "store:/abs", "store:a b", "file:/etc/x", "store:"]
)
async def test_only_well_formed_store_names_are_writable(tmp_path: Path, ref: str) -> None:
    """Only well formed store names are writable."""
    with pytest.raises(SecretUnavailable, match="not a writable"):
        await LocalSecrets(tmp_path).write(ref, SecretStr("x"))


async def test_without_a_store_directory_every_store_operation_fails_closed() -> None:
    """Without a store directory every store operation fails closed."""
    secrets = LocalSecrets(None)
    with pytest.raises(SecretUnavailable, match="no writable"):
        await secrets.write("store:a", SecretStr("x"))
    with pytest.raises(SecretUnavailable, match="no writable"):
        await secrets.delete("store:a")
    with pytest.raises(SecretUnavailable, match="no writable"):
        await secrets.list_refs("a")


async def test_os_errors_become_unavailable(tmp_path: Path) -> None:
    """Os errors become unavailable."""
    secrets = LocalSecrets(tmp_path)
    (tmp_path / "blocked").write_text("a file where a directory is needed")
    with pytest.raises(SecretUnavailable, match="unwritable"):
        await secrets.write("store:blocked/x", SecretStr("x"))
    (tmp_path / "dir").mkdir()
    with pytest.raises(SecretUnavailable, match="undeletable"):
        await secrets.delete("store:dir")  # unlink on a directory fails


async def test_the_memory_store_serves_store_refs_and_falls_back_for_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memory store serves store refs and falls back for env."""
    monkeypatch.setenv("TB_TEST_SECRET", "from-env")
    secrets = MemorySecrets({"store:a": "1"})
    await secrets.write("store:b/c", SecretStr("2"))
    assert (await secrets.read("store:a")).get_secret_value() == "1"
    assert (await secrets.read("env:TB_TEST_SECRET")).get_secret_value() == "from-env"
    assert await secrets.list_refs("b/") == ["store:b/c"]
    await secrets.delete("store:b/c")
    await secrets.delete("store:b/c")
    with pytest.raises(SecretUnavailable):
        await secrets.read("store:b/c")
    secrets.down = True
    with pytest.raises(SecretUnavailable):
        await secrets.read("store:a")
    assert secrets.reads == 4
