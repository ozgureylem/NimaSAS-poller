"""Post-RAM-clear identity verification.

The serial a gateway sees is typed in by a technician during the clear, not
read off the chassis (HANDOVER section 5e). SAS gives a host no way to tell a
mistyped serial from a real one, so the only check that exists is a
comparison against the label -- and this tool is it.

What is tested here is mostly the refusals and the exit codes, because this
will be run from a script across a floor and the exit code is what decides
whether a cabinet goes live.
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from verify_identity import (
    BAD_USAGE,
    MISMATCH,
    NOT_REPORTED,
    OK,
    compare,
    is_placeholder,
    load_roster,
)

SCRIPT = ROOT / "examples" / "verify_identity.py"


# --- the comparison -----------------------------------------------------

def test_a_matching_serial_passes():
    code, lines = compare("20884471", 7, "20884471", 7)
    assert code == OK
    assert any("serial OK" in ln for ln in lines)


def test_leading_zeros_are_not_a_mismatch():
    """A label is read by eye and typed; a SAS field is zero-padded by the
    cabinet. Failing this would send a technician to re-clear a correctly
    configured machine, destroying working configuration to fix nothing."""
    assert compare("0020884471", None, "20884471")[0] == OK
    assert compare("20884471", None, "0020884471")[0] == OK


def test_whitespace_is_not_a_mismatch():
    assert compare("  20884471 ", None, "20884471")[0] == OK


def test_a_different_serial_is_a_mismatch_and_says_what_it_cannot_tell():
    code, lines = compare("418305962", None, "20884471")
    assert code == MISMATCH
    note = " ".join(lines)
    assert "MISMATCH" in note
    assert "indistinguishable from a swapped machine" in note


@pytest.mark.parametrize("blank", [None, "", "   ", "0", "000000000", "N/A"])
def test_a_blank_field_is_its_own_outcome_not_a_mismatch(blank):
    """Left empty at the clear is a different fault from typed wrong, and it
    has a different fix. Collapsing them would send the technician looking
    for the wrong thing."""
    assert is_placeholder(blank)
    code, lines = compare(blank, None, "20884471")
    assert code == NOT_REPORTED
    assert "BLANK" in " ".join(lines)


def test_a_real_serial_is_not_a_placeholder():
    for serial in ("20884471", "418305962", "000106719"):
        assert not is_placeholder(serial)


def test_asset_zero_is_unset_not_a_mismatch():
    code, lines = compare("20884471", 0, "20884471", 7)
    assert code == NOT_REPORTED
    assert "unset value" in " ".join(lines)


def test_a_wrong_asset_is_a_mismatch():
    assert compare("20884471", 8, "20884471", 7)[0] == MISMATCH


def test_a_serial_fault_outranks_an_asset_fault():
    """Both wrong: report the serial, because that is the key the ticket
    binding uses and the one that must be fixed first."""
    code, lines = compare("999", 8, "20884471", 7)
    assert code == MISMATCH
    assert "SERIAL MISMATCH" in lines[0]


def test_checking_nothing_is_a_usage_error_not_a_pass():
    """The dangerous failure: a scripted floor walk that exits 0 on every
    cabinet because nobody passed an expectation."""
    code, lines = compare("20884471", 7)
    assert code == BAD_USAGE
    assert "nothing to check" in " ".join(lines)


# --- the roster ---------------------------------------------------------

def _roster(tmp_path, rows, fields=("machine", "serial", "asset", "note")):
    path = tmp_path / "floor.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(fields))
        w.writeheader()
        for row in rows:
            w.writerow(row)
    return path


def test_a_roster_row_is_found_by_machine(tmp_path):
    path = _roster(tmp_path, [
        {"machine": "7", "serial": "20884471", "asset": "7", "note": "left"},
        {"machine": "8", "serial": "20884470", "asset": "8", "note": "right"},
    ])
    assert load_roster(path, "7") == ("20884471", 7)
    assert load_roster(path, "8") == ("20884470", 8)


def test_a_missing_asset_column_value_is_allowed(tmp_path):
    """Plenty of floors will have labels before they have asset numbers."""
    path = _roster(tmp_path, [
        {"machine": "7", "serial": "20884471", "asset": "", "note": ""}])
    assert load_roster(path, "7") == ("20884471", None)


def test_a_duplicate_machine_is_refused_rather_than_guessed(tmp_path):
    """Picking one would validate a cabinet against the wrong label."""
    path = _roster(tmp_path, [
        {"machine": "7", "serial": "20884471", "asset": "7", "note": ""},
        {"machine": "7", "serial": "20884479", "asset": "7", "note": ""}])
    with pytest.raises(ValueError, match="2 rows"):
        load_roster(path, "7")


def test_an_unknown_machine_is_refused(tmp_path):
    path = _roster(tmp_path, [
        {"machine": "7", "serial": "20884471", "asset": "7", "note": ""}])
    with pytest.raises(ValueError, match="no row for machine"):
        load_roster(path, "99")


def test_a_roster_without_a_serial_column_says_what_it_has(tmp_path):
    path = _roster(tmp_path, [{"machine": "7", "sn": "20884471"}],
                   fields=("machine", "sn"))
    with pytest.raises(ValueError, match="'serial' column"):
        load_roster(path, "7")


# --- the command line ---------------------------------------------------

def test_it_refuses_to_run_with_no_expectation():
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "/dev/null"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == BAD_USAGE
    assert "nothing to check against" in out.stderr


def test_a_roster_without_machine_is_refused(tmp_path):
    path = _roster(tmp_path, [
        {"machine": "7", "serial": "20884471", "asset": "7", "note": ""}])
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "/dev/null", "--roster", str(path)],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == BAD_USAGE
    assert "--roster needs --machine" in out.stderr


def test_an_address_is_required_unless_scanning(tmp_path):
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "/dev/null",
         "--expect-serial", "20884471"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == BAD_USAGE


def test_help_documents_the_exit_codes():
    """It will be run from a script across a floor; the exit code is the
    whole interface."""
    out = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    for code in ("0", "1", "2", "3", "4"):
        assert f"    {code}  " in out.stdout


def test_it_reuses_the_pollers_identity_reader_rather_than_copying_it():
    """A second implementation would drift from the poller's, and the one
    place they disagreed would be the one that mattered."""
    source = SCRIPT.read_text()
    assert "from sql_poll_logger import read_identity" in source
    assert "def read_identity" not in source


def test_it_never_writes_to_the_machine():
    """Matched as METHOD CALLS, not as substrings.

    The first version of this test blacklisted the substring 'set_' and
    failed on `identity.get("asset_number")`. That is the same mistake twice
    in this repository -- a substring guard that fires on an unrelated
    identifier -- so this one looks for `.name(` and nothing else.
    """
    import re
    source = SCRIPT.read_text()
    mutating = re.compile(
        r"\.(redeem_ticket|aft_transfer_funds|aft_interrogate_transfer"
        r"|reset_handpay|aft_register_gaming_machine|enable_[a-z_]+"
        r"|set_[a-z_]+)\s*\(")
    found = mutating.findall(source)
    assert not found, f"these change the machine: {found}"
