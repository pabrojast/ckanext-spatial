#!/usr/bin/env bash
#
# ckanext-spatial -- reproducible CKAN 2.10 verification driver.
#
# Builds Dockerfile.test (real CKAN 2.10 + the plugin) and runs:
#   1. an import smoke check (both search backends still share the base class
#      whose parse_geojson this fork patches),
#   2. the geometry-parsing unit tests.
#
# Usage (from the repo root, requires Docker):
#   bash scripts/run-ckan-tests.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

IMAGE="spatial-test"

echo "== ckanext-spatial: CKAN 2.10 verification harness =="

echo "-- docker build -f Dockerfile.test -t ${IMAGE} ."
docker build -f Dockerfile.test -t "${IMAGE}" .

echo "-- import smoke check (search backends + plugin module)"
docker run --rm -i "${IMAGE}" python - <<'PY'
import sys

from ckanext.spatial.search import (
    search_backends,
    SpatialSearchBackend,
    SolrBBoxSearchBackend,
    SolrSpatialFieldSearchBackend,
)

expected = {"solr-bbox", "solr-spatial-field"}
if set(search_backends) != expected:
    print("FAIL: backends changed: %s" % sorted(search_backends))
    sys.exit(1)

# parse_geojson is defined once on the base class; both backends must keep
# inheriting it or the fix silently stops covering one of them.
for name, backend in search_backends.items():
    if not issubclass(backend, SpatialSearchBackend):
        print("FAIL: %s no longer derives from SpatialSearchBackend" % name)
        sys.exit(1)
    if backend.parse_geojson is not SpatialSearchBackend.parse_geojson:
        print("FAIL: %s overrides parse_geojson" % name)
        sys.exit(1)

# Importing the plugin module is what breaks every CKAN worker at startup if
# a dependency is missing, so check it here rather than at deploy time.
import ckanext.spatial.plugin as plugin
if not (plugin.SpatialQuery and plugin.SpatialMetadata):
    print("FAIL: plugin classes missing")
    sys.exit(1)

print("PLUGIN OK: backends=%s" % sorted(search_backends))
PY

echo "-- unit tests"
# --noconftest is required here, unlike the other forks in this stack: the
# repo root conftest.py loads ckanext.spatial.tests.fixtures, and
# ckanext/spatial/tests/conftest.py imports ckanext.harvest.model at module
# level. ckanext-harvest is not installed in this image (the existing
# integration suite needs Postgres + Solr + harvest), so collecting those
# conftests fails before any test runs. The unit tests deliberately live in
# tests_unit/ for the same reason -- do not move them into tests/.
docker run --rm "${IMAGE}" \
    pytest -p no:ckan --noconftest -q \
    /plugin/ckanext/spatial/tests_unit/test_parse_geojson.py

echo "== PASS: all verification layers green =="
