"""An unsupported meter must not stop a machine writing meter rows.

Section 4.4 requires a machine that does not support a long poll to ignore
it silently, so a single-meter poll gives a timeout for "unsupported" and a
timeout for "no answer" -- the same byte stream for two different facts.
The sweep used to treat both as cycle-aborting, which meant a cashless
machine (no hopper, so LP 0x2C never answers) aborted every cycle before
writing a single row. Found on an EGT Premier.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

from saspy.exceptions import SASChecksumError, SASTimeoutError
from sql_poll_logger import METER_SUPPORT, _MeterPollFailure, _MeterSupport


def test_a_poll_that_never_answered_is_treated_as_unsupported():
    support = _MeterSupport()
    assert not support.is_known_good("send_meter(SEND_CURRENT_HOPPER_LEVEL)")
    support.record_unsupported("send_meter(SEND_CURRENT_HOPPER_LEVEL)")
    assert "send_meter(SEND_CURRENT_HOPPER_LEVEL)" in support.unsupported


def test_a_poll_that_answered_before_is_known_good():
    """This is the whole distinction: silence from something that used to
    answer is a real failure, and must keep aborting the cycle."""
    support = _MeterSupport()
    support.record_success("send_meter(SEND_TOTAL_COIN_IN_METER)")
    assert support.is_known_good("send_meter(SEND_TOTAL_COIN_IN_METER)")


def test_success_clears_a_previous_unsupported_mark():
    """A meter can start answering -- a printer fitted, a hopper installed --
    and must not stay written off for the life of the process."""
    support = _MeterSupport()
    support.record_unsupported("send_meter(SEND_CURRENT_HOPPER_LEVEL)")
    support.record_success("send_meter(SEND_CURRENT_HOPPER_LEVEL)")
    assert support.is_known_good("send_meter(SEND_CURRENT_HOPPER_LEVEL)")
    assert "send_meter(SEND_CURRENT_HOPPER_LEVEL)" not in support.unsupported


def test_an_unsupported_meter_returns_none_instead_of_aborting():
    """The behaviour the EGT Premier needed: a hopper meter that never
    answers NULLs its column and the sweep carries on."""
    from sql_poll_logger import _poll_optional

    support = _MeterSupport()

    def never_answers():
        raise SASTimeoutError("got 0/8 bytes before timeout")

    assert _poll_optional(support, "send_meter(HOPPER)", never_answers) is None
    assert "send_meter(HOPPER)" in support.unsupported


def test_a_meter_that_stops_answering_still_aborts():
    """Not leniency for its own sake. Something that answered and then went
    silent is a real failure, and a row half-fresh misrepresents polled_at.
    """
    from sql_poll_logger import _poll_optional

    support = _MeterSupport()
    support.record_success("send_meter(COIN_IN)")

    def never_answers():
        raise SASTimeoutError("got 0/8 bytes before timeout")

    with pytest.raises(_MeterPollFailure):
        _poll_optional(support, "send_meter(COIN_IN)", never_answers)


def test_a_checksum_error_is_never_treated_as_unsupported():
    """Section 4.4 specifies silence, not corruption. Garbled bytes mean a
    link problem, which must abort however new the meter is."""
    from sql_poll_logger import _poll_optional

    support = _MeterSupport()

    def garbled():
        raise SASChecksumError(expected=0x1234, actual=0x5678)

    with pytest.raises(_MeterPollFailure):
        _poll_optional(support, "send_meter(ANYTHING)", garbled)


def test_a_successful_poll_is_recorded_and_returned():
    from sql_poll_logger import _poll_optional

    support = _MeterSupport()
    assert _poll_optional(support, "send_meter(COIN_IN)", lambda: 21388883) == 21388883
    assert support.is_known_good("send_meter(COIN_IN)")


def test_only_a_timeout_means_unsupported():
    """A checksum error is a real fault whatever the machine supports -- 4.4
    specifies silence, not corruption, so the two must not be conflated.
    """
    assert issubclass(SASTimeoutError, Exception)
    assert not issubclass(SASChecksumError, SASTimeoutError)


def test_the_module_level_ledger_exists_for_the_running_poller():
    """The poller keeps one ledger for the life of the process, so a meter
    only has to prove itself once."""
    assert isinstance(METER_SUPPORT, _MeterSupport)
