#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${REPO_ROOT}/scripts/config.sh"

PROFILE="${REPO_ROOT}/profiles/linux-x86_64-gcc-cxx17"
BUILD_DIR="${SCRIPT_DIR}/build"

echo "=== Installing protobuf/6.33.5 from Artifactory ==="
conan install "${SCRIPT_DIR}" \
    --profile:build "${PROFILE}" \
    --profile:host  "${PROFILE}" \
    --remote "${CONAN_REMOTE_NAME}" \
    --output-folder "${BUILD_DIR}" \
    --build missing

echo ""
echo "=== Locating Conan-installed protoc ==="
PROTOBUF_PKG_ID=$(conan list "protobuf/6.33.5:*" --format=json | python3 -c "
import sys, json
data = json.load(sys.stdin)
revs = data['Local Cache']['protobuf/6.33.5']['revisions']
rev = list(revs.keys())[0]
print(list(revs[rev]['packages'].keys())[0])
")
PROTOC_BIN_DIR="$(conan cache path "protobuf/6.33.5:${PROTOBUF_PKG_ID}")/bin"
echo "Using protoc from: ${PROTOC_BIN_DIR}"
export PATH="${PROTOC_BIN_DIR}:${PATH}"
protoc --version

echo ""
echo "=== Cleaning CMake cache to force protoc re-detection ==="
rm -f "${BUILD_DIR}/CMakeCache.txt"
rm -rf "${BUILD_DIR}/proto"

echo ""
echo "=== Configuring with CMake ==="
cmake "${SCRIPT_DIR}" \
    -B "${BUILD_DIR}" \
    -DCMAKE_TOOLCHAIN_FILE="${BUILD_DIR}/conan_toolchain.cmake" \
    -DCMAKE_BUILD_TYPE=Release

echo ""
echo "=== Building ==="
cmake --build "${BUILD_DIR}" --parallel "$(nproc)"

echo ""
echo "=== Running protobuf_demo ==="
"${BUILD_DIR}/protobuf_demo"
