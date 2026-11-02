"""The adapter core: drives adapters through write-ahead credential operations (ADR 0029, 0028).

Every secret-bearing call goes through a `credential_operations` row that is committed before
the adapter is called (with the deterministic platform-side name). Returned secret values are
parked in the value store until the core has delivered them to GitHub, then removed. Rotation
is two-phase and resumable: `rotate_begin`, deliver, set `retire_after` on the previous slot,
and `rotate_finish` once it has passed. The core alone decides OIDC eligibility (A12), enforces
the host-kind check (A7), the privileged-credential rule (A13), the credential limit and the
call timeout, and the adapter never calls GitHub.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import NoReturn
from uuid import UUID, uuid4, uuid5

from trustbridge.adapters.eligibility import (
    EnvironmentInspector,
    is_trust_eligible,
    oidc_offered,
)
from trustbridge.adapters.interface import (
    AdapterConfigurationError,
    AdapterError,
    Capabilities,
    ChangeState,
    CredentialOutcome,
    CredentialScope,
    CredentialSlot,
    CredentialState,
    GitHubOutput,
    IssuedSecret,
    OidcTrust,
    PlatformAdapter,
    Tenant,
    credential_name,
    tenant_prefix,
)
from trustbridge.adapters.loader import instantiate
from trustbridge.adapters.store import (
    ConfigResolver,
    CoreSettings,
    CredentialKind,
    CredentialStore,
    DeliveredOutput,
    Lease,
    LeaseProvider,
    Operation,
    OperationKind,
    OperationStatus,
    OutputError,
    OutputWriter,
    ScopeId,
    StoredCredential,
    TenantResolver,
    ValueStore,
    now_utc_default,
)

_log = logging.getLogger(__name__)

_CONFIG_ERROR_NS = UUID("6f1b6f0e-5c1a-4f57-9a43-0d1e7c0f5a11")
_SWEEP_KEY = "sweep"
_ADAPTER_WIDE_KEY = "*"


class RetryableOperationError(Exception):
    """The operation hit a retryable failure; it stays non-terminal and goes to the retry path."""

    def __init__(self, operation_id: UUID, message: str) -> None:
        """Record the operation and an audit-safe message."""
        super().__init__(message)
        self.operation_id = operation_id


class OperationFailedError(Exception):
    """The operation ended in `failed` or `abandoned`; the message is audit-safe."""

    def __init__(self, operation_id: UUID, message: str) -> None:
        """Record the operation and an audit-safe message."""
        super().__init__(message)
        self.operation_id = operation_id


class RotationNotApplicableError(Exception):
    """A rotation was requested for a scope that has no rotatable secret."""


@dataclass(frozen=True, slots=True)
class BoundAdapter:
    """An adapter instantiated for one tenant, with its capabilities and config."""

    adapter: PlatformAdapter
    caps: Capabilities
    privileged: bool


class AdapterRegistry:
    """Instantiates the enabled adapters per tenant, from classes loaded by the loader."""

    def __init__(
        self, classes: Mapping[str, type[PlatformAdapter]], configs: ConfigResolver
    ) -> None:
        """Keep the enabled adapter classes and the connection-data resolver."""
        self._classes = dict(classes)
        self._configs = configs

    async def open(self, tenant: Tenant, name: str) -> BoundAdapter:
        """Instantiate adapter `name` for the tenant and check the host kind (invariant A7)."""
        cls = self._classes.get(name)
        if cls is None:
            msg = f"adapter {name!r} is not enabled"
            raise AdapterConfigurationError(msg)
        config = await self._configs.config_for(tenant, name)
        adapter = instantiate(cls, config)
        caps = adapter.capabilities()
        if tenant.github_host.kind not in caps.github_hosts:
            msg = f"adapter {name!r} does not support host kind {tenant.github_host.kind!r}"
            raise AdapterConfigurationError(msg)
        return BoundAdapter(
            adapter=adapter,
            caps=caps,
            privileged=config.privileged_credential is not None,
        )


@dataclass(frozen=True, slots=True)
class CoreDeps:
    """Everything the core needs from the rest of the application."""

    registry: AdapterRegistry
    store: CredentialStore
    values: ValueStore
    leases: LeaseProvider
    writer: OutputWriter
    inspector: EnvironmentInspector
    tenants: TenantResolver
    clock: Callable[[], datetime] = now_utc_default


@dataclass(frozen=True, slots=True)
class ScopeResult:
    """What a core call achieved for one scope."""

    operation_id: UUID | None
    status: OperationStatus | None
    change: ChangeState
    manual_action: str | None = None
    wait_until: datetime | None = None


@dataclass(slots=True)
class _Ctx:
    lease: Lease
    bound: BoundAdapter
    tenant: Tenant
    scope: CredentialScope
    stored: StoredCredential


def _empty(
    tenant: Tenant, adapter: str, scope: CredentialScope, ident: ScopeId
) -> StoredCredential:
    return StoredCredential(
        tenant_id=tenant.id,
        adapter=adapter,
        scope_key=scope.key,
        scope=ident,
        kind=None,
        state=CredentialState(current=None),
    )


def _delivered_outputs(outputs: Sequence[GitHubOutput]) -> tuple[DeliveredOutput, ...]:
    return tuple(
        DeliveredOutput(
            kind=o.kind,
            name=o.name,
            repository_id=o.repository_id,
            environment=o.environment,
        )
        for o in outputs
    )


def expected_claims(scope: CredentialScope) -> dict[str, str]:
    """Return exactly the claims an `OidcTrust` for this scope must carry (invariant A6)."""
    claims = {"repository_owner_id": str(scope.repository_owner_id)}
    if scope.repository_id is not None:
        claims["repository_id"] = str(scope.repository_id)
    if scope.environment is not None:
        claims["environment"] = scope.environment
    return claims


class CredentialCore:
    """Runs credential and resource operations for adapters."""

    def __init__(self, deps: CoreDeps, settings: CoreSettings | None = None) -> None:
        """Keep the dependencies and settings."""
        self._d = deps
        self._s = settings or CoreSettings()

    # ---- public API ------------------------------------------------------
    async def ensure_scope(
        self,
        tenant: Tenant,
        adapter: str,
        scope_id: ScopeId,
        siblings: Sequence[ScopeId] = (),
    ) -> ScopeResult:
        """Converge one credential scope; resumes an open operation first."""
        async with self._locked(tenant, adapter, scope_id) as ctx:
            open_op = await self._d.store.get_open_operation(tenant.id, adapter, ctx.scope.key)
            if open_op is not None:
                return await self._advance(ctx, open_op)
            offered = await self._offered(ctx, siblings)
            return await self._reconcile(ctx, offered)

    async def rotate_scope(self, tenant: Tenant, adapter: str, scope_id: ScopeId) -> ScopeResult:
        """Start a rotation, or resume the scope's in-flight operation (invariant A9)."""
        async with self._locked(tenant, adapter, scope_id) as ctx:
            open_op = await self._d.store.get_open_operation(tenant.id, adapter, ctx.scope.key)
            if open_op is not None:
                return await self._advance(ctx, open_op)
            stored = ctx.stored
            if stored.kind != "secret" or ctx.bound.caps.rotation == "none":
                msg = "scope has no rotatable secret credential"
                raise RotationNotApplicableError(msg)
            return await self._start(ctx, OperationKind.ROTATE, offered=False)

    async def delete_scope(self, tenant: Tenant, adapter: str, scope_id: ScopeId) -> ScopeResult:
        """Delete a scope's credential and its GitHub outputs."""
        async with self._locked(tenant, adapter, scope_id) as ctx:
            open_op = await self._d.store.get_open_operation(tenant.id, adapter, ctx.scope.key)
            if open_op is not None:
                result = await self._advance(ctx, open_op)
                if result.status not in {OperationStatus.FINISHED, OperationStatus.FAILED}:
                    return result
            if ctx.stored.state.current is None:
                return ScopeResult(None, None, ChangeState.UNCHANGED)
            return await self._start(ctx, OperationKind.DELETE, offered=False)

    async def resume_operation(self, operation_id: UUID) -> ScopeResult | None:
        """Resume one non-terminal operation (the reconciler's entry point); None if it is done."""
        op = await self._d.store.get_operation(operation_id)
        if op is None or op.terminal:
            return None
        tenant = await self._d.tenants.get_tenant(op.tenant_id)
        stored = await self._d.store.get_credential(op.tenant_id, op.adapter, op.scope_key)
        if stored is None:
            return None
        async with self._locked(tenant, op.adapter, stored.scope) as ctx:
            fresh = await self._d.store.get_operation(operation_id)
            if fresh is None or fresh.terminal:
                return None
            return await self._advance(ctx, fresh)

    async def resume_all(self) -> list[ScopeResult]:
        """Resume every open operation whose lease is free; failures do not stop the others."""
        results: list[ScopeResult] = []
        for op in await self._d.store.list_open_operations():
            try:
                result = await self.resume_operation(op.id)
            except RetryableOperationError, OperationFailedError, AdapterError:
                _log.warning("operation %s did not complete this round", op.id)
                continue
            if result is not None:
                results.append(result)
        return results

    async def sweep_orphans(self, tenant: Tenant, adapter: str) -> list[str]:
        """Delete platform credentials no slot and no open operation accounts for (invariant 22).

        Lists the platform first and reads the database after, and deletes only credentials that
        are in the list and unaccounted for in that later read, so a credential created after
        the list is never touched.
        """
        bound = await self._d.registry.open(tenant, adapter)
        key = _ADAPTER_WIDE_KEY if bound.caps.credential_limit else _SWEEP_KEY
        async with self._d.leases.hold(tenant_id=tenant.id, adapter=adapter, key=key):
            listed = await self._bounded(bound.adapter.list_credentials(tenant))
            known = await self._d.store.known_names(tenant.id, adapter)
            prefix = tenant_prefix(tenant.id)
            deleted: list[str] = []
            for item in listed:
                if not item.name.startswith(prefix) or item.name in known:
                    continue
                scope, slot = _orphan_scope(tenant, item.name, dict(item.ref))
                await self._bounded(
                    bound.adapter.delete_credential(scope, CredentialState(current=slot))
                )
                deleted.append(item.name)
            return deleted

    async def sweep_parked_values(self) -> int:
        """Remove parked values whose operation is terminal or missing (invariant 23)."""
        removed = 0
        for op_id in await self._d.values.list_ids():
            op = await self._d.store.get_operation(op_id)
            if op is None or op.terminal:
                await self._d.values.delete(op_id)
                removed += 1
        return removed

    # ---- locking and context --------------------------------------------
    @asynccontextmanager
    async def _locked(self, tenant: Tenant, adapter: str, scope_id: ScopeId) -> AsyncIterator[_Ctx]:
        bound = await self._d.registry.open(tenant, adapter)
        scope = CredentialScope.build(
            tenant,
            repository_id=scope_id.repository_id,
            repository_owner_id=scope_id.repository_owner_id,
            environment=scope_id.environment,
            permission=scope_id.permission,
        )
        # An adapter with credential_limit is serialised adapter-wide (invariant 11).
        key = _ADAPTER_WIDE_KEY if bound.caps.credential_limit else scope.key
        async with self._d.leases.hold(tenant_id=tenant.id, adapter=adapter, key=key) as lease:
            stored = await self._d.store.get_credential(tenant.id, adapter, scope.key)
            ctx = _Ctx(
                lease=lease,
                bound=bound,
                tenant=tenant,
                scope=scope,
                stored=stored or _empty(tenant, adapter, scope, scope_id),
            )
            yield ctx

    async def _offered(self, ctx: _Ctx, siblings: Sequence[ScopeId]) -> bool:
        """Decide whether OIDC trust may be offered (A12, A6, A7); re-evaluated on every call."""
        sibling_scopes = [
            CredentialScope.build(
                ctx.tenant,
                repository_id=s.repository_id,
                repository_owner_id=s.repository_owner_id,
                environment=s.environment,
                permission=s.permission,
            )
            for s in siblings
        ]
        eligible = await is_trust_eligible(ctx.scope, sibling_scopes, self._d.inspector)
        return oidc_offered(ctx.bound.caps, ctx.scope, eligible=eligible)

    # ---- reconcile -------------------------------------------------------
    async def _reconcile(self, ctx: _Ctx, offered: bool) -> ScopeResult:
        await self._require_privileged(ctx)
        kind = ctx.stored.kind
        if ctx.stored.state.current is None or kind is None:
            return await self._start(ctx, OperationKind.ISSUE, offered=offered)
        if kind == "secret":
            if offered:
                return await self._start(ctx, OperationKind.SWITCH, offered=True)
            return ScopeResult(None, None, ChangeState.UNCHANGED)
        if kind == "oidc" and not offered:
            return await self._start(ctx, OperationKind.SWITCH, offered=False)
        return await self._refresh(ctx, offered=offered)

    async def _require_privileged(self, ctx: _Ctx) -> None:
        """Invariant A13: no state-changing call without the privileged credential."""
        caps = ctx.bound.caps
        if not caps.needs_privileged_credential or ctx.bound.privileged:
            return
        message = "adapter requires a privileged credential that the tenant has not supplied"
        op = Operation(
            id=uuid5(_CONFIG_ERROR_NS, f"{ctx.tenant.id}|{ctx.stored.adapter}|{ctx.scope.key}"),
            tenant_id=ctx.tenant.id,
            adapter=ctx.stored.adapter,
            scope_key=ctx.scope.key,
            kind=OperationKind.ISSUE,
            status=OperationStatus.FAILED,
            oidc_offered=False,
            base_state=None,
            base_kind=None,
            error=message,
        )
        await self._d.store.save(ctx.lease, ctx.stored, op)
        raise AdapterConfigurationError(message)

    async def _refresh(self, ctx: _Ctx, *, offered: bool) -> ScopeResult:
        """Re-run `ensure_credential` for an OIDC or no-credential scope and rewrite its outputs."""
        scope = replace(ctx.scope, oidc_eligible=offered)
        adapter = ctx.bound.adapter
        try:
            outcome = await self._bounded(adapter.ensure_credential(scope, ctx.stored.state))
            self._validate(scope, outcome, offered=offered)
        except TimeoutError as exc:
            raise AdapterError(True, "adapter call timed out") from exc
        cred = outcome.credential
        if cred is None:
            return ScopeResult(
                None,
                None,
                ChangeState.NEEDS_MANUAL_ACTION,
                manual_action=outcome.change.manual_action,
            )
        if isinstance(cred, IssuedSecret):
            msg = "adapter issued a secret during a refresh of a non-secret scope"
            raise AdapterError(False, msg)
        outputs = list(adapter.github_outputs(scope, cred))
        await self._write_outputs(ctx, outputs)
        state = _with_current_delivered(outcome.state)
        kind: CredentialKind = "oidc" if isinstance(cred, OidcTrust) else "none"
        stored = replace(ctx.stored, state=state, kind=kind, outputs=_delivered_outputs(outputs))
        await self._d.store.save(ctx.lease, stored, None)
        ctx.stored = stored
        return ScopeResult(None, None, outcome.change.state)

    # ---- starting an operation ------------------------------------------
    async def _start(self, ctx: _Ctx, kind: OperationKind, *, offered: bool) -> ScopeResult:
        await self._require_privileged(ctx)
        stored = ctx.stored
        old = stored.state.current
        caps = ctx.bound.caps
        in_place = kind is OperationKind.ROTATE and caps.rotation == "single"
        if kind is not OperationKind.DELETE:
            await self._check_limit(ctx, new_slots=0 if in_place else 1)
        state = stored.state
        if kind is not OperationKind.DELETE:
            generation = (
                old.generation
                if (in_place and old is not None)
                else (old.generation + 1 if old is not None else 1)
            )
            slot = CredentialSlot(
                generation=generation,
                name=credential_name(ctx.tenant.id, ctx.scope.key, generation),
                ref={},
                delivered=False,
            )
            keep_previous = old if (old is not None and not in_place) else None
            state = CredentialState(current=slot, previous=keep_previous)
        op = Operation(
            id=uuid4(),
            tenant_id=ctx.tenant.id,
            adapter=stored.adapter,
            scope_key=ctx.scope.key,
            kind=kind,
            status=OperationStatus.INTENT,
            oidc_offered=offered,
            base_state=stored.state if old is not None else None,
            base_kind=stored.kind,
        )
        committed = replace(stored, state=state)
        await self._persist(ctx, committed, op)
        return await self._advance(ctx, op)

    async def _check_limit(self, ctx: _Ctx, *, new_slots: int) -> None:
        limit = ctx.bound.caps.credential_limit
        if limit is None or new_slots == 0:
            return
        used = await self._d.store.count_slots(ctx.tenant.id, ctx.stored.adapter)
        if used + new_slots > limit:
            msg = f"credential limit of {limit} reached; the existing credential stays valid"
            raise AdapterError(False, msg)

    # ---- advancing ------------------------------------------------------
    async def _advance(self, ctx: _Ctx, op: Operation) -> ScopeResult:
        current = op
        while not current.terminal:
            if current.status in {OperationStatus.INTENT, OperationStatus.MANUAL}:
                current = await self._step_call(ctx, current)
                if current.status is OperationStatus.MANUAL:
                    break
            elif current.status is OperationStatus.PARKED:
                current = await self._step_deliver(ctx, current)
            elif current.status is OperationStatus.DELIVERED:
                nxt = await self._step_finish(ctx, current)
                if nxt.status is OperationStatus.DELIVERED:
                    return self._result(ctx, nxt, wait=True)
                current = nxt
            else:
                current = await self._step_cleanup(ctx, current)
        return self._result(ctx, current, wait=False)

    def _result(self, ctx: _Ctx, op: Operation, *, wait: bool) -> ScopeResult:
        wait_until = None
        if wait and ctx.stored.state.previous is not None:
            wait_until = ctx.stored.state.previous.retire_after
        if op.status is OperationStatus.MANUAL:
            change = ChangeState.NEEDS_MANUAL_ACTION
        elif op.kind is OperationKind.DELETE:
            change = ChangeState.DELETED
        else:
            change = ChangeState.CREATED if op.kind is OperationKind.ISSUE else ChangeState.UPDATED
        return ScopeResult(op.id, op.status, change, op.manual_action, wait_until)

    def _method(
        self, ctx: _Ctx, op: Operation
    ) -> Callable[[CredentialScope, CredentialState], Awaitable[CredentialOutcome]]:
        adapter = ctx.bound.adapter
        if op.kind is OperationKind.ROTATE:
            return adapter.rotate_begin
        if op.kind is OperationKind.DELETE:
            return adapter.delete_credential
        return adapter.ensure_credential

    async def _step_call(self, ctx: _Ctx, op: Operation) -> Operation:
        scope = replace(ctx.scope, oidc_eligible=op.oidc_offered)
        try:
            outcome = await self._bounded(self._method(ctx, op)(scope, ctx.stored.state))
            self._validate(scope, outcome, offered=op.oidc_offered)
        except TimeoutError:
            await self._on_error(ctx, op, AdapterError(True, "adapter call timed out"))
        except AdapterError as err:
            await self._on_error(ctx, op, err)
        if op.kind is OperationKind.DELETE:
            return await self._finish_delete(ctx, op)
        return await self._apply_outcome(ctx, op, scope, outcome)

    def _validate(
        self, scope: CredentialScope, outcome: CredentialOutcome, *, offered: bool
    ) -> None:
        """Enforce A6 on what an adapter returned: trust only when offered, with exact claims."""
        cred = outcome.credential
        if isinstance(cred, OidcTrust):
            if not offered:
                msg = "adapter returned OIDC trust for a scope that was not offered it"
                raise AdapterError(False, msg)
            if dict(cred.claims) != expected_claims(scope):
                msg = "adapter OIDC claims differ from the scope's required claims"
                raise AdapterError(False, msg)
            if cred.issuer != scope.github_host.oidc_issuer:
                msg = "adapter OIDC issuer differs from the host's issuer"
                raise AdapterError(False, msg)

    async def _apply_outcome(
        self, ctx: _Ctx, op: Operation, scope: CredentialScope, outcome: CredentialOutcome
    ) -> Operation:
        cred = outcome.credential
        state = outcome.state
        if cred is None:
            if outcome.change.state is not ChangeState.NEEDS_MANUAL_ACTION:
                await self._on_error(ctx, op, AdapterError(False, "adapter returned no credential"))
            new_op = replace(
                op,
                status=OperationStatus.MANUAL,
                manual_action=outcome.change.manual_action,
                attempts=0,
            )
            await self._persist(ctx, replace(ctx.stored, state=state), new_op)
            return new_op
        if isinstance(cred, IssuedSecret):
            await self._d.values.put(op.id, cred)
            new_op = replace(op, status=OperationStatus.PARKED, attempts=0, manual_action=None)
            await self._persist(ctx, replace(ctx.stored, state=state, kind="secret"), new_op)
            return new_op
        outputs = list(ctx.bound.adapter.github_outputs(scope, cred))
        kind: CredentialKind = "oidc" if isinstance(cred, OidcTrust) else "none"
        ctx.stored = replace(ctx.stored, kind=kind)
        return await self._deliver(ctx, op, state, outputs)

    async def _step_deliver(self, ctx: _Ctx, op: Operation) -> Operation:
        secret = await self._d.values.get(op.id)
        if secret is None:
            # Values lost (value store wiped): re-issue under the same name; the adapter
            # replaces the credential held for that name (ADR 0029, secret issuance).
            new_op = replace(op, status=OperationStatus.INTENT, attempts=0)
            await self._persist(ctx, ctx.stored, new_op)
            return new_op
        scope = replace(ctx.scope, oidc_eligible=op.oidc_offered)
        outputs = list(ctx.bound.adapter.github_outputs(scope, secret))
        new_op = await self._deliver(ctx, op, ctx.stored.state, outputs)
        await self._unpark(op.id)
        return new_op

    async def _deliver(
        self,
        ctx: _Ctx,
        op: Operation,
        state: CredentialState,
        outputs: list[GitHubOutput],
    ) -> Operation:
        await self._write_outputs(ctx, outputs, op=op)
        now = self._d.clock()
        grace = 0.0 if op.base_kind == "oidc" else self._s.retire_grace_seconds
        delivered = _with_current_delivered(state)
        previous = delivered.previous
        if previous is not None and previous.retire_after is None:
            previous = replace(previous, retire_after=now + timedelta(seconds=grace))
        delivered = CredentialState(current=delivered.current, previous=previous)
        status = OperationStatus.FINISHED if previous is None else OperationStatus.DELIVERED
        new_op = replace(op, status=status, attempts=0, manual_action=None)
        stored = replace(ctx.stored, state=delivered, outputs=_delivered_outputs(outputs))
        await self._persist(ctx, stored, new_op)
        return new_op

    async def _write_outputs(
        self, ctx: _Ctx, outputs: list[GitHubOutput], *, op: Operation | None = None
    ) -> None:
        wanted = {_output_key(o) for o in _delivered_outputs(outputs)}
        stale = [o for o in ctx.stored.outputs if _output_key(o) not in wanted]
        try:
            await self._d.writer.apply(ctx.tenant, outputs)
            if stale:
                await self._d.writer.remove(ctx.tenant, stale)
        except OutputError as err:
            if op is None:
                raise AdapterError(err.retryable, str(err)) from err
            await self._on_error(ctx, op, AdapterError(err.retryable, str(err)))

    async def _step_finish(self, ctx: _Ctx, op: Operation) -> Operation:
        state = ctx.stored.state
        previous = state.previous
        if previous is None:
            new_op = replace(op, status=OperationStatus.FINISHED)
            await self._persist(ctx, ctx.stored, new_op)
            return new_op
        if previous.retire_after is not None and self._d.clock() < previous.retire_after:
            return op
        scope = replace(ctx.scope, oidc_eligible=op.oidc_offered)
        adapter = ctx.bound.adapter
        try:
            if op.kind is OperationKind.ROTATE:
                outcome = await self._bounded(adapter.rotate_finish(scope, state))
                final = outcome.state
            else:
                revoke = CredentialState(current=previous)
                await self._bounded(adapter.delete_credential(scope, revoke))
                final = CredentialState(current=state.current)
        except TimeoutError:
            await self._on_error(ctx, op, AdapterError(True, "adapter call timed out"))
        except AdapterError as err:
            await self._on_error(ctx, op, err)
        new_op = replace(op, status=OperationStatus.FINISHED, attempts=0)
        await self._persist(ctx, replace(ctx.stored, state=final), new_op)
        return new_op

    async def _finish_delete(self, ctx: _Ctx, op: Operation) -> Operation:
        try:
            await self._d.writer.remove(ctx.tenant, ctx.stored.outputs)
        except OutputError as err:
            await self._on_error(ctx, op, AdapterError(err.retryable, str(err)))
        new_op = replace(op, status=OperationStatus.FINISHED, attempts=0)
        stored = replace(ctx.stored, state=CredentialState(current=None), kind=None, outputs=())
        await self._persist(ctx, stored, new_op)
        return new_op

    # ---- failure and cleanup --------------------------------------------
    async def _on_error(self, ctx: _Ctx, op: Operation, err: AdapterError) -> NoReturn:
        attempts = op.attempts + 1
        exhausted = (not err.retryable) or attempts >= self._s.max_attempts
        message = err.message
        if op.status is OperationStatus.DELIVERED or not exhausted:
            # Nothing to undo after delivery (the new credential is live): keep retrying.
            new_op = replace(op, attempts=attempts, error=message)
            await self._persist(ctx, ctx.stored, new_op)
            raise RetryableOperationError(op.id, message) from err
        if op.status is OperationStatus.CLEANUP:
            new_op = replace(op, status=OperationStatus.ABANDONED, attempts=attempts, error=message)
            await self._persist(ctx, ctx.stored, new_op)
            await self._unpark(op.id)
            raise OperationFailedError(op.id, message) from err
        cleanup = replace(op, status=OperationStatus.CLEANUP, attempts=0, error=message)
        await self._persist(ctx, ctx.stored, cleanup)
        await self._step_cleanup(ctx, cleanup)
        raise OperationFailedError(op.id, message) from err

    async def _step_cleanup(self, ctx: _Ctx, op: Operation) -> Operation:
        """Invariant A10: delete the new credential, restore the previous state, mark failed."""
        state = ctx.stored.state
        new_slot = state.current
        base = op.base_state
        in_place = (
            base is not None
            and base.current is not None
            and new_slot is not None
            and base.current.name == new_slot.name
        )
        if op.kind is not OperationKind.DELETE and new_slot is not None and not in_place:
            scope = replace(ctx.scope, oidc_eligible=op.oidc_offered)
            try:
                await self._bounded(
                    ctx.bound.adapter.delete_credential(scope, CredentialState(current=new_slot))
                )
            except TimeoutError:
                await self._on_error(ctx, op, AdapterError(True, "adapter call timed out"))
            except AdapterError as err:
                await self._on_error(ctx, op, err)
        restored = base if base is not None else CredentialState(current=None)
        failed = replace(op, status=OperationStatus.FAILED, attempts=0)
        stored = replace(ctx.stored, state=restored, kind=op.base_kind)
        await self._persist(ctx, stored, failed)
        await self._unpark(op.id)
        return failed

    # ---- helpers ---------------------------------------------------------
    async def _persist(self, ctx: _Ctx, stored: StoredCredential, op: Operation) -> None:
        await self._d.store.save(ctx.lease, stored, op)
        ctx.stored = stored

    async def _unpark(self, operation_id: UUID) -> None:
        try:
            await self._d.values.delete(operation_id)
        except Exception:
            # The sweep removes it later (invariant 23); never fail an operation over cleanup.
            _log.warning("could not remove parked values of operation %s", operation_id)

    async def _bounded[T](self, awaitable: Awaitable[T]) -> T:
        """Bound an adapter call by a timeout shorter than the lease TTL (invariant 11)."""
        async with asyncio.timeout(self._s.adapter_call_timeout_seconds):
            return await awaitable


def _output_key(o: DeliveredOutput) -> tuple[str, str, int | None, str | None]:
    return (o.kind, o.name, o.repository_id, o.environment)


def _with_current_delivered(state: CredentialState) -> CredentialState:
    if state.current is None:
        return state
    return CredentialState(current=replace(state.current, delivered=True), previous=state.previous)


def _orphan_scope(
    tenant: Tenant, name: str, ref: dict[str, str]
) -> tuple[CredentialScope, CredentialSlot]:
    """Build a synthetic scope and slot to delete an orphan found only by its platform name."""
    rest = name.removeprefix(tenant_prefix(tenant.id))
    key16, _, gen = rest.rpartition("-g")
    generation = int(gen) if gen.isdigit() else 0
    scope = CredentialScope(
        tenant=tenant,
        github_host=tenant.github_host,
        repository_id=None,
        repository_owner_id=0,
        environment=None,
        permission="pull",
        key=key16,
    )
    slot = CredentialSlot(generation=generation, name=name, ref=ref, delivered=True)
    return scope, slot
