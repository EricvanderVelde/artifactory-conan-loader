#!/usr/bin/env bash
# Shared configuration sourced by 01_setup_artifactory.sh.
# Override any variable by setting it before sourcing this file,
# e.g.:  ARTIFACTORY_URL=http://myserver:8082/artifactory source scripts/config.sh

ARTIFACTORY_URL="${ARTIFACTORY_URL:-http://localhost:8082/artifactory}"
ARTIFACTORY_USER="${ARTIFACTORY_USER:-admin}"
ARTIFACTORY_PASSWORD="${ARTIFACTORY_PASSWORD:-password}"

SOURCES_REPO="conan-sources"   # generic repo for source tarballs
CONAN_REPO="conan-local"       # Conan 2 package repository

CONAN_REMOTE_NAME="${CONAN_REMOTE_NAME:-artifactory}"
