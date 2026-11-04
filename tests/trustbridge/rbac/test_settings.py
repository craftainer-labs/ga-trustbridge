"""The RBAC settings fields (ADR 0033: `RBAC_ALIAS_TEMPLATE_PERSONAS`, `RBAC_ROLE_SEPARATOR`)."""

from typing import Any

import pytest
from pydantic import ValidationError

from tests.trustbridge.helpers import host, make_settings


def test_defaults() -> None:
    """Personas aliased and `:` as the separator by default."""
    settings = make_settings([host()])
    assert settings.rbac_alias_template_personas is True
    assert settings.rbac_role_separator == ":"


def test_both_can_be_overridden() -> None:
    """The alias can be switched off and the separator changed."""
    settings = make_settings([host()], rbac_alias_template_personas=False, rbac_role_separator="|")
    assert settings.rbac_alias_template_personas is False
    assert settings.rbac_role_separator == "|"


@pytest.mark.parametrize("value", ["", "::", " ", "a", "7", ".", "-", "\t", "é"])
def test_bad_separators_are_refused(value: Any) -> None:  # noqa: ANN401
    """One printable ASCII char that host names and org logins cannot contain."""
    with pytest.raises(ValidationError, match="RBAC_ROLE_SEPARATOR"):
        make_settings([host()], rbac_role_separator=value)
