"""The secret store behind `SecretReader`, `SecretWriter` and the parked-value store.

References are `file:/path` (a mounted secret; trailing newline stripped) and `env:NAME`
(read-only), and `store:<name>` (read, write, delete and list; files under `SECRET_STORE_DIR`,
mode 0600, names limited to `[A-Za-z0-9._/-]` without `..`). An external store (Vault KV, a
cloud secret manager) implements the same methods. `MemorySecrets` is the `MODE=mock`
and unit-test twin. Values are `SecretStr` and never logged; failures carry no reference.
"""

import asyncio
import os
import re
from pathlib import Path

from pydantic import SecretStr

_STORE_NAME = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_STORE_PREFIX = "store:"


class SecretUnavailable(Exception):  # noqa: N818 -- a result, named for what it says
    """The secret could not be read or written (store down, ref unknown, empty value)."""


def _store_name(ref: str) -> str:
    """The validated `store:` name of `ref`; anything else is not writable."""
    name = ref.removeprefix(_STORE_PREFIX)
    bad = (
        not ref.startswith(_STORE_PREFIX)
        or not _STORE_NAME.match(name)
        or ".." in name
        or name.startswith("/")
    )
    if bad:
        raise SecretUnavailable("not a writable secret reference")
    return name


class LocalSecrets:
    """`file:` and `env:` references to read; `store:` references under a directory to write."""

    def __init__(self, store_dir: Path | None = None) -> None:
        """`store_dir` None makes every `store:` operation fail closed."""
        self._dir = store_dir

    def _path(self, ref: str) -> Path:
        if self._dir is None:
            raise SecretUnavailable("no writable secret store configured")
        return self._dir / _store_name(ref)

    async def read(self, ref: str) -> SecretStr:
        """Read the referenced value; raise `SecretUnavailable` if it cannot be read."""
        scheme, _, target = ref.partition(":")
        if scheme == "env":
            value = os.environ.get(target, "")
        elif scheme in {"file", "store"}:
            path = Path(target) if scheme == "file" else self._path(ref)
            try:
                value = await asyncio.to_thread(path.read_text, "utf-8")
            except OSError as exc:
                raise SecretUnavailable("secret file unreadable") from exc
        else:
            raise SecretUnavailable("unknown secret reference scheme")
        value = value.rstrip("\r\n")
        if not value:
            raise SecretUnavailable("secret is empty")
        return SecretStr(value)

    async def write(self, ref: str, value: SecretStr) -> None:
        """Write (or replace) a `store:` secret with mode 0600."""
        path = self._path(ref)
        try:
            await asyncio.to_thread(self._write, path, value.get_secret_value())
        except OSError as exc:
            raise SecretUnavailable("secret file unwritable") from exc

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        tmp.replace(path)

    async def delete(self, ref: str) -> None:
        """Remove a `store:` secret; absent is fine."""
        path = self._path(ref)
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError as exc:
            raise SecretUnavailable("secret file undeletable") from exc

    async def list_refs(self, prefix: str) -> list[str]:
        """The `store:` references whose name starts with `prefix` (a directory-style prefix)."""
        if self._dir is None:
            raise SecretUnavailable("no writable secret store configured")
        root = self._dir
        base = _store_name(_STORE_PREFIX + prefix.rstrip("/")) if prefix else ""
        target = root / base

        def _scan() -> list[str]:
            if not target.is_dir():
                return []
            return sorted(
                f"{_STORE_PREFIX}{p.relative_to(root).as_posix()}"
                for p in target.rglob("*")
                if p.is_file() and not p.name.endswith(".tmp")
            )

        return await asyncio.to_thread(_scan)


class MemorySecrets:
    """Dict-backed `store:` secrets (mock mode and unit tests); `down` simulates an outage.

    `file:` and `env:` references are still read from the real file system and environment.
    """

    def __init__(self, values: dict[str, str] | None = None) -> None:
        """Start with `{ref: value}`."""
        self.values = dict(values or {})
        self.down = False
        self.reads = 0
        self._fallback = LocalSecrets()

    async def read(self, ref: str) -> SecretStr:
        """Return the value or fail like an outage."""
        self.reads += 1
        if self.down:
            raise SecretUnavailable("unavailable")
        if not ref.startswith(_STORE_PREFIX):
            return await self._fallback.read(ref)
        if ref not in self.values:
            raise SecretUnavailable("unavailable")
        return SecretStr(self.values[ref])

    async def write(self, ref: str, value: SecretStr) -> None:
        """Store the value."""
        self.values[_STORE_PREFIX + _store_name(ref)] = value.get_secret_value()

    async def delete(self, ref: str) -> None:
        """Remove the value; absent is fine."""
        self.values.pop(ref, None)

    async def list_refs(self, prefix: str) -> list[str]:
        """The references starting with `store:<prefix>`."""
        return sorted(r for r in self.values if r.startswith(_STORE_PREFIX + prefix))
