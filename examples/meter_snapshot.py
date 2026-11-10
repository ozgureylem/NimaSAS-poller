"""Write one machine's complete readable state to a committable JSON file.

Read-only. Meter polls, identity polls and one query-only 0x74; nothing changes
machine state.

The point is a file that can be committed, so the gateway's view of a machine
travels with the repo and anyone -- or anything -- reading the repo later is
reading real figures rather than figures quoted from memory into a chat. Each
snapshot is a separate file, so `git log` on the folder is the machine's
history and `git diff` between two of them is exactly what moved.

    python3 examples/meter_snapshot.py /dev/ttyS0 --address 1
    python3 examples/meter_snapshot.py /dev/ttyS0 --address 1 --note "after 3.00 promo push"

WHERE THESE BELONG. A snapshot carries the machine's asset number, serial
number, and its complete accounting history -- coin in, coin out, drop,
cashless totals. That is operator business data. Commit it to the PRIVATE
repository. Do not mirror captures/ into a public mirror: a public repo is
indexed and forked, and a file removed later stays in the history, so the
decision is effectively one-way. This tool writes the folder's own README
saying so the first time it runs, but nothing here can enforce it.

Values are RAW, exactly as the machine reported them, in accounting units --
not divided, not rounded, not relabelled. A snapshot is evidence; interpreting
it is a separate job and belongs in the tools that interpret
(accounting_summary.py, meter_crosscheck.py). Where a meter did not answer,
the value is null and the code is listed under "unsupported", which is a fact
about the machine worth recording rather than a gap to paper over.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.constants import (
    LongPoll,
    METER_NOTES,
    MeterCode,
    derive_meter_labels,
    meter_name,
)
from saspy.exceptions import SASError

# The read-only RTE-mode detector, from the poller: under real time event
# mode a general poll answers with the machine's OWN ADDRESS instead of an
# exception code, so the reply alone says which mode the cabinet is in and
# nothing has to be written to find out.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sql_poll_logger import rte_looks_stuck  # noqa: E402
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

CHUNK = 12  # LP 0x6F's own per-request limit (§7.21)

# --- What each raw code is, where we have PROOF --------------------------
#
# Every entry below was confirmed by reading a non-zero value off this
# machine's own screen and matching it, to the unit, against the code the
# gateway reported. Twenty-three screen lines were checked line by line on two
# separate occasions (25 Oct, before and after a three-credit-type test), and
# again after a clean cashless run.
#
# The bar is deliberately high, and three kinds of near-evidence are
# DELIBERATELY EXCLUDED as empty:
#
#   Zero matching zero. 0020 and 0021 (attendant paid progressives, external
#   bonuses) have read 0.00 on every capture, against screen lines also
#   reading 0.00. Any zero matches any zero; that is not a correspondence.
#
#   Arithmetic agreement without a screen line. 0023 satisfies the spec's own
#   definition (0003 + 0002) exactly, but no screen line was ever seen
#   carrying it, so what the machine CALLS it is unknown.
#
#   Agreement with another SAS response. 00A0 matched 002E exactly, which
#   established that one credit is one cent -- but both come from SAS, so it
#   says nothing about what the machine's screen calls either of them.
#
# An empty string means "we do not have hard evidence", NOT "no such meter".
# saspy/constants.py carries the spec's name for every code; that is what the
# spec says it should be, while this is what this machine demonstrated.
# Where the two ever disagree, this file is the one with a photograph behind
# it. Add an entry only after seeing a non-zero match with your own eyes.
SCREEN_VERIFIED = {
    # Machine Accounting -- Total Bet / Total Won
    0x0000: "Total Bet (Coin In)",
    # 0001 and 0022 currently hold the SAME number (16.16), because C-7
    # defines 0022 as "sum of total coin out and total jackpot" and this
    # machine's jackpot meter is zero. Both are kept because the screen
    # names both lines and the names match the spec's own wording -- but
    # neither value distinguishes them, so an attendant-paid or progressive
    # win is what will finally separate the pair.
    0x0001: "Winnings from Paytable (Coin Out)",
    0x0019: "Total Non-cashable Played",
    0x001A: "Total Promotional Played",
    0x0022: "Total Won",
    0x001F: "Attendant Paid Paytable Winnings",
    # Machine Accounting -- Total In
    #
    # 0024 was doubted alongside 0004 and is likewise fine. C-7 calls it
    # "total drop ... including but not limited to coins to drop, bills to
    # drop, tickets to drop, and electronic in", and across the captures it
    # equals 0017 + 0015 exactly in this epoch, 54,554 against the screen's
    # 545.54. The pre-clear captures add a constant 69,400 on top, which is
    # the bill activity of that epoch -- the same formula with a term this
    # epoch has not used.
    0x0024: "Total In",
    0x000B: "Bill In",
    0x0017: "Cashless In",
    0x002E: "Cashable Electronic In",
    0x0030: "Promotional Electronic In",
    0x002F: "Non-cashable Electronic In",
    # Machine Accounting -- Total Out
    #
    # 0004 was briefly withdrawn from this table on the claim that it
    # excluded AFT transfers to the host, and that was WRONG. The claim
    # rested on a figure of 365.10 that was never read from the machine: it
    # was computed as 0003 + 000E + 0010 and then written up as an
    # observation. Every one of the 20 committed captures says otherwise --
    # 0004 equals 0003 + 000E + 0010 + 0032 + 0033 + 0034 in all of them,
    # electronic out included, matching the screen's 518.80 to the cent.
    # test_hardware_vectors.py now asserts that identity over the captures,
    # which is the check that would have stopped the withdrawal.
    0x0004: "Total Cancelled Credits",
    0x0003: "Cancelled Credits Paid by Attendant",
    0x0018: "Cashless Out",
    0x0032: "Cashable Electronic Out",
    0x0034: "Promotional Electronic Out",
    # 0033 was claimed here while it read 0.00, which the rest of this table's
    # own standard forbids. Earned properly on 2026-10-26: a 44.44 restricted
    # AFT push followed by a host-initiated cashout put 44.44 down this path
    # for the first time, and the screen line reads 44.44.
    0x0033: "Non-cashable Electronic Out",
    # Machine Accounting -- Voucher In. Zero until the first redemptions, so
    # unclaimable before: confirmed at 40.00 / 10.00 / 5.00 against the screen,
    # and against the machine's own Voucher In Log, whose two entries name the
    # types the gateway asked for and the validation numbers it honoured.
    #
    # 0028 is DELIBERATELY ABSENT and is the trap here. "Total cashable ticket
    # in" looks like the screen's "Cashable Voucher In", and is not: C-7
    # defines 0028 as cashable INCLUDING nonrestricted promotional tickets, so
    # after a 25.00 cashable and a 10.00 promotional redemption it read 35.00
    # against a screen line showing 25.00. The screen's line is 0029, regular
    # cashable ticket in. 0028 has no screen line at all.
    0x0015: "Voucher In",
    0x0029: "Cashable Voucher In",
    0x002B: "Promotional Voucher In",
    0x002A: "Non-cashable Voucher In",
    # Machine Accounting -- Voucher Out. Zero on every comparison until the
    # first ticket printed, so these could not be claimed before: a zero
    # matching a zero proves nothing. Confirmed at 140.25 against the screen,
    # and against the machine's own Voucher Out Log entry for the same ticket.
    0x0016: "Voucher Out",
    0x002C: "Cashable Voucher Out",
    0x002D: "Non-cashable Voucher Out",
    # Machine Accounting -- games
    0x0005: "Total Games Played",
    0x0006: "Total Games Won",
    0x0007: "Total Games Lost",
    # The machine's live credit display, not the accounting screens: observed
    # reading 57.00 as 50.00 cashable + 7.00 non-cashable while these two read
    # 5700 and 700.
    0x000C: "credit display -- total credits",
    0x001B: "credit display -- non-cashable portion",
}



README = """# captures/

Machine state snapshots written by `examples/meter_snapshot.py`.

One JSON file per snapshot, named by UTC timestamp and asset number. Values are
raw, in accounting units, exactly as the machine reported them.

**These are private.** A snapshot carries the machine's asset number, serial
number and complete accounting history. Keep this folder in the private
repository only. Do not copy it into a public mirror -- a public repo is indexed
and forked, and deleting a file later does not remove it from the history.

`git diff` between two snapshots shows what moved between them. `INDEX.md` lists
them with their headline figures.
"""


def is_git_ignored(path: Path) -> bool:
    """Whether this checkout's .gitignore excludes the file just written.

    The public mirror ignores captures/ on purpose, so a snapshot taken from a
    clone of it cannot be committed there -- and printing `git add <path>`
    regardless sends someone to a command that fails. Better to say where the
    file actually belongs.
    """
    try:
        out = subprocess.run(["git", "check-ignore", "-q", str(path)],
                             capture_output=True, timeout=5, check=False)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def git_commit() -> str | None:
    """Which revision of this gateway took the reading."""
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent.parent),
             "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def read_meters(client) -> tuple[dict[str, int | None], list[str]]:
    """Every Table C-7 code this client knows, via LP 0x6F/0xAF in chunks.

    §7.21b makes 0x6F self-describing per meter: a code the machine does not
    implement comes back with size 0, which is 'absent', not 'failed'. Those
    become null here and are named separately. Alternating 0x6F/0xAF is what
    lets consecutive meter polls be distinguished from an implied NACK (§3.2,
    and the spec's own 6.00 note on long poll AF).
    """
    codes = list(MeterCode)
    values: dict[str, int | None] = {}
    unsupported: list[str] = []
    for i in range(0, len(codes), CHUNK):
        chunk = codes[i:i + CHUNK]
        alternate = (i // CHUNK) % 2 == 1
        poll = (client.send_extended_meters_alternate if alternate
                else client.send_extended_meters)
        try:
            result = poll([int(c) for c in chunk])
        except SASError as e:
            print(f"  chunk {i // CHUNK + 1} failed ({type(e).__name__}: {e});"
                  " its meters are recorded as null", file=sys.stderr)
            for code in chunk:
                values[f"{int(code):04X}"] = None
            continue
        for code in chunk:
            got = result.meters.get(int(code))
            values[f"{int(code):04X}"] = got
            if got is None:
                unsupported.append(f"{int(code):04X}")
    return values, unsupported


def try_call(label: str, fn, *args):
    """Identity polls are optional per §4.4; record silence as silence."""
    try:
        return fn(*args)
    except SASError as e:
        print(f"  {label}: no answer ({type(e).__name__})", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# THE PROBE TABLE: every long poll this tool is allowed to send.
#
# Asked for 2026-11-08: "I want to query every machine with safe read only
# polls and their replies... Our findings must be showing which meters are
# available, which long polls are answered (values aren't required but they
# also are welcomed) and most certainly brand of the machine."
#
# WHICH POLLS ANSWER IS ITSELF THE FINDING. A cabinet that ignores LP 0xA0
# cannot be asked what features it has; one that returns nothing for 0x4D
# has no validation history to migrate, or was RAM-cleared (amendment A15).
# Either way the gateway's behaviour has to be decided per model, and that
# decision needs a list of what each model actually answers -- not a list of
# what the specification says it should.
#
# AN ALLOWLIST, NOT A DENYLIST, and for the same reason the public mirror is
# one: a denylist fails open. A new client method is invisible here until
# somebody adds it deliberately, which is the failure direction to be on
# when the thing being added could move money.
#
# Each entry: (label, callable name, args, kwargs). The label leads with the
# long poll's hex code because that is how a reader looks it up in the spec
# -- and NINE of the first twenty-four I wrote carried the wrong code, from
# memory rather than from the client. A label that sends the reader to the
# wrong page of the specification is worse than no label, so a test now
# reads the real LongPoll out of each method's source and compares.
SAFE_PROBES: tuple[tuple[str, str, tuple, dict], ...] = (
    ("LP 0x1B handpay information", "send_handpay_information", (), {}),
    ("LP 0x18 games since power up / door closure",
     "send_games_since_power_up_and_door_closure", (), {}),
    ("LP 0x1E total bill meters", "send_total_bill_meters", (), {}),
    ("LP 0x2D total hand paid cancelled credits",
     "send_total_hand_paid_cancelled_credits", (), {}),
    ("LP 0xB3 token denomination", "send_token_denomination", (), {}),
    ("LP 0x55 selected game number", "send_selected_game_number", (), {}),
    ("LP 0x56 enabled game numbers", "send_enabled_game_numbers", (), {}),
    ("LP 0xA0 enabled features", "send_enabled_features", (), {"game_number": 0}),
    ("LP 0x50 validation meters", "send_validation_meters", (), {}),
    ("LP 0x3D cash out ticket information",
     "send_cash_out_ticket_information", (), {}),
    ("LP 0x57 pending cashout info", "send_pending_cashout_info", (), {}),
    ("LP 0x4F current hopper status", "send_current_hopper_status", (), {}),
    ("LP 0x52 game N meters (game 0)", "send_game_n_meters", (), {"game_number": 0}),
    ("LP 0x53 game N configuration (game 0)",
     "send_game_n_configuration", (0,), {}),
    ("LP 0x0F meters 10 through 15", "send_meters_10_through_15", (), {}),
    ("LP 0x19 meters 11 through 15", "send_meters_11_through_15", (), {}),
    ("LP 0x70 ticket validation data", "send_ticket_validation_data", (), {}),
    ("LP 0x8F physical reel stop information",
     "send_physical_reel_stop_information", (), {}),
    ("LP 0xB4 wager category information (game 0)",
     "send_wager_category_information", (), {"game_number": 0,
                                             "wager_category": 0}),
    # §7.11 warns some older machines do not implement 0x48 at all, and that
    # it fails as a SASError on the whole exchange rather than a blank
    # field. try_call() absorbs that -- it is recorded as unanswered, which
    # for a WMS or an IGT is exactly the fact worth having.
    ("LP 0x48 last accepted bill information",
     "send_last_accepted_bill_information", (), {}),
    # QUERY FORMS ONLY, and the keyword arguments below are the safety.
    #
    #   0x7B with control_mask = 0 sets nothing; a non-zero mask WRITES the
    #        cabinet's ticket expiry settings.
    #   0x72 with acknowledge = False reads a transfer record; the default
    #        is True, which ACKNOWLEDGES it and moves the machine on.
    #   0x4D by INDEX leaves the read/unread pointer where it was, which is
    #        how ticket_history.py walks it without disturbing a cashier.
    ("LP 0x7B extended validation status (query, control_mask=0)",
     "extended_validation_status", (), {"control_mask": 0}),
    ("LP 0x72 AFT interrogate (read, no acknowledge)",
     "aft_interrogate_transfer", (0,), {"acknowledge": False}),
    ("LP 0x4D enhanced validation, newest record (by index, pointer untouched)",
     "send_enhanced_validation_information", (), {"function_code": 0}),
)

# NEVER PROBED, named rather than merely absent -- so the next person to
# extend the table reads why these are missing instead of assuming an
# oversight. Every one of them changes the machine.
#
#   send_startup / send_shutdown  LP 0x06/0x07 ENABLE AND DISABLE the
#                                 cabinet. A survey that disables a machine
#                                 on a live floor is the worst thing in this
#                                 file's reach.
#   redeem_ticket                 pays a ticket.
#   send_validation_number        issues one.
#   reset_handpay                 LP 0x94 clears a lockup, which on a floor
#                                 records a jackpot as PAID when nobody paid.
#   aft_transfer_funds            moves money.
#   aft_register_gaming_machine   changes AFT registration.
#   set_secure_enhanced_validation_id   changes validation configuration.
#   enable_real_time_events       changes the machine's reporting MODE, and
#                                 a survey that left RTE on would make every
#                                 later tool misread the address byte.
NEVER_PROBE = frozenset({
    "send_startup", "send_shutdown", "redeem_ticket", "redeem_ticket_status",
    "send_validation_number", "reset_handpay", "aft_transfer_funds",
    "aft_register_gaming_machine", "set_secure_enhanced_validation_id",
    "enable_real_time_events",
})

assert not ({name for _, name, _, _ in SAFE_PROBES} & NEVER_PROBE), (
    "a state-changing call reached the probe table")


# IS THIS CABINET REALLY RTE-CAPABLE? Measured, not assumed.
#
# Operator, 2026-11-10: "I guess we should add enable RTE during the survey.
# To see if machine that's being surveyed is really RTE compatible."
#
# It is the one question a survey cannot answer by reading. SAS 6.02 has no
# "do you support real time events" query: §4.4 lets a machine ignore a long
# poll it does not implement, so a cabinet that cannot do RTE answers LP 0x0E
# with silence -- indistinguishable from a cable fault until you try.
#
# WHICH IS WHY enable_real_time_events IS IN NEVER_PROBE, and stays there.
# This is not an exception to that rule; it is a separate, opt-in procedure
# with a teardown, because the hazard it names is real and worse on a floor
# than in the lab:
#
#   "a survey that left RTE on would make every later tool misread the
#    address byte"
#
# A surveyed floor cabinet goes back to the incumbent CMS afterwards. Left in
# RTE it would feed that CMS Table 12.5 frames where it expects single bytes,
# on a machine nobody is watching. So the design rule here is not "enable it
# carefully" -- it is **leaving it on must not be able to happen quietly**:
#
#   1. the disable runs in a finally, so an exception still tears down;
#   2. the disable is VERIFIED by an ordinary general poll, not assumed;
#   3. an unverified disable is returned as a failure the caller must print,
#      not a warning buried in a field;
#   4. §12 requires machines to power up in ordinary mode, so a power cycle
#      is the backstop -- stated here because the person reading this at a
#      cabinet needs to know it.
#
# The probe itself is deliberately small: enable, a handful of polls, disable.
# It is not a soak. What it establishes is capability, not behaviour.
RTE_PROBE_POLLS = 6


def probe_rte(client, *, polls: int = RTE_PROBE_POLLS, sleep=time.sleep,
              interval: float = 0.3) -> dict:
    """Turn RTE on, see whether the cabinet speaks it, turn it off again.

    Returns a dict that always carries `disabled_confirmed`. A caller that
    does not look at that field is using this wrong.
    """
    out = {
        "attempted": True,
        "enable_accepted": False,
        "events_seen": 0,
        "answered_in_rte_shape": False,
        "supported": None,          # None = could not be established
        "disabled_confirmed": False,
        "error": None,
    }
    enabled = False
    try:
        try:
            client.enable_real_time_events(True)
            enabled = True
            out["enable_accepted"] = True
        except SASError as e:
            # §4.4: a machine that does not implement LP 0x0E ignores it.
            # That IS the answer -- not RTE-capable -- and no teardown is
            # owed, because nothing was turned on.
            out["supported"] = False
            out["error"] = f"{type(e).__name__}: {e}"
            out["disabled_confirmed"] = True
            return out

        for _ in range(polls):
            try:
                event = client.general_poll_rte()
            except SASError as e:
                out["error"] = f"{type(e).__name__}: {e}"
                break
            # §12.6: silence is "no activity", not a failure, and a cabinet
            # idling on a floor before opening is exactly that. The frame
            # parsing succeeding at all is what proves the mode took.
            out["answered_in_rte_shape"] = True
            if event is not None:
                out["events_seen"] += 1
            sleep(interval)
        out["supported"] = bool(out["answered_in_rte_shape"])
    finally:
        if enabled:
            for attempt in range(3):
                try:
                    client.enable_real_time_events(False)
                except SASError as e:
                    out["error"] = f"disable attempt {attempt + 1}: {e}"
                    sleep(0.3)
                    continue
                # VERIFY, do not assume. In ordinary mode a general poll
                # returns ONE byte, the exception code. If the cabinet were
                # still in RTE the reply would be Table 12.5's address/FF
                # frame and this read would mis-frame -- so a clean small
                # byte back is the evidence the mode really changed.
                try:
                    code = client.general_poll()
                except SASError as e:
                    out["error"] = f"post-disable poll: {e}"
                    sleep(0.3)
                    continue
                if code != client.address:
                    out["disabled_confirmed"] = True
                    out["error"] = None
                    break
                # Reading back our own address byte is the signature of a
                # machine still in RTE being read one byte at a time.
                out["error"] = ("after disabling, a general poll returned the "
                                "machine's ADDRESS byte -- it is still in RTE")
                sleep(0.3)
    return out


def run_probes(client, probes=SAFE_PROBES) -> list[dict]:
    """Send each safe probe once and record what came back.

    A value is recorded where the reply has one, but the POINT is the
    answered flag: "which long polls does this model implement" is the
    question a floor survey exists to answer, and §4.4 makes silence the
    machine's way of saying "not implemented" -- so an unanswered probe is
    data, not a gap.
    """
    out = []
    for label, name, a, kw in probes:
        fn = getattr(client, name, None)
        if fn is None:
            out.append({"poll": label, "method": name, "answered": False,
                        "error": "this saspy build has no such method"})
            continue
        row = {"poll": label, "method": name}
        try:
            value = fn(*a, **kw)
        except SASError as e:
            row.update(answered=False, error=f"{type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            # A decode failure is still an ANSWER -- the machine replied and
            # this tool could not read it, which is a different finding from
            # silence and must not be recorded as the same thing.
            row.update(answered=True, undecodable=True,
                       error=f"{type(e).__name__}: {e}")
        else:
            row["answered"] = True
            row["value"] = _jsonable(value)
        out.append(row)
    return out


def _jsonable(value):
    """Whatever the reply was, in something json.dump can write."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if hasattr(value, "__dict__") or hasattr(value, "__dataclass_fields__"):
        try:
            import dataclasses
            if dataclasses.is_dataclass(value):
                return {k: _jsonable(v)
                        for k, v in dataclasses.asdict(value).items()}
        except Exception:  # noqa: BLE001
            pass
        return {k: _jsonable(v) for k, v in vars(value).items()
                if not k.startswith("_")}
    return repr(value)


def gateway_clock_evidence() -> dict:
    """How far this gateway's own clock can be trusted, recorded not assumed.

    The operator, 2026-11-08: *"during surveys we will rely more on
    gateway's time. Yes we will still check time and date on the EGM, but
    gateway's time is definitive."*

    That is the right call and the 2026-10-27 survey is why: of four
    cabinets that answered LP 0x7E, one was on UTC to the second, one was
    six hours off and two minutes slow, and one reported **June 2002**. A
    clock that is wrong differently on every machine cannot order anything.
    The gateway's is one clock, so even when it is wrong it is wrong
    CONSISTENTLY, and a consistent offset can be corrected afterwards.

    But "definitive" raises the stakes rather than settling them. This
    gateway boots headless with no network, so NTP never runs; if it has no
    RTC battery its clock starts wherever the last shutdown left it, and
    then **every snapshot of the survey is wrong together while looking
    perfectly consistent** -- which is worse than noisy, because nothing in
    the data disagrees. So the evidence travels with the readings:

      rtc_present     a hardware clock exists at all
      ntp_synchronized  whether anything ever corrected it this boot
      uptime_s        seconds since boot, which with taken_at_utc gives the
                      implied boot time -- an implied boot time in the
                      future, or decades back, is the tell
      monotonic_s     a clock that cannot be set, so the ORDER and the
                      SPACING of a survey's snapshots survive even if the
                      wall clock is nonsense

    None of this corrects anything. It lets a reader decide whether the
    timestamps are usable, which is a judgement the tool is not entitled
    to make on their behalf.
    """
    import time as _time

    out: dict = {
        "monotonic_s": round(_time.monotonic(), 3),
        "rtc_present": None,
        "ntp_synchronized": None,
        "uptime_s": None,
    }
    try:
        out["rtc_present"] = any(Path("/sys/class/rtc").iterdir())
    except OSError:
        out["rtc_present"] = False
    try:
        out["uptime_s"] = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        r = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized",
                            "--value"], capture_output=True, text=True,
                           timeout=5)
        if r.returncode == 0:
            out["ntp_synchronized"] = r.stdout.strip() == "yes"
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def egm_clock_offset(egm_clock, taken_at) -> dict | None:
    """The EGM's clock against the gateway's, in seconds, with the sign.

    Still read, still recorded -- "yes we will still check time and date on
    the EGM" -- but as a MEASUREMENT of that cabinet rather than as a
    candidate for the truth. A six-hour offset is a finding about the
    machine; a 2002 date is a finding about its battery.

    Returns None where the machine gave no clock or gave one that will not
    parse, which is itself worth seeing: the field is left null rather than
    filled with a guess.
    """
    if egm_clock is None:
        return None
    date, time_s = (egm_clock.date or "").strip(), (egm_clock.time or "").strip()
    row: dict = {"machine_date": date or None, "machine_time": time_s or None,
                 "offset_seconds": None, "parsed": False}
    try:
        # MMDDYYYY and HHMMSS, as the machine reports them. Treated as UTC
        # ONLY to measure the gap -- SAS carries no timezone and nothing
        # here is entitled to assume the cabinet is on one.
        when = dt.datetime.strptime(date + time_s, "%m%d%Y%H%M%S").replace(
            tzinfo=dt.timezone.utc)
    except ValueError:
        row["note"] = ("the machine's date/time did not parse as MMDDYYYY "
                       "HHMMSS; left unresolved rather than guessed")
        return row
    row["parsed"] = True
    row["offset_seconds"] = round((when - taken_at).total_seconds())
    row["note"] = ("positive means the cabinet's clock is AHEAD of the "
                   "gateway's. The gateway's is definitive for this survey; "
                   "this is a measurement of the machine, not a correction "
                   "to apply.")
    return row


def read_exception_queue(client, address: int, max_polls: int = 25) -> dict:
    """Drain the cabinet's exception queue and record what was in it.

    The operator, 2026-11-08, overruling an earlier caution here:

        "Consuming exception poll of the EGM isn't a problem, actually it
         also is a requirement. Just make sure that we don't write anything
         to the egm. No AFT fund transfer, no machine state change, read
         only with RTE poll included."

    So a general poll is in, and what it finds is survey data: a cabinet
    holding a queue of events when the survey arrives is a cabinet whose
    host has not been draining it, which is worth knowing before a gateway
    is pointed at it.

    THE RTE POLL IS INCLUDED, AND RTE IS NEVER ENABLED. Under real-time
    event mode the general poll answers with the machine's OWN ADDRESS
    rather than an exception code, which `rte_looks_stuck` detects from the
    reply alone -- no poll required to find out. Where the cabinet is
    already in that mode the structured Table 12.5 read is used instead,
    so the survey reads RTE on a machine that is in RTE and changes nothing
    either way.

    What is deliberately NOT done: LP 0x0E. Enabling real-time events is a
    machine STATE CHANGE, which the operator excluded in the same sentence
    -- and this project has the case on record. On 2026-11-02 a teardown
    was pre-empted (§12.3), RTE was left on, and the next tool read the
    Table 12.5 address byte as an exception code three times before anyone
    noticed. A survey that enabled RTE and failed to disable it would hand
    the next visitor a cabinet that misframes every long poll.
    """
    out: dict = {"polls": [], "mode": None, "left_in_queue": None}
    try:
        first = client.general_poll()
    except SASError as e:
        out["mode"] = f"no answer to a general poll ({type(e).__name__}: {e})"
        return out

    if rte_looks_stuck(int(first), address):
        out["mode"] = ("real time event mode is ON: the general poll answered "
                       f"0x{int(first):02X}, this machine's own address. "
                       "Reading structured Table 12.5 events instead. "
                       "NOT changed -- LP 0x0E is a write.")
        for _ in range(max_polls):
            try:
                event = client.general_poll_rte()
            except SASError as e:
                out["polls"].append({"error": f"{type(e).__name__}: {e}"})
                break
            if event is None or int(event.code) == 0:
                break
            out["polls"].append({"code": f"0x{int(event.code):02X}",
                                 "fields": _jsonable(getattr(event, "fields", None))})
        else:
            out["left_in_queue"] = f"still answering after {max_polls} polls"
        return out

    out["mode"] = "ordinary framing (not in real time event mode)"
    code = int(first)
    for _ in range(max_polls):
        if code == 0:
            break
        out["polls"].append({"code": f"0x{code:02X}"})
        try:
            code = int(client.general_poll())
        except SASError as e:
            out["polls"].append({"error": f"{type(e).__name__}: {e}"})
            break
    else:
        out["left_in_queue"] = f"still answering after {max_polls} polls"
    return out


def sweep_addresses(port_factory, addresses, timeout: float,
                    *, general_poll: bool = True) -> list[dict]:
    """Does anything answer on an address other than 1?

    Asked for 2026-11-08: "I know machines' SAS addresses are set to 1, but
    I also would like to make an address sweep after short and long polls,
    to see if they answer to other addresses as well."

    He asked for both kinds, and they can disagree: a general poll is one
    byte that some machines answer from any address on the bus, while a
    long poll is addressed explicitly and a machine that ignores the first
    may still answer the second.

    BOTH ARE SENT BY DEFAULT. An earlier version defaulted the general
    poll OFF, reasoning that §2.2.1's queue is consumed by one and that on
    a live cabinet it takes somebody's accounting data. The operator
    overruled it on 2026-11-08 -- "consuming exception poll of the EGM
    isn't a problem, actually it also is a requirement" -- and he is the
    one who knows what else is polling these machines. The caution was
    mine, not the specification's. `--no-address-sweep-general-poll`
    turns it off for a case where it does matter.

    Each address that does NOT answer costs one timeout, so the range is
    the caller's decision and the default stays narrow.
    """
    rows = []
    for address in addresses:
        client = port_factory(address)
        row = {"address": address, "general_poll": None, "long_poll": None}
        if general_poll:
            try:  # noqa: SIM105
                code = client.general_poll()
                row["general_poll"] = f"0x{int(code):02X}"
            except SASError as e:
                row["general_poll_error"] = type(e).__name__
            except Exception as e:  # noqa: BLE001
                row["general_poll_error"] = f"{type(e).__name__}: {e}"
        else:
            row["general_poll"] = "not sent (--no-address-sweep-general-poll)"
        try:
            v = client.send_sas_version_and_serial()
            row["long_poll"] = {"sas_version": v.sas_version.strip(),
                                "serial_number": v.serial_number.strip()}
        except SASError as e:
            row["long_poll_error"] = type(e).__name__
        except Exception as e:  # noqa: BLE001
            row["long_poll_error"] = f"{type(e).__name__}: {e}"
        row["answered"] = bool(row["long_poll"]) or bool(
            general_poll and row.get("general_poll")
            and not row.get("general_poll_error"))
        rows.append(row)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--address", type=int, default=1)
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--dir", default="captures",
                    help="where to write (default: captures/, relative to cwd)")
    ap.add_argument("--skip-games", action="store_true",
                    help="skip the per-game LP 0xB5 sweep (one poll per game; a "
                         "48-game cabinet is 48 extra exchanges)")
    ap.add_argument("--position", type=int, default=None, metavar="N",
                    help="WHICH MACHINE THIS IS in the order you are walking "
                         "the floor: 1 for the first cabinet surveyed, 2 for "
                         "the second. The operator sends photographs in that "
                         "same order, one group per machine, and on a floor "
                         "where serials are missing and asset numbers repeat "
                         "this is the ONLY key that ties a photograph to a "
                         "snapshot. Record it even when the cabinet answers "
                         "cleanly -- especially then, because that is when it "
                         "is easiest to skip.")
    ap.add_argument("--brand", default=None,
                    help="what the cabinet IS, read off the machine: 'WMS', "
                         "'IGT Game King', 'Novomatic Impera Link HD'. SAS "
                         "does not carry a manufacturer field, so unless this "
                         "is typed in at the machine nothing in the snapshot "
                         "will ever say what it was.")
    ap.add_argument("--floor-label", default=None,
                    help="the serial printed on the cabinet, which §5d "
                         "measured as having NO relationship to the SAS "
                         "serial. On a machine that reports neither serial "
                         "nor asset this is the only identifier that exists.")
    ap.add_argument("--no-probes", action="store_true",
                    help="skip the read-only long-poll probe table (~25 extra "
                         "exchanges). The probes are the point of a survey; "
                         "this exists for a quick re-read of one cabinet.")
    ap.add_argument("--probe-rte", action="store_true",
                    help="establish whether this cabinet really supports real "
                         "time events, by turning RTE on, polling a few times "
                         "and turning it OFF again. SAS has no capability "
                         "query -- a machine that cannot do RTE simply ignores "
                         "LP 0x0E (section 4.4), so trying is the only way to "
                         "know. OFF by default and deliberately so: this is "
                         "the one probe that changes the machine's reporting "
                         "MODE, and a cabinet left in RTE feeds the incumbent "
                         "CMS Table 12.5 frames where it expects single bytes. "
                         "The disable runs in a finally and is VERIFIED by an "
                         "ordinary general poll; if it cannot be verified the "
                         "survey says so loudly and the cabinet needs a power "
                         "cycle (section 12 requires machines to power up in "
                         "ordinary mode).")
    ap.add_argument("--address-sweep", default=None, metavar="LO-HI",
                    help="after the snapshot, general-poll AND long-poll every "
                         "address in the range to see whether anything answers "
                         "beyond this one. Each silent address costs one "
                         "timeout, so 1-8 is a floor check and 1-127 is a "
                         "coffee break.")
    ap.add_argument("--no-address-sweep-general-poll", action="store_true",
                    help="send only the long poll in the address sweep. Both "
                         "are sent by default: a general poll consumes "
                         "§2.2.1's exception queue, which the operator has "
                         "confirmed is wanted rather than avoided, and the "
                         "two kinds can disagree about who is on the bus.")
    ap.add_argument("--no-exception-queue", action="store_true",
                    help="skip reading the cabinet's exception queue. The "
                         "read is on by default and is a general poll "
                         "repeated until the queue is empty; what was "
                         "waiting is survey data. Where the cabinet is "
                         "already in real time event mode the structured "
                         "Table 12.5 read is used instead -- detected from "
                         "the reply, never by enabling it. LP 0x0E is a "
                         "WRITE and this tool does not send it.")
    ap.add_argument("--note", default=None,
                    help="what this snapshot is of -- 'before the soak', 'after a "
                         "3.00 promo push'. Recorded in the file.")
    args = ap.parse_args()

    try:
        port = open_serial_port(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2

    client = SASClient(
        SASTransport(port, byte_timeout=args.timeout), args.address, timeout=args.timeout)

    taken_at = dt.datetime.now(dt.timezone.utc)
    print(f"Reading {args.port} address {args.address} ...", file=sys.stderr)

    version = try_call("LP 0x54 version/serial", client.send_sas_version_and_serial)
    machine = try_call("LP 0x1F machine id", client.send_gaming_machine_id)
    lock = try_call("LP 0x74 lock/status", client.aft_game_lock_and_status_request, 0xFF)
    # THE GATEWAY'S CLOCK IS DEFINITIVE, the machine's is a measurement.
    #
    # Set by the operator 2026-11-08: "during surveys we will rely more on
    # gateway's time. Yes we will still check time and date on the EGM, but
    # gateway's time is definitive."
    #
    # This file previously said "record both clocks and resolve neither",
    # and the evidence it cited is what settles it the other way: of the
    # four cabinets that answered LP 0x7E on 2026-10-27, one was on UTC to
    # the second, one was six hours off and two minutes slow, and one
    # reported June 2002. Four clocks disagreeing four different ways order
    # nothing. The gateway's is ONE clock -- wrong consistently if wrong at
    # all, and a consistent offset can be corrected afterwards.
    #
    # What that costs is recorded rather than waved away: a headless
    # gateway with no network never runs NTP, so gateway_clock_evidence()
    # travels with every snapshot. And survey_seq still depends on no clock
    # at all, which is why it stays.
    egm_clock = try_call("LP 0x7E date/time", client.send_current_date_and_time)
    games = try_call("LP 0x51 games implemented",
                     client.send_meter, LongPoll.SEND_TOTAL_GAMES_IMPLEMENTED)

    # LP 0xB5 per game: the name, the paytable name, max bet, and how many
    # wager categories the paytable has. Worth capturing because a game NUMBER
    # is only meaningful next to the name it refers to, and that mapping is
    # per-machine and changes when games are added or reordered -- so a meter
    # history that records game numbers without it ages into something nobody
    # can read. A machine may implement some of these fields and not others:
    # the EGT Premier reports game names and leaves every paytable name empty.
    # Game 0 FIRST, and unconditionally. Table 7.23b: "When the game number
    # is zero, all games supported by the gaming machine are considered ...
    # the max bet field will contain the largest max bet value." That is the
    # only unsaturated machine-level max bet SAS offers.
    #
    # LP 0x1F's own max bet is ONE BINARY BYTE and Table 7.10 defines it as
    # "01-FF ... or FF if largest configured max bet greater than or equal to
    # 255". So 255 is a sentinel meaning "at least 255", never a reading. The
    # lab EGT reports 255 while eleven of its forty-eight games are configured
    # above it and the true largest is 500; every zoom roulette station
    # reported 255 too, which we recorded as a value and should not have.
    #
    # Unconditional because LP 0xB5 game 0 is also reachable on a machine
    # that answers LP 0x51 with zero games -- where the per-game sweep has
    # nothing to iterate and the old code therefore asked for nothing at all.
    game_info = []
    if not args.skip_games:
        print(f"  reading LP 0xB5 for game 0 and {games or 0} game(s) ...",
              file=sys.stderr)
        for n in [0, *range(1, (games or 0) + 1)]:
            info = try_call(f"LP 0xB5 game {n}",
                            client.send_extended_game_n_information, n)
            if info is None:
                game_info.append({"game_number": n, "answered": False})
                continue
            game_info.append({
                "game_number": info.game_number,
                "answered": True,
                "game_name": info.game_name.strip(),
                "paytable_name": info.paytable_name.strip(),
                "max_bet": info.max_bet,
                "progressive_group": info.progressive_group,
                "wager_categories": info.wager_categories,
            })

    # RTE LAST, after every read-only probe. If the teardown somehow fails,
    # everything else in this survey has already been collected in the
    # ordinary mode rather than half of it in each.
    rte = None
    if args.probe_rte:
        print("\n  RTE capability probe: enabling real time events...",
              file=sys.stderr)
        rte = probe_rte(client)
        if rte["supported"]:
            print(f"  RTE: SUPPORTED ({rte['events_seen']} event(s) seen "
                  f"while enabled)", file=sys.stderr)
        elif rte["supported"] is False:
            print("  RTE: NOT supported -- the cabinet ignored LP 0x0E",
                  file=sys.stderr)
        else:
            print("  RTE: could not be established", file=sys.stderr)
        if rte["disabled_confirmed"]:
            print("  RTE: confirmed OFF again", file=sys.stderr)
        else:
            # Loud, and on its own lines. This is the one outcome that
            # leaves a floor cabinet in a state the incumbent CMS cannot
            # read, and it must not be a field somebody scrolls past.
            print("", file=sys.stderr)
            print("  " + "!" * 68, file=sys.stderr)
            print("  RTE COULD NOT BE CONFIRMED OFF ON THIS CABINET.",
                  file=sys.stderr)
            print(f"  reason: {rte['error']}", file=sys.stderr)
            print("  Do not return this machine to service until it has been",
                  file=sys.stderr)
            print("  POWER CYCLED -- SAS 6.02 section 12 requires a machine to",
                  file=sys.stderr)
            print("  power up in the ordinary reporting mode.", file=sys.stderr)
            print("  " + "!" * 68, file=sys.stderr)
            print("", file=sys.stderr)

    queue = (None if args.no_exception_queue
             else read_exception_queue(client, args.address))
    if queue:
        print(f"  exception queue: {len(queue['polls'])} event(s); "
              f"{queue['mode']}", file=sys.stderr)

    probes = [] if args.no_probes else run_probes(client)
    if probes:
        answered = sum(1 for r in probes if r.get("answered"))
        print(f"  probes: {answered} of {len(probes)} long polls answered",
              file=sys.stderr)

    sweep = None
    if args.address_sweep:
        lo, _, hi = args.address_sweep.partition("-")
        lo, hi = int(lo), int(hi or lo)
        # 00 is the SAS broadcast address and is never swept: a broadcast is
        # not a question to one machine and nothing useful answers it.
        addresses = [a for a in range(max(1, lo), min(0x7F, hi) + 1)]
        print(f"  sweeping addresses {addresses[0]}-{addresses[-1]} "
              f"(each silent one costs {args.timeout}s) ...", file=sys.stderr)
        sweep = sweep_addresses(
            lambda a: SASClient(client.transport, a, timeout=args.timeout),
            addresses, args.timeout,
            general_poll=not args.no_address_sweep_general_poll)
        hits = [r["address"] for r in sweep if r["answered"]]
        print(f"  answered on: {hits or 'nothing'}", file=sys.stderr)

    meters, unsupported = read_meters(client)
    derived_labels = derive_meter_labels(
        SCREEN_VERIFIED, {int(c, 16): v for c, v in meters.items()})

    asset = lock.asset_number if lock is not None else None
    snapshot = {
        "taken_at_utc": taken_at.isoformat(timespec="seconds"),
        "note": args.note,
        # THE KEY THAT TIES A PHOTOGRAPH TO A SNAPSHOT. On a floor where a
        # cabinet may report no serial, no asset, or an asset another
        # cabinet also reports, the order the survey was walked in is the
        # only identifier that cannot collide -- and the operator sends
        # photographs in that same order, one group per machine.
        "survey_position": args.position,
        # SAS has no manufacturer field. Unless these are typed in at the
        # machine, nothing in this file will ever say what the cabinet was.
        "brand": args.brand,
        "floor_label": args.floor_label,
        "gateway": {
            "port": args.port,
            "sas_address": args.address,
            "baud": args.baud,
            "repo_commit": git_commit(),
            # taken_at_utc above is THIS clock. How far it can be trusted
            # is a question the reader has to be able to answer, because
            # a headless gateway with no network never syncs.
            "clock_evidence": gateway_clock_evidence(),
        },
        "identity": {
            "asset_number": asset,
            "serial_number": version.serial_number.strip() if version else None,
            "sas_version": version.sas_version.strip() if version else None,
            "game_id": machine.game_id.strip() if machine else None,
            "additional_id": machine.additional_id.strip() if machine else None,
            "denomination_code": machine.denomination if machine else None,
            "max_bet": machine.max_bet if machine else None,
            "games_implemented": games,
            # Table 7.10's max bet saturates at 255; this is LP 0xB5 game 0,
            # which is 2 BCD (0000-9999) and carries the largest max bet
            # across every game the machine supports. Where the two disagree,
            # 0x1F is the one that is wrong.
            "max_bet_largest_lp_b5": next(
                (g["max_bet"] for g in game_info
                 if g.get("game_number") == 0 and g.get("answered")), None),
            # MMDDYYYY and HHMMSS as the machine reports them (Table 7.?),
            # left as strings: reformatting invites a timezone assumption
            # nothing here is entitled to make.
            "machine_date": egm_clock.date if egm_clock else None,
            "machine_time": egm_clock.time if egm_clock else None,
            # The gap, with its sign, so nobody has to subtract two string
            # formats by hand to notice a cabinet is six hours out.
            "egm_clock_vs_gateway": egm_clock_offset(egm_clock, taken_at),
        },
        "games": game_info,
        # From 0x74, the only native source for the nonrestricted balance --
        # Table C-7 stops at 000C and 001B. In cents.
        "balances_cents": None if lock is None else {
            "cashable": lock.current_cashable_amount,
            "restricted": lock.current_restricted_amount,
            "nonrestricted": lock.current_non_restricted_amount,
        },
        # WHICH LONG POLLS THIS MODEL ANSWERS, which is a finding in its own
        # right: §4.4 lets a machine ignore a poll it does not implement, so
        # silence is the machine saying "not implemented" and belongs in the
        # record. Values are included where there were any, but the answered
        # flag is the column that decides what a gateway can rely on.
        "probes": probes,
        "rte_probe": rte,
        # What the cabinet had queued when the survey arrived, and which
        # framing mode it was in. A queue with events in it is a cabinet
        # whose host was not draining it -- worth knowing before a gateway
        # is pointed at it. null when skipped, which is NOT the same as
        # "the queue was empty".
        "exception_queue": queue,
        # Whether anything answers on an address other than the one polled.
        # null when not asked for -- NOT the same as "nothing else is there".
        "address_sweep": sweep,
        "meters_raw": meters,
        # One entry per code in meters_raw. A label is this machine's own
        # screen wording where that has been confirmed against a non-zero
        # reading; an EMPTY STRING means no such proof exists yet, which is a
        # statement about our evidence and not about the meter.
        # Labels inherited from the screen-verified ones through Table C-7's
        # own relationships, each carrying where it came from. Kept in a
        # SEPARATE field from meter_screen_labels on purpose: a screen match
        # is a photograph and a derived label is an argument, and a consumer
        # that cannot tell them apart will eventually quote the argument as
        # the photograph -- this project's recurring failure mode.
        "meter_derived_labels": {
            f"{code:04X}": {"label": label, "basis": basis}
            for code, (label, basis) in sorted(derived_labels.items())
        },
        "meter_screen_labels": {
            code: SCREEN_VERIFIED.get(int(code, 16), "") for code in meters
        },
        # Table C-7's own name for every code, which is neither a photograph
        # nor an argument but the specification. A third field rather than a
        # merge into either of the two above, for the same reason those two
        # are separate: "this cabinet's screen calls it X", "C-7 says it is
        # the same figure as Y" and "C-7 names it Z" are three different
        # claims and a report that flattens them loses the ability to say
        # which one it is standing on.
        "meter_spec_names": {code: meter_name(int(code, 16)) for code in meters},
        "meter_spec_notes": {
            code: METER_NOTES[int(code, 16)] for code in meters
            if int(code, 16) in METER_NOTES
        },
        "unsupported_meter_codes": unsupported,
    }

    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    readme = out_dir / "README.md"
    if not readme.exists():
        readme.write_text(README)
        print(f"wrote {readme}", file=sys.stderr)

    # NEVER overwrite an existing snapshot. The name is built from this
    # host's clock and the machine's asset number, and on a gateway carried
    # round a floor BOTH can repeat: no network and a flat RTC battery can
    # hand every boot the same time, and asset numbers are entered by hand so
    # two cabinets can share one. Either alone is survivable; together they
    # produce the identical filename, and a plain write would then destroy the
    # previous machine's survey without a word. A suffix is the whole fix.
    stamp = taken_at.strftime("%Y-%m-%dT%H-%M-%SZ")
    name = f"{stamp}-asset{asset}" if asset is not None else stamp
    path = out_dir / f"{name}.json"
    suffix = 1
    while path.exists():
        suffix += 1
        path = out_dir / f"{name}-{suffix}.json"
    if suffix > 1:
        print(f"note: {name}.json already existed, so this is {path.name}.",
              file=sys.stderr)
        print("      Two snapshots wanting one name means a repeated clock, a",
              file=sys.stderr)
        print("      repeated asset number, or both. Nothing was overwritten.",
              file=sys.stderr)
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")

    print(f"\nwrote {path}")
    print(f"  {sum(1 for v in meters.values() if v is not None)} of {len(meters)}"
          f" meters answered; {len(unsupported)} not implemented")
    labelled = sum(1 for c in meters if int(c, 16) in SCREEN_VERIFIED)
    print(f"  {labelled} of {len(meters)} carry a screen label confirmed against"
          " this machine;")
    print("  the rest are empty, meaning no hard evidence rather than no meter")
    named = sum(1 for c in meters if meter_name(int(c, 16)))
    print(f"  all {named} of {len(meters)} carry Table C-7's own name")
    unnamed = [c for c in meters if not meter_name(int(c, 16))]
    if unnamed:
        # 00BE-FFFF are reserved in 6.02. A machine answering one is a finding.
        print(f"  {len(unnamed)} answered a code C-7 does not define: "
              f"{', '.join(sorted(unnamed))}")
    if lock is not None:
        total = (lock.current_cashable_amount + lock.current_restricted_amount
                 + lock.current_non_restricted_amount)
        print(f"  on the machine now: {total / 100:,.2f}"
              f"  ({lock.current_cashable_amount / 100:,.2f} cashable"
              f" + {lock.current_restricted_amount / 100:,.2f} restricted"
              f" + {lock.current_non_restricted_amount / 100:,.2f} nonrestricted)")
    label = f"asset {asset}" if asset is not None else "this machine"
    if is_git_ignored(path):
        print("\nThis checkout ignores captures/ -- it is the PUBLIC mirror, which")
        print("excludes it on purpose: a snapshot carries the asset number, serial")
        print("number and full accounting history. Move it to the private clone:")
        print(f"  mv {path} /path/to/private-clone/captures/")
        print(f'  cd /path/to/private-clone && git add captures/ && git commit'
              f' -m "capture: snapshot of {label}" && git push')
        print("\nOr re-run with --dir pointing straight into the private clone.")
    else:
        print("\nCommit it (private repo only -- see this tool's docstring):")
        print(f"  git add {path} {readme}")
        print(f'  git commit -m "capture: snapshot of {label}"')
        print("  git push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
