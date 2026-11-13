"""The deletion engine over fakes: order, lease and fence, budget, failure policy, audit."""

import pytest

from tests.trustbridge.deletion.fakes import Clock, FakeStore, ScriptedStep, record
from trustbridge.audit import Action, Outcome
from trustbridge.deletion.engine import DeletionEngine, EngineConfig, EngineSteps
from trustbridge.deletion.model import (
    LeaseLost,
    NonRetryableError,
    StepContext,
    StepOutcome,
)
from trustbridge.deletion.ports import DeletionDriver

CANARY = "canary-secret-7f3a9c1e"


def _config(**overrides: object) -> EngineConfig:
    values: dict[str, object] = {
        "holder": "reconciler-1",
        "lease_ttl_seconds": 60,
        "backoff_base_seconds": 5,
        "backoff_cap_seconds": 60,
    }
    values.update(overrides)
    return EngineConfig(**values)  # type: ignore[arg-type]


def _engine(
    store: FakeStore, *, clock: Clock | None = None, **steps: ScriptedStep
) -> tuple[DeletionEngine, dict[str, ScriptedStep]]:
    slots = {n: steps.get(n, ScriptedStep()) for n in ("drain", "platform", "github", "database")}
    engine = DeletionEngine(
        store,
        EngineSteps(**slots),
        _config(),
        clock=clock or Clock(),
    )
    return engine, slots


async def test_no_runnable_deletion_returns_false() -> None:
    """Nothing claimed: nothing runs."""
    store = FakeStore(None)
    engine, _ = _engine(store)
    assert await engine.run_once() is False
    assert store.names() == ["claim"]


async def test_a_deletion_walks_every_step_in_order_and_finishes_with_events() -> None:
    """requested > draining > platform > github > database > done, one step event per step."""
    store = FakeStore(record())
    engine, slots = _engine(
        store,
        platform=ScriptedStep(StepOutcome(counts={"adapter_resources": 2})),
        github=ScriptedStep(
            StepOutcome(skipped=[{"category": "installation_gone", "reason": "gone", "count": 3}])
        ),
        database=ScriptedStep(StepOutcome(counts={"tenant_repositories": 4})),
    )
    assert await engine.run_once() is True
    advances = [c[1][0] for c in store.calls if c[0] == "advance"]
    assert advances == ["draining", "platform", "github", "database"]
    assert store.final == "done"
    actions = [e.action for e in store.events]
    assert actions.count(Action.TENANT_DELETION_STEP) == 4
    assert actions.count(Action.TENANT_DELETION_SKIPPED) == 1
    assert actions[-1] is Action.TENANT_DELETION_COMPLETED
    completed = store.events[-1]
    assert completed.after is not None
    assert completed.after["counts"] == {"adapter_resources": 2, "tenant_repositories": 4}
    assert completed.tenant_id == record().tenant_id
    assert all(len(s.contexts) == 1 for s in slots.values())


async def test_the_step_events_carry_ids_states_and_counts_only() -> None:
    """ADR 0034 inv. 14: no organisation login, no row content."""
    store = FakeStore(record(state="platform"))
    engine, _ = _engine(
        store,
        platform=ScriptedStep(StepOutcome(counts={"adapter_resources": 1})),
    )
    await engine.run_once()
    step = store.events[0]
    assert step.action is Action.TENANT_DELETION_STEP
    assert step.outcome is Outcome.SUCCESS
    assert step.after == {
        "counts": {"adapter_resources": 1},
        "deletion_id": str(step.resource_id),
        "from_state": "platform",
        "step": "platform",
        "to_state": "github",
    }
    assert step.before == {"deletion_id": str(step.resource_id), "from_state": "platform"}


async def test_a_partial_pass_saves_the_cursor_and_runs_the_step_again() -> None:
    """`done` false: save the cursor and counts in the same state, then continue."""
    store = FakeStore(record(state="github"))
    github = ScriptedStep(
        StepOutcome(counts={"secrets": 5}, cursor={"after": 9}, done=False),
        StepOutcome(counts={"secrets": 2}),
    )
    engine, _ = _engine(store, github=github)
    await engine.run_once()
    saves = [c[1] for c in store.calls if c[0] == "advance"]
    assert saves[0] == ("github", {"after": 9}, {"secrets": 5}, [])
    assert saves[1][0] == "database"
    assert saves[1][2] == {"secrets": 7}
    assert github.contexts[1].record.step_cursor == {"after": 9}
    assert github.contexts[1].record.counts == {"secrets": 5}


async def test_a_pass_without_a_cursor_keeps_the_saved_one() -> None:
    """A same-state save with no cursor leaves the cursor, as the function does."""
    store = FakeStore(record(state="github", step_cursor={"after": 1}))
    github = ScriptedStep(StepOutcome(done=False), StepOutcome())
    engine, _ = _engine(store, github=github)
    await engine.run_once()
    assert github.contexts[1].record.step_cursor == {"after": 1}


async def test_the_lease_is_renewed_before_each_step_and_through_the_context() -> None:
    """Renewal happens inside the run, also from a step's own `ctx.renew()`."""
    store = FakeStore(record(state="platform"))
    platform = ScriptedStep()
    engine, _ = _engine(store, platform=platform)
    await engine.run_once()
    renewed = store.names().count("renew")
    assert renewed >= 3  # before platform, github and database
    await platform.contexts[0].renew()
    assert store.names().count("renew") == renewed + 1
    assert ("renew", 60) in store.calls


@pytest.mark.parametrize("where", ["renew", "advance", "finish"])
async def test_lease_lost_anywhere_stops_without_a_retry_or_a_failure(where: str) -> None:
    """TB003 and TB004 both mean stop now: nothing is written afterwards."""
    store = FakeStore(record(state="database" if where == "finish" else "requested"))
    store.lose_on = {where}
    engine, _ = _engine(store)
    assert await engine.run_once() is True
    assert "retry" not in store.names()
    assert "fail" not in store.names()
    assert "release" not in store.names()
    assert store.final is None


async def test_the_budget_ends_the_run_before_the_lease_could_lapse() -> None:
    """After half the TTL the run releases the lease; the state is kept for the next claim."""
    clock = Clock()
    store = FakeStore(record(state="platform"))
    platform = ScriptedStep(StepOutcome(cursor={"n": 1}, done=False))
    platform.on_run = lambda: setattr(clock, "now", 31.0)  # past 60 * 0.5
    engine, _ = _engine(store, clock=clock, platform=platform)
    assert await engine.run_once() is True
    assert store.names()[-2:] == ["advance", "release"]
    assert store.final is None
    assert len(platform.contexts) == 1


async def test_a_step_calling_renew_past_the_budget_stops_the_run() -> None:
    """`ctx.renew` raises the budget stop for a long step."""
    clock = Clock()
    store = FakeStore(record(state="platform"))

    class Long(ScriptedStep):
        async def run(self, ctx: StepContext) -> StepOutcome:
            clock.now = 31.0
            await ctx.renew()
            raise AssertionError  # pragma: no cover

    engine, _ = _engine(store, clock=clock, platform=Long())
    await engine.run_once()
    assert store.names()[-1] == "release"


async def test_a_lost_lease_during_the_budget_release_is_logged_not_raised() -> None:
    """The release itself can find the lease gone."""
    clock = Clock()
    store = FakeStore(record())
    store.lose_on = {"release"}
    drain = ScriptedStep(StepOutcome(done=False))
    drain.on_run = lambda: setattr(clock, "now", 99.0)
    engine, _ = _engine(store, clock=clock, drain=drain)
    assert await engine.run_once() is True
    assert "release" in store.names()


async def test_a_transient_error_retries_with_capped_exponential_backoff_and_counts() -> None:
    """base * 2**attempts, capped; the lease is freed by the retry; no failed state."""
    for attempts, expected in [(0, 5), (1, 10), (3, 40), (4, 60), (40, 60)]:
        store = FakeStore(record(state="platform", attempts=attempts))
        engine, _ = _engine(store, platform=ScriptedStep(RuntimeError(f"down token={CANARY}")))
        await engine.run_once()
        assert store.delay == expected
        assert store.final is None
        assert store.last_error is not None
        assert store.last_error.startswith("RuntimeError: down")
        assert CANARY not in store.last_error


async def test_attempts_increment_the_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    """The attempts counter is incremented once per transient failure, tagged by step."""
    from trustbridge.deletion import metrics  # noqa: PLC0415

    added: list[tuple[int, dict[str, str]]] = []
    monkeypatch.setattr(metrics.attempts, "add", lambda n, attrs: added.append((n, attrs)))
    store = FakeStore(record(state="github"))
    engine, _ = _engine(store, github=ScriptedStep(ValueError("x")))
    await engine.run_once()
    assert added == [(1, {"step": "github"})]


async def test_a_store_error_after_a_step_is_transient_too() -> None:
    """A failing advance retries the deletion; it never writes `failed`."""
    store = FakeStore(record())
    store.boom_on = {"advance": OSError("connection reset")}
    engine, _ = _engine(store)
    await engine.run_once()
    assert store.names()[-1] == "retry"
    assert store.final is None


async def test_a_lost_lease_while_recording_a_retry_is_not_an_error() -> None:
    """The retry call itself can find the lease gone."""
    store = FakeStore(record(state="github"))
    store.lose_on = {"retry"}
    engine, _ = _engine(store, github=ScriptedStep(RuntimeError("x")))
    assert await engine.run_once() is True


async def test_only_a_non_retryable_error_writes_failed_with_one_audit_event() -> None:
    """`failed` carries the scrubbed reason; the audit event only the fixed code."""
    store = FakeStore(record(state="platform", counts={"a": 1}))
    err = NonRetryableError("adapter_missing", f"no adapter, token={CANARY}")
    engine, _ = _engine(store, platform=ScriptedStep(err))
    await engine.run_once()
    assert store.final == "failed"
    assert store.last_error is not None
    assert store.last_error.startswith("adapter_missing: no adapter")
    assert CANARY not in store.last_error
    (event,) = store.events
    assert event.action is Action.TENANT_DELETION_FAILED
    assert event.outcome is Outcome.FAILURE
    assert event.after is not None
    assert event.after["error_code"] == "adapter_missing"
    assert event.after["to_state"] == "failed"
    assert CANARY not in repr(event)


async def test_a_non_retryable_error_without_a_message_records_the_code() -> None:
    """The reason is the bare code."""
    store = FakeStore(record(state="github"))
    engine, _ = _engine(store, github=ScriptedStep(NonRetryableError("bad")))
    await engine.run_once()
    assert store.last_error == "bad"


async def test_a_lost_lease_while_failing_is_not_an_error() -> None:
    """`fail` can find the lease gone."""
    store = FakeStore(record(state="github"))
    store.lose_on = {"fail"}
    engine, _ = _engine(store, github=ScriptedStep(NonRetryableError("bad")))
    assert await engine.run_once() is True
    assert store.final is None


async def test_the_retry_after_a_step_change_uses_the_new_states_attempts() -> None:
    """A run that advanced then failed backs off from attempt 0, not the claimed record's."""
    store = FakeStore(record(state="draining", attempts=3))
    engine, _ = _engine(store, platform=ScriptedStep(RuntimeError("x")))
    await engine.run_once()
    assert store.delay == 5


@pytest.mark.parametrize(
    "bad",
    [
        {"lease_ttl_seconds": 0},
        {"lease_ttl_seconds": 3601},
        {"backoff_base_seconds": 61},
        {"backoff_cap_seconds": 86401},
        {"backoff_base_seconds": -1},
        {"budget_fraction": 0.0},
        {"budget_fraction": 1.0},
        {"holder": ""},
        {"holder": "x" * 256},
    ],
)
def test_the_config_rejects_values_the_database_functions_refuse(bad: dict[str, object]) -> None:
    """The ttl, the delay and the holder are bounded like the definer functions' arguments."""
    with pytest.raises(ValueError, match="must"):
        _config(**bad)


def test_lease_lost_is_the_one_stop_signal() -> None:
    """Both SQLSTATEs map to this one exception (see the SQL store tests)."""
    assert issubclass(LeaseLost, Exception)


async def test_the_engine_satisfies_the_driver_protocol_the_reconciler_holds() -> None:
    """B13: the reconciler depends on `DeletionDriver` only (mypy checks the assignment)."""
    engine, _ = _engine(FakeStore(None))
    driver: DeletionDriver = engine
    assert await driver.run_once() is False
