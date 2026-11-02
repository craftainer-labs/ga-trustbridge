"""Check runs: registry scan results and "manual action needed" notes on a commit."""

from dataclasses import dataclass
from typing import Literal

from trustbridge.adapters.interface import Tenant
from trustbridge.github.gateway import Call, TenantGateway

MAX_TEXT = 60_000
"""GitHub caps check output text at 65535 characters; stay below it."""

CheckStatus = Literal["queued", "in_progress", "completed"]
CheckConclusion = Literal["success", "failure", "neutral", "action_required"]


@dataclass(frozen=True, slots=True)
class CheckReport:
    """A check run to create or update on `head_sha` of a repository."""

    repository_id: int
    head_sha: str
    name: str
    title: str
    summary: str
    status: CheckStatus = "completed"
    conclusion: CheckConclusion | None = "neutral"
    details_url: str | None = None


@dataclass(frozen=True, slots=True)
class ScanResult:
    """A registry vulnerability scan summary (from an adapter's re-read, never a raw webhook)."""

    repository_id: int
    head_sha: str
    image: str
    critical: int
    high: int
    medium: int
    low: int
    report_url: str | None = None


def scan_report(scan: ScanResult, *, fail_on_high: bool = True) -> CheckReport:
    """Turn a scan summary into a check run: failure on critical (by default also high) findings."""
    failed = scan.critical > 0 or (fail_on_high and scan.high > 0)
    summary = (
        f"Image `{scan.image}`: {scan.critical} critical, {scan.high} high, "
        f"{scan.medium} medium, {scan.low} low."
    )
    return CheckReport(
        repository_id=scan.repository_id,
        head_sha=scan.head_sha,
        name="trustbridge / registry scan",
        title="Vulnerability scan " + ("failed" if failed else "passed"),
        summary=summary,
        conclusion="failure" if failed else "success",
        details_url=scan.report_url,
    )


def manual_action_report(
    repository_id: int, head_sha: str, adapter: str, instructions: str
) -> CheckReport:
    """A check run telling the user a platform step must be done by hand (`NEEDS_MANUAL_ACTION`)."""
    return CheckReport(
        repository_id=repository_id,
        head_sha=head_sha,
        name=f"trustbridge / {adapter}",
        title="Manual action required",
        summary=instructions,
        conclusion="action_required",
    )


class CheckRunReporter:
    """Creates or updates a check run, one per `(sha, name)`: reporting twice updates in place."""

    def __init__(self, gateway: TenantGateway) -> None:
        """Use `gateway` for tokens and requests."""
        self._gw = gateway

    async def report(self, tenant: Tenant, report: CheckReport) -> int:
        """Create the check run or update the existing one; return its id."""
        full = await self._gw.full_name(tenant, report.repository_id)
        ids = [report.repository_id]
        perms = {"checks": "write"}
        found = await self._gw.call(
            tenant,
            Call(
                "GET",
                f"/repos/{full}/commits/{report.head_sha}/check-runs",
                perms,
                ids,
                params={"check_name": report.name},
            ),
        )
        body: dict[str, object] = {
            "name": report.name,
            "head_sha": report.head_sha,
            "status": report.status,
            "output": {
                "title": report.title[:255],
                "summary": report.summary[:MAX_TEXT],
            },
        }
        if report.conclusion is not None and report.status == "completed":
            body["conclusion"] = report.conclusion
        if report.details_url is not None:
            body["details_url"] = report.details_url
        runs = (found.data or {}).get("check_runs", [])
        if runs:
            run_id = int(runs[0]["id"])
            await self._gw.call(
                tenant, Call("PATCH", f"/repos/{full}/check-runs/{run_id}", perms, ids, body)
            )
            return run_id
        created = await self._gw.call(
            tenant, Call("POST", f"/repos/{full}/check-runs", perms, ids, body)
        )
        return int(created.data["id"])
