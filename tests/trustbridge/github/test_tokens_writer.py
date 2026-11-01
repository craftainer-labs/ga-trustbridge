"""Installation tokens (cache, down-scoping), the secrets/variables/environments writer."""

import asyncio
import base64
from datetime import datetime

import httpx2
import pytest
from nacl.public import SealedBox
from pydantic import SecretStr

from tests.trustbridge.github.conftest import Env, ScriptedGitHub
from trustbridge.adapters.interface import GitHubOutput
from trustbridge.adapters.store import DeliveredOutput, OutputError
from trustbridge.github.availability import HostNotReadyError
from trustbridge.github.tokens import REFRESH_MARGIN_SECONDS
from trustbridge.github.writer import GitHubEnvironmentInspector, GitHubOutputWriter


def decrypt(gh: ScriptedGitHub, encrypted_b64: str) -> str:
    """Open a sealed-box value with the scripted GitHub's private key."""
    return SealedBox(gh.sodium).decrypt(base64.b64decode(encrypted_b64)).decode()


async def test_token_is_cached_until_five_minutes_before_expiry(env: Env) -> None:
    """One mint serves repeated calls; a token near expiry is replaced."""
    first = await env.tokens.token("github.com", 4242)
    assert await env.tokens.token("github.com", 4242) == first
    assert env.mint_count == 1
    expires = datetime.fromisoformat("2027-01-15T09:00:00+00:00").timestamp()
    env.clock_now = expires - REFRESH_MARGIN_SECONDS - 1
    assert await env.tokens.token("github.com", 4242) == first
    env.clock_now = expires - REFRESH_MARGIN_SECONDS + 1
    assert await env.tokens.token("github.com", 4242) != first
    assert env.mint_count == 2


async def test_tokens_are_down_scoped_per_repository_and_permission(env: Env) -> None:
    """The mint request carries repository_ids and permissions; each scope has its own cache."""
    await env.tokens.token(
        "github.com", 4242, repository_ids=[10], permissions={"secrets": "write"}
    )
    body = env.gh.json_of("POST", "/app/installations/4242/access_tokens")
    assert body == {"repository_ids": [10], "permissions": {"secrets": "write"}}
    await env.tokens.token(
        "github.com", 4242, repository_ids=[11], permissions={"secrets": "write"}
    )
    await env.tokens.token(
        "github.com", 4242, repository_ids=[10], permissions={"secrets": "write"}
    )
    assert env.mint_count == 2
    env.tokens.invalidate("github.com", 4242)
    await env.tokens.token(
        "github.com", 4242, repository_ids=[10], permissions={"secrets": "write"}
    )
    assert env.mint_count == 3


async def test_concurrent_callers_share_one_mint(env: Env) -> None:
    """A stampede for one key mints once."""
    tokens = await asyncio.gather(*[env.tokens.token("github.com", 4242) for _ in range(5)])
    assert len(set(tokens)) == 1
    assert env.mint_count == 1


async def test_no_token_for_a_host_that_is_not_ready(env: Env) -> None:
    """No GitHub call is made for an unavailable host."""
    env.checker.issuer = "https://evil.example"
    await env.monitor.check_all()
    mints = env.mint_count
    with pytest.raises(HostNotReadyError):
        await env.tokens.token("github.com", 4242)
    assert env.mint_count == mints


def script_repo(env: Env) -> None:
    """GET /repositories/10 -> acme/web."""
    env.gh.route("GET", "/repositories/10", body={"full_name": "acme/web"})


async def test_repository_secret_is_sealed_and_put(env: Env) -> None:
    """A repo secret: public key, libsodium sealed box, PUT with key_id; plaintext never sent."""
    script_repo(env)
    env.gh.route("GET", "/repos/acme/web/actions/secrets/public-key", body=env.gh.public_key_body())
    env.gh.route("PUT", "/repos/acme/web/actions/secrets/REG_TOKEN", 201)
    writer = GitHubOutputWriter(env.gateway)
    await writer.apply(
        env.tenant, [GitHubOutput("secret", "REG_TOKEN", SecretStr("s3cret"), 10, None)]
    )
    sent = env.gh.json_of("PUT", "/repos/acme/web/actions/secrets/REG_TOKEN")
    assert sent["key_id"] == "kid1"
    assert "s3cret" not in str(sent)
    assert decrypt(env.gh, sent["encrypted_value"]) == "s3cret"
    assert env.gh.json_of("POST", "/app/installations/4242/access_tokens") == {
        "repository_ids": [10],
        "permissions": {"secrets": "write"},
    }


async def test_environment_secret_uses_the_repository_id_route(env: Env) -> None:
    """Environment secrets are addressed by repository id and the percent-encoded name."""
    base = "/repositories/10/environments/prod eu"  # the mock sees the decoded path
    env.gh.route("GET", f"{base}/secrets/public-key", body=env.gh.public_key_body())
    env.gh.route("PUT", f"{base}/secrets/T", 204)
    await GitHubOutputWriter(env.gateway).apply(
        env.tenant, [GitHubOutput("secret", "T", "plain", 10, "prod eu")]
    )
    raw = [r.url.raw_path.decode() for r in env.gh.requests if r.method == "PUT"]
    assert raw == ["/repositories/10/environments/prod%20eu/secrets/T"]


async def test_org_wide_secret_is_visible_to_private_repositories_only(env: Env) -> None:
    """An org-wide output goes to the org with visibility `private` and the org permission."""
    env.gh.route("GET", "/orgs/acme/actions/secrets/public-key", body=env.gh.public_key_body())
    env.gh.route("PUT", "/orgs/acme/actions/secrets/ORG_T", 201)
    await GitHubOutputWriter(env.gateway).apply(
        env.tenant, [GitHubOutput("secret", "ORG_T", "x", None, None)]
    )
    assert env.gh.json_of("PUT", "/orgs/acme/actions/secrets/ORG_T")["visibility"] == "private"
    body = env.gh.json_of("POST", "/app/installations/4242/access_tokens")
    assert body == {"permissions": {"organization_secrets": "write"}}


async def test_variable_is_updated_or_created(env: Env) -> None:
    """PATCH first; on 404 create with POST (idempotent upsert)."""
    script_repo(env)
    env.gh.route(
        "PATCH", "/repos/acme/web/actions/variables/REGISTRY", 404, {"message": "Not Found"}
    )
    env.gh.route("POST", "/repos/acme/web/actions/variables", 201)
    await GitHubOutputWriter(env.gateway).apply(
        env.tenant, [GitHubOutput("variable", "REGISTRY", "ghcr.io", 10, None)]
    )
    assert env.gh.json_of("POST", "/repos/acme/web/actions/variables") == {
        "name": "REGISTRY",
        "value": "ghcr.io",
    }


async def test_environment_is_created_with_put(env: Env) -> None:
    """An environment output ensures the environment exists (administration: write)."""
    script_repo(env)
    env.gh.route("PUT", "/repos/acme/web/environments/prod", 200, {})
    await GitHubOutputWriter(env.gateway).apply(
        env.tenant, [GitHubOutput("environment", "prod", "", 10, None)]
    )
    assert env.gh.json_of("POST", "/app/installations/4242/access_tokens")["permissions"] == {
        "administration": "write"
    }


async def test_remove_deletes_secrets_and_variables_but_never_environments(env: Env) -> None:
    """Absent (404) is fine; environments keep their protection rules and other secrets."""
    script_repo(env)
    env.gh.route("DELETE", "/repos/acme/web/actions/secrets/A", 404, {})
    env.gh.route("DELETE", "/repos/acme/web/actions/variables/B", 204)
    await GitHubOutputWriter(env.gateway).remove(
        env.tenant,
        [
            DeliveredOutput("secret", "A", 10, None),
            DeliveredOutput("variable", "B", 10, None),
            DeliveredOutput("environment", "prod", 10, None),
        ],
    )
    assert not any("environments" in p for p in env.gh.paths())


@pytest.mark.parametrize(("status", "retryable"), [(500, True), (422, False)])
async def test_github_failures_become_output_errors(env: Env, status: int, retryable: bool) -> None:
    """The core retries retryable errors; a 422 is not retried."""
    script_repo(env)
    env.gh.route("GET", "/repos/acme/web/actions/secrets/public-key", status, {"message": "x"})
    with pytest.raises(OutputError) as err:
        await GitHubOutputWriter(env.gateway).apply(
            env.tenant, [GitHubOutput("secret", "A", "v", 10, None)]
        )
    assert err.value.retryable is retryable
    assert "v" not in str(err.value).replace("GitHub write failed", "")


async def test_environment_without_a_repository_is_a_permanent_error(env: Env) -> None:
    """An environment output needs a repository."""
    with pytest.raises(OutputError) as err:
        await GitHubOutputWriter(env.gateway).apply(
            env.tenant, [GitHubOutput("environment", "prod", "", None, None)]
        )
    assert err.value.retryable is False


async def test_transport_errors_are_retryable(env: Env) -> None:
    """A network failure is retryable."""
    script_repo(env)

    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    env.gh.on("GET", "/repos/acme/web/actions/secrets/public-key", boom)
    with pytest.raises(OutputError) as err:
        await GitHubOutputWriter(env.gateway).apply(
            env.tenant, [GitHubOutput("secret", "A", "v", 10, None)]
        )
    assert err.value.retryable is True


async def test_environment_protection_is_read_from_protection_rules(env: Env) -> None:
    """A12 input: any protection rule means protected; none or a missing environment, not."""
    script_repo(env)
    inspector = GitHubEnvironmentInspector(env.gateway)
    env.gh.route(
        "GET",
        "/repos/acme/web/environments/prod",
        body={"protection_rules": [{"type": "required_reviewers"}]},
    )
    env.gh.route("GET", "/repos/acme/web/environments/open", body={"protection_rules": []})
    env.gh.route("GET", "/repos/acme/web/environments/gone", 404, {})
    assert await inspector.is_protected(env.tenant, 10, "prod") is True
    assert await inspector.is_protected(env.tenant, 10, "open") is False
    assert await inspector.is_protected(env.tenant, 10, "gone") is False
