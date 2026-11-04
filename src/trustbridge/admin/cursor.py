"""The opaque pagination cursor (ADR 0033 decision 6, NG6d decision A3).

An unsigned base64url encoding of `[sort key, id]` as JSON. It carries only a position: a forged
cursor can choose where to resume inside rows the caller may already read, because every
query is still scoped by the caller's grants. A malformed one is a 400.
"""

import base64
import binascii
import json
from collections.abc import Callable, Sequence

from trustbridge.admin.problems import AdminProblem

MAX_CURSOR_LENGTH = 512


def invalid_cursor() -> AdminProblem:
    """The 400 for a cursor that does not decode."""
    return AdminProblem(400, "invalid-cursor", "The cursor is not valid")


def encode_cursor(key: str | int, ident: str) -> str:
    """The cursor for the position after the row with this sort key and id."""
    raw = json.dumps([key, ident], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_cursor(cursor: str) -> tuple[str | int, str]:
    """The `(sort key, id)` of a cursor; raises the 400 `AdminProblem` when it is malformed."""
    if not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise invalid_cursor()
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except binascii.Error, ValueError:
        raise invalid_cursor() from None
    if (
        not isinstance(value, list)
        or len(value) != 2  # noqa: PLR2004 -- the pair
        or not isinstance(value[1], str)
        or isinstance(value[0], bool)
        or not isinstance(value[0], str | int)
    ):
        raise invalid_cursor()
    return value[0], value[1]


def paginate[T](
    rows: Sequence[T], *, key: Callable[[T], str], limit: int, cursor: str | None
) -> tuple[list[T], str | None]:
    """One page of `rows` (sorted ascending by `key`, unique) after `cursor`, and the next cursor.

    The cursor names the last row sent, so rows inserted or removed between pages are neither
    repeated nor skipped (the position is the key, not an offset).
    """
    remaining = list(rows)
    if cursor is not None:
        after, _ident = decode_cursor(cursor)
        if not isinstance(after, str):
            raise invalid_cursor()
        remaining = [row for row in remaining if key(row) > after]
    page = remaining[:limit]
    more = len(remaining) > limit
    return page, encode_cursor(key(page[-1]), key(page[-1])) if more else None
