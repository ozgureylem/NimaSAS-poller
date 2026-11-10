"""Commissioning one gateway to one EGM.

The failure this guards against is not subtle and not recoverable from the
data afterwards: a gateway keyed into the wrong cabinet authenticates
perfectly and reports someone else's money. The specification's identity
check then fires forever on a floor nobody can locate.

So almost everything here is a refusal.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))
sys.path.insert(0, str(ROOT / "deploy"))

from saspy.constants import HOSTNAME_ASSET_DIGITS, HOSTNAME_PREFIX

from commission_machine import (
    ED25519_PUBLIC_KEY_BYTES,
    EXIT_REFUSED,
    EXIT_USAGE,
    MIN_HMAC_KEY_BYTES,
    check_hmac_key,
    check_pool_key,
    hostname_for,
    read_key_material,
    write_secret,
)

SCRIPT = ROOT / "deploy" / "commission_machine.py"


# --- the hostname -------------------------------------------------------

def test_the_hostname_is_the_prefix_and_a_SIX_DIGIT_asset_number():
    """Operator, 2026-11-10: "naming is going to be RamSys# (# will be 6
    digits filled with zeros at the beginning of the asset number) So; ie
    'RamSys000246'". Was RamSYS and five digits.

    A fixed width is what makes a list of hostnames sort and read like a
    list of asset numbers: RamSys7, RamSys70 and RamSys700 sort into each
    other's company on a switch page; RamSys000007, RamSys000070 and
    RamSys000700 do not."""
    assert hostname_for(HOSTNAME_PREFIX, 7) == "RamSys000007"
    assert hostname_for(HOSTNAME_PREFIX, 246) == "RamSys000246"
    assert hostname_for(HOSTNAME_PREFIX, 999999) == "RamSys999999"


def test_the_default_prefix_is_the_operators_spelling():
    """RamSys, not RamSYS. A hostname is compared by eye on a switch page
    far more often than by a resolver, and two spellings in one fleet is
    the thing a convention exists to prevent."""
    assert HOSTNAME_PREFIX == "RamSys"
    assert HOSTNAME_ASSET_DIGITS == 6


def test_an_asset_WIDER_than_six_digits_is_never_truncated():
    """A truncated asset number is a DIFFERENT machine's name, which is the
    one mistake this naming scheme exists to prevent."""
    assert hostname_for(HOSTNAME_PREFIX, 1234567) == "RamSys1234567"


@pytest.mark.parametrize("unset", [None, 0, "", "0"])
def test_a_missing_asset_refuses_rather_than_naming_everything_the_same(unset):
    """Zero is the asset field's UNSET value, not an asset number.
    'RamSYS00000' on every unconfigured cabinet collides across the whole floor and is
    worse than having no hostname at all -- the hostname exists precisely so
    the switch page can tell the boxes apart."""
    with pytest.raises(ValueError, match="asset number"):
        hostname_for("RamSYS", unset)


def test_a_hostname_that_is_not_a_valid_dns_label_is_refused():
    with pytest.raises(ValueError, match="valid hostname"):
        hostname_for("-bad-", 7)


# --- key material -------------------------------------------------------

def test_a_short_hmac_key_is_refused():
    """A truncated paste is the usual cause, and a short key is the usual
    weakness in HMAC."""
    with pytest.raises(ValueError, match="at least"):
        check_hmac_key(bytes(range(16)))


def test_a_key_of_all_one_value_is_refused():
    """Placeholder or failed read, not a minted key."""
    with pytest.raises(ValueError, match="placeholder"):
        check_hmac_key(b"\x00" * 32)
    with pytest.raises(ValueError, match="placeholder"):
        check_hmac_key(b"AB" * 16)


def test_a_real_key_passes():
    check_hmac_key(bytes(range(MIN_HMAC_KEY_BYTES)))


def test_the_pool_key_must_be_exactly_an_ed25519_public_key():
    check_pool_key(bytes(range(ED25519_PUBLIC_KEY_BYTES)))
    for wrong in (31, 33, 64):
        with pytest.raises(ValueError, match="exactly"):
            check_pool_key(bytes(wrong))


def test_hex_and_raw_are_both_accepted(tmp_path):
    raw = bytes(range(32))
    as_hex = tmp_path / "k.hex"
    as_hex.write_text(raw.hex())
    assert read_key_material(as_hex, False, "k") == raw

    as_raw = tmp_path / "k.bin"
    as_raw.write_bytes(raw)
    # 32 random bytes are not valid hex, so this falls through to raw.
    assert read_key_material(as_raw, False, "k") == raw


def test_trailing_newline_in_a_hex_file_does_not_corrupt_the_key(tmp_path):
    """Every editor and every `echo` adds one."""
    raw = bytes(range(32))
    p = tmp_path / "k.hex"
    p.write_text(raw.hex() + "\n")
    assert read_key_material(p, False, "k") == raw


# --- secret files -------------------------------------------------------

def test_a_secret_is_created_0600_not_chmodded_afterwards(tmp_path):
    """Between an open() and a later chmod the file is world-readable. On a
    box whose whole point is that the key does not leak, that window is
    avoidable for free."""
    p = tmp_path / "sub" / "hmac.key"
    write_secret(p, b"x" * 32)
    assert oct(p.stat().st_mode)[-3:] == "600"
    assert p.read_bytes() == b"x" * 32

    source = SCRIPT.read_text()
    assert "os.O_CREAT, 0o600" in source or "os.O_TRUNC, 0o600" in source


def test_dry_run_writes_nothing(tmp_path):
    p = tmp_path / "hmac.key"
    write_secret(p, b"x" * 32, dry_run=True)
    assert not p.exists()


# --- the refusals that matter -------------------------------------------

def _run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=60)


def test_a_client_id_is_required_and_is_not_the_asset_number():
    out = _run("/dev/null", "--address", "1")
    assert out.returncode == EXIT_USAGE
    assert "--client-id is required" in out.stderr
    assert "NOT the asset number" in out.stderr


def test_a_client_id_longer_than_the_frame_header_is_refused():
    out = _run("/dev/null", "--address", "1", "--client-id", "X" * 25)
    assert out.returncode == EXIT_USAGE
    assert "24 bytes" in out.stderr


def test_commissioning_with_nothing_to_verify_against_is_refused():
    """The dangerous default: a floor walk that keys every gateway without
    ever checking which cabinet it is plugged into."""
    out = _run("/dev/null", "--address", "1", "--client-id", "TROP-042")
    assert out.returncode == EXIT_USAGE
    assert "nothing to verify this cabinet against" in out.stderr


def test_keys_are_never_accepted_as_command_line_arguments():
    """Arguments are visible in the process table to every user on the box
    and land in shell history."""
    source = SCRIPT.read_text()
    for forbidden in ('"--hmac-key"', "'--hmac-key'",
                      '"--pool-key"', "'--pool-key'"):
        assert forbidden not in source, \
            "key material must come from a file or stdin, never argv"
    assert "--hmac-key-file" in source and "--hmac-key-stdin" in source


def test_both_keys_cannot_come_from_stdin_at_once():
    out = _run("/dev/null", "--address", "1", "--client-id", "T",
               "--hmac-key-stdin", "--pool-key-stdin")
    assert out.returncode == EXIT_USAGE
    assert "only one" in out.stderr


def test_the_ceremony_record_never_contains_the_key():
    """Not even a hash: a hash of a secret this short is brute-forcible, so
    the log would weaken the thing it records."""
    source = SCRIPT.read_text()
    record = source[source.index("    record = {"):source.index("if args.log")]
    assert "hmac_key_bytes" in record
    for leak in ("hmac_key.hex()", '"hmac_key": hmac_key',
                 "sha256(hmac_key", "hashlib"):
        assert leak not in record


def test_it_documents_that_the_hmac_key_is_not_split_in_halves():
    """The specification's symmetric pre-shared key is ONE secret stored
    twice. Only the Ed25519 pair is genuinely split, and conflating them
    leads to inventing a key-splitting scheme that nothing asks for."""
    doc = SCRIPT.read_text()
    assert "no \"half on each side\"" in doc or 'no "half on each side"' in doc
    assert "SYMMETRIC" in doc
    assert "PUBLIC half" in doc


def test_identity_is_verified_before_any_key_is_written():
    """Order is the whole design. A gateway keyed into the wrong cabinet
    authenticates perfectly and reports someone else's money."""
    source = SCRIPT.read_text()
    assert source.index("REFUSING TO COMMISSION") < source.index("write_secret(hmac_path")


def _joined_source() -> str:
    """Source with adjacent string literals joined.

    Python concatenates `"a " "b"` at compile time, so a message wrapped
    across lines is one string to the user and two to a grep. Matching the
    raw text finds neither half of a sentence that is really there.
    """
    return re.sub(r'"\s*\n\s*"', "", SCRIPT.read_text())


def test_an_already_commissioned_gateway_is_not_silently_rekeyed():
    source = _joined_source()
    assert "--replace-keys" in source
    assert "ALREADY COMMISSIONED" in source
    assert "two live keys for one client_id" in source


# --- the headless first-boot fallback chain -----------------------------
#
# A technician plugs a gateway into a cabinet with no screen and no
# keyboard. It has to name itself, because the switch page is the only
# place anyone will ever see which box is which.

def test_there_is_no_separator():
    """A hyphen was drafted and withdrawn: a floor may already carry boxes
    named the old way, and two conventions are worse than either."""
    for name in (hostname_for("RamSYS", 246),
                 hostname_for("RamSYS", 0, serial="20884471"),
                 hostname_for("RamSYS", 0, mac="001BEB5CB05D")):
        assert "-" not in name, name
        assert name.startswith("RamSYS")


def test_an_asset_WIDER_than_five_digits_is_never_truncated():
    """A truncated asset number is a DIFFERENT MACHINE'S NAME, which is the
    one mistake this whole naming scheme exists to prevent. Padding is a
    minimum width, not a maximum."""
    assert hostname_for("RamSYS", 123456) == "RamSYS123456"
    assert hostname_for("RamSYS", 1234567) == "RamSYS1234567"


def test_the_serial_is_used_when_there_is_no_asset():
    """Second preference because it names the CABINET rather than its
    position: a swapped machine keeps its name, which is confusing but not
    wrong."""
    assert hostname_for("RamSYS", 0, serial="20884471") == "RamSYS20884471"


def test_the_MAC_is_the_last_resort_and_its_separators_go():
    """Last because it names the GATEWAY and says nothing about the cabinet
    at all. A box named after its MAC is a box nobody has finished
    commissioning, and it should look like one."""
    got = hostname_for("RamSYS", 0, serial=None, mac="00:1B:EB:5C:B0:5D")
    assert got == "RamSYS001BEB5CB05D"
    # Dashes appear instead of colons depending on where it is read from.
    assert hostname_for("RamSYS", 0, mac="001B-EB5C-B05D") == "RamSYS001BEB5CB05D"


def test_a_PLACEHOLDER_serial_falls_through_to_the_MAC():
    """§4.4 lets a machine ignore LP 0x54, and both identifiers are TYPED IN
    by a technician during the RAM clear -- this project's own lab cabinet
    was once left with a blank serial. 'the machine reported a serial' and
    'the machine reported a real serial' are different facts."""
    for absent in ("", "   ", "0", "n/a", "N/A", "unknown", "-"):
        assert hostname_for("RamSYS", 0, serial=absent,
                            mac="001BEB5CB05D") == "RamSYS001BEB5CB05D"


def test_the_preference_order_is_asset_then_serial_then_mac():
    """All three present: the asset must win. It is what the floor already
    calls a machine and is unique nationally by operator policy."""
    assert hostname_for(HOSTNAME_PREFIX, 246, serial="20884471",
                        mac="001BEB5CB05D") == "RamSys000246"
    assert hostname_for(HOSTNAME_PREFIX, 0, serial="20884471",
                        mac="001BEB5CB05D") == "RamSys20884471"


def test_with_nothing_usable_it_refuses_rather_than_inventing_a_name():
    with pytest.raises(ValueError, match="no usable identifier"):
        hostname_for("RamSYS", 0, serial="n/a", mac=None)


def test_a_BAD_PREFIX_is_reported_as_a_prefix_fault_not_a_cabinet_fault():
    """These are different faults and the first version conflated them.
    Given a perfectly good asset and a malformed prefix, it fell through to
    the serial, then the MAC, and reported 'no usable identifier' -- sending
    a technician to the machine to fix a string in a config file."""
    with pytest.raises(ValueError, match="valid hostname"):
        hostname_for("-bad-", 7, serial="20884471", mac="001BEB5CB05D")


def test_the_missing_asset_message_is_unchanged_where_no_fallback_was_offered():
    """The runbook quotes it, and a technician reading it knows to go and
    type the asset number in at the machine."""
    with pytest.raises(ValueError, match="asset number"):
        hostname_for("RamSYS", 0)


# --- protocol state, and the commit point -------------------------------
#
# Added 2026-11-10, the first work aimed at the gateway-to-server link.
# §10.3: "Recommissioning resets key and sequence state atomically on both
# sides. Otherwise a recommissioned gateway starting from zero has every
# frame rejected." The tool installed keys and never touched sequence
# state, because there was none.

import json          # noqa: E402
import time          # noqa: E402

from commission_machine import fresh_protocol_state  # noqa: E402


def _commissioned(dirpath, client_id="TROP-042", seq_tx=0, pool_epoch=0):
    """A gateway.json as a previous ceremony would have left it."""
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "hmac.key").write_bytes(b"\xab" * MIN_HMAC_KEY_BYTES)
    state = fresh_protocol_state()
    state["seq_tx"] = seq_tx
    state["pool_epoch"] = pool_epoch
    (dirpath / "gateway.json").write_text(json.dumps({
        "client_id": client_id,
        "sas_address": 1,
        "port": "/dev/ttyS0",
        "commissioned_at": "2026-11-01T10:00:00+0000",
        "commission_id": "deadbeef" * 4,
        "protocol": state,
    }))


def _commission(tmp_path, *extra, keydir=None):
    keydir = keydir or (tmp_path / "keys")
    key = tmp_path / "k.hex"
    # Varied bytes on purpose: the tool rejects an all-one-value key as a
    # placeholder or a failed read, and it is right to.
    key.write_text(bytes(range(MIN_HMAC_KEY_BYTES)).hex())
    return subprocess.run(
        [sys.executable, str(SCRIPT), "/dev/null",
         "--client-id", "TROP-042", "--skip-identity-check",
         "--hmac-key-file", str(key), "--key-dir", str(keydir),
         "--dry-run", *extra],
        capture_output=True, text=True, timeout=120)


def test_fresh_protocol_state_starts_every_counter_at_a_known_value():
    """A commissioning IS the moment both sides agree to start again, so
    nothing may be carried over. A stale pool_epoch silently refuses every
    new pool batch (§7.2); a stale seq_tx has the server reject every
    frame (§10.3). Neither failure says what it is."""
    st = fresh_protocol_state()
    assert st["seq_tx"] == 0
    assert st["seq_rx_floor"] == 0
    assert st["pool_epoch"] == 0
    assert st["batch_uuid"] is None
    assert st["rotation_in_progress"] is None, (
        "a freshly commissioned gateway must not boot believing a key "
        "rotation is half-finished")


def test_an_ALREADY_COMMISSIONED_gateway_is_refused(tmp_path):
    """§10.5: reassignment is always unassign-then-assign. Re-keying a live
    unit invalidates the key the server holds; if the server was not told,
    the gateway goes silent and the floor loses a machine's accounting
    until somebody notices."""
    keys = tmp_path / "keys"
    _commissioned(keys)
    proc = _commission(tmp_path, keydir=keys)
    assert proc.returncode == EXIT_REFUSED
    assert "ALREADY COMMISSIONED" in proc.stderr
    assert "TROP-042" in proc.stderr
    assert "--replace-keys" in proc.stderr, (
        "the refusal must say how to proceed deliberately")


def test_replace_keys_is_allowed_and_SAYS_the_counters_are_reset(tmp_path):
    """The reset is the dangerous half, so it is announced rather than
    done quietly: the server has to do the same thing in the same
    operation or every frame is rejected."""
    keys = tmp_path / "keys"
    _commissioned(keys, seq_tx=48291, pool_epoch=7)
    proc = _commission(tmp_path, "--replace-keys", keydir=keys)
    # It gets PAST the guard and then fails on /dev/null not being a serial
    # port, which is the right place for this test to stop: everything
    # after the guard needs a cabinet.
    assert "ALREADY COMMISSIONED" not in proc.stderr
    assert "RECOMMISSIONING" in proc.stdout
    assert "48291" in proc.stdout and "pool_epoch=7" in proc.stdout, (
        "the old counters must be shown; a technician needs to tell the "
        "server exactly what is being reset")
    assert "§10.3" in proc.stdout


def test_an_INTERRUPTED_ceremony_is_not_treated_as_commissioned(tmp_path):
    """Keys on disk with no gateway.json were never live. gateway.json is
    the commit marker and is written LAST, so this state means the previous
    run died mid-ceremony -- there is nothing to protect and no reason to
    make a technician pass --recommission to finish it."""
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "hmac.key").write_bytes(b"\xcd" * MIN_HMAC_KEY_BYTES)
    proc = _commission(tmp_path, keydir=keys)
    assert "ALREADY COMMISSIONED" not in proc.stderr, (
        "an interrupted ceremony was refused as though it were live")
    assert "could not open" in proc.stderr, (
        "it should have got past the guard and on to the cabinet")


def test_an_UNPARSEABLE_gateway_json_does_not_lock_the_unit_out(tmp_path):
    """A corrupt marker is not a live commissioning. Refusing on it would
    strand a gateway that can only be fixed by a technician deleting a
    file he has no reason to know about."""
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "gateway.json").write_text("{ this is not json")
    proc = _commission(tmp_path, keydir=keys)
    assert "will not parse" in proc.stderr
    assert "ALREADY COMMISSIONED" not in proc.stderr


def test_write_secret_leaves_no_temporary_file_and_is_owner_only(tmp_path):
    """The write is now atomic-rename plus fsync, so the failure mode to
    check is a .tmp left behind holding key material at the wrong mode."""
    target = tmp_path / "sub" / "hmac.key"
    write_secret(target, b"\x01" * 32)
    assert target.exists()
    assert oct(target.stat().st_mode)[-3:] == "600"
    leftovers = [p.name for p in (tmp_path / "sub").iterdir()
                 if p.name != "hmac.key"]
    assert not leftovers, f"temporary files left holding key material: {leftovers}"


def test_write_secret_replaces_an_existing_file_in_place(tmp_path):
    target = tmp_path / "hmac.key"
    write_secret(target, b"\x01" * 32)
    write_secret(target, b"\x02" * 32)
    assert target.read_bytes() == b"\x02" * 32
    assert oct(target.stat().st_mode)[-3:] == "600"
