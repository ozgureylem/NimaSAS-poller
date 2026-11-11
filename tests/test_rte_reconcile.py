"""The reconciler, and the 40 ms claim it exists to settle.

A detector that cannot fail is not a detector. These drive synthetic
databases in both directions: a clean run must come back clean, and a run
with one event deliberately removed must be caught -- by each detector
independently, since the point of having four is that a clean result is not
one tool's opinion.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "examples"))

from examples.rte_reconcile import (  # noqa: E402
    SPEC_MIN_QUEUE,
    check_bill_continuity,
    check_buffer_overflow,
    check_game_start_continuity,
    _indexed,
    _total_between,
    check_credit_ledger,
    check_meters,
    check_repeated_events,
)
from saspy.client import decode_rte_event_fields  # noqa: E402
from saspy.models import EnabledFeatures  # noqa: E402


def ev(code, at="2026-10-28T20:00:00", **fields):
    return {"id": 0, "at": at, "code": code, "description": "x",
            "source": "rte", "fields": fields}


def game_starts(wagers, start_coin_in=10_000, dropped=()):
    """A run of game-start events whose total_coin_in tracks the wagers, per
    §12.5.3. Indices in ``dropped`` are removed AFTER the meter is advanced,
    which is exactly what a lost event looks like: the meter moved, the
    event never arrived."""
    out, coin_in = [], start_coin_in
    for i, w in enumerate(wagers):
        coin_in += w
        if i in dropped:
            continue
        out.append(ev(0x7E, at=f"2026-10-28T20:{i:02d}:00",
                      credits_wagered=w // 100, total_coin_in=coin_in,
                      wager_cents=w, denomination_code=1))
    return out


# --- detector 1: the machine's own admission -----------------------------

def test_no_buffer_overflow_passes():
    verdict, lines = check_buffer_overflow([ev(0x7E), ev(0x7F)], window_start=None)
    assert verdict == "PASS"
    assert "never reported losing an event" in " ".join(lines)


def test_a_single_buffer_overflow_fails():
    """§2.2.1 requires a machine that drops an event to say so with 0x70.
    One is enough: it is a confession, not a statistic."""
    verdict, lines = check_buffer_overflow(
        [ev(0x7E), ev(0x70), ev(0x7F)], window_start="2026-10-28T19:00:00")
    assert verdict == "FAIL"
    assert "DURING steady-state" in " ".join(lines)


# --- detector 2: the stream audits itself --------------------------------

def test_a_clean_game_start_run_passes():
    verdict, lines = check_game_start_continuity(game_starts([100, 250, 100, 500]))
    assert verdict == "PASS", lines
    assert "no game-start event went missing" in " ".join(lines)


def test_one_dropped_game_start_is_caught_and_quantified():
    """The excess coin-in over the surviving game's own wager is the wager of
    the game whose event vanished -- so the detector does not just say
    'something is missing', it says how much."""
    verdict, lines = check_game_start_continuity(
        game_starts([100, 250, 100, 500], dropped=(1,)))
    assert verdict == "FAIL"
    text = " ".join(lines)
    assert "250 credits of wagering unaccounted for" in text


def test_several_dropped_game_starts_sum():
    verdict, lines = check_game_start_continuity(
        game_starts([100, 250, 300, 500], dropped=(1, 2)))
    assert verdict == "FAIL"
    assert "550 credits of wagering unaccounted for" in " ".join(lines)


def test_a_meter_clear_mid_run_is_not_called_a_gap():
    """total_coin_in going DOWN is a RAM clear, not a lost event. Flagging it
    would teach the reader to skim past the gaps that matter."""
    evs = game_starts([100, 100])
    evs.append(ev(0x7E, at="2026-10-28T21:00:00", total_coin_in=5,
                  wager_cents=100, credits_wagered=1))
    verdict, _lines = check_game_start_continuity(evs)
    assert verdict == "PASS"


def test_an_idle_run_says_no_data_rather_than_pass():
    """The failure mode this guards: a quiet soak clearing every detector and
    being read as proof that the rate is safe."""
    for events in ([], [ev(0x7E, total_coin_in=1, wager_cents=1)]):
        verdict, lines = check_game_start_continuity(events)
        assert verdict == "NO DATA"
        assert "nothing to check" in " ".join(lines).lower() or \
               "nothing to check continuity against" in " ".join(lines)


# --- detector 3: the bill ladder -----------------------------------------

def test_consecutive_bills_of_one_denomination_must_step_by_one():
    clean = [ev(0x4F, denomination_code=d, bills_of_this_type=n)
             for d, n in ((1, 5), (1, 6), (4, 2), (1, 7), (4, 3))]
    verdict, lines = check_bill_continuity(clean)
    assert verdict == "PASS", lines

    gapped = [ev(0x4F, denomination_code=1, bills_of_this_type=n)
              for n in (5, 6, 9)]
    verdict, lines = check_bill_continuity(gapped)
    assert verdict == "FAIL"
    assert "2 bill event(s) missing" in " ".join(lines)


# --- the capability bit --------------------------------------------------

def test_the_40ms_bit_is_the_machines_declaration_not_the_hosts_duty():
    """Table 7.14e bit 0. Exposing it is the point: the claim under test is
    that a host MUST poll under 40 ms, and the only place the figure appears
    as a requirement is as something the MACHINE must support."""
    fields = EnabledFeatures.__dataclass_fields__
    assert "max_polling_rate_40ms" in fields
    assert "multiple_sas_progressive_win_reporting" in fields
    # Feature codes 2 came along with it and must not have been dropped.
    for name in ("meter_model", "tickets_in_total_drop", "extended_meters",
                 "component_authentication", "advanced_funds_transfer",
                 "multi_denom_extensions"):
        assert name in fields


def test_the_spec_queue_floor_is_recorded():
    """§2.2.1's twenty elements is what retains events between polls, and is
    the number that makes a slow poll safe or unsafe -- not the 40 ms."""
    assert SPEC_MIN_QUEUE == 20


# --- the field the whole thing rests on ----------------------------------

def test_game_start_carries_the_coin_in_meter_after_the_wager():
    """§12.5.3. Without this field in the event the stream could not audit
    itself and detector 2 would not exist."""
    data = bytes([0x00, 0x05]) + bytes([0x00, 0x01, 0x00, 0x00]) + bytes([0x01, 0x00])
    fields = decode_rte_event_fields(0x7E, data)
    assert fields["credits_wagered"] == 5
    assert fields["total_coin_in"] == 10000
    assert fields["denomination_code"] == 1
    assert "wager_cents" in fields


def test_reel_stop_and_card_held_are_rte_only_events():
    """Both are marked in §12.5.5 and §12.5.7 as 'sent only if real time
    event reporting is enabled'. They are why the event rate under RTE is
    strictly higher than the exception rate without it, and so why the
    40 ms question is not silly even though the claim is wrong."""
    assert decode_rte_event_fields(0x88, bytes([3, 17])) == {
        "reel_number": 3, "physical_stop": 17}
    assert decode_rte_event_fields(0x8B, bytes([0x82]))["held"] is True


# --- the windowing mistake this tool made on its first real run ----------

def test_an_overflow_before_the_window_is_not_blamed_on_the_poll_rate():
    """§2.2.1's queue is non-volatile and fills whether or not a host is
    listening, and 0x70 is reported "at the next opportunity" after a loss.
    A 0x70 collected in the startup drain is therefore about a loss that
    happened earlier -- very likely with nobody polling at all.

    The tool's own first lab run called that a failure.
    """
    pre = ev(0x70, at="2026-10-28T05:01:29")
    pre["source"] = "startup-backlog"
    verdict, lines = check_buffer_overflow([pre], window_start="2026-10-28T05:01:29")
    assert verdict == "PASS"
    assert "not evidence about this run's polling rate" in " ".join(lines)


def test_an_overflow_during_steady_state_polling_still_fails():
    """The distinction must not become an excuse that swallows every 0x70."""
    during = ev(0x70, at="2026-10-28T05:30:00")
    verdict, lines = check_buffer_overflow([during], window_start="2026-10-28T05:01:29")
    assert verdict == "FAIL"
    assert "DURING steady-state" in " ".join(lines)


def test_an_overflow_with_no_window_is_still_reported():
    """No meters_history means no window to judge against, and the safe
    reading of a confession you cannot place is to surface it."""
    verdict, _lines = check_buffer_overflow([ev(0x70)], window_start=None)
    assert verdict == "FAIL"


# --- the gateway's own downtime is not the machine's fault ---------------

def test_a_gap_spanning_a_gateway_restart_is_not_a_lost_event():
    """The overnight run of 2026-10-28. Three restarts during debugging, and
    the reconciler reported the 1,223,200 credits played while the program
    was stopped as "events were lost. At this polling rate this cabinet does
    NOT retain every event" -- a sentence about a polling rate drawn entirely
    from evidence about a process being stopped.

    §2.2.1's queue overflows after about eight seconds of nobody draining
    it. Twenty-three minutes of downtime loses everything past the twentieth
    event no matter how fast the host would have polled had it been running.
    """
    def start(at, coin):
        return ev(0x7E, at=at, total_coin_in=coin, wager_cents=1000,
                  credits_wagered=10, denomination_code=1)

    # Two games, the gateway stops, 870 more are played with nobody
    # listening, then it comes back.
    evs = [start("2026-10-28T05:01:30", 11_000),
           start("2026-10-28T05:01:50", 12_000),
           start("2026-10-28T05:25:15", 12_000 + 870_000)]
    boundaries = ["2026-10-28T05:25:11"]

    blamed, _ = check_game_start_continuity(evs)
    assert blamed == "FAIL", "without the boundary it blames the machine"

    verdict, lines = check_game_start_continuity(evs, boundaries)
    text = " ".join(lines)
    assert "span a GATEWAY RESTART" in text
    assert "869,000 credits" in text  # the rise, less the one game that did report
    assert "no polling rate could have captured" in text
    assert verdict == "PASS", "the remaining pairs are clean"


def test_a_real_loss_after_a_restart_is_still_caught():
    """The exclusion must not become an amnesty. A genuine gap on the far
    side of a restart is still a genuine gap."""
    def start(at, coin):
        return ev(0x7E, at=at, total_coin_in=coin, wager_cents=1000,
                  credits_wagered=10, denomination_code=1)

    evs = [start("2026-10-28T05:01:30", 11_000),
           start("2026-10-28T05:01:50", 12_000),
           start("2026-10-28T05:25:15", 882_000),
           # ...and then 200 credits wagered with no event for it.
           start("2026-10-28T05:27:00", 882_000 + 200 + 1000)]

    verdict, lines = check_game_start_continuity(evs, ["2026-10-28T05:25:11"])
    assert verdict == "FAIL"
    assert "200 credits of wagering unaccounted for" in " ".join(lines)


def test_restart_boundaries_come_from_the_interrogations_table(tmp_path):
    import sqlite3 as _sq
    import sys as _sys
    _sys.path.insert(0, str(REPO))
    from examples.sql_poll_logger import SCHEMA, migrate_epoch_columns
    from examples.rte_reconcile import gateway_restarts

    conn = _sq.connect(tmp_path / "db.sqlite3")
    conn.executescript(SCHEMA)
    migrate_epoch_columns(conn)
    conn.executemany(
        "INSERT INTO interrogations (started_at, reason) VALUES (?,?)",
        [("2026-10-28T05:01:29", "first_connect"),
         ("2026-10-28T05:25:11", "first_connect"),
         ("2026-10-28T09:00:00", "comms_restored")])  # not a gateway restart
    conn.commit()
    assert gateway_restarts(conn) == ["2026-10-28T05:01:29", "2026-10-28T05:25:11"]


def test_a_database_without_interrogations_degrades_quietly():
    """Soak databases predate this table by weeks. Reconciling one must not
    crash; it just cannot exclude restarts it has no record of."""
    import sqlite3 as _sq
    from examples.rte_reconcile import gateway_restarts
    conn = _sq.connect(":memory:")
    assert gateway_restarts(conn) == []


def test_every_rte_source_is_reconciled():
    """A source left out of the filter does not under-report events -- it
    INVENTS losses, because the meter still moves for a game whose event the
    detectors were never shown.

    'rte-heartbeat' was missing for exactly one run. The heartbeat is the
    general poll issued between long polls during a meter sweep, so on a
    quiet machine it is often the poll that catches a lone game -- precisely
    the case a continuity check examines. The 2026-10-28 soak reported "200
    credits of wagering unaccounted for" across 7.7 hours and the event was
    in the database the whole time, filed under that source.
    """
    from examples.rte_reconcile import RTE_SOURCES
    import pathlib
    poller = (REPO / "examples" / "sql_poll_logger.py").read_text()
    for source in ("rte", "rte-interleaved", "rte-heartbeat", "startup-backlog"):
        assert f'"{source}"' in poller, f"{source} is not written by the poller"
        assert source in RTE_SOURCES, f"{source} is written but not reconciled"


def test_the_cabinets_own_chain_reconciles():
    """The five consecutive game starts around the phantom gap, verbatim from
    the 2026-10-28 soak: three at 1,800 credits, the heartbeat-sourced one at
    200, then two at 600. Every pair must close exactly."""
    chain = [(17555610, 1800), (17557410, 1800), (17559210, 1800),
             (17559410, 200), (17560010, 600), (17560610, 600)]
    evs = []
    for i, (coin, wager) in enumerate(chain):
        e = ev(0x7E, at=f"2026-10-28T08:{33 + i:02d}:00",
               total_coin_in=coin, wager_cents=wager,
               credits_wagered=wager // 20, denomination_code=11)
        # The one the filter used to drop.
        e["source"] = "rte-heartbeat" if coin == 17559410 else "rte"
        evs.append(e)

    verdict, lines = check_game_start_continuity(evs)
    assert verdict == "PASS", lines

    without = [e for e in evs if e["source"] != "rte-heartbeat"]
    verdict, lines = check_game_start_continuity(without)
    assert verdict == "FAIL"
    assert "200 credits of wagering unaccounted for" in " ".join(lines)


# --- detector 5: the payout identity -------------------------------------
#
# The operator's acceptance rule, in his words: "unless there's a buffer
# overflow, rte coin in coin out (including jackpot and progressive pays)
# must match meter deltas. And if there's buffer overflow, we also need to
# find the underlying source of the overflow and prevent it."
#
# Detector 4 only ever checked the WAGER side plus one award meter. These
# tests exist because the award side has a failure mode detector 4 cannot
# see: a jackpot paid by an attendant never touches total_coin_out, so
# summing wins against coin out alone under-counts by exactly the jackpots
# and looks like a bad return-to-player rather than a missing meter.

from examples.rte_reconcile import check_payout_identity  # noqa: E402


def _payout_db(tmp_path, rows, extra_cols=()):
    """A meters_history with just the payout columns. ``rows`` are dicts."""
    cols = ["total_coin_out", "total_jackpot", "c7_total_won_credits",
            "total_cancelled_credits",
            "lp2d_total_hand_paid_cancelled_credits", *extra_cols]
    path = tmp_path / "m.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meters_history (id INTEGER PRIMARY KEY, "
                 "polled_at TEXT, " + ", ".join(f"{c} INTEGER" for c in cols) + ")")
    for i, r in enumerate(rows):
        conn.execute(
            f"INSERT INTO meters_history (polled_at, {', '.join(cols)}) "
            f"VALUES (?, {', '.join('?' * len(cols))})",
            [f"2026-10-28T20:{i:02d}:00"] + [r.get(c) for c in cols])
    conn.commit()
    return conn


def game_ends(wins):
    """Game ends placed INSIDE the first minute of any `_payout_db` window.

    They used to sit at 20:00:30, 20:01:30, ... -- one per meter row, with
    the last event arriving AFTER the last meter reading. That is not a
    thing a cabinet can do: a meter read at 20:01:00 cannot contain a win
    whose event lands at 20:01:30, so the fixture was asserting an identity
    that could only hold if the comparison ignored time. It did, until
    detector 5 started comparing each meter against the game ends its own
    first and last readings actually bracket.
    """
    return [ev(0x7F, at=f"2026-10-28T20:00:{10 + i * 5:02d}", game_win=w)
            for i, w in enumerate(wins)]


def test_the_identity_closes_on_coin_out_when_nothing_was_handpaid(tmp_path):
    conn = _payout_db(tmp_path, [
        {"total_coin_out": 1000, "total_jackpot": 500},
        {"total_coin_out": 1900, "total_jackpot": 500},
    ])
    verdict, lines = check_payout_identity(
        conn, game_ends([400, 500]), None, None)
    assert verdict == "PASS", lines
    assert "total_coin_out alone" in " ".join(lines)


def test_a_jackpot_paid_by_attendant_needs_the_jackpot_meter(tmp_path):
    """The whole reason this detector exists. Coin out moved by 900 and the
    games awarded 2,900; the missing 2,000 is in total_jackpot, and a
    reconciler that only knew about coin out would call this a 31% return to
    player instead of naming the meter."""
    conn = _payout_db(tmp_path, [
        {"total_coin_out": 1000, "total_jackpot": 0},
        {"total_coin_out": 1900, "total_jackpot": 2000},
    ])
    verdict, lines = check_payout_identity(
        conn, game_ends([400, 500, 2000]), None, None)
    assert verdict == "PASS", lines
    text = " ".join(lines)
    assert "total_coin_out + total_jackpot" in text
    assert "jackpots metered separately" in text


def test_a_win_no_payout_meter_accounts_for_FAILS(tmp_path):
    """A progressive award is not in LP 0x0F at all. If one is paid and no
    meter this tool reads moves, that must FAIL and say how much is adrift
    -- it is money the host cannot see.

    The big win is in the MIDDLE of the window deliberately: a game at an
    edge can legitimately put its event and its meter movement on opposite
    sides of the boundary, and the next test covers that."""
    conn = _payout_db(tmp_path, [
        {"total_coin_out": 1000, "total_jackpot": 0},
        {"total_coin_out": 1400, "total_jackpot": 0},
    ])
    verdict, lines = check_payout_identity(
        conn, game_ends([100, 50_000, 300]), None, None)
    assert verdict == "FAIL", lines
    text = " ".join(lines)
    assert "NO COMBINATION OF PAYOUT METERS" in text
    assert "50000" in text
    # It must point at the likely cause rather than just the arithmetic.
    assert "progressive" in text
    # And say why an edge cannot explain it: 100 + 300, not 50,000.
    assert "400 between" in text


def test_an_unexplained_win_AT_THE_EDGE_is_inconclusive_not_a_failure(tmp_path):
    """The honest limit of this detector, asserted so nobody tightens it into
    crying wolf. If the one unexplained win is the LAST game in the window,
    'its meter movement fell outside the window' and 'a meter we do not read
    carried it' predict the same numbers. Narrowing the window is the only
    thing that separates them, so say that instead of guessing."""
    conn = _payout_db(tmp_path, [
        {"total_coin_out": 1000, "total_jackpot": 0},
        {"total_coin_out": 1400, "total_jackpot": 0},
    ])
    verdict, lines = check_payout_identity(
        conn, game_ends([400, 50_000]), None, None)
    assert verdict == "INCONCLUSIVE", lines
    assert "Narrow with --since/--until" in " ".join(lines)


def test_cashout_meters_are_reported_but_never_summed_into_the_win_side(tmp_path):
    """The trap: a jackpot awarded and then handpaid moves an award meter AND
    a cash-out meter. Adding the cash-out side would make this 'pass' at
    4,000 against an awarded 2,000, which is how a reconciler ends up
    certifying a cabinet that is losing events."""
    conn = _payout_db(tmp_path, [
        {"total_coin_out": 0, "total_jackpot": 0,
         "lp2d_total_hand_paid_cancelled_credits": 0},
        {"total_coin_out": 0, "total_jackpot": 2000,
         "lp2d_total_hand_paid_cancelled_credits": 2000},
    ])
    verdict, lines = check_payout_identity(conn, game_ends([2000]), None, None)
    assert verdict == "PASS", lines
    text = " ".join(lines)
    assert "CASH-OUT, not a win" in text
    assert "double-counts" in text
    # And it must have closed on the award meters alone, not on the sum.
    assert "total_coin_out + total_jackpot" in text


def test_an_overflow_in_the_window_refuses_to_report_PASS(tmp_path):
    """The operator's rule has two halves and this is the second one. A lost
    game-end event makes the awarded total an UNDER-count, so the identity
    closing is luck, not evidence. It must not read as a clean run."""
    conn = _payout_db(tmp_path, [
        {"total_coin_out": 1000}, {"total_coin_out": 1400},
    ])
    clean, _ = check_payout_identity(conn, game_ends([400]), None, None)
    assert clean == "PASS"
    gated, lines = check_payout_identity(
        conn, game_ends([400]), None, None, overflowed=True)
    assert gated == "INCONCLUSIVE"
    text = " ".join(lines)
    assert "cannot be clean" in text
    assert "names its source" in text


def test_a_c7_meter_that_is_null_on_the_newest_row_is_still_read(tmp_path):
    """c7_* only refreshes on a Table C-7 sweep and routine runs throttle it,
    so the newest row is almost always NULL there. Taking the first and last
    ROW would report the meter as absent from the database -- which is a much
    more comforting statement than 'the sweep did not run this cycle'."""
    conn = _payout_db(tmp_path, [
        {"total_coin_out": 1000, "c7_total_won_credits": 7000},
        {"total_coin_out": 1200, "c7_total_won_credits": None},
        {"total_coin_out": 1400, "c7_total_won_credits": 7400},
        {"total_coin_out": 1400, "c7_total_won_credits": None},
    ])
    verdict, lines = check_payout_identity(conn, game_ends([400]), None, None)
    text = " ".join(lines)
    assert "c7_total_won_credits: not in this database" not in text
    assert "c7_total_won_credits: +400" in text
    assert verdict == "PASS", lines


def test_no_win_bearing_events_is_NO_DATA_not_a_pass(tmp_path):
    """An idle machine must not certify anything. Same discipline as the
    other four detectors."""
    conn = _payout_db(tmp_path, [{"total_coin_out": 1000},
                                 {"total_coin_out": 1000}])
    verdict, lines = check_payout_identity(conn, [ev(0x7E)], None, None)
    assert verdict == "NO DATA"
    assert "nothing to reconcile" in " ".join(lines)


def test_the_legacy_bonus_is_reported_separately_not_added(tmp_path):
    """12.5.2's bonus may or may not already be inside the game-end win on a
    given cabinet. Adding it would invent a discrepancy on a machine where it
    is already included, and that is a per-model fact to measure."""
    conn = _payout_db(tmp_path, [{"total_coin_out": 0}, {"total_coin_out": 500}])
    evs = game_ends([500]) + [ev(0x7C, bonus_amount=300)]
    verdict, lines = check_payout_identity(conn, evs, None, None)
    assert verdict == "PASS", lines
    text = " ".join(lines)
    assert "legacy-bonus" in text
    assert "reported SEPARATELY and not added" in text


# --- detector 6: the sign detector 2 cannot see --------------------------

def _repeat(events, index):
    """Deliver the event at ``index`` twice.

    A repeat carries the SAME total_coin_in as its original -- the meter
    cannot un-move -- while still claiming its own wager. That is the whole
    signature, and it is what makes the arithmetic detect it.
    """
    dup = dict(events[index])
    dup["fields"] = dict(events[index]["fields"])
    dup["at"] = events[index]["at"][:-2] + "30"
    return events[:index + 1] + [dup] + events[index + 1:]


def test_detector_2_IS_BLIND_TO_A_DUPLICATE_which_is_why_6_exists():
    """The hole, pinned so nobody closes detector 6 believing 2 covers it.

    Detector 2 flags `(after - before) - wager > 0`. A duplicate gives
    `after - before == 0` and a positive wager, so the excess is NEGATIVE
    and the `> 0` test drops it. Detector 2 reports PASS on a stream that
    delivered a game twice.

    The operator asked this directly on 2026-11-07 -- "I feel like a few
    exceptions were issued more than once" -- and no tool on this project
    could answer him.
    """
    doubled = _repeat(game_starts([100, 250, 100, 500]), 1)
    verdict, lines = check_game_start_continuity(doubled)
    assert verdict == "PASS", (
        "detector 2 has started seeing duplicates; rewrite this test rather "
        "than deleting it, because detector 6's reason for existing changed")
    assert "no game-start event went missing" in " ".join(lines)


def test_a_clean_run_has_nothing_repeated():
    verdict, lines = check_repeated_events(game_starts([100, 250, 100, 500]))
    assert verdict == "PASS", lines
    assert "nothing was delivered twice" in " ".join(lines)


def test_a_duplicated_game_start_is_caught_and_quantified():
    verdict, lines = check_repeated_events(_repeat(game_starts([100, 250, 100, 500]), 1))
    assert verdict == "FAIL", lines
    text = " ".join(lines)
    assert "1 game start(s) REPEAT" in text
    assert "250 credits of wagering with no meter movement behind it" in text


def test_two_duplicates_sum_their_phantom_wagers():
    doubled = _repeat(_repeat(game_starts([100, 250, 100, 500]), 1), 3)
    verdict, lines = check_repeated_events(doubled)
    assert verdict == "FAIL"
    assert "2 game start(s) REPEAT" in " ".join(lines)


def test_an_out_of_order_pair_is_a_REORDERING_not_a_repeat():
    """A10: arrival order is not event order.

    Two starts delivered swapped give a positive excess immediately
    followed by a negative one that cancels it exactly. That is one pair of
    events in the wrong order -- nothing lost, nothing repeated -- and it is
    the 2026-10-29/30 'fossil' this project spent a day diagnosing by hand.
    Detector 2 counts the positive half as a gap, so the count it prints is
    too high by one per swap, and detector 6 says so.
    """
    runs = game_starts([100, 250, 300, 500])
    runs[1], runs[2] = runs[2], runs[1]
    verdict, lines = check_repeated_events(runs)
    text = " ".join(lines)
    assert "arrived OUT OF ORDER" in text
    assert "NOT losses" in text
    assert "subtract 2 from its gap count" in text
    assert "REPEAT" not in text, "a swap must not be reported as duplication"
    assert verdict != "FAIL"


def test_a_swap_is_what_detector_2_would_have_called_a_gap():
    """The two halves of the same claim, so the correction is checked
    against the thing it corrects rather than asserted on its own."""
    runs = game_starts([100, 250, 300, 500])
    runs[1], runs[2] = runs[2], runs[1]
    v2, l2 = check_game_start_continuity(runs)
    assert v2 == "FAIL", "detector 2 is expected to misread a swap as a gap"
    assert "2 gap(s)" in " ".join(l2), (
        "a swap shows detector 2 TWO gaps, one either side of the pair it "
        "skips -- that count is what detector 6 corrects")
    v6, _ = check_repeated_events(runs)
    assert v6 != "FAIL"


def test_a_repeat_spanning_a_gateway_restart_is_not_counted():
    """Same exemption detector 2 takes: the gateway was not there, so the
    stream either side of the gap is not one stream."""
    runs = _repeat(game_starts([100, 250, 100, 500]), 1)
    verdict, _ = check_repeated_events(
        runs, boundaries=["2026-10-28T20:01:15"])
    assert verdict == "PASS"


def test_an_idle_run_says_no_data_rather_than_pass_for_repeats():
    verdict, lines = check_repeated_events([])
    assert verdict == "NO DATA"
    assert "nothing to compare" in " ".join(lines)


def test_an_earlier_CONNECTED_overflow_is_not_called_an_undrained_queue():
    """The mislabel the operator's question exposed, 2026-11-07.

    In a whole-database run `window_start` is the LAST gateway restart, so
    every overflow in the file's history falls before it. The first version
    of detector 1 tested the timestamp first and printed all of them under
    "the machine is played with no host draining it" -- including 61 whose
    source is `rte`/`rte-interleaved`, meaning they were collected WHILE A
    GATEWAY WAS POLLING. Those are the one C5 violation on record, and a
    green PASS was describing them as a normal undrained queue.

    Scope is still scope: an earlier run's overflow does not fail THIS
    window. Only the words had to change.
    """
    verdict, lines = check_buffer_overflow(
        [ev(0x70, at="2026-10-30T18:08:53")],
        window_start="2026-11-06T23:08:38")
    text = " ".join(lines)
    assert verdict == "PASS", "an earlier run's overflow is out of scope here"
    assert "WHILE A GATEWAY WAS POLLING" in text
    assert "C5 violations" in text
    assert "no host draining" not in text, (
        "a polled overflow must never carry the undrained-queue sentence")


def test_a_startup_backlog_overflow_keeps_the_undrained_queue_wording():
    """The other half: that sentence is correct for a startup drain, and
    removing it would lose a true explanation while fixing a false one."""
    e = ev(0x70, at="2026-10-30T18:06:57")
    e["source"] = "startup-backlog"
    verdict, lines = check_buffer_overflow([e], window_start="2026-11-06T23:08:38")
    text = " ".join(lines)
    assert verdict == "PASS"
    assert "drained at STARTUP" in text
    assert "no host draining" in text
    assert "WHILE A GATEWAY WAS POLLING" not in text


def test_a_startup_backlog_overflow_is_pre_even_with_no_window():
    """Source decides, not the clock: with no window there is no timestamp
    test left, and a startup drain must still not fail the run."""
    e = ev(0x70, at="2026-10-30T18:06:57")
    e["source"] = "startup-backlog"
    verdict, _ = check_buffer_overflow([e], window_start=None)
    assert verdict == "PASS"


def test_a_win_after_the_last_meter_reading_is_not_charged_to_that_meter(tmp_path):
    """Detector 5's per-meter window, 2026-11-07.

    A meter delta is `last - first`. A win whose game-end event arrives
    after the last reading cannot be inside it, and comparing it anyway
    reports credits as missing that were only ever late.
    """
    conn = _payout_db(tmp_path, [
        {"total_coin_out": 1000},
        {"total_coin_out": 1400},     # +400: the first win only
    ])
    evs = (game_ends([400])
           + [ev(0x7F, at="2026-10-28T20:05:00", game_win=999)])
    verdict, lines = check_payout_identity(conn, evs, None, None)
    text = " ".join(lines)
    assert verdict == "PASS", lines
    assert "IDENTITY CLOSES EXACTLY" in text
    assert "400 == 400" in text


def test_two_meters_on_different_cadences_get_different_spans(tmp_path):
    """The real shape of the bug, from the lab database.

    `total_coin_out` refreshes every cycle; `c7_total_won_credits` only on
    a Table C-7 sweep, throttled to once a minute. Their first and last
    readings therefore bracket different spans, and one event sum compared
    against both reported the offset between them as event loss: 2.2% over
    45 minutes, 0.1% over 5h52m -- shrinking with the window, which a real
    loss cannot do.
    """
    conn = _payout_db(tmp_path, [
        {"total_coin_out": 1000, "c7_total_won_credits": 5000},
        {"total_coin_out": 1300},                 # no sweep this cycle
        {"total_coin_out": 1600, "c7_total_won_credits": 5600},
        {"total_coin_out": 1900},                 # no sweep this cycle
    ])
    # Four wins of 300. The C-7 pair brackets only the first two.
    evs = [ev(0x7F, at=f"2026-10-28T20:0{i}:30", game_win=300) for i in range(4)]
    verdict, lines = check_payout_identity(conn, evs, None, None)
    text = " ".join(lines)
    assert "its OWN reading span" in text, text
    assert "c7_total_won_credits" in text
    # coin out spans 20:00:00..20:03:00 and holds 3 of the 4 ends (900);
    # c7 spans 20:00:00..20:02:00 and holds 2 (600). Both close on their own
    # span, which is the entire point.
    assert "IDENTITY CLOSES EXACTLY" in text, text


# --- detector 7: the operator's credit ledger ---------------------------

def _ledger_db(tmp_path, readings, money=None, name="ledger.sqlite3"):
    """meters_history with a credit gauge, one row per reading.

    ``readings`` are (polled_at, credits). ``money`` optionally gives a
    total_cancelled_credits per row, to exercise the money-moved skip.
    """
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meters_history (id INTEGER PRIMARY KEY, "
                 "polled_at TEXT, c7_current_credits INTEGER, "
                 "total_cancelled_credits INTEGER)")
    for i, (at, credits) in enumerate(readings):
        conn.execute("INSERT INTO meters_history (polled_at, c7_current_credits,"
                     " total_cancelled_credits) VALUES (?,?,?)",
                     (at, credits, (money or [0] * len(readings))[i]))
    conn.commit()
    return conn


def _play(at, wager, win):
    """One game: a start carrying its wager and an end carrying its win."""
    return [ev(0x7E, at=at, wager_cents=wager, credits_wagered=wager // 100,
               total_coin_in=0),
            ev(0x7F, at=at[:-1] + str(int(at[-1]) + 1), game_win=win)]


def test_the_ledger_closes_when_every_event_arrived(tmp_path):
    """credits - wagers + wins = credits, which is the operator's identity."""
    conn = _ledger_db(tmp_path, [("2026-10-28T20:00:00", 10_000),
                                 ("2026-10-28T20:01:00", 9_700)])
    evs = _play("2026-10-28T20:00:10", 400, 100) + _play("2026-10-28T20:00:20", 300, 300)
    verdict, lines = check_credit_ledger(conn, evs, None, None)
    assert verdict == "PASS", lines
    assert "every interval closes" in " ".join(lines)


def _run(tmp_path, intervals, games_per, wager, win, drop_end_each=False,
         dup_start_each=False):
    """A run of credit readings with `games_per` identical games between each.

    One minute per interval, games spaced inside it. Returns (conn, events).
    """
    readings, evs, credits = [], [], 100_000
    for k in range(intervals + 1):
        readings.append((f"2026-10-28T{20 + k // 60:02d}:{k % 60:02d}:00", credits))
        if k == intervals:
            break
        for g in range(games_per):
            at = f"2026-10-28T{20 + k // 60:02d}:{k % 60:02d}:{5 + g * 5:02d}"
            evs.append(ev(0x7E, at=at, wager_cents=wager,
                          credits_wagered=wager // 100, total_coin_in=0))
            if dup_start_each and g == 0:
                evs.append(ev(0x7E, at=at[:-1] + "1", wager_cents=wager,
                              credits_wagered=wager // 100, total_coin_in=0))
            if not (drop_end_each and g == 0):
                evs.append(ev(0x7F, at=at[:-1] + "2", game_win=win))
            credits += win - wager
    conn = _ledger_db(tmp_path, readings)
    return conn, evs


def test_a_clean_multi_interval_run_closes_and_does_not_accumulate(tmp_path):
    conn, evs = _run(tmp_path, intervals=10, games_per=6, wager=100, win=50)
    verdict, lines = check_credit_ledger(conn, evs, None, None)
    assert verdict == "PASS", lines
    assert "nothing accumulated either" in " ".join(lines)


def test_ONE_lost_game_end_is_BELOW_what_a_single_interval_can_resolve(tmp_path):
    """A limitation worth pinning rather than discovering later.

    A boundary can displace one start and one end at each edge, so an
    interval's tolerance is two games' worth -- larger than a one-game
    fault. No tightening fixes it: a displaced start moves the residual by
    its whole wager, so the bound is correct and simply coarse. This is
    why the signed total, not the per-interval count, is the signal.
    """
    conn, evs = _run(tmp_path, intervals=1, games_per=6, wager=100, win=50,
                     drop_end_each=True)
    verdict, _ = check_credit_ledger(conn, evs, None, None)
    assert verdict == "PASS", (
        "one interval cannot resolve one lost game; if this starts failing, "
        "the tolerance has been tightened past what a boundary can do")


def test_LOST_GAME_ENDS_ACCUMULATE_into_more_credits_than_events_explain(tmp_path):
    """The side nothing else on this project scores.

    Detectors 2 and 6 read game STARTS only; detector 5 reads ends in
    aggregate against the payout meters. A steadily dropped 0x7F is
    invisible to all of them, and §19 measures ends as the half that
    survives MORE often -- so the unaudited side was the one the evidence
    pointed at.
    """
    conn, evs = _run(tmp_path, intervals=10, games_per=6, wager=100, win=500,
                     drop_end_each=True)
    verdict, lines = check_credit_ledger(conn, evs, None, None)
    text = " ".join(lines)
    assert verdict == "FAIL", lines
    assert "MORE credits than the events explain" in text
    assert "a game end was lost" in text
    assert "DO NOT cancel" in text


def test_DUPLICATED_GAME_STARTS_ACCUMULATE_the_other_way(tmp_path):
    conn, evs = _run(tmp_path, intervals=10, games_per=6, wager=500, win=100,
                     dup_start_each=True)
    verdict, lines = check_credit_ledger(conn, evs, None, None)
    text = " ".join(lines)
    assert verdict == "FAIL", lines
    assert "FEWER credits than the events explain" in text
    assert "a game start arrived twice" in text


def test_an_interval_where_money_moved_is_SKIPPED_not_accused(tmp_path):
    """The operator's own clause: a discontinuity may be money in or out.

    The gauge cannot tell a cash-out from a lost event, so an interval
    where a money meter moved is counted and reported, never scored.
    """
    conn = _ledger_db(tmp_path,
                      [("2026-10-28T20:00:00", 10_000),
                       ("2026-10-28T20:01:00", 2_000)],
                      money=[0, 8_000])          # an 8,000-credit cash-out
    evs = _play("2026-10-28T20:00:10", 400, 400)
    verdict, lines = check_credit_ledger(conn, evs, None, None)
    text = " ".join(lines)
    assert "1 skipped because money crossed the credit meter" in text
    assert verdict == "NO DATA", lines


def test_displacement_CANCELS_and_is_not_reported_as_loss(tmp_path):
    """A game landing on the wrong side of a reading moves the residual in
    one interval and back in the next. Loss accumulates; displacement does
    not, and only the SIGNED total tells them apart."""
    conn = _ledger_db(tmp_path, [("2026-10-28T20:00:00", 10_000),
                                 ("2026-10-28T20:01:00", 9_000),
                                 ("2026-10-28T20:02:00", 8_000)])
    # Both games wager 500 and win nothing, but the first game's events land
    # just after the reading that already took its credits.
    evs = (_play("2026-10-28T20:00:01", 500, 0)
           + _play("2026-10-28T20:00:02", 500, 0)
           + _play("2026-10-28T20:01:01", 500, 0))
    verdict, lines = check_credit_ledger(conn, evs, None, None)
    text = " ".join(lines)
    assert verdict != "FAIL", lines
    if "do not close" in text:
        assert "CANCEL" in text


def test_a_database_with_no_credit_gauge_says_so(tmp_path):
    path = tmp_path / "nogauge.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meters_history (id INTEGER PRIMARY KEY, "
                 "polled_at TEXT, total_coin_in INTEGER)")
    conn.commit()
    verdict, lines = check_credit_ledger(conn, [], None, None)
    assert verdict == "NO DATA"
    assert "the gauge is a poll, not an event" in " ".join(lines)


def test_an_interval_spanning_a_gateway_restart_is_skipped(tmp_path):
    conn = _ledger_db(tmp_path, [("2026-10-28T20:00:00", 10_000),
                                 ("2026-10-28T20:01:00", 5_000)])
    evs = _play("2026-10-28T20:00:10", 400, 0)
    verdict, lines = check_credit_ledger(
        conn, evs, None, None, boundaries=["2026-10-28T20:00:30"])
    assert "1 skipped for spanning a gateway restart" in " ".join(lines)
    assert verdict == "NO DATA"


def test_the_ledger_PREFERS_credit_samples_when_the_poller_wrote_them(tmp_path):
    """--credit-poll-every's whole purpose: resolution.

    One reading per cycle is ~25 games, coarser than a one-game fault. The
    sampler writes a reading every few seconds instead, which brings the
    interval down to roughly one game -- the operator's per-game chain as
    he actually wrote it.
    """
    conn = _ledger_db(tmp_path, [("2026-10-28T20:00:00", 100_000),
                                 ("2026-10-28T20:01:00", 99_700)])
    conn.execute("CREATE TABLE credit_samples (id INTEGER PRIMARY KEY, "
                 "sampled_at TEXT, credits INTEGER, heartbeats INTEGER)")
    for i, (at, credits) in enumerate([
            ("2026-10-28T20:00:03", 100_000),
            ("2026-10-28T20:00:08", 99_900),
            ("2026-10-28T20:00:13", 99_800),
            ("2026-10-28T20:00:18", 99_700)]):
        conn.execute("INSERT INTO credit_samples VALUES (?,?,?,?)",
                     (i + 1, at, credits, i + 1))
    evs = []
    for at, w, v in [("2026-10-28T20:00:05", 100, 0),
                     ("2026-10-28T20:00:10", 100, 0),
                     ("2026-10-28T20:00:15", 100, 0)]:
        evs += _play(at, w, v)
    verdict, lines = check_credit_ledger(conn, evs, None, None)
    text = " ".join(lines)
    assert "credit_samples" in text, text
    assert "the resolution" in text
    assert verdict == "PASS", lines


def test_without_the_sampler_it_says_how_to_get_the_resolution(tmp_path):
    conn, evs = _run(tmp_path, intervals=4, games_per=6, wager=100, win=50)
    _, lines = check_credit_ledger(conn, evs, None, None)
    text = " ".join(lines)
    assert "one per cycle" in text
    assert "--credit-poll-every" in text


def test_a_cash_out_between_fine_samples_is_still_skipped(tmp_path):
    """credit_samples carries only the gauge, so the money check borrows
    meters_history. An interval whose bracketing cycle readings disagree on
    money is skipped -- conservative in the right direction."""
    conn = _ledger_db(tmp_path,
                      [("2026-10-28T20:00:00", 100_000),
                       ("2026-10-28T20:01:00", 10_000)],
                      money=[0, 89_000])
    conn.execute("CREATE TABLE credit_samples (id INTEGER PRIMARY KEY, "
                 "sampled_at TEXT, credits INTEGER, heartbeats INTEGER)")
    for i, (at, credits) in enumerate([
            ("2026-10-28T20:00:03", 100_000),
            ("2026-10-28T20:00:08", 99_900),
            ("2026-10-28T20:01:03", 10_000)]):
        conn.execute("INSERT INTO credit_samples VALUES (?,?,?,?)",
                     (i + 1, at, credits, i + 1))
    evs = _play("2026-10-28T20:00:05", 100, 0) + _play("2026-10-28T20:00:30", 100, 0)
    verdict, lines = check_credit_ledger(conn, evs, None, None)
    text = " ".join(lines)
    assert "skipped because money crossed the credit meter" in text, text
    assert verdict != "FAIL", (
        "an 89,000-credit cash-out must never be reported as event loss")


def test_total_between_matches_the_naive_scan_it_replaced():
    """The fast path has to give the same answer, not just a fast one."""
    import random
    rng = random.Random(7)
    pairs = [(f"2026-10-28T20:{i // 60:02d}:{i % 60:02d}", rng.randint(0, 900))
             for i in range(400)]
    rng.shuffle(pairs)                      # order must not matter
    idx = _indexed(pairs)
    ordered = sorted(pairs)
    for t0, t1 in [("2026-10-28T20:00:00", "2026-10-28T20:06:39"),
                   ("2026-10-28T20:01:00", "2026-10-28T20:02:00"),
                   ("2026-10-28T20:03:30", "2026-10-28T20:03:30"),
                   ("2026-10-28T19:00:00", "2026-10-28T21:00:00")]:
        want = [v for at, v in ordered if t0 < at <= t1]
        total, n, edges = _total_between(idx, t0, t1)
        assert total == sum(want)
        assert n == len(want)
        assert edges == ([] if not want else
                         [want[0]] if len(want) == 1 else [want[0], want[-1]])


def test_the_ledger_does_not_go_QUADRATIC_on_a_real_sized_database(tmp_path):
    """The bug that stalled the operator's analysis bundle, 2026-11-07.

    The first version asked "which events fall in this interval?" with a
    list comprehension over every event, once per interval. On the
    whole-database run that is ~27,000 intervals against ~114,000 events --
    3.1 billion string comparisons, twice -- and it did not finish. The
    report had already pushed, so the bundle simply sat there with nothing
    to say why.

    The per-cycle gauge hid the cost; --credit-poll-every multiplies the
    interval count, so it would only have got worse. A detector nobody can
    afford to run is a detector that does not exist.

    The bound here is deliberately loose -- this is a complexity guard, not
    a benchmark. The quadratic version took minutes on a tenth of this.
    """
    import time

    intervals, per = 4_000, 6
    readings, evs, credits, t = [], [], 1_000_000, 0
    for k in range(intervals + 1):
        readings.append((f"2026-10-28T{t // 3600 % 24:02d}:"
                         f"{t // 60 % 60:02d}:{t % 60:02d}", credits))
        if k == intervals:
            break
        for _ in range(per):
            t += 2
            at = (f"2026-10-28T{t // 3600 % 24:02d}:"
                  f"{t // 60 % 60:02d}:{t % 60:02d}")
            evs += _play(at, 100, 50)
            credits -= 50
        t += 2
    conn = _ledger_db(tmp_path, readings)

    began = time.monotonic()
    verdict, _ = check_credit_ledger(conn, evs, None, None)
    elapsed = time.monotonic() - began
    assert elapsed < 10.0, (
        f"{intervals} intervals x {len(evs)} events took {elapsed:.1f}s -- "
        f"the interval lookup has gone back to scanning every event")
    assert verdict in ("PASS", "INCONCLUSIVE", "FAIL")


def test_the_two_gauges_are_NEVER_mixed(tmp_path):
    """What the first real run taught, 2026-11-07.

    c7_current_credits is read during the Table C-7 sweep;
    sm_current_credits during the single-meter sweep. Same quantity,
    different long polls, read tens of seconds apart in the same cycle.
    coalesce() produced a reading sequence whose boundaries jumped back and
    forth inside each cycle, so the events "between" two readings were not
    the events whose credits moved between them: 246 of 369 intervals did
    not close, almost evenly split either way, with 50,000-credit residuals
    in intervals holding a few thousand credits of play. Two clocks read as
    one.
    """
    path = tmp_path / "both.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meters_history (id INTEGER PRIMARY KEY, "
                 "polled_at TEXT, c7_current_credits INTEGER, "
                 "sm_current_credits INTEGER, total_cancelled_credits INTEGER)")
    # sm is written every cycle; c7 only on the sweep. Mixing them would
    # interleave two different sampling instants.
    for i, (at, c7, sm) in enumerate([
            ("2026-10-28T20:00:00", 10_000, 10_000),
            ("2026-10-28T20:01:00", None, 9_900),
            ("2026-10-28T20:02:00", 9_800, 9_800)]):
        conn.execute("INSERT INTO meters_history VALUES (?,?,?,?,0)",
                     (i + 1, at, c7, sm))
    conn.commit()
    evs = (_play("2026-10-28T20:00:30", 100, 0)
           + _play("2026-10-28T20:01:30", 100, 0))
    _, lines = check_credit_ledger(conn, evs, None, None)
    text = " ".join(lines)
    assert "sm_current_credits" in text and "NOT mixed in" in text, text
    assert "gauge sm_current_credits (3 readings)" in text, (
        "the column with the most readings must win, and only it may be used")


def test_a_boundary_model_that_fails_on_most_intervals_says_so(tmp_path):
    """Honesty guard. If two thirds of intervals do not close, that is not
    a loss -- a loss that frequent would light up detectors 2, 4 and 5 too.
    It means the boundary is mis-modelled, and the detector must say that
    rather than totalling the residuals and announcing a verdict."""
    # Each interval holds five games of 1,000 but the gauge moves 10,000:
    # a systematic mismatch between the two clocks, in every interval. The
    # per-interval tolerance is two games (2,000), so all 29 fail.
    readings, evs, credits = [], [], 100_000
    for k in range(30):
        readings.append((f"2026-10-28T20:{k:02d}:00", credits))
        if k == 29:
            break
        for g in range(5):
            evs += _play(f"2026-10-28T20:{k:02d}:{10 + g * 8:02d}", 1000, 0)
        credits -= 10_000
    conn = _ledger_db(tmp_path, readings)
    verdict, lines = check_credit_ledger(conn, evs, None, None)
    text = " ".join(lines)
    assert verdict == "INCONCLUSIVE", lines
    assert "THE BOUNDARY MODEL DOES NOT FIT THIS DATA" in text
    assert "Do not act on this detector" in text


def test_the_cancel_bound_is_the_TELESCOPING_one_not_the_widest_interval():
    """The bound that was doing no work.

    Pure displacement moves a game from one interval into its neighbour,
    so residuals come in equal and opposite pairs and the sum telescopes to
    the window's two outer edges. Testing the total against the LARGEST
    single interval instead made the bound 202,550 on the first real run --
    something almost nothing could exceed, so "the residuals CANCEL" was
    printed without having been earned.
    """
    from guards import python_code_only
    import inspect

    import examples.rte_reconcile as rr
    code = python_code_only(inspect.getsource(rr.check_credit_ledger))
    assert "tight" in code and "loose" in code
    # The clean-cancellation branch must test the TIGHT bound.
    assert "abs ( signed ) <= tight" in code, code[-2000:]


# --- detector 4's meter-anchored window ---------------------------------

def _meters_db(tmp_path, rows, name="m4.sqlite3"):
    """meters_history with polled_at, total_coin_in and games_played."""
    conn = sqlite3.connect(tmp_path / name)
    conn.execute("CREATE TABLE meters_history (id INTEGER PRIMARY KEY, "
                 "polled_at TEXT, total_coin_in INTEGER, games_played INTEGER, "
                 "c7_total_won_credits INTEGER)")
    for i, (at, coin, games) in enumerate(rows):
        conn.execute("INSERT INTO meters_history VALUES (?,?,?,?,NULL)",
                     (i + 1, at, coin, games))
    conn.commit()
    return conn


def test_the_anchored_window_is_EXACT_where_the_timestamp_one_is_smeared(tmp_path):
    """The whole point, and the lab run is the case it was built for.

    Detector 4 selected events by arrival time, and arrival time is not
    when the game happened: delivery lag, the startup drain and §12.3's
    interleaving smear the edge. Eighteen hours on the lab cabinet it
    reported "+18 games, -790 credits" -- frozen since the five-hour mark,
    because a fixed edge effect does not grow while a real loss does.

    §12.5.3 removes the boundary: every game start carries the coin-in it
    caused, so the games in a METER range telescope and their wagers sum
    to the delta exactly.

    Here the meters are read at 20:00 and 20:04 while every event arrives
    LATE, after the last reading -- the worst smear there is. The
    timestamp window sees nothing; the anchored one still closes.
    """
    conn = _meters_db(tmp_path, [("2026-10-28T20:00:00", 10_000, 100),
                                 ("2026-10-28T20:04:00", 10_400, 104)])
    evs = [ev(0x7E, at=f"2026-10-28T20:05:{i:02d}", wager_cents=100,
              credits_wagered=1, total_coin_in=10_100 + i * 100)
           for i in range(4)]
    _, lines = check_meters(conn, evs, None, None)
    text = " ".join(lines)
    assert "METER-ANCHORED" in text, text
    assert "EXACT: 4 game(s) in the meter range" in text, text
    assert "wagers sum to 400" in text


def test_the_anchored_window_reports_a_DROPPED_start_as_short(tmp_path):
    """It can only come out short, and only by the wager of a game whose
    event never arrived. No edge left to blame it on."""
    conn = _meters_db(tmp_path, [("2026-10-28T20:00:00", 10_000, 100),
                                 ("2026-10-28T20:04:00", 10_400, 104)])
    evs = [ev(0x7E, at=f"2026-10-28T20:0{1 + i}:00", wager_cents=100,
              credits_wagered=1, total_coin_in=10_100 + i * 100)
           for i in range(4)]
    del evs[2]                      # the third game's start never arrived
    verdict, lines = check_meters(conn, evs, None, None)
    text = " ".join(lines)
    assert "100 credits SHORT" in text, text
    assert "did not arrive" in text
    assert verdict == "FAIL"


def test_both_windows_are_printed_because_the_anchored_one_is_not_independent(tmp_path):
    """The cost, stated in the output rather than only in a commit.

    Anchoring to the events' own coin-in gives up detector 4's
    independence -- it stops being two unrelated sources of truth and
    becomes closer to a restatement of detector 2. So the timestamp
    comparison stays, and the reader gets both.
    """
    conn = _meters_db(tmp_path, [("2026-10-28T20:00:00", 10_000, 100),
                                 ("2026-10-28T20:04:00", 10_400, 104)])
    evs = [ev(0x7E, at=f"2026-10-28T20:0{1 + i}:00", wager_cents=100,
              credits_wagered=1, total_coin_in=10_100 + i * 100)
           for i in range(4)]
    _, lines = check_meters(conn, evs, None, None)
    text = " ".join(lines)
    assert "coin in: meters moved" in text, "the timestamp row must survive"
    assert "METER-ANCHORED" in text


def test_the_anchored_window_says_so_when_the_meter_is_unreadable(tmp_path):
    conn = sqlite3.connect(tmp_path / "nocoin.sqlite3")
    conn.execute("CREATE TABLE meters_history (id INTEGER PRIMARY KEY, "
                 "polled_at TEXT, games_played INTEGER)")
    for i, at in enumerate(["2026-10-28T20:00:00", "2026-10-28T20:04:00"]):
        conn.execute("INSERT INTO meters_history VALUES (?,?,?)",
                     (i + 1, at, 100 + i))
    conn.commit()
    _, lines = check_meters(conn, game_starts([100, 100]), None, None)
    assert "no meter-anchored check" in " ".join(lines)


def test_an_EXACT_anchored_window_outranks_the_smeared_timestamp_rows(tmp_path):
    """The noise the operator asked to be rid of.

    The anchored identity can only come out short, and only by a missing
    game start. Where it is exact, nothing is missing -- so the timestamp
    rows disagreeing is the boundary being fuzzy, not loss, and reporting
    FAIL on their account is the thing to stop doing.
    """
    conn = _meters_db(tmp_path, [("2026-10-28T20:00:00", 10_000, 100),
                                 ("2026-10-28T20:04:00", 10_400, 104)])
    evs = [ev(0x7E, at=f"2026-10-28T20:05:{i:02d}", wager_cents=100,
              credits_wagered=1, total_coin_in=10_100 + i * 100)
           for i in range(4)]
    verdict, lines = check_meters(conn, evs, None, None)
    text = " ".join(lines)
    assert verdict == "PASS", lines
    assert "VERDICT FROM THE ANCHORED WINDOW" in text


def test_a_failing_TOTAL_WON_still_fails_even_when_the_wager_side_is_exact(tmp_path):
    """The award side is not covered by the anchored check -- §12.5.4
    carries no cumulative win meter -- so it must still be able to fail.
    Downgrading the whole detector on an exact wager side would hide the
    one half that §19 says is the unaudited one."""
    conn = sqlite3.connect(tmp_path / "won.sqlite3")
    conn.execute("CREATE TABLE meters_history (id INTEGER PRIMARY KEY, "
                 "polled_at TEXT, total_coin_in INTEGER, games_played INTEGER, "
                 "c7_total_won_credits INTEGER)")
    for i, (at, coin, games, won) in enumerate([
            ("2026-10-28T20:00:00", 10_000, 100, 50_000),
            ("2026-10-28T20:04:00", 10_400, 104, 99_000)]):
        conn.execute("INSERT INTO meters_history VALUES (?,?,?,?,?)",
                     (i + 1, at, coin, games, won))
    conn.commit()
    evs = []
    for i in range(4):
        evs.append(ev(0x7E, at=f"2026-10-28T20:0{i}:10", wager_cents=100,
                      credits_wagered=1, total_coin_in=10_100 + i * 100))
        evs.append(ev(0x7F, at=f"2026-10-28T20:0{i}:20", game_win=25))
    # Meter says 49,000 was won; the events account for 100.
    verdict, lines = check_meters(conn, evs, None, None)
    text = " ".join(lines)
    assert "EXACT: 4 game(s) in the meter range" in text, text
    assert verdict == "FAIL", (
        "an exact wager side must not suppress a failing award side")


# --- the operator's end-side count: games won + games lost ---------------

def _ends_db(tmp_path, rows, name="ends.sqlite3"):
    """meters_history with the won/lost pair beside games_played."""
    conn = sqlite3.connect(tmp_path / name)
    conn.execute("CREATE TABLE meters_history (id INTEGER PRIMARY KEY, "
                 "polled_at TEXT, total_coin_in INTEGER, games_played INTEGER, "
                 "sm_games_won INTEGER, sm_games_lost INTEGER)")
    for i, r in enumerate(rows):
        conn.execute("INSERT INTO meters_history VALUES (?,?,?,?,?,?)",
                     (i + 1, *r))
    conn.commit()
    return conn


def _game(at, coin, win):
    return [ev(0x7E, at=at, wager_cents=100, credits_wagered=1,
               total_coin_in=coin),
            ev(0x7F, at=at[:-1] + "9", game_win=win)]


def test_game_ends_are_counted_against_games_won_plus_lost(tmp_path):
    """The operator's idea, 2026-11-07, and the gap it fills.

    Detectors 2 and 6 audit game STARTS. Detector 5 audits award CREDITS.
    Nothing had ever counted game ENDS -- and §19 measures ends as the
    half that survives more often, so the unaudited side was the one the
    evidence pointed at.

    It costs no new polling: games played is LP 0x0F every cycle, won and
    lost are LP 0x16/0x17 in the single-meter sweep already running.
    """
    conn = _ends_db(tmp_path, [
        ("2026-10-28T20:00:00", 10_000, 100, 60, 40),
        ("2026-10-28T20:04:00", 10_400, 104, 61, 43)])      # 4 more games
    evs = []
    for i in range(4):
        evs += _game(f"2026-10-28T20:0{i}:10", 10_100 + i * 100, 0)
    _, lines = check_meters(conn, evs, None, None)
    text = " ".join(lines)
    assert "game ends (won + lost): meters moved 4, events account for 4" in text, text
    assert "games won 1 + lost 3" in text


def test_a_LOST_GAME_END_is_caught_by_the_count(tmp_path):
    conn = _ends_db(tmp_path, [
        ("2026-10-28T20:00:00", 10_000, 100, 60, 40),
        ("2026-10-28T20:09:00", 10_900, 109, 64, 45)])      # 9 more games
    evs = []
    for i in range(9):
        evs += _game(f"2026-10-28T20:0{i}:10", 10_100 + i * 100, 0)
    evs = [e for e in evs if not (e["code"] == 0x7F and e["at"].startswith(
        "2026-10-28T20:04"))]                               # one end dropped
    verdict, lines = check_meters(conn, evs, None, None)
    text = " ".join(lines)
    assert "game ends (won + lost): meters moved 9, events account for 8" in text, text
    assert verdict in ("FAIL", "INCONCLUSIVE")


def test_the_identity_is_CHECKED_not_assumed(tmp_path):
    """If won + lost is further from played than a cycle's sampling skew
    can explain, the sum cannot stand in for a count of ends, and saying
    so beats inventing a finding."""
    rows, coin, played, won, lost = [], 10_000, 100, 60, 40
    for k in range(11):
        rows.append((f"2026-10-28T20:{k:02d}:00", coin, played, won, lost))
        coin += 1_000
        played += 10
        won += 2          # won + lost gains 7 where played gains 10:
        lost += 5         # a third category, far beyond one cycle's skew
    conn = _ends_db(tmp_path, rows)
    evs = []
    for i in range(100):
        evs += _game(f"2026-10-28T20:{i // 10:02d}:{10 + (i % 10) * 4:02d}",
                     10_100 + i * 100, 0)
    _, lines = check_meters(conn, evs, None, None)
    text = " ".join(lines)
    assert "END-SIDE COUNT UNUSABLE on this cabinet" in text, text
    assert "more than the" in text and "sampling skew can explain" in text
    assert "game ends (won + lost): meters moved" not in text, (
        "it must not compare against a sum it has just shown to be wrong")


def test_a_few_games_apart_is_SKEW_not_a_broken_meter(tmp_path):
    """The bug the lab run exposed, 2026-11-07, one function away from the
    identical bug just fixed in detector 5.

    games played comes from LP 0x0F; won and lost from LP 0x16/0x17 tens
    of seconds later in the same sweep. Three meters, three sampling
    instants. Demanding exact agreement declared the cabinet broken on a
    difference of 3 in 28,021 -- about seven seconds of play.
    """
    rows, coin, played, won, lost = [], 10_000, 100, 60, 40
    for k in range(11):
        rows.append((f"2026-10-28T20:{k:02d}:00", coin, played, won, lost))
        coin += 1_000
        played += 10
        won += 3
        lost += 7
    rows[-1] = (rows[-1][0], rows[-1][1], rows[-1][2] - 2, rows[-1][3],
                rows[-1][4])          # played trails by 2: skew, not loss
    conn = _ends_db(tmp_path, rows)
    evs = []
    for i in range(100):
        evs += _game(f"2026-10-28T20:{i // 10:02d}:{10 + (i % 10) * 4:02d}",
                     10_100 + i * 100, 0)
    _, lines = check_meters(conn, evs, None, None)
    text = " ".join(lines)
    assert "UNUSABLE" not in text, text
    assert "Inside one cycle's worth of play" in text
    # And it is MEASURED, not guessed: the gap is computed at every reading
    # and the first third compared against the last.
    assert "NOT DRIFTING" in text, text
    assert "sampling skew" in text


def test_a_DRIFTING_gap_is_a_real_third_category_not_skew(tmp_path):
    """The question two endpoint readings cannot answer.

    On 2026-11-07 the gap read +3 on one bundle and +4 twenty minutes
    later, and guessing from two samples is not a measurement. Every
    meters_history row already holds all three meters, so the gap can be
    computed per reading for nothing: skew oscillates around a constant,
    a real third category accumulates.
    """
    rows, coin, played, won, lost = [], 10_000, 100, 60, 40
    for k in range(12):
        rows.append((f"2026-10-28T20:{k:02d}:00", coin, played, won, lost))
        coin += 1_000
        played += 10
        won += 5          # won + lost gains 15 where played gains 10,
        lost += 10        # every single cycle: it accumulates
    conn = _ends_db(tmp_path, rows)
    evs = []
    for i in range(110):
        evs += _game(f"2026-10-28T20:{i // 10:02d}:{10 + (i % 10) * 4:02d}",
                     10_100 + i * 100, 0)
    _, lines = check_meters(conn, evs, None, None)
    text = " ".join(lines)
    assert "DRIFTING" in text and "NOT DRIFTING" not in text, text
    assert "real third category accumulating" in text


def test_no_won_lost_pair_says_the_end_side_has_no_anchor(tmp_path):
    conn = _meters_db(tmp_path, [("2026-10-28T20:00:00", 10_000, 100),
                                 ("2026-10-28T20:04:00", 10_400, 104)])
    evs = []
    for i in range(4):
        evs += _game(f"2026-10-28T20:0{i}:10", 10_100 + i * 100, 0)
    _, lines = check_meters(conn, evs, None, None)
    assert "game ENDS have no count anchor" in " ".join(lines)


def test_an_UNMOVED_meter_is_not_independent_corroboration(tmp_path):
    """Found in the 2026-11-07 bundle, 18h39m into a clean run.

    "total_coin_out alone" and "total_coin_out + total_jackpot" differ by
    exactly one meter. With no attendant jackpot in the window that meter
    is zero, so the two candidates are the SAME NUMBER -- and the detector
    announced "TWO INDEPENDENT METER PATHS AGREE... off by 0 -- 0.000%".
    One path counted twice.

    Not cosmetic: `best` is picked from the agreeing pair, so it anchored
    on total_coin_out and reported -3,220 unexplained, where
    c7_total_won_credits -- a genuinely different poll -- sat +520 from
    the events. Six times the residual, and the opposite sign.
    """
    conn = _payout_db(tmp_path, [
        {"total_coin_out": 1000, "total_jackpot": 500,
         "c7_total_won_credits": 1000},
        # coin out +900, jackpot UNMOVED, c7 +910
        {"total_coin_out": 1900, "total_jackpot": 500,
         "c7_total_won_credits": 1910},
    ])
    _, lines = check_payout_identity(conn, game_ends([400, 510]), None, None)
    text = " ".join(lines)
    assert "off by 0 -- 0.000%" not in text, text
    if "TWO INDEPENDENT METER PATHS AGREE" in text:
        assert "total_jackpot" not in text.split(
            "TWO INDEPENDENT METER PATHS AGREE")[1][:400], (
            "the jackpot pair is the tautology; it must not be the one "
            "reported as agreement")


def test_two_genuinely_different_polls_still_count_as_agreement(tmp_path):
    """The other half: where the jackpot meter DID move, coin_out and
    coin_out+jackpot are different numbers from different meters, and
    their agreeing with c7 is the real signal this block exists for."""
    conn = _payout_db(tmp_path, [
        {"total_coin_out": 1000, "total_jackpot": 500,
         "c7_total_won_credits": 1500},
        {"total_coin_out": 1600, "total_jackpot": 800,
         "c7_total_won_credits": 2400},
    ])
    _, lines = check_payout_identity(conn, game_ends([450, 450]), None, None)
    text = " ".join(lines)
    assert "TWO INDEPENDENT METER PATHS AGREE" in text, text


def test_a_nested_pair_is_not_corroboration_even_when_the_meter_MOVED(tmp_path):
    """The 2026-11-11 case, and the other half of the unmoved-meter bug.

    The earlier guard skipped a nested pair only when the extra meter was
    ZERO. Here it moved -- by 205,000 on the real cabinet -- and the pair
    was accepted anyway, because 205,000 is 0.986% of a 20.8M total and
    the agreement bar is 1%.

    Read what accepting it says. "coin_out" and "coin_out + jackpot"
    differ by exactly the meter whose inclusion IS the question. Calling
    them agreed is asserting that the disputed quantity is small and then
    using that assertion to settle the dispute. They are not two
    measurements; they are one measurement and one hypothesis about it.

    Scaled down from the real numbers, same shape: the jackpot delta is
    just under 1% of the award total.
    """
    conn = _payout_db(tmp_path, [
        {"total_coin_out": 0, "total_jackpot": 0, "c7_total_won_credits": 0},
        # coin out +1,000,000; jackpot +9,000 (0.89% of 1,009,000);
        # c7 +1,009,000 == coin_out + jackpot, to the unit.
        {"total_coin_out": 1_000_000, "total_jackpot": 9_000,
         "c7_total_won_credits": 1_009_000},
    ])
    _, lines = check_payout_identity(conn, game_ends([500_000, 500_000]),
                                     None, None)
    text = " ".join(lines)
    assert "TWO INDEPENDENT METER PATHS AGREE" in text, text
    block = text.split("TWO INDEPENDENT METER PATHS AGREE")[1]
    # The ONLY pair that may be named is the disjoint one. A nested pair
    # would show the 0.89% agreement; the real one is 0.000%.
    assert "0.892%" not in block and "0.890%" not in block, block
    assert "off by 0 -- 0.000%" in block, block


def test_the_tightest_agreement_anchors_the_comparison(tmp_path):
    """`best` takes agreeing[0], so the ordering of that list is a finding,
    not a detail.

    It used to be whichever pair the nested loops emitted first -- candidate
    order, which is arbitrary with respect to the evidence. Two polls landing
    on the same number to three decimals is a different quality of evidence
    from two landing within a percent, and the ranking has to say so.

    Here the events are deliberately SHORT of the meters, which is the
    lost-game-end signature. Anchoring on coin_out alone would understate
    the residual and, on the real data, flipped its sign.
    """
    conn = _payout_db(tmp_path, [
        {"total_coin_out": 0, "total_jackpot": 0, "c7_total_won_credits": 0},
        {"total_coin_out": 1_000_000, "total_jackpot": 9_000,
         "c7_total_won_credits": 1_009_000},
    ])
    # Events account for 1,004,000 -- 5,000 short of the 1,009,000 the two
    # independent polls agree on, and 4,000 OVER coin_out alone.
    _, lines = check_payout_identity(conn, game_ends([502_000, 502_000]),
                                     None, None)
    text = " ".join(lines)
    if "closest was" in text:
        anchor = text.split("closest was")[1][:200]
        assert "1009000" in anchor or "corroborated" in anchor, anchor
        assert "total_coin_out alone" not in anchor.split(",")[0], (
            "anchored on the nested candidate again: " + anchor)
