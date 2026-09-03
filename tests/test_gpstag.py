import pytest

from varmap.domain.gpstag import (format_gps_tag, parse_coordinate_text, parse_gps_tag, parse_position_text,
                                  unmangle)

# All 13 distinct payloads observed in the live database (design doc 3.5)
OBSERVED = [
    ("33.86000,-84.30000", (33.86000, -84.30000)),
    ("34.062727, -78.031431", (34.062727, -78.031431)),
    ("35.42864, -99.40437", (35.42864, -99.40437)),
    ("40.9226215, -74.0180964", (40.9226215, -74.0180964)),
    ("38.00977 N 78.83755W", (38.00977, -78.83755)),
    ("36° 53.660 N 76° 30.030 W", (36 + 53.660 / 60, -(76 + 30.030 / 60))),
    ("36°22.51128 N 095°39.65377 W", (36 + 22.51128 / 60, -(95 + 39.65377 / 60))),
    ("38° 54.89166N 76° 43.76664W", (38 + 54.89166 / 60, -(76 + 43.76664 / 60))),
    ("38° 59.03440 N 076° 44.28153 W", (38 + 59.03440 / 60, -(76 + 44.28153 / 60))),
    ("38°31.6257 N 077°13.4588 W", (38 + 31.6257 / 60, -(77 + 13.4588 / 60))),
    ("38°59.5438 N 076°45.1768 W", (38 + 59.5438 / 60, -(76 + 45.1768 / 60))),
    ("41°27.9620 N 072°02.5392 W", (41 + 27.9620 / 60, -(72 + 2.5392 / 60))),
    ("46° 32.616' N  87° 32.616' W", (46 + 32.616 / 60, -(87 + 32.616 / 60))),
]


@pytest.mark.parametrize("payload,expected", OBSERVED)
def test_observed_payloads(payload, expected):
    got = parse_gps_tag(f"blah <GPS:{payload}> blah")
    assert got is not None, payload
    assert got[0] == pytest.approx(expected[0], abs=1e-6)
    assert got[1] == pytest.approx(expected[1], abs=1e-6)


@pytest.mark.parametrize("payload,expected", OBSERVED)
def test_mangled_forms(payload, expected):
    mangled = payload.replace("0", "Ø")
    got = parse_gps_tag(f"«GPS:{mangled}»")
    assert got is not None
    assert got[0] == pytest.approx(expected[0], abs=1e-6)


def test_extra_forms():
    got = parse_coordinate_text("N38 31.6257 W077 13.4588")
    assert got == pytest.approx((38 + 31.6257 / 60, -(77 + 13.4588 / 60)))
    got = parse_coordinate_text("38°31'22.5\"N 77°13'27.5\"W")
    assert got[0] == pytest.approx(38 + 31 / 60 + 22.5 / 3600)
    assert parse_coordinate_text("-33.9, 151.2") == (-33.9, 151.2)


def test_rejects():
    assert parse_gps_tag("no tag here") is None
    assert parse_gps_tag("<GPS:0,0>") is None          # null island
    assert parse_gps_tag("<GPS:95.0,10.0>") is None    # out of range
    assert parse_gps_tag("<GPS:hello>") is None


def test_unmangle():
    assert unmangle("«GPS:38.ØØ977»") == "<GPS:38.00977>"
    assert unmangle(None) == ""


def test_position_text_priority():
    p = parse_position_text("«NAME:Franco» «QTH:Duluth» «LOC:EM73WX»")
    assert p.kind == "loc_tag" and p.grid == "EM73WX"
    p = parse_position_text("WX: 0420z Glendale, AZ (DM33vo) - Mostly clear")
    assert p.kind == "bare_grid" and p.grid == "DM33VO"
    p = parse_position_text("FM4TI    Op : Olivier Loc :FK94lp")
    assert p.kind == "bare_grid" and p.grid == "FK94LP"
    p = parse_position_text("KK4ODA <GPS:33.86000,-84.30000> EM73UU")
    assert p.kind == "gps" and p.lat == pytest.approx(33.86000)
    assert parse_position_text("Goodnight everyone...QSYing over to 40m calling.  73!") is None
    assert parse_position_text("QSO at 2300Hz on 14.111.000 until 19:45z") is None
    assert parse_position_text("KK4ODA chk in. Check out Tiles on the Air for VarAC") is None


def test_bare_grid_does_not_match_callsigns():
    for text in ("KK4ODA checking in", "W1AW de N9PMI", "CQ CQ de KQ4WUH", "Keith in Sanford, NC"):
        assert parse_position_text(text) is None


def test_format_roundtrip():
    tag = format_gps_tag(33.860001, -84.300002, 5)
    assert tag == "<GPS:33.86000,-84.30000>"
    assert parse_gps_tag(tag) == pytest.approx((33.86000, -84.30000))
