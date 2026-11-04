"""The opaque cursor (decision A3): a position, not a capability."""

import base64

import pytest

from trustbridge.admin.cursor import decode_cursor, encode_cursor, paginate
from trustbridge.admin.problems import AdminProblem


def test_a_cursor_round_trips_and_is_url_safe() -> None:
    """Strings and integers; no padding, no characters needing escapes."""
    for key in ("abc", 42, "ü/+="):
        cursor = encode_cursor(key, "id-1")
        assert "=" not in cursor
        assert decode_cursor(cursor) == (key, "id-1")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "x" * 513,
        "!!!!",
        base64.urlsafe_b64encode(b"not json").decode(),
        base64.urlsafe_b64encode(b"[1]").decode(),
        base64.urlsafe_b64encode(b'["a",1]').decode(),
        base64.urlsafe_b64encode(b'[true,"a"]').decode(),
        base64.urlsafe_b64encode(b'{"a":"b"}').decode(),
        base64.urlsafe_b64encode(b'[1.5,"a"]').decode(),
    ],
)
def test_a_malformed_cursor_is_a_400(bad: str) -> None:
    """Every failure is the same typed problem."""
    with pytest.raises(AdminProblem) as caught:
        decode_cursor(bad)
    assert (caught.value.status_code, caught.value.kind) == (400, "invalid-cursor")


def test_paginate_resumes_after_the_key_not_an_offset() -> None:
    """A row inserted before the cursor, or removed, changes neither the position nor the rest."""
    rows = ["a", "b", "c", "d", "e"]
    page, cursor = paginate(rows, key=str, limit=2, cursor=None)
    assert (page, cursor is not None) == (["a", "b"], True)
    rows.insert(0, "0")
    rows.remove("a")
    page, cursor = paginate(rows, key=str, limit=2, cursor=cursor)
    assert page == ["c", "d"]
    last, none = paginate(rows, key=str, limit=2, cursor=cursor)
    assert (last, none) == (["e"], None)


def test_paginate_rejects_an_integer_key_for_a_string_listing() -> None:
    """A cursor from the audit list is not valid for hosts."""
    with pytest.raises(AdminProblem):
        paginate(["a"], key=str, limit=1, cursor=encode_cursor(5, "x"))
