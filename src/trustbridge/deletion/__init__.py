"""The tenant deletion engine (ADR 0034, NG6h-4a-2).

State machine and step order (`engine`, `model`), the lease and fence loop, the failure policy,
the drain step (`drain`) and the audit events (`audit`), over a record store (`store`, with the
Postgres one in `sql`). The step slots (platform, github, database) are Protocols in `steps`;
NG6h-4b, -5 and -6 fill them. Nothing is re-exported here, so importing `deletion.ports` (the one
module the reconciler may import) loads no engine code. See the package `README.md`.
"""
