# NFR-0014. Don't assume claim shape beyond `sub` is portable

## Attribute

Constraint / documentation.

## Description

Code shall not assume any claim beyond `sub` is present on every
provider's tokens. The `resource_access.<client>.roles` role-claim
shape is explicitly Keycloak-specific and not guaranteed to be
portable to an arbitrary OIDC provider.

## Source

Developers maintaining the template. Documented in
`src/app/README.md`'s "Don't" section.

## Verification

Code review of any new claim-reading code against this documented
constraint; no automated check.
