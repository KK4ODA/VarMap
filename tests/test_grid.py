import pytest

from varmap.domain.grid import (distance_inside_cell_m, grid_accuracy_m, grid_bounds, grid_to_latlon,
                                latlon_to_grid, normalise_grid, split_locator)

# Verified against the live VarAC install (design doc 3.4)
FIXTURES = {
    "EM73UU": (33.85417, -84.29167),
    "FM05JL": (35.47917, -79.20833),
    "JN73TS": (43.77083, 15.62500),
    "CN87OD": (47.14583, -122.79167),
}


@pytest.mark.parametrize("grid,expected", FIXTURES.items())
def test_grid_centroids(grid, expected):
    lat, lon = grid_to_latlon(grid)
    assert lat == pytest.approx(expected[0], abs=1e-5)
    assert lon == pytest.approx(expected[1], abs=1e-5)


@pytest.mark.parametrize("grid", FIXTURES)
def test_roundtrip(grid):
    lat, lon = grid_to_latlon(grid)
    assert latlon_to_grid(lat, lon) == grid


def test_lowercase_and_4char():
    assert grid_to_latlon("em73uu") == grid_to_latlon("EM73UU")
    lat, lon = grid_to_latlon("EM73")
    assert lat == pytest.approx(33.5) and lon == pytest.approx(-85.0)
    assert latlon_to_grid(33.5, -85.0, 4) == "EM73"


def test_8char():
    lat, lon = grid_to_latlon("EM73UU55")
    assert latlon_to_grid(lat, lon, 8) == "EM73UU55"


def test_invalid():
    for bad in ("", None, "ZZ99ZZ", "EM7", "EM73U", "EM73UY", "12ABCD", "EM73UU ⌛"):
        assert grid_to_latlon(bad) is None
        assert normalise_grid(bad) is None


def test_split_locator():
    assert split_locator("EM73UU ⌛") == ("EM73UU", True)
    assert split_locator(" em73uu ") == ("EM73UU", False)
    assert split_locator("") == ("", False)
    assert split_locator(None) == ("", False)


def test_bounds_contains_centroid():
    s, w, n, e = grid_bounds("EM73UU")
    lat, lon = grid_to_latlon("EM73UU")
    assert s < lat < n and w < lon < e
    assert n - s == pytest.approx(1 / 24)
    assert e - w == pytest.approx(2 / 24)


def test_known_position_inside_its_grid():
    # Operator's true position vs transmitted grid (design doc 3.4): ~2.2 km off centroid
    assert latlon_to_grid(33.86000, -84.30000) == "EM73UU"
    assert distance_inside_cell_m(33.86000, -84.30000, "EM73UU") > 0
    assert distance_inside_cell_m(33.86000, -84.30000, "EM73UT") < 0


def test_accuracy():
    assert grid_accuracy_m("EM73UU") == 3800.0
    assert grid_accuracy_m("EM73") > grid_accuracy_m("EM73UU") > grid_accuracy_m("EM73UU55")


def test_poles_do_not_crash():
    assert latlon_to_grid(90, 180) is not None
    assert latlon_to_grid(-90, -180) == "AA00AA"
