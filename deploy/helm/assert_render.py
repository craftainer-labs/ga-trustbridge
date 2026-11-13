"""Assertions over a rendered chart (run by check.sh): `assert_render.py NAME FILE`.

NAME is the ci values file (`full-values` or `minimal-values`) or `health-off` (the minimal
values with both health ports set to null); FILE is the `helm template` output.
Also checks that the chart maps every setting `trustbridge.settings` reads.
"""

import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from trustbridge.settings import known_env_names  # noqa: E402

SECRET_ENV = {
    "POSTGRES_PASSWORD",
    "REDIS_URL",
    "RUSTFS_ACCESS_KEY",
    "RUSTFS_SECRET_KEY",
    "MQTT_USERNAME",
    "MQTT_PASSWORD",
    "SUBSCRIBER_SECRET",
    "REGISTRATION_STATE_KEY",
}
# Read by the settings but deliberately not mapped: MODE=mock disables auth, and the S3 pair
# is read through the RUSTFS_* aliases.
UNMAPPED = {"ALLOW_MOCK_MODE", "S3_ACCESS_KEY", "S3_SECRET_KEY"}
CA_VARS = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "AWS_CA_BUNDLE",
    "PGSSLROOTCERT",
)
CA_PATH = "/etc/trustbridge/ca/ca-bundle.crt"
PROCESSES = ("api", "worker", "reconciler")
PROBES = {"startupProbe", "livenessProbe", "readinessProbe"}
HEALTH_PORTS = {"worker": 8081, "reconciler": 8081}
FULL_HEALTH_PORTS = {"worker": 8081, "reconciler": 9091}  # ci/full-values.yaml overrides one
PROBE_PATHS = {
    "startupProbe": "/health/live",
    "livenessProbe": "/health/live",
    "readinessProbe": "/health/ready",
}
IMAGE = "craftainer-labs/ga-trustbridge"

Doc = dict[str, Any]


def check(condition: object, message: str) -> None:
    """Exit with `message` unless `condition` holds."""
    if not condition:
        raise SystemExit(f"assert_render: {message}")


def container_of(doc: Doc) -> Doc:
    """The one container of a Deployment or Job."""
    return doc["spec"]["template"]["spec"]["containers"][0]


def env_of(container: Doc) -> Doc:
    """The container's environment by name; names must be unique."""
    names = [entry["name"] for entry in container["env"]]
    check(len(names) == len(set(names)), f"duplicate env names in {container['name']}")
    return {entry["name"]: entry for entry in container["env"]}


def check_structure(by_kind: dict[str, list[Doc]], full: bool) -> dict[str, Doc]:
    """Which resources exist; returns the Deployments by component."""
    deployments = {
        d["metadata"]["labels"]["app.kubernetes.io/component"]: d
        for d in by_kind.get("Deployment", [])
    }
    check(
        set(deployments) == {"api", "worker", "reconciler"}, f"deployments: {sorted(deployments)}"
    )
    check(len(by_kind.get("PodDisruptionBudget", [])) == len(PROCESSES), "a PDB per process")
    check(len(by_kind.get("Service", [])) == 1, "one Service (the api)")
    check(len(by_kind.get("Ingress", [])) == (1 if full else 0), "Ingress only when enabled")
    policies = len(by_kind.get("NetworkPolicy", []))
    check(policies == (2 if full else 0), "NetworkPolicy only when enabled")
    jobs = by_kind.get("Job", [])
    check(len(jobs) == 1, "one migrate Job")
    hook = jobs[0]["metadata"]["annotations"]["helm.sh/hook"]
    check(hook == "pre-install,pre-upgrade", f"migrate hook is {hook}")
    return deployments


def check_commands(deployments: dict[str, Doc], ports: dict[str, int] | None) -> None:
    """Commands are explicit; the api has HTTP probes on its port, the worker and reconciler on
    their health port (`ports`, or none at all when the endpoint is off)."""
    api = container_of(deployments["api"])
    check(
        api["command"][:3] == ["uvicorn", "trustbridge.main:app", "--no-proxy-headers"],
        "api command",
    )
    check(api.keys() >= PROBES, "api probes")
    check("HEALTH_PORT" not in env_of(api), "the api does not run the health endpoint")
    for process in ("worker", "reconciler"):
        container = container_of(deployments[process])
        want = ["python", "-m", "trustbridge.worker", process]
        check(container["command"] == want, f"{process} command")
        env = env_of(container)
        if ports is None:
            check(not PROBES & container.keys(), f"{process}: probes with the endpoint off")
            check("HEALTH_PORT" not in env, f"{process}: HEALTH_PORT with the endpoint off")
            check("ports" not in container, f"{process}: containerPort with the endpoint off")
            continue
        port = ports[process]
        check(env["HEALTH_PORT"]["value"] == str(port), f"{process}: HEALTH_PORT")
        check(
            container["ports"] == [{"name": "health", "containerPort": port, "protocol": "TCP"}],
            f"{process}: containerPort {container.get('ports')}",
        )
        check(container.keys() >= PROBES, f"{process}: probes")
        for kind, path in PROBE_PATHS.items():
            get = container[kind]["httpGet"]
            check(get == {"path": path, "port": "health"}, f"{process} {kind}: {get}")
            for field in ("periodSeconds", "timeoutSeconds", "failureThreshold"):
                check(container[kind][field] > 0, f"{process} {kind}: {field}")


def check_security(label: str, doc: Doc, *, automount: bool) -> None:
    """Pod Security `restricted` settings, resources and the token mount."""
    spec = doc["spec"]["template"]["spec"]
    check(spec["securityContext"]["runAsNonRoot"] is True, f"{label}: runAsNonRoot")
    check(
        spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault", f"{label}: seccomp"
    )
    check(spec["automountServiceAccountToken"] is automount, f"{label}: token automount")
    container = container_of(doc)
    sc = container["securityContext"]
    check(sc["readOnlyRootFilesystem"] is True, f"{label}: readOnlyRootFilesystem")
    check(sc["allowPrivilegeEscalation"] is False, f"{label}: allowPrivilegeEscalation")
    check(sc["capabilities"]["drop"] == ["ALL"], f"{label}: drop ALL")
    resources = container["resources"]
    check(resources["requests"] and resources["limits"], f"{label}: resources")


def check_settings(label: str, env: Doc) -> None:
    """The consistency rules of the settings validation, and secrets only by reference."""
    for secret in SECRET_ENV & env.keys():
        ok = "valueFrom" in env[secret] and "value" not in env[secret]
        check(ok, f"{label}: {secret} must come from a secret")
    check(env["RUN_MIGRATIONS_ON_STARTUP"]["value"] == "false", f"{label}: startup migrations")
    check(env["MODE"]["value"] == "production", f"{label}: MODE")
    check(len(json.loads(env["GITHUB_HOSTS"]["value"])) == 1, f"{label}: GITHUB_HOSTS")
    check(isinstance(json.loads(env["TRUSTED_PROXIES"]["value"]), list), f"{label}: proxies")
    check(env["WEBHOOK_MAX_BODY_BYTES"]["value"] == "26214400", f"{label}: integer formatting")
    check(env["ADMIN_MAX_BODY_BYTES"]["value"] == "16384", f"{label}: ADMIN_MAX_BODY_BYTES")
    check(env["POSTGRES_SSLMODE"]["value"] == "verify-full", f"{label}: sslmode")
    owner = label == "migrate"
    want = "trustbridge_owner" if owner else "trustbridge_app"
    check(env["POSTGRES_USER"]["value"] == want, f"{label}: POSTGRES_USER")
    check(("MIGRATE_EXPECTED_ROLE" in env) == owner, f"{label}: MIGRATE_EXPECTED_ROLE")


def check_full(label: str, doc: Doc, env: Doc) -> None:
    """Air-gap, CA and proxy values reach the pod."""
    spec = doc["spec"]["template"]["spec"]
    image = container_of(doc)["image"]
    prefix = f"registry.example.internal/mirror/{IMAGE}@sha256:"
    check(image.startswith(prefix), f"{label}: air-gap image {image}")
    check({"name": "mirror-pull"} in spec["imagePullSecrets"], f"{label}: imagePullSecrets")
    for var in CA_VARS:
        check(env[var]["value"] == CA_PATH, f"{label}: {var}")
    check(any(v["name"] == "ca-bundle" for v in spec["volumes"]), f"{label}: CA volume")
    check(env["HTTP_PROXY"]["value"] == "http://proxy.example.internal:3128", f"{label}: proxy")
    check("secretKeyRef" in env["HTTPS_PROXY"]["valueFrom"], f"{label}: HTTPS_PROXY by secret")
    check(env["NO_PROXY"]["value"] == ".svc,.cluster.local", f"{label}: NO_PROXY")
    check(env["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"].startswith("http://"), f"{label}: OTLP")


def check_defaults(label: str, doc: Doc, env: Doc) -> None:
    """Nothing optional is on by default: no CA, proxy, OTLP or pull secrets."""
    image = container_of(doc)["image"]
    check(image == f"ghcr.io/{IMAGE}:0.1.0", f"{label}: default image {image}")
    check(not set(CA_VARS) & env.keys(), f"{label}: no CA variables by default")
    check("HTTP_PROXY" not in env, f"{label}: no proxy by default")
    check("OTEL_EXPORTER_OTLP_ENDPOINT" not in env, f"{label}: no OTLP by default")
    check(not doc["spec"]["template"]["spec"].get("imagePullSecrets"), f"{label}: pull secrets")


def check_all_mapped() -> None:
    """Every setting the application reads is mapped by the chart."""
    helpers = (ROOT / "deploy/helm/trustbridge/templates/_helpers.tpl").read_text()
    missing = sorted(n for n in known_env_names() - UNMAPPED if f'"{n}"' not in helpers)
    check(not missing, f"settings not mapped by the chart: {missing}")


def main(name: str, path: str) -> None:
    """Check the rendering `path` of the values file `name`."""
    docs = [d for d in yaml.safe_load_all(Path(path).read_text()) if d]
    by_kind: dict[str, list[Doc]] = {}
    for doc in docs:
        by_kind.setdefault(doc["kind"], []).append(doc)
    full = name == "full-values"
    ports = None if name == "health-off" else (FULL_HEALTH_PORTS if full else HEALTH_PORTS)
    deployments = check_structure(by_kind, full)
    check_commands(deployments, ports)
    workloads = {**deployments, "migrate": by_kind["Job"][0]}
    for label, doc in workloads.items():
        check_security(label, doc, automount=full and label != "migrate")
        env = env_of(container_of(doc))
        check_settings(label, env)
        (check_full if full else check_defaults)(label, doc, env)
    check_all_mapped()
    print(f"assert_render: {name} ok ({len(docs)} documents)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
