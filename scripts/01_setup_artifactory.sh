#!/usr/bin/env bash
# Creates the required Artifactory repositories and configures the Conan remote.
# Run once after `docker compose up -d`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/config.sh"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
art_curl() {
    curl --silent --show-error --fail \
         -u "${ARTIFACTORY_USER}:${ARTIFACTORY_PASSWORD}" \
         "$@"
}

wait_for_artifactory() {
    local url="${ARTIFACTORY_URL}/api/system/ping"
    echo "Waiting for Artifactory at ${ARTIFACTORY_URL} ..."
    local max_attempts=40
    local attempt=0
    until art_curl -o /dev/null "${url}" 2>/dev/null; do
        attempt=$((attempt + 1))
        if [[ ${attempt} -ge ${max_attempts} ]]; then
            echo "ERROR: Artifactory did not become ready after $((max_attempts * 15)) seconds." >&2
            exit 1
        fi
        echo "  attempt ${attempt}/${max_attempts} – retrying in 15 s ..."
        sleep 15
    done
    echo "Artifactory is up."
}

create_repo() {
    local repo_key="$1"
    local package_type="$2"   # "generic" or "conan"

    # Check if repo already exists via the list API
    local exists
    exists=$(curl --silent -u "${ARTIFACTORY_USER}:${ARTIFACTORY_PASSWORD}" \
        "${ARTIFACTORY_URL}/api/repositories" | python3 -c \
        "import sys,json; repos=json.load(sys.stdin); print(any(r['key']=='${repo_key}' for r in repos))" 2>/dev/null)
    if [[ "${exists}" == "True" ]]; then
        echo "  -> Repository '${repo_key}' already exists."
        return 0
    fi

    # The repository management REST API requires a paid Artifactory license.
    # We work around this by inserting directly into PostgreSQL, which
    # Artifactory reads on startup (or on container restart).
    echo "Creating local repository '${repo_key}' (${package_type}) via PostgreSQL ..."
    local NOW
    NOW=$(python3 -c "import time; print(int(time.time()*1000))")

    python3 - <<PYEOF
import subprocess, json
blob = json.dumps({
    "type": "local", "key": "${repo_key}", "packageType": "${package_type}",
    "baseConfig": {
        "modelVersion": 2,
        "description": "${repo_key} repository",
        "repoLayoutRef": "simple-default",
        "includesPattern": "**/*",
        "federationConfig": {"federationOnGrid": False, "members": [], "modificationDate": 0, "federated": False}
    },
    "repoTypeConfig": {
        "archiveBrowsingEnabled": False, "blackedOut": False,
        "downloadRedirectConfig": {"enabled": False},
        "propertySetRefs": [], "checksumPolicyType": "client-checksums",
        "priorityResolution": False, "maxUniqueSnapshots": 0,
        "handleReleases": True, "handleSnapshots": True, "snapshotVersionBehavior": "unique"
    },
    "packageTypeConfig": {},
    "securityConfig": {"hideUnauthorizedResources": False, "signedUrlTtl": 90},
    "repoType": "LOCAL"
})
sql = (
    "INSERT INTO repository_config "
    "(repository_key, type, package_type, revision, created, modified, config_blob) "
    "VALUES ('${repo_key}', 'local', '${package_type}', 0, ${NOW}, ${NOW}, "
    "convert_to(\$\$" + blob + "\$\$, 'UTF8')) "
    "ON CONFLICT (repository_key) DO NOTHING;"
)
r = subprocess.run(
    ["docker", "exec", "artifactory-db", "psql", "-U", "artifactory", "-c", sql],
    capture_output=True, text=True)
print("  DB insert:", r.stdout.strip() or r.stderr.strip())
PYEOF

    echo "  Restarting Artifactory to load new repository ..."
    docker restart artifactory > /dev/null 2>&1
    echo "  Waiting for Artifactory to recover ..."
    local max=20; local n=0
    until curl --silent --fail -u "${ARTIFACTORY_USER}:${ARTIFACTORY_PASSWORD}" \
               "${ARTIFACTORY_URL}/api/system/ping" > /dev/null 2>&1; do
        n=$((n+1)); [[ $n -ge $max ]] && echo "  ERROR: Artifactory did not recover." && return 1
        sleep 15
    done
    echo "  -> Done."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
wait_for_artifactory

echo ""
echo "=== Creating repositories ==="
create_repo "${SOURCES_REPO}" "generic"
create_repo "${CONAN_REPO}"   "conan"

echo ""
echo "=== Configuring Conan remote ==="
CONAN_REMOTE_URL="${ARTIFACTORY_URL}/api/conan/${CONAN_REPO}"

# Remove existing remote if present (idempotent)
conan remote remove "${CONAN_REMOTE_NAME}" 2>/dev/null || true
conan remote add "${CONAN_REMOTE_NAME}" "${CONAN_REMOTE_URL}"

echo "Logging in to Conan remote '${CONAN_REMOTE_NAME}' ..."
conan remote login "${CONAN_REMOTE_NAME}" "${ARTIFACTORY_USER}" \
    --password "${ARTIFACTORY_PASSWORD}"

echo ""
echo "Done. Summary:"
echo "  Generic sources repo : ${ARTIFACTORY_URL}/${SOURCES_REPO}/"
echo "  Conan package repo   : ${CONAN_REMOTE_URL}"
echo "  Conan remote alias   : ${CONAN_REMOTE_NAME}"
echo ""
echo "Next step: run  scripts/provision.py --packages-file packages.yml"
