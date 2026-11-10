"""The Table A-1 names that mislead, and the rule about not renaming them.

The question that prompted this: should "ticket transfer complete" be
renamed to something clearer? No -- it is the specification's own wording and
a log that renames a spec code cannot be cross-referenced against the spec.
But every one of these four has already made somebody on this project draw
the wrong conclusion, so the clarification is carried beside the name rather
than instead of it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from saspy.constants import (
    EXCEPTION_CLARIFICATIONS,
    GENERAL_POLL_EXCEPTIONS,
    clarify_exception,
    describe_exception,
)


def test_the_spec_name_is_never_replaced():
    """Anyone cross-referencing the SAS document must find the same words."""
    assert describe_exception(0x68) == "ticket transfer complete"
    assert describe_exception(0x2B) == "bill rejected"


def test_every_clarified_code_is_a_real_table_a1_code():
    for code in EXCEPTION_CLARIFICATIONS:
        assert code in GENERAL_POLL_EXCEPTIONS, f"0x{code:02X} is not in Table A-1"


def test_only_codes_that_actually_misled_are_listed():
    """Not a glossary. Four entries, each one earned."""
    assert set(EXCEPTION_CLARIFICATIONS) == {0x2B, 0x51, 0x68, 0x70}


def test_ordinary_codes_get_no_clarification():
    """A note on every code is a note nobody reads."""
    for code in (0x7E, 0x7F, 0x11, 0x4F, 0x67):
        assert clarify_exception(code) is None


def test_0x68_says_it_does_not_mean_accepted():
    note = clarify_exception(0x68)
    assert "does NOT" in note.replace("DOES NOT", "does NOT")
    assert "0x71/FF" in note


def test_0x2B_points_at_the_ticket_window():
    note = clarify_exception(0x2B)
    assert "RETURNED TICKET" in note
    assert "0x67" in note and "60s" in note


def test_0x70_says_events_were_lost():
    assert "LOST" in clarify_exception(0x70)


def test_0x51_distinguishes_a_queue_entry_from_a_lockup():
    note = clarify_exception(0x51)
    assert "QUEUE ENTRY" in note
    assert "not necessarily a live lockup" in note


def test_an_unknown_code_clarifies_to_nothing_rather_than_raising():
    assert clarify_exception(0xFE) is None


def test_the_poller_prints_the_clarification_beside_the_name():
    source = (ROOT / "examples" / "sql_poll_logger.py").read_text()
    assert "clarify_exception" in source
    # beside, not instead of
    assert source.count("describe_exception(code)") >= 2
