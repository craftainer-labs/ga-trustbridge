"""GitHub host registration through the admin API (ADR 0030 "Registration", ADR 0033, NG6e-4).

Three operations over `ManifestFlow` (the flow itself is `trustbridge.github.manifest`):

* **start** hands back what the BFF renders: the URL to POST the manifest to and the manifest,
  with a signed one-time `state` bound to the caller. It returns no secret.
* **complete** is called by the BFF, with the user's bearer token, when GitHub redirected the
  browser back with a `code`. There is no public trustbridge callback. The flow exchanges the
  code server-side, imports the PEM straight into the host's signer and writes the webhook secret
  to the secret store.
* **manual** takes the App id, PEM and webhook secret of a hand-registered App (GHES enterprise
  Apps), with the same storage.

Rules enforced here: a host that already has an App (configured, or registered, or not
`unregistered` in the answering replica's view) is a 409; only `kind=trustbridge` is offered (the
Argo CD App would write connection data, which the admin API never does); every call writes
exactly one audit event, in the transaction that saves the registration when it gets that far and
as a `failure` event otherwise. The PEM, the webhook secret and the one-time code are never
logged, recorded or returned: request bodies are parsed here so a validation error cannot echo
them, and failures are reported with fixed texts.
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from pydantic import BaseModel, SecretStr, ValidationError
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from trustbridge.admin.guards import principal_key
from trustbridge.admin.problems import AdminProblem
from trustbridge.admin.schemas import (
    CompleteIn,
    ManualIn,
    RegistrationOut,
    RegistrationStartOut,
)
from trustbridge.audit import (
    Action,
    ActorType,
    AuditEvent,
    AuditWriter,
    Outcome,
    project,
)
from trustbridge.audit.emit import with_trace
from trustbridge.audit.store import append
from trustbridge.github.availability import HostState
from trustbridge.github.hosts import GitHubHostConfig
from trustbridge.github.manifest import (
    AppKind,
    CompleteRequest,
    ManifestDeps,
    ManifestFlow,
    ManifestFlowError,
    ManualApp,
    StartRequest,
)
from trustbridge.github.models import HostRegistrationRow, Registration
from trustbridge.github.ports import NonceStore, SecretWriter
from trustbridge.rbac import Principal
from trustbridge.rbac.deps import client_ip, safe_request_id
from trustbridge.signer.base import AppKeySigner, SignerError, SignerImportUnsupportedError

logger = logging.getLogger(__name__)
NONCE_PREFIX = "tb:registration:nonce:"


class MemoryNonceStore:
    """`NonceStore` in this process (`MODE=mock`, tests); a nonce is consumed once."""

    def __init__(self) -> None:
        """Start empty."""
        self._issued: set[str] = set()

    async def issue(self, nonce: str, ttl_seconds: int) -> None:
        """Remember `nonce` (the state's own expiry bounds its life)."""
        del ttl_seconds
        self._issued.add(nonce)

    async def consume(self, nonce: str) -> bool:
        """True exactly once per issued nonce."""
        if nonce in self._issued:
            self._issued.discard(nonce)
            return True
        return False


class ValkeyNonceStore:
    """`NonceStore` over Valkey, shared by every replica: `SET EX` to issue, `GETDEL` to consume."""

    def __init__(self, redis: Redis) -> None:
        """Use `redis` (the api's connection)."""
        self._redis = redis

    async def issue(self, nonce: str, ttl_seconds: int) -> None:
        """Store the nonce for `ttl_seconds`."""
        await self._redis.set(NONCE_PREFIX + nonce, "1", ex=ttl_seconds)

    async def consume(self, nonce: str) -> bool:
        """True exactly once: `GETDEL` returns the value for the one caller that removes it."""
        return await self._redis.getdel(NONCE_PREFIX + nonce) is not None


class _Unbound:
    """The `RegistrationSink` of the shared deps: every call binds its own audited sink."""

    async def save(self, host_name: str, registration: Registration) -> None:
        raise RuntimeError("a registration is saved through the per-call audited sink")


@dataclass(frozen=True, slots=True)
class RegistrationRuntime:
    """What the registration routes need (`AdminRuntime.registration`)."""

    hosts: Mapping[str, GitHubHostConfig]
    deps: ManifestDeps
    states: Callable[[], Mapping[str, HostState]]
    """The answering replica's state per host (`HostMonitor.statuses`, reduced to the state)."""


class _NoConnections:
    """The flow's connection store: the admin API never writes connection data (ADR 0033)."""

    async def put(self, tenant_id: UUID, adapter: str, values: Mapping[str, SecretStr]) -> None:
        raise RuntimeError("the admin API never writes connection data")


def build_deps(
    *,
    signers: Mapping[str, AppKeySigner],
    secrets: SecretWriter,
    nonces: NonceStore,
    state_key: bytes,
    public_base_url: str,
) -> ManifestDeps:
    """The flow's dependencies: trustbridge Apps only, and no connection data to write."""
    return ManifestDeps(
        signers=signers,
        secrets=secrets,
        registrations=_Unbound(),
        connections=_NoConnections(),
        nonces=nonces,
        state_key=state_key,
        public_base_url=public_base_url,
        kinds=frozenset({AppKind.TRUSTBRIDGE}),
    )


class Conflict(Exception):  # noqa: N818 -- a result, mapped to a 409
    """The host got a registration while this call was in flight."""


_CLAIM = text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))")


@asynccontextmanager
async def claim(factory: async_sessionmaker[AsyncSession], host: str) -> AsyncIterator[None]:
    """Hold the host's registration claim for the length of a registration, or raise `Conflict`.

    A transaction-scoped Postgres advisory lock, taken before anything is written and held (in an
    open transaction) until the registration has been saved, so a second registration of the same
    host, on any replica, finds the claim taken and loses before it can touch the key or the
    secret. Nothing is stored for the claim: it needs no migration, is never read as a
    registration by another process, and a crashed process releases it with its connection.
    """
    async with factory() as session, session.begin():
        won = (await session.execute(_CLAIM, {"key": f"tb:host-registration:{host}"})).scalar()
        if not won:
            raise Conflict
        yield


class _AuditedSink:
    """Saves the registration and appends the call's event in one transaction.

    `INSERT ... ON CONFLICT DO NOTHING`: a registration that appeared meanwhile is never
    replaced. `recorded` tells the caller the success event exists, so it writes no second one.
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        event_for: Callable[[Registration], AuditEvent],
    ) -> None:
        self._factory = factory
        self._event_for = event_for
        self.recorded = False

    async def save(self, host_name: str, registration: Registration) -> None:
        stmt = (
            insert(HostRegistrationRow)
            .values(
                host_name=host_name,
                app_id=registration.app_id,
                webhook_secret_ref=registration.webhook_secret_ref,
                app_slug=registration.app_slug,
            )
            .on_conflict_do_nothing(index_elements=["host_name"])
            .returning(HostRegistrationRow.host_name)
        )
        async with self._factory() as session, session.begin():
            if (await session.execute(stmt)).first() is None:
                raise Conflict
            await append(session, with_trace(self._event_for(registration)))
        self.recorded = True


@dataclass(slots=True)
class Context:
    """One call: the request, the caller, the host and where to write."""

    request: Request
    principal: Principal
    host: str
    factory: async_sessionmaker[AsyncSession]
    writer: AuditWriter | None

    def event(
        self,
        action: Action,
        outcome: Outcome,
        after: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """The call's event: the host is the resource, the identity columns are the caller's."""
        return AuditEvent(
            actor_type=ActorType.USER,
            actor_sub=self.principal.sub,
            actor_iss=self.principal.iss,
            tenant_id=None,
            action=action,
            resource_type="host",
            resource_id=self.host,
            outcome=outcome,
            after=after,
            source_ip=client_ip(self.request),
            request_id=safe_request_id(self.request),
        )


def parse_body[M: BaseModel](raw: bytes, model: type[M]) -> M:
    """Parse a secret-bearing body without ever echoing it.

    FastAPI's own validation puts the offending input (here the PEM or the webhook secret) into
    the 422 detail, so these routes validate here and report only the names of the bad fields.
    """
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "body" for e in exc.errors()})
        raise AdminProblem(422, "validation", "Invalid fields: " + ", ".join(fields)) from None


def _config(rt: RegistrationRuntime, name: str) -> GitHubHostConfig:
    host = rt.hosts.get(name)
    if host is None:
        raise AdminProblem(404, "not-found", "Host not found")
    return host


async def _require_unregistered(
    rt: RegistrationRuntime, host: GitHubHostConfig, factory: async_sessionmaker[AsyncSession]
) -> None:
    """409 unless the host has no App: not configured with one, not registered, `unregistered`."""
    state = rt.states().get(host.name)
    if host.registered or (state is not None and state is not HostState.UNREGISTERED):
        raise AdminProblem(409, "conflict", "The host already has a registered App")
    async with factory() as session:
        stored = await session.scalar(
            select(HostRegistrationRow.host_name).where(HostRegistrationRow.host_name == host.name)
        )
    if stored is not None:
        raise AdminProblem(409, "conflict", "The host already has a registered App")


async def _run[T](
    ctx: Context,
    rt: RegistrationRuntime,
    action: Action,
    call: Callable[[ManifestFlow, GitHubHostConfig], Awaitable[T]],
    *,
    writes: bool = True,
) -> T:
    """Run one registration call and leave exactly one audit event.

    The success event is written by the audited sink in the transaction that saves the
    registration (`start` has nothing to save and writes its own). Any other outcome, a refusal
    included, leaves a `failure` event in its own transaction, best effort.

    A call that `writes` (the key, the secret, the row) first takes the host's claim and only then
    checks that the host is unregistered, so two registrations of one host cannot both get to
    write: the loser is a 409 before it touches anything.
    """
    sink: _AuditedSink | None = None
    try:
        host = _config(rt, ctx.host)
        async with AsyncExitStack() as stack:
            if writes:
                await stack.enter_async_context(claim(ctx.factory, ctx.host))
            await _require_unregistered(rt, host, ctx.factory)
            sink = _AuditedSink(
                ctx.factory,
                lambda reg: ctx.event(action, Outcome.SUCCESS, _registration_state(reg)),
            )
            return await call(ManifestFlow(replace(rt.deps, registrations=sink)), host)
    except AdminProblem:
        await _failure(ctx, action, sink)
        raise
    except Conflict:
        await _failure(ctx, action, sink)
        raise AdminProblem(409, "conflict", "The host already has a registered App") from None
    except SignerImportUnsupportedError:
        await _failure(ctx, action, sink)
        raise AdminProblem(
            409, "conflict", "The host's signer cannot import a key; import it out of band"
        ) from None
    except SignerError:
        await _failure(ctx, action, sink)
        raise AdminProblem(422, "validation", "The private key could not be read") from None
    except ManifestFlowError as exc:
        logger.warning("registration refused for host %s: %s", ctx.host, exc)
        await _failure(ctx, action, sink)
        raise AdminProblem(
            400, "bad-request", "The registration state or code is invalid, expired or used"
        ) from None
    except Exception:
        await _failure(ctx, action, sink)
        raise


def _registration_state(reg: Registration) -> dict[str, Any]:
    return project(
        "host_registration",
        {
            "kind": AppKind.TRUSTBRIDGE.value,
            "app_id": reg.app_id,
            "app_slug": reg.app_slug,
            "webhook_secret_ref": reg.webhook_secret_ref,
        },
    )


async def _failure(ctx: Context, action: Action, sink: _AuditedSink | None = None) -> None:
    """Best effort: a `failure` event in its own transaction; never hides the original error.

    Nothing is written once the sink has committed the success event: one event per call.
    """
    if ctx.writer is None or (sink is not None and sink.recorded):
        return
    try:
        await ctx.writer.record(ctx.event(action, Outcome.FAILURE))
    except Exception:
        logger.exception("could not record the failed %s event", action.value)


def _out(result: Any) -> RegistrationOut:  # noqa: ANN401 -- a RegistrationResult
    return RegistrationOut(
        kind=result.kind.value,
        host=result.host,
        app_id=result.app_id,
        slug=result.slug,
        html_url=result.html_url,
        webhook_secret_ref=result.webhook_secret_ref,
    )


async def start(ctx: Context, rt: RegistrationRuntime) -> RegistrationStartOut:
    """Issue the manifest, the POST URL and a signed one-time `state` bound to the caller."""
    principal = ctx.principal

    async def call(flow: ManifestFlow, config: GitHubHostConfig) -> RegistrationStartOut:
        started = await flow.start(
            config, StartRequest(AppKind.TRUSTBRIDGE, principal_key(principal.iss, principal.sub))
        )
        await _record_success(ctx, Action.HOST_REGISTRATION_START, {"kind": "trustbridge"})
        return RegistrationStartOut(
            post_url=started.post_url, manifest_json=started.manifest_json, state=started.state
        )

    return await _run(ctx, rt, Action.HOST_REGISTRATION_START, call, writes=False)


async def _record_success(ctx: Context, action: Action, after: dict[str, Any]) -> None:
    """The success event of a call that saves nothing (`start`), in its own transaction."""
    if ctx.writer is None:
        raise AdminProblem(503, "unavailable", "No database is configured")
    await ctx.writer.record(ctx.event(action, Outcome.SUCCESS, project("host_registration", after)))


async def complete(ctx: Context, rt: RegistrationRuntime, body: CompleteIn) -> RegistrationOut:
    """Exchange the code GitHub returned and store the App the manifest created."""
    session_id = principal_key(ctx.principal.iss, ctx.principal.sub)

    async def call(flow: ManifestFlow, config: GitHubHostConfig) -> RegistrationOut:
        done = await flow.complete(
            config, CompleteRequest(body.code.get_secret_value(), body.state, session_id)
        )
        return _out(done)

    return await _run(ctx, rt, Action.HOST_REGISTRATION_COMPLETE, call)


async def manual(ctx: Context, rt: RegistrationRuntime, body: ManualIn) -> RegistrationOut:
    """Store a hand-registered App: the App id, the PEM (into the signer) and the webhook secret."""

    async def call(flow: ManifestFlow, config: GitHubHostConfig) -> RegistrationOut:
        done = await flow.register_manually(
            config, ManualApp(body.app_id, body.webhook_secret, body.slug), body.pem
        )
        return _out(done)

    return await _run(ctx, rt, Action.HOST_REGISTRATION_MANUAL, call)
