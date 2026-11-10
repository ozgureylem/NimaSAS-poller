"""Meters that go down because they are supposed to.

On 2026-10-28 the lab EGM lost power in the middle of a soak. The poller
survived it, recovered, and then logged "meter decrease detected" -- for
meters that had behaved exactly as the specification says they should.

"Games since last power reset" (C-7 0x25) counts up from zero and is reset
by a power cycle. It is neither a cumulative counter nor a gauge that
wanders; it is a third thing, and a drop in it is not an anomaly, it IS the
event. The same goes for "games since slot door closure" (0x26) and both
fields of LP 0x18.

The spurious burst is the small cost. The large one is outside this file:
the rollover correction in the Linux migration spec §8.2 responds to a
confirmed drop by prepending an unbounded generation digit, and the
corrected value is defined to be monotonic forever. Applied to a legitimate
reset of 13 -> 0 that yields 100,000,000, permanently. These have to be
excluded by name wherever missing-digit correction runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

from sql_poll_logger import (
    ALL_METER_FIELDS, DECREASE_CHECK_FIELDS, GAUGE_METER_FIELDS,
    RESET_ON_EVENT_METER_FIELDS)


@pytest.mark.parametrize("field", sorted(RESET_ON_EVENT_METER_FIELDS))
def test_a_reset_on_event_meter_is_a_real_column(field):
    assert field in ALL_METER_FIELDS


@pytest.mark.parametrize("field", sorted(RESET_ON_EVENT_METER_FIELDS))
def test_a_reset_on_event_meter_never_arms_the_decrease_burst(field):
    assert field not in DECREASE_CHECK_FIELDS


def test_the_power_up_counters_are_covered_through_both_long_polls():
    """LP 0x18 and Table C-7 0x25/0x26 report the same two figures. Catching
    one route and not the other would leave the burst still firing."""
    assert "lp18_games_since_power_up" in RESET_ON_EVENT_METER_FIELDS
    assert "c7_games_since_last_power_reset" in RESET_ON_EVENT_METER_FIELDS
    assert "lp18_games_since_door_closure" in RESET_ON_EVENT_METER_FIELDS
    assert "c7_games_since_slot_door_closure" in RESET_ON_EVENT_METER_FIELDS


def test_the_door_and_power_COUNT_meters_are_still_checked():
    """Table 7.2c calls these the "slot door opened meter" and "power reset
    meter" -- how many times each has happened. They only ever rise, so a
    drop in one is a genuine anomaly and must keep arming the burst."""
    for field in ("lp1c_slot_door_opened", "lp1c_power_reset"):
        assert field not in RESET_ON_EVENT_METER_FIELDS
        assert field in DECREASE_CHECK_FIELDS


def test_the_three_categories_do_not_overlap():
    """Cumulative, gauge, and reset-on-event are different claims about a
    meter. Collapsing the last two into one would lose the reason the
    exclusion exists, which is the part that has to survive into §8.2."""
    assert not (GAUGE_METER_FIELDS & RESET_ON_EVENT_METER_FIELDS)
    checked = set(DECREASE_CHECK_FIELDS)
    assert not (checked & GAUGE_METER_FIELDS)
    assert not (checked & RESET_ON_EVENT_METER_FIELDS)


def test_the_core_counters_are_still_checked():
    for field in ("total_coin_in", "total_coin_out", "games_played",
                  "c7_total_coin_in_credits", "total_cancelled_credits"):
        assert field in DECREASE_CHECK_FIELDS
