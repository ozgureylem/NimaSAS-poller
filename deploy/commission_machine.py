#!/usr/bin/env python3
"""Commission one gateway to one EGM: verify, name, key, record.

Run over key-only SSH on the commissioning network, with no screen on the
gateway. One cabinet at a time. Everything it does is in this order and it
stops at the first thing that is wrong:

  1. find the EGM on the serial line and read its identity
  2. CHECK that identity against the roster built from the labels
  3. set the hostname so the switch page says which cabinet this is
  4. install the per-gateway HMAC key and the server's Ed25519 PUBLIC key
  5. write the client_id configuration
  6. append a record of the ceremony

WHY THE ORDER MATTERS. Step 2 before step 4 is the whole point. Keying a
gateway that is plugged into the wrong cabinet produces a unit that
authenticates perfectly and reports someone else's money, and the spec's
identity check (§8.2) then fires forever on a floor nobody can find. The
serial is typed in by a technician during the RAM clear (HANDOVER §5e), so
it is exactly the thing that must be verified at the moment of
commissioning and never assumed.

TWO DIFFERENT KEYS, AND THEY ARE NOT HALVES OF EACH OTHER.

  --hmac-key   SYMMETRIC, per gateway, HMAC-SHA256 (§10.4). The server holds
               THE SAME key. There is no "half on each side": a symmetric
               pre-shared key is one secret stored twice. Minted by the
               server at the moment of commissioning (§10.5) and entered
               here; never transmitted over the floor segment.

  --pool-key   The PUBLIC half of the server's Ed25519 signing pair (§7.3).
               This one genuinely is split -- the server keeps the private
               half and no gateway ever holds it, which is what stops a
               compromised gateway minting its own validation numbers. A
               public key is not a secret; it is installed with the same
               care only because installing the WRONG one breaks pool
               verification silently.

NEITHER KEY IS ACCEPTED ON THE COMMAND LINE. Arguments are visible in the
process table to every user on the box and land in shell history. Both are
read from a file or from standard input.

    # key material in files, mode 0600, deleted afterwards
    sudo python3 deploy/commission_machine.py /dev/ttyS0 --scan \
        --roster floor.csv --machine 7 \
        --client-id TROP-042 \
        --hmac-key-file /run/key.hex --pool-key-file /run/pool.pub

    # or piped, so it never touches the disk of the commissioning laptop
    printf '%s' "$HMAC_HEX" | sudo python3 deploy/commission_machine.py \
        /dev/ttyS0 --scan --roster floor.csv --machine 7 \
        --client-id TROP-042 --hmac-key-stdin --pool-key-file /run/pool.pub

    # see every step without changing anything
    sudo python3 deploy/commission_machine.py ... --dry-run

client_id IS NOT THE ASSET NUMBER. The specification defines it as the
stable logical identity of a floor POSITION (for example TROP-042) that
survives hardware replacement, and §8.2 is explicit that identity checking
uses the manufacturer ID and serial, "not the operator-writable asset
number". The hostname uses the asset because a hostname is a label for
finding the box on a switch page, not an identity claim. Those are
different jobs and this tool keeps them apart.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

from saspy.constants import gateway_hostname
from saspy.client import SASClient
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

from sql_poll_logger import read_identity
from verify_identity import OK, compare, find_address, load_roster

EXIT_OK = 0
EXIT_IDENTITY = 1
EXIT_USAGE = 2
EXIT_NO_ANSWER = 4
EXIT_REFUSED = 5

# HMAC-SHA256 keys shorter than the hash are the usual weakness; the block
# size is 64 bytes and anything under 32 is below the security level the
# construction is being chosen for.
MIN_HMAC_KEY_BYTES = 32
ED25519_PUBLIC_KEY_BYTES = 32

# Hostnames are a DNS label: letters, digits and hyphens, not starting or
# ending with a hyphen, 63 characters at most.
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def hostname_for(prefix: str, asset, *, serial: str | None = None,
                 mac: str | None = None) -> str:
    """'RamSys' + asset number, with the fallbacks a headless box needs.

    SIX DIGITS, ZERO-PADDED, no separator -- 'RamSys000246'. Set by the
    operator on 2026-11-10, replacing RamSYS and five digits. The FALLBACK
    CHAIN below is for
    the first-boot case where a technician plugs a gateway into a cabinet
    with no screen and no keyboard.

    The order is a PREFERENCE, not a tie-break: asset, then serial, then the
    gateway's own MAC with the separators stripped. Asset because it is what
    the floor already calls a machine and is unique nationally by operator
    policy; serial because it names the CABINET rather than its position, so
    a swapped machine keeps its name -- confusing, but not wrong; MAC last
    because it names the GATEWAY and says nothing about the cabinet at all.
    A box named after its MAC is a box nobody has finished commissioning,
    and it should look like one.

    Refuses rather than inventing a name. 'RamSys0' on every unconfigured
    cabinet collides across the whole floor and is worse than no hostname at
    all; zero is the asset field's UNSET value, not an asset number.

    Lives in saspy.constants.gateway_hostname() so the first-boot path reads
    the same function. One convention, one place.
    """
    try:
        return gateway_hostname(prefix, asset=asset, serial=serial, mac=mac)
    except ValueError as e:
        # Keep the historical wording for the MISSING-ASSET case only: the
        # tests and the runbook both quote it, and a technician reading it
        # knows to go and type the asset number in at the machine.
        #
        # Not for a bad PREFIX, which is a configuration typo on the gateway
        # and has nothing to do with the cabinet. The first version of this
        # wrapper rewrote both into the asset message and would have sent
        # somebody to the machine to fix a string in a config file.
        if ("no usable identifier" in str(e)
                and serial is None and mac is None):
            raise ValueError(
                "the cabinet reports no asset number (or zero, which is the "
                "field's unset value). A hostname built from it would collide "
                "with every other unconfigured cabinet. Enter the asset number "
                "at the machine first, or pass --hostname explicitly.") from e
        raise


def read_key_material(path: Path | None, from_stdin: bool, what: str) -> bytes:
    """Hex or raw bytes, from a file or stdin. NEVER from a command line."""
    if from_stdin:
        data = sys.stdin.buffer.read()
    elif path is not None:
        data = path.read_bytes()
    else:
        raise ValueError(f"no {what} given")
    text = data.strip()
    try:
        return bytes.fromhex(text.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        # Not hex, so take it as raw bytes. A key file written by `head -c 32
        # /dev/urandom` is perfectly valid and is not hex.
        return bytes(data)


def check_hmac_key(key: bytes) -> None:
    if len(key) < MIN_HMAC_KEY_BYTES:
        raise ValueError(
            f"the HMAC key is {len(key)} bytes; HMAC-SHA256 wants at least "
            f"{MIN_HMAC_KEY_BYTES}. A short key is the usual weakness in this "
            "construction, and a truncated paste is the usual cause.")
    if len(set(key)) <= 2:
        raise ValueError(
            "the HMAC key is all one or two byte values. That is a "
            "placeholder or a failed read, not a minted key.")


def check_pool_key(key: bytes) -> None:
    if len(key) != ED25519_PUBLIC_KEY_BYTES:
        raise ValueError(
            f"an Ed25519 public key is exactly {ED25519_PUBLIC_KEY_BYTES} "
            f"bytes; this is {len(key)}. Check you are installing the PUBLIC "
            "half -- a private key or a PEM wrapper will not be this length.")


def write_secret(path: Path, data: bytes, dry_run: bool = False) -> None:
    """0600, owner only, created with those permissions from the start, and
    on the disk before this returns.

    Not chmod-after-write: between the create and the chmod the file is
    readable, and on a box where the whole point is that the key never
    leaks, that window is avoidable for free.

    FSYNC, AND FSYNC THE DIRECTORY TOO. The first version wrote and closed.
    On a gateway that is a micro-PC on a casino floor, the realistic next
    event after commissioning is somebody unplugging it to move it -- and a
    key that was written but never flushed is a unit that authenticates
    against nothing, discovered when the technician has left the building.
    The directory entry needs its own fsync or the file can exist with no
    name after a power cut.
    """
    if dry_run:
        print(f"    would write {len(data)} bytes to {path} (0600)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)          # atomic within the directory
    dirfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


# THE PROTOCOL STATE A COMMISSIONED GATEWAY MUST START FROM.
#
# §10.3: "Recommissioning resets key and sequence state atomically on both
# sides. Otherwise a recommissioned gateway starting from zero has every
# frame rejected." The first version of this tool installed the keys and
# never touched sequence state, because there was none -- so a re-keyed
# gateway would have come up with whatever counters its last life left
# behind, or with none at all, and the server would have refused every
# frame it sent. Neither failure says what it is.
#
#   seq_tx          §10.3, one monotonic counter per direction, in the frame
#                   header, checked before parsing. Ours starts at 0 and the
#                   first frame sends 1.
#   seq_rx_floor    the highest seq accepted FROM the server. A frame is
#                   rejected unless strictly greater.
#   pool_epoch      §7.2. A batch is accepted only if its epoch is strictly
#                   greater than this, or equal with a matching batch_uuid.
#                   A stale epoch here silently refuses every new pool.
#   batch_uuid      the active batch, for the idempotent-resend case.
#   rotation_in_progress
#                   §10.5. Set to the pending key id before acknowledging a
#                   ROTATE_KEY, cleared on the first frame verified under
#                   the new key. On boot with it set, try the new key first
#                   and fall back to the old.
#
# All of it is zeroed by a commissioning, because a commissioning IS the
# moment both sides agree to start again.
def fresh_protocol_state() -> dict:
    return {
        "seq_tx": 0,
        "seq_rx_floor": 0,
        "pool_epoch": 0,
        "batch_uuid": None,
        "rotation_in_progress": None,
    }


def set_hostname(name: str, dry_run: bool = False) -> None:
    if dry_run:
        print(f"    would set hostname to {name}")
        return
    subprocess.run(["hostnamectl", "set-hostname", name], check=True)
    # /etc/hosts too: without a 127.0.1.1 entry, sudo and anything else that
    # resolves the local hostname stalls on a DNS timeout.
    hosts = Path("/etc/hosts")
    lines = hosts.read_text().splitlines()
    out, seen = [], False
    for line in lines:
        if line.split()[:1] == ["127.0.1.1"]:
            out.append(f"127.0.1.1\t{name}")
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f"127.0.1.1\t{name}")
    hosts.write_text("\n".join(out) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=None)
    ap.add_argument("--scan", action="store_true",
                    help="find the SAS address by polling 1-8")
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)

    ap.add_argument("--roster", type=Path, default=None)
    ap.add_argument("--machine", default=None)
    ap.add_argument("--expect-serial", default=None)
    ap.add_argument("--expect-asset", type=int, default=None)
    ap.add_argument("--skip-identity-check", action="store_true",
                    help="commission WITHOUT verifying which cabinet this is. "
                         "Only for a bench unit attached to no floor machine.")

    ap.add_argument("--client-id", default=None,
                    help="the floor POSITION, e.g. TROP-042. Not the asset "
                         "number; see this tool's docstring.")
    ap.add_argument("--hostname-prefix", default="RamSys")
    ap.add_argument("--hostname", default=None,
                    help="override the derived name entirely")

    ap.add_argument("--hmac-key-file", type=Path, default=None)
    ap.add_argument("--hmac-key-stdin", action="store_true")
    ap.add_argument("--pool-key-file", type=Path, default=None)
    ap.add_argument("--pool-key-stdin", action="store_true")
    ap.add_argument("--key-dir", type=Path, default=Path("/etc/nimasas"))
    ap.add_argument("--replace-keys", action="store_true",
                    help="overwrite key material already on this gateway. The "
                         "spec's reassignment is unassign-then-assign, so a "
                         "second key on one unit should never happen by "
                         "accident.")

    ap.add_argument("--log", type=Path, default=None,
                    help="append the ceremony record, one JSON object a line")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.hmac_key_stdin and args.pool_key_stdin:
        print("only one of the two keys can come from stdin", file=sys.stderr)
        return EXIT_USAGE
    if not args.client_id:
        print("--client-id is required: the stable floor position, e.g. "
              "TROP-042.\n  It is NOT the asset number -- the specification "
              "says identity checking uses the serial, 'not the "
              "operator-writable asset number'.", file=sys.stderr)
        return EXIT_USAGE
    if len(args.client_id.encode()) > 24:
        print("client_id is 24 bytes at most (§10.4 frame header).",
              file=sys.stderr)
        return EXIT_USAGE

    expect_serial, expect_asset = args.expect_serial, args.expect_asset
    if args.roster:
        if not args.machine:
            print("--roster needs --machine", file=sys.stderr)
            return EXIT_USAGE
        try:
            expect_serial, expect_asset = load_roster(args.roster, args.machine)
        except (OSError, ValueError) as e:
            print(f"{e}", file=sys.stderr)
            return EXIT_USAGE
    if expect_serial is None and expect_asset is None and not args.skip_identity_check:
        print("nothing to verify this cabinet against. Give --roster with "
              "--machine, or --expect-serial, or say --skip-identity-check "
              "and mean it.", file=sys.stderr)
        return EXIT_USAGE

    # --- key material first, so a bad paste fails before anything changes --
    try:
        hmac_key = read_key_material(args.hmac_key_file, args.hmac_key_stdin,
                                     "HMAC key")
        check_hmac_key(hmac_key)
    except (OSError, ValueError) as e:
        print(f"HMAC key: {e}", file=sys.stderr)
        return EXIT_USAGE
    pool_key = None
    if args.pool_key_file or args.pool_key_stdin:
        try:
            pool_key = read_key_material(args.pool_key_file,
                                         args.pool_key_stdin, "pool key")
            check_pool_key(pool_key)
        except (OSError, ValueError) as e:
            print(f"pool key: {e}", file=sys.stderr)
            return EXIT_USAGE
    else:
        print("WARNING: no --pool-key. This gateway will not be able to "
              "verify a signed validation pool (§7.3), so ticket-out will "
              "refuse every top-up the server sends.", file=sys.stderr)

    hmac_path = args.key_dir / "hmac.key"
    pool_path = args.key_dir / "pool-signing.pub"

    # IS THIS GATEWAY ALREADY COMMISSIONED, AND WHAT IS BEING RESET?
    #
    # The marker is gateway.json, not hmac.key. Key material is written
    # FIRST and gateway.json LAST (see the commit protocol below), so keys
    # with no gateway.json are an INTERRUPTED ceremony -- never live, with
    # nothing to protect. Keying off hmac.key, as this did until
    # 2026-11-10, refused those and left a technician holding a unit that
    # could only be finished by deleting a file he had no reason to know
    # about.
    #
    # When it IS commissioned, §10.3 adds a second thing the refusal has to
    # say: "Recommissioning resets key and sequence state atomically on
    # both sides. Otherwise a recommissioned gateway starting from zero has
    # every frame rejected." The reset is the dangerous half and the server
    # must do the same thing in the same operation, so the counters being
    # discarded are printed rather than silently zeroed.
    existing = None
    config_path = args.key_dir / "gateway.json"
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except (OSError, ValueError) as e:
            print(f"{config_path} exists but will not parse ({e}). A corrupt "
                  f"marker is not a live commissioning; treating this unit "
                  f"as NOT commissioned.", file=sys.stderr)
    if existing is not None and not args.replace_keys:
        print(f"THIS GATEWAY IS ALREADY COMMISSIONED.\n", file=sys.stderr)
        print(f"  client_id        {existing.get('client_id')}", file=sys.stderr)
        print(f"  commissioned_at  {existing.get('commissioned_at')}", file=sys.stderr)
        print(f"  commission_id    {existing.get('commission_id')}", file=sys.stderr)
        print(f"  sas_address      {existing.get('sas_address')}", file=sys.stderr)
        print("\n  Reassignment is unassign-then-assign on the server "
              "(§10.5); re-keying\n  here without that leaves two live keys "
              "for one client_id.\n  Pass --replace-keys if the server side "
              "has already been unassigned.", file=sys.stderr)
        return EXIT_REFUSED
    if existing is not None:
        print(f"\nRECOMMISSIONING. Replacing client_id "
              f"{existing.get('client_id')!r}, commissioned "
              f"{existing.get('commissioned_at')}.")
        prev = existing.get("protocol") or {}
        if prev.get("seq_tx") or prev.get("pool_epoch"):
            print(f"  RESETTING sequence and pool state to zero (was "
                  f"seq_tx={prev.get('seq_tx')}, "
                  f"pool_epoch={prev.get('pool_epoch')}).\n"
                  f"  §10.3 requires the SERVER to reset its side in the "
                  f"same operation, or every\n  frame this gateway sends "
                  f"will be rejected.")

    # --- which cabinet is this? -------------------------------------------
    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"could not open {args.port}: {e}", file=sys.stderr)
        return EXIT_USAGE
    transport = SASTransport(port, byte_timeout=args.timeout)

    address = args.address
    if address is None:
        if not args.scan:
            print("give --address, or --scan", file=sys.stderr)
            return EXIT_USAGE
        address = find_address(transport)
        if address is None:
            print("nothing answered at any address 1-8 -- cable, wiring or "
                  "baud rate, not a wrong address.", file=sys.stderr)
            return EXIT_NO_ANSWER
    print(f"EGM answered at address {address}")

    identity = read_identity(SASClient(transport, address, timeout=args.timeout))
    seen_serial = identity.get("serial_number")
    seen_asset = identity.get("asset_number")
    print(f"  serial {seen_serial!r}, asset {seen_asset!r}, "
          f"game {identity.get('game_id')!r}")

    if not args.skip_identity_check:
        code, lines = compare(seen_serial, seen_asset, expect_serial,
                              expect_asset)
        for line in lines:
            print(f"  {line}")
        if code != OK:
            print("\nREFUSING TO COMMISSION. Keying a gateway into the wrong "
                  "cabinet produces a unit that authenticates perfectly and "
                  "reports someone else's money.", file=sys.stderr)
            return EXIT_IDENTITY

    # --- name it ----------------------------------------------------------
    try:
        hostname = args.hostname or hostname_for(args.hostname_prefix, seen_asset)
    except ValueError as e:
        print(f"hostname: {e}", file=sys.stderr)
        return EXIT_USAGE
    print(f"\nhostname -> {hostname}  (was {socket.gethostname()!r})")
    try:
        set_hostname(hostname, args.dry_run)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"could not set the hostname: {e}", file=sys.stderr)
        return EXIT_USAGE

    # --- key it -----------------------------------------------------------
    #
    # ORDER IS THE COMMIT PROTOCOL. Key material first, gateway.json LAST.
    #
    # Three files cannot be written atomically to a filesystem, so instead
    # of pretending, one of them is the commit marker: a gateway is
    # commissioned if and only if gateway.json exists and parses. Keys on
    # disk without it are an interrupted ceremony, and the next run
    # overwrites them without needing --recommission, because nothing was
    # ever live. That is the difference between "half-written" and
    # "committed", and it is only knowable because the order is fixed here.
    print(f"keys -> {args.key_dir}")
    write_secret(hmac_path, hmac_key, args.dry_run)
    if pool_key is not None:
        write_secret(pool_path, pool_key, args.dry_run)
    config = {
        "client_id": args.client_id,
        "sas_address": address,
        "port": args.port,
        "commissioned_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        # Changes on every ceremony. The server records it too, so a gateway
        # that was re-keyed while the server was not looking can be told
        # from one that was not -- which is the state §10.5's
        # sequence-collision detection would otherwise have to infer.
        "commission_id": uuid.uuid4().hex,
        "protocol": fresh_protocol_state(),
    }
    write_secret(config_path, (json.dumps(config, indent=2) + "\n").encode(),
                 args.dry_run)

    record = {
        **config,
        "hostname": hostname,
        "seen_serial": seen_serial,
        "seen_asset": seen_asset,
        "expected_serial": expect_serial,
        "expected_asset": expect_asset,
        "identity_checked": not args.skip_identity_check,
        "pool_key_installed": pool_key is not None,
        # Never the key, and not a hash of it either: a hash of a secret that
        # short is brute-forcible, so the log would weaken the thing it
        # records. Length is enough to tell a truncated paste afterwards.
        "hmac_key_bytes": len(hmac_key),
        "dry_run": args.dry_run,
    }
    if args.log and not args.dry_run:
        with args.log.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
        print(f"recorded in {args.log}")

    print("\nCOMMISSIONED." if not args.dry_run else "\n--dry-run: nothing changed.")
    if not args.dry_run:
        print("  The first authenticated frame binds this gateway to "
              f"{args.client_id} on the server (§10.5).")
        print("  Reboot or restart the poller so the new hostname takes "
              "effect everywhere.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
