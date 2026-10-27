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


# -- Derived meter labels (Table C-7 relationships) -------------------------


def test_a_zero_meter_never_takes_a_derived_label():
    """The rule the screen-verified table already applies to itself: a zero
    equals every other zero, so inheriting a name through one proves nothing.

    An earlier version of derive_meter_labels() broke this and labelled 0002,
    total jackpot, as "Total Won" -- because 0022 = 0001 + 0002 and 0002 was
    zero. Pinned here because the label was plausible-looking and wrong.
    """
    from saspy.constants import derive_meter_labels

    known = {0x0001: "Winnings from Paytable (Coin Out)", 0x0022: "Total Won"}
    values = {0x0001: 1616, 0x0022: 1616, 0x0002: 0}
    derived = derive_meter_labels(known, values)
    assert 0x0002 not in derived, derived.get(0x0002)
    assert all(values.get(code) for code in derived)


def test_a_declared_identity_is_inherited_unconditionally():
    """C-7 annotates 0010 as "[same as meter 0088]". One component, declared,
    so there is no caveat to attach.
    """
    from saspy.constants import derive_meter_labels

    derived = derive_meter_labels({0x0010: "Non-cashable Voucher Out"},
                                  {0x0010: 2910, 0x0088: 2910})
    label, basis = derived[0x0088]
    assert label == "Non-cashable Voucher Out"
    assert "C-7 identity" in basis
    assert "while" not in basis


def test_a_multi_component_relation_says_what_it_depends_on():
    """0086 equals 002C only while 008A -- debit tickets out -- is zero. The
    provenance must name 008A rather than presenting them as the same meter.
    """
    from saspy.constants import derive_meter_labels

    derived = derive_meter_labels({0x002C: "Cashable Voucher Out"},
                                  {0x002C: 18500, 0x0086: 18500, 0x008A: 0})
    _label, basis = derived[0x0086]
    assert "while 008A is zero" in basis


def test_a_real_sum_lends_its_name_to_no_component():
    """With two live components the total is a genuine sum, and equal to
    neither part. Nothing may be derived in either direction.
    """
    from saspy.constants import derive_meter_labels

    derived = derive_meter_labels({0x000D: "would-be label"},
                                  {0x000D: 3500, 0x0080: 2500, 0x0084: 1000})
    assert 0x0080 not in derived and 0x0084 not in derived


def test_labels_travel_along_a_chain_and_the_caveat_is_said_once():
    """The screen names 002D; 0088 takes it as a SAS-only twin; 0010 takes it
    from 0088 as a declared identity. And a caveat picked up mid-chain is
    stated once -- an earlier version produced "while 008A is zero while 008A
    is zero".
    """
    from saspy.constants import derive_meter_labels

    derived = derive_meter_labels(
        {0x002D: "Non-cashable Voucher Out", 0x002C: "Cashable Voucher Out"},
        {0x002D: 2910, 0x0088: 2910, 0x0010: 2910,
         0x002C: 18500, 0x0086: 18500, 0x000E: 18500, 0x008A: 0})
    assert derived[0x0010][0] == "Non-cashable Voucher Out"
    assert "0088" in derived[0x0010][1] or "002D" in derived[0x0010][1]
    assert derived[0x000E][1].count("while") == 1, derived[0x000E][1]


def test_derivation_never_overwrites_a_screen_verified_label():
    from saspy.constants import derive_meter_labels

    known = {0x002D: "Non-cashable Voucher Out", 0x0088: "something measured"}
    derived = derive_meter_labels(known, {0x002D: 2910, 0x0088: 2910})
    assert 0x0088 not in derived
