# NFR-0003. Keep the database model representing only the current API shape

## Attribute

Maintainability / architecture.

## Description

The persisted (SQLAlchemy) model shall never encode a deprecated API
version's shape; version-specific representation is a views/CRUD
concern only, layered on top of one current-shape model.

## Source

Developers maintaining the template. See
[0002-api-and-model-versioning](../adrs/0002-api-and-model-versioning.md).

## Verification

Enforced by code review and the layering rule in
[NFR-0018](NFR-0018-strict-module-layering.md); no automated check
beyond that today.
