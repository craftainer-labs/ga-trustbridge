#!/usr/bin/env bash
# `kcadm` reaches the sibling `keycloak` container's Admin REST API from
# the devcontainer -- see .devcontainer/stack/keycloak/README.md. A thin
# curl wrapper rather than the official kcadm.sh, which ships only inside
# Keycloak's full server distribution and needs a JVM neither this stage
# nor the app otherwise requires.
set -euo pipefail

kcadm_tmpfile="$(mktemp)"
sudo chmod a+rwx "$kcadm_tmpfile"
cat > "$kcadm_tmpfile" <<'KCADM'
#!/usr/bin/env bash
# Thin curl wrapper for the Keycloak Admin REST API. See
# .devcontainer/stack/keycloak/README.md for usage and required env vars.
set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "Usage: kcadm METHOD PATH [JSON_BODY]" >&2
    echo "Example: kcadm GET /admin/realms/template-fastapi/users" >&2
    exit 1
fi

method=$1
path=$2
body=${3:-}
base_url=${KEYCLOAK_URL:-http://keycloak:8080}

token=$(curl -sf -X POST "${base_url}/realms/master/protocol/openid-connect/token" \
    -d grant_type=password \
    -d client_id=admin-cli \
    -d "username=${KEYCLOAK_ADMIN}" \
    -d "password=${KEYCLOAK_ADMIN_PASSWORD}" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')

curl_args=(-sf -X "$method" "${base_url}${path}" \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json")
[[ -n "$body" ]] && curl_args+=(-d "$body")

curl "${curl_args[@]}" | { python3 -m json.tool 2>/dev/null || cat; }
KCADM
sudo -u vscode install -Dm755 "$kcadm_tmpfile" /home/vscode/.local/bin/kcadm
rm -f "$kcadm_tmpfile"
ln -s /home/vscode/.local/bin/kcadm /usr/local/bin/kcadm
