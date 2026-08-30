# -*- coding: utf-8 -*-
"""Unit tests for geometry parsing in the Solr search backends.

These deliberately live outside ``ckanext/spatial/tests/``: that package's
conftest imports ``ckanext.harvest.model`` at module level and the suite in
it needs Postgres + Solr. Everything here is pure logic -- ``search`` only
imports json, shapely, ckantoolkit and ckanext.spatial.lib -- so it runs with
``pytest -p no:ckan --noconftest`` and no CKAN site at all.

Run them with ``bash scripts/run-ckan-tests.sh``.
"""
import logging

import pytest
import shapely.geometry
import shapely.wkt

from ckanext.spatial.search import (
    SpatialSearchBackend,
    SolrBBoxSearchBackend,
    SolrSpatialFieldSearchBackend,
)


POLYGON = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [0.0, 2.0], [2.0, 2.0], [2.0, 0.0], [0.0, 0.0]]],
}

# A "bounding box" whose five corners are the same point. index_dataset has a
# dedicated branch that turns this into a POINT rather than a degenerate
# polygon.
DEGENERATE_BBOX = {
    "type": "Polygon",
    "coordinates": [[[5.0, 7.0]] * 5],
}

# Two overlapping members, which makes the multipolygon invalid under OGC
# rules. Real datasets in the IHP-WINS portal hit this (large multi-country
# unions). It has to be something other than a single five-point Polygon:
# that shape takes the bounding-box branch of index_dataset, which never
# reaches the is_valid check.
INVALID_MULTIPOLYGON = {
    "type": "MultiPolygon",
    "coordinates": [
        [[[0.0, 0.0], [0.0, 2.0], [2.0, 2.0], [2.0, 0.0], [0.0, 0.0]]],
        [[[1.0, 1.0], [1.0, 3.0], [3.0, 3.0], [3.0, 1.0], [1.0, 1.0]]],
    ],
}


@pytest.fixture
def backend():
    return SpatialSearchBackend()


# ---------------------------------------------------------------------------
# parse_geojson
# ---------------------------------------------------------------------------

def test_returns_an_already_parsed_geometry_unchanged(backend):
    """The regression this fix exists for.

    CKAN copies extras to the top level of the dict it indexes, and an
    IPackageController ordered before ckanext-spatial (ckanext-schemingdcat)
    replaces the `spatial` string with the parsed mapping. Before the fix
    json.loads() raised TypeError here and aborted indexing of the whole
    document.
    """
    assert backend.parse_geojson(POLYGON) is POLYGON


def test_parses_a_json_string(backend):
    assert backend.parse_geojson('{"type": "Point", "coordinates": [1, 2]}') == {
        "type": "Point",
        "coordinates": [1, 2],
    }


def test_a_string_and_its_parsed_form_agree(backend):
    import json

    assert backend.parse_geojson(json.dumps(POLYGON)) == backend.parse_geojson(POLYGON)


def test_invalid_json_returns_none_and_logs(backend, caplog):
    with caplog.at_level(logging.ERROR, logger="ckanext.spatial.search"):
        assert backend.parse_geojson("not json{") is None
    assert "not indexing" in caplog.text


@pytest.mark.parametrize(
    "value",
    [None, 12345, 4.5, ["a", "list"], object()],
    ids=["none", "int", "float", "list", "object"],
)
def test_unparseable_types_return_none_without_raising(backend, value):
    """json.loads raises TypeError for any non str/bytes input.

    Before the fix TypeError was not caught at all; and once caught, building
    the log message sliced the value, which raised a *second* TypeError from
    inside the handler ("unhashable type: 'slice'" for a dict, "'int' object
    is not subscriptable" for a number).
    """
    assert backend.parse_geojson(value) is None


@pytest.mark.parametrize(
    "value", [None, 12345, ["a", "list"]], ids=["none", "int", "list"]
)
def test_the_error_handler_itself_logs_instead_of_raising(backend, caplog, value):
    with caplog.at_level(logging.ERROR, logger="ckanext.spatial.search"):
        backend.parse_geojson(value)
    # If the handler raised while formatting, no record would ever be emitted.
    assert len(caplog.records) == 1


# ---------------------------------------------------------------------------
# SolrSpatialFieldSearchBackend.index_dataset -- the production backend
# ---------------------------------------------------------------------------

def test_indexes_a_dict_geometry_end_to_end():
    """Reproduces the production pipeline without Solr.

    A single five-point Polygon takes the bounding-box branch, which collects
    WKT into a list; other shapes get shape.wkt, a plain string. That
    inconsistency is upstream behaviour, asserted here as-is.

    The ring is compared geometrically, not textually: index_dataset rewrites
    it counter-clockwise and may start it at a different corner.
    """
    result = SolrSpatialFieldSearchBackend().index_dataset({"spatial": POLYGON})
    assert isinstance(result["spatial_geom"], list)
    indexed = shapely.wkt.loads(result["spatial_geom"][0])
    assert indexed.equals(shapely.geometry.shape(POLYGON))


def test_dict_and_string_geometries_index_identically():
    import json

    from_dict = SolrSpatialFieldSearchBackend().index_dataset({"spatial": POLYGON})
    from_string = SolrSpatialFieldSearchBackend().index_dataset(
        {"spatial": json.dumps(POLYGON)}
    )
    assert from_dict["spatial_geom"] == from_string["spatial_geom"]


def test_a_degenerate_bbox_is_indexed_as_a_point():
    result = SolrSpatialFieldSearchBackend().index_dataset({"spatial": DEGENERATE_BBOX})
    assert result["spatial_geom"] == ["POINT(5.0 7.0)"]


def test_an_invalid_geometry_is_skipped_without_raising():
    """Documents the behaviour of the datasets that stay without a geometry.

    Not a regression of the fix: before it they failed to index at all.
    """
    result = SolrSpatialFieldSearchBackend().index_dataset(
        {"spatial": INVALID_MULTIPOLYGON, "name": "overlapping"}
    )
    assert "spatial_geom" not in result
    assert result["name"] == "overlapping"


@pytest.mark.parametrize("dataset_dict", [{}, {"spatial": ""}, {"spatial": None}])
def test_datasets_without_a_geometry_are_left_alone(dataset_dict):
    """schemingdcat drops empty facet keys, so `spatial` can be absent."""
    for backend_class in (SolrSpatialFieldSearchBackend, SolrBBoxSearchBackend):
        result = backend_class().index_dataset(dict(dataset_dict))
        assert "spatial_geom" not in result
        assert "minx" not in result


# ---------------------------------------------------------------------------
# SolrBBoxSearchBackend.index_dataset -- not used in production, but the
# missing guard made the parse_geojson None branch fatal here.
# ---------------------------------------------------------------------------

def test_bbox_backend_indexes_a_dict_geometry():
    result = SolrBBoxSearchBackend().index_dataset({"spatial": POLYGON})
    assert (result["minx"], result["miny"]) == (0.0, 0.0)
    assert (result["maxx"], result["maxy"]) == (2.0, 2.0)


def test_bbox_backend_skips_unparseable_geometry_without_raising():
    """Without the guard, shape(None) raises AttributeError, which
    shape_from_geometry does not catch (it only catches GeometryTypeError
    and TypeError)."""
    result = SolrBBoxSearchBackend().index_dataset(
        {"spatial": "not json{", "name": "broken"}
    )
    assert "minx" not in result
    assert result["name"] == "broken"
