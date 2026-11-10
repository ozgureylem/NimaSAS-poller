"""The per-note currency tag, and the two tables it is read through.

SAS 6.02 has exactly one place where a note's currency appears: the country
code in LP 0x48. The cumulative bill meters are by face value alone, so on a
floor accepting more than one currency they cannot be separated afterwards
at all. That makes Tables C-5 and C-6 load-bearing, and a transcription slip
in either one is the same class of error as the meter mis-tagging found in
the migration specification -- a plausible name attached to the wrong code.

Both tables here were checked against docs/spec/SAS602.pdf by parsing the
extracted text: all 39 named country codes and all 24 denomination codes
matched. These tests pin the result so it stays matched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

from bill_in_watch import (
    BILL_EXCEPTIONS, COUNTRY_CODES, DENOMINATION_UNITS, country, denomination)


# --- Table C-5 ----------------------------------------------------------

def test_the_country_table_runs_from_unknown_to_euro():
    assert set(COUNTRY_CODES) == set(range(0, 40))
    assert COUNTRY_CODES[0] == "Unknown country code"
    assert COUNTRY_CODES[39] == "Euro"


@pytest.mark.parametrize("code, name", [
    (7, "Canada"), (15, "Great Britain"), (25, "Mexico"),
    (37, "United States"), (38, "Holland"),
])
def test_country_codes_that_are_easy_to_shift_by_one(code, name):
    assert COUNTRY_CODES[code] == name


def test_belize_is_not_in_table_c5():
    """The whole reason this tool exists. A validator reflashed for BZD has
    nothing correct to report and must send 00, or 37 United States -- wrong
    by exactly half at the 2:1 peg -- or a reserved code."""
    assert not any("beliz" in n.lower() for n in COUNTRY_CODES.values())


def test_the_reserved_range_is_named_as_reserved_not_guessed():
    for code in range(40, 48):
        assert "reserved" in country(code).lower()


def test_a_code_outside_the_table_says_so_loudly():
    """Silence here would be the failure mode: a validator reporting an
    undefined country must not be rendered as though SAS defined it."""
    assert "NOT IN TABLE C-5" in country(55)
    assert "55" in country(55)


# --- Table C-6 ----------------------------------------------------------

def test_the_denomination_table_has_all_twenty_four_codes():
    assert set(DENOMINATION_UNITS) == set(range(0, 24))


@pytest.mark.parametrize("code, units", [
    (0, 1), (1, 2), (2, 5), (3, 10), (4, 20), (5, 25), (6, 50), (7, 100),
    (10, 500), (23, 1_000_000),
])
def test_denomination_codes_against_the_specification(code, units):
    assert DENOMINATION_UNITS[code] == units


def test_denominations_are_reporting_currency_units_not_dollars():
    """Table C-6 labels the column 'US Denomination' and then its own note
    says a '$1 bill' means 100 minor units of the reporting currency. The
    wording that follows -- 'legacy bill reporting issues may take
    precedence' -- is why this is never printed as a dollar amount."""
    assert "units of the reporting currency" in denomination(2)
    assert "$" not in denomination(2)


def test_an_undefined_denomination_says_so():
    assert "NOT IN TABLE C-6" in denomination(31)


# --- which exceptions mean "a note went in" -----------------------------

def test_every_bill_exception_is_watched_including_the_catch_all():
    """47-4E and 50 are the per-denomination forms; 4F is the catch-all and
    §7.11 prefers it exclusively in RTE mode. A host that watches only the
    specific codes sees nothing at all on an RTE machine."""
    assert BILL_EXCEPTIONS == {0x47, 0x48, 0x49, 0x4A, 0x4B, 0x4C,
                               0x4D, 0x4E, 0x4F, 0x50}
    assert 0x4F in BILL_EXCEPTIONS


def test_the_handpay_and_validation_exceptions_are_not_mistaken_for_bills():
    for code in (0x51, 0x52, 0x57, 0x66, 0x67, 0x68, 0x6A):
        assert code not in BILL_EXCEPTIONS
