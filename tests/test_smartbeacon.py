from datetime import datetime, timedelta, timezone

from varmap.domain.grid import grid_to_latlon
from varmap.domain.smartbeacon import HARD_FLOOR_SECONDS, Fix, FixedIntervalPolicy, SmartBeaconPolicy

T0 = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def at(s):
    return T0 + timedelta(seconds=s)


def test_fixed_arms_then_waits_one_interval():
    p = FixedIntervalPolicy({"interval_seconds": 600, "only_if_moved": False})
    f = Fix(33.85, -84.29, at(0))
    d = p.evaluate(f, at(0))
    assert not d.send and d.reason == "armed" and d.next_due_seconds == 600   # no beacon on enable
    assert p.armed
    assert not p.evaluate(f, at(300)).send
    assert p.evaluate(f, at(600)).send


def test_smart_arms_then_waits():
    p = SmartBeaconPolicy({"min_interval_seconds": 60, "slow_rate_seconds": 900, "only_if_moved": False})
    f = Fix(33.85, -84.29, at(0), speed_kmh=0.0)
    d = p.evaluate(f, at(0))
    assert not d.send and d.reason == "armed"
    assert not p.evaluate(f, at(300), ).send
    assert p.evaluate(f, at(900)).reason == "rate"


def test_fixed_only_if_moved_stays_silent():
    p = FixedIntervalPolicy({"interval_seconds": 300, "only_if_moved": True, "min_move_m": 500})
    f = Fix(33.85, -84.29, at(0))
    p.mark_sent(f, at(0))
    assert p.evaluate(f, at(600)).reason == "not_moved"
    moved = Fix(33.86, -84.29, at(700))   # ~1.1 km north
    assert p.evaluate(moved, at(700)).reason == "moved"
    p.mark_sent(moved, at(700))
    for s in (1800, 36000, 360000):       # parked forever: never a keepalive
        d = p.evaluate(moved, at(700 + s))
        assert not d.send and d.reason == "not_moved" and d.next_due_seconds is None
    p2 = FixedIntervalPolicy({"interval_seconds": 300, "only_if_moved": False})
    p2.mark_sent(f, at(0))
    assert p2.evaluate(f, at(300)).reason == "fixed"


def test_hard_floor_cannot_be_lowered():
    p = SmartBeaconPolicy({"min_interval_seconds": 1})
    assert p.min_interval == HARD_FLOOR_SECONDS
    p.mark_sent(Fix(33.85, -84.29, at(0)), at(0))
    d = p.evaluate(Fix(34.0, -84.0, at(30), speed_kmh=100, course_deg=0), at(30))
    assert not d.send and d.reason == "floor"


def test_smart_stationary_silent_unless_only_if_moved_off():
    p = SmartBeaconPolicy({"min_interval_seconds": 60, "slow_rate_seconds": 1800})
    p.mark_sent(Fix(33.85, -84.29, at(0), speed_kmh=0.0), at(0))
    for s in (120, 600, 1500, 1800, 36000):
        d = p.evaluate(Fix(33.85, -84.29, at(s), speed_kmh=0.0), at(s))
        assert not d.send, (s, d)
    p2 = SmartBeaconPolicy({"min_interval_seconds": 60, "slow_rate_seconds": 1800, "only_if_moved": False})
    p2.mark_sent(Fix(33.85, -84.29, at(0), speed_kmh=0.0), at(0))
    assert not p2.evaluate(Fix(33.85, -84.29, at(1500), speed_kmh=0.0), at(1500)).send
    assert p2.evaluate(Fix(33.85, -84.29, at(1800), speed_kmh=0.0), at(1800)).reason == "rate"


def test_smart_rate_scales_with_speed():
    p = SmartBeaconPolicy({"min_interval_seconds": 60, "fast_rate_seconds": 120, "fast_speed_kmh": 90,
                           "slow_rate_seconds": 1800, "slow_speed_kmh": 5})
    assert p.rate_for_speed(90) == 120
    assert p.rate_for_speed(200) == 120
    assert p.rate_for_speed(45) == 240
    assert p.rate_for_speed(0) == 1800
    assert p.rate_for_speed(None) == 1800


def test_smart_moving_sends_at_rate():
    p = SmartBeaconPolicy({"min_interval_seconds": 60, "fast_rate_seconds": 120, "fast_speed_kmh": 90})
    p.mark_sent(Fix(33.85, -84.29, at(0), speed_kmh=90, course_deg=0), at(0))
    assert not p.evaluate(Fix(33.8725, -84.29, at(100), speed_kmh=90, course_deg=0), at(100)).send
    d = p.evaluate(Fix(33.88, -84.29, at(125), speed_kmh=90, course_deg=0), at(125))
    assert d.send and d.reason == "rate"


def test_smart_corner_peg():
    p = SmartBeaconPolicy({"min_interval_seconds": 60, "min_turn_time_seconds": 60, "turn_min_deg": 30,
                           "turn_slope": 255, "fast_rate_seconds": 600, "fast_speed_kmh": 90})
    p.mark_sent(Fix(33.85, -84.29, at(0), speed_kmh=60, course_deg=0), at(0))
    # 90 s later, 1 km away, heading changed by 90 deg -> threshold = 30 + 255/60 = 34.25
    d = p.evaluate(Fix(33.859, -84.29, at(90), speed_kmh=60, course_deg=90), at(90))
    assert d.send and d.reason == "turn"
    p.mark_sent(Fix(33.859, -84.29, at(90), speed_kmh=60, course_deg=90), at(90))
    d = p.evaluate(Fix(33.868, -84.29, at(180), speed_kmh=60, course_deg=100), at(180))
    assert not d.send


def test_smart_grid_change_with_hysteresis():
    p = SmartBeaconPolicy({"min_interval_seconds": 60, "grid_dwell_seconds": 90, "grid_edge_margin_m": 300,
                           "slow_rate_seconds": 3600})
    lat, lon = grid_to_latlon("EM73UU")
    p.mark_sent(Fix(lat, lon, at(0), speed_kmh=0), at(0))
    n_edge = lat + (1 / 48)
    # ~55 m across the northern edge into EM73UV: never accepted (edge margin)
    barely = Fix(n_edge + 0.0005, lon, at(100), speed_kmh=0)
    assert not p.evaluate(barely, at(100)).send
    assert not p.evaluate(barely, at(300)).send
    # Well inside the new cell: same pending grid, dwell already satisfied -> send
    deep = Fix(n_edge + 0.01, lon, at(400), speed_kmh=0)
    d = p.evaluate(deep, at(400))
    assert d.send and d.reason == "grid_change"


def test_smart_grid_change_requires_dwell():
    p = SmartBeaconPolicy({"min_interval_seconds": 60, "grid_dwell_seconds": 90, "grid_edge_margin_m": 300,
                           "slow_rate_seconds": 3600})
    lat, lon = grid_to_latlon("EM73UU")
    p.mark_sent(Fix(lat, lon, at(0), speed_kmh=0), at(0))
    deep = Fix(lat + (1 / 48) + 0.01, lon, at(100), speed_kmh=0)
    assert not p.evaluate(deep, at(100)).send       # first sighting starts the dwell
    assert not p.evaluate(deep, at(150)).send       # 50 s < 90 s
    assert p.evaluate(deep, at(200)).reason == "grid_change"


def test_smart_no_speed_degrades_gracefully():
    p = SmartBeaconPolicy({"min_interval_seconds": 60, "slow_rate_seconds": 900,
                           "min_move_m": 500, "grid_change_triggers": False})
    p.mark_sent(Fix(33.85, -84.29, at(0)), at(0))
    assert not p.evaluate(Fix(33.90, -84.29, at(300)), at(300)).send   # moved 5.5 km but slow rate not reached
    d = p.evaluate(Fix(33.90, -84.29, at(900)), at(900))
    assert d.send and d.reason == "rate"
    p.mark_sent(Fix(33.90, -84.29, at(900)), at(900))
    assert not p.evaluate(Fix(33.90, -84.29, at(9000)), at(9000)).send   # parked again: silent
