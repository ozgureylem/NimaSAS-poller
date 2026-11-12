"""The migration's own scorecard, detected from the SEQUENCE.

The reason for moving off the current system is "unwanted handpays and
wrong player data". Nothing counted the first until this tool, so "fewer
unwanted handpays" was a claim nobody could audit.

    0x66 cash out pressed -> 0x57 validation request -> 0x51 handpay

A handpay that follows a validation request with no ticket printed in
between was caused by the host not answering §15.7's ten seconds. The
sequence reads the cause off the wire instead of inferring it from the
amount, and an earlier version of this tool did infer it from the amount --
which needed a ticket-out limit that is variable per property, per cabinet
and per operator decision.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from unwanted_handpays import (  # noqa: E402
    CASHOUT_PRESSED, HANDPAY_PENDING, TICKET_PRINTED, VALIDATION_REQUEST,
    arrived_as_backlog, find_unwanted, where_it_went_wrong,
)

SCRIPT = ROOT / "examples" / "unwanted_handpays.py"


def _db(tmp_path, events, handpays=(), pool_errors=()):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "s.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE machine_events (id INTEGER PRIMARY KEY, "
                 "occurred_at TEXT, exception_code INTEGER, description TEXT, "
                 "source TEXT, rte_fields TEXT)")
    conn.execute("CREATE TABLE handpay_queue (id INTEGER PRIMARY KEY, "
                 "captured_at TEXT, progressive_group INTEGER, level INTEGER, "
                 "amount INTEGER, partial_pay INTEGER, reset_id INTEGER, "
                 "all_zeros INTEGER NOT NULL)")
    conn.execute("CREATE TABLE poll_errors (id INTEGER PRIMARY KEY, "
                 "occurred_at TEXT, poll_name TEXT, error_type TEXT, "
                 "message TEXT)")
    for event in events:
        # (at, code) keeps every existing test unchanged; (at, code, source)
        # is for the ones that care HOW the event reached the gateway.
        at, code, source = (event if len(event) == 3 else (*event, "rte"))
        conn.execute("INSERT INTO machine_events (occurred_at, "
                     "exception_code, description, source, rte_fields) "
                     "VALUES (?,?,'x',?,NULL)", (at, code, source))
    for at, amount in handpays:
        conn.execute("INSERT INTO handpay_queue (captured_at, "
                     "progressive_group, level, amount, partial_pay, "
                     "reset_id, all_zeros) VALUES (?,0,0,?,0,1,0)", (at, amount))
    for at in pool_errors:
        conn.execute("INSERT INTO poll_errors (occurred_at, poll_name, "
                     "error_type, message) VALUES (?, 'validation_pool', "
                     "'PoolExhausted', 'none available')", (at,))
    conn.commit()
    return path, conn


def _run(db, *args):
    return subprocess.run([sys.executable, str(SCRIPT), str(db), *args],
                          capture_output=True, text=True, timeout=60)


# --- the sequence -------------------------------------------------------

def test_request_then_handpay_with_no_ticket_is_unwanted(tmp_path):
    _, conn = _db(tmp_path, [
        ("2026-11-06T12:00:00", CASHOUT_PRESSED),
        ("2026-11-06T12:00:01", VALIDATION_REQUEST),
        ("2026-11-06T12:00:33", HANDPAY_PENDING),
    ])
    found = find_unwanted(conn)
    assert len(found) == 1
    assert found[0]["seconds"] == 32
    assert found[0]["button_at"] == "2026-11-06T12:00:00"


def test_a_ticket_printed_in_between_clears_it(tmp_path):
    """The successful path. The host answered, the ticket printed, and a
    later lockup is a different event entirely."""
    _, conn = _db(tmp_path, [
        ("2026-11-06T12:00:00", CASHOUT_PRESSED),
        ("2026-11-06T12:00:01", VALIDATION_REQUEST),
        ("2026-11-06T12:00:02", TICKET_PRINTED),
        ("2026-11-06T12:00:40", HANDPAY_PENDING),
    ])
    assert find_unwanted(conn) == []


def test_A_JACKPOT_IS_INVISIBLE_TO_THIS(tmp_path):
    """The reason the sequence beats a threshold.

    A jackpot lockup raises no validation request -- it never asks the host
    for anything. So it cannot be counted here however large or small, and
    NO jackpot limit has to be configured to exclude it. The earlier,
    amount-based version needed one, and needed it to be correct on a floor
    where the ticket-out limit varies by property and by cabinet."""
    _, conn = _db(tmp_path,
                  [("2026-11-06T11:00:00", HANDPAY_PENDING)],
                  handpays=[("2026-11-06T11:00:00", 504_500)])
    assert find_unwanted(conn) == []


def test_a_handpay_far_outside_the_window_is_not_attributed(tmp_path):
    """A request answered an hour ago cannot have caused this lockup."""
    _, conn = _db(tmp_path, [
        ("2026-11-06T12:00:00", VALIDATION_REQUEST),
        ("2026-11-06T13:00:00", HANDPAY_PENDING),
    ])
    assert find_unwanted(conn) == []


def test_the_window_is_wider_than_SS15_7s_ten_seconds(tmp_path):
    """These are GATEWAY CAPTURE times. Under a full Table C-7 sweep an
    event can be stamped up to a whole cycle after it happened -- 46 to 58
    seconds measured here -- so a 10-second window would miss the slow
    captures and UNDER-count the defect. Wrong direction for a scorecard."""
    from unwanted_handpays import DEFAULT_WINDOW_SECONDS
    assert DEFAULT_WINDOW_SECONDS >= 60
    _, conn = _db(tmp_path, [
        ("2026-11-06T12:00:00", VALIDATION_REQUEST),
        ("2026-11-06T12:00:45", HANDPAY_PENDING),
    ])
    assert len(find_unwanted(conn)) == 1


def test_one_request_is_charged_at_most_once(tmp_path):
    """Two lockups after one request must not count as two defects: the
    second 0x51 is a reissue of the same queue entry (§7.8.1 reissues every
    15 s until LP 0x1B reads it)."""
    _, conn = _db(tmp_path, [
        ("2026-11-06T12:00:00", VALIDATION_REQUEST),
        ("2026-11-06T12:00:20", HANDPAY_PENDING),
        ("2026-11-06T12:00:35", HANDPAY_PENDING),
    ])
    assert len(find_unwanted(conn)) == 1


# --- separating the causes ----------------------------------------------

def test_an_empty_pool_is_named_as_OUR_failure(tmp_path):
    """The one cause the gateway's own database can settle, because it is
    the gateway's own: PoolExhausted is written the moment a cashout finds
    no available number."""
    db, _ = _db(tmp_path, [
        ("2026-11-06T13:00:00", CASHOUT_PRESSED),
        ("2026-11-06T13:00:01", VALIDATION_REQUEST),
        ("2026-11-06T13:00:20", HANDPAY_PENDING),
    ], pool_errors=["2026-11-06T13:00:02"])
    out = _run(db)
    assert out.returncode == 1
    assert "pool-empty" in out.stdout
    assert "Ours, not the network's" in out.stdout


def test_a_printer_fault_nearby_is_offered_as_an_alternative(tmp_path):
    """A ticket that could not print would have failed whatever the host
    did, so this must not be reported as a host defect without a caveat."""
    db, _ = _db(tmp_path, [
        ("2026-11-06T14:00:00", VALIDATION_REQUEST),
        ("2026-11-06T14:00:05", 0x61),            # printer paper out
        ("2026-11-06T14:00:20", HANDPAY_PENDING),
    ])
    out = _run(db)
    assert out.returncode == 1
    assert "printer fault" in out.stdout
    assert "unprintable whatever the host did" in out.stdout


def test_with_pool_and_printer_both_fine_it_points_at_the_host(tmp_path):
    db, _ = _db(tmp_path, [
        ("2026-11-06T12:00:00", VALIDATION_REQUEST),
        ("2026-11-06T12:00:30", HANDPAY_PENDING),
    ])
    out = _run(db)
    assert out.returncode == 1
    assert "host-silent" in out.stdout
    assert "NOBODY ANSWERED" in out.stdout


# --- the refusals -------------------------------------------------------

def test_no_validation_request_at_all_is_NOT_a_pass(tmp_path):
    """An idle window proves nothing. Returning 0 would let a gateway that
    never saw a cashout look like one that handled them all."""
    db, _ = _db(tmp_path, [("2026-11-06T11:00:00", HANDPAY_PENDING)])
    out = _run(db)
    assert out.returncode == 3
    assert "nothing proves the gateway would answer one" in out.stdout


def test_all_requests_answered_is_a_clean_pass(tmp_path):
    db, _ = _db(tmp_path, [
        ("2026-11-06T10:00:00", CASHOUT_PRESSED),
        ("2026-11-06T10:00:01", VALIDATION_REQUEST),
        ("2026-11-06T10:00:02", TICKET_PRINTED),
    ])
    out = _run(db)
    assert out.returncode == 0
    assert "No unwanted handpay" in out.stdout


def test_it_takes_NO_money_limits_at_all(tmp_path):
    """The correction that produced this version. The ticket-out limit is
    variable per property, per cabinet and per operator decision, so a
    classifier keyed to it is only right on the day somebody types it in."""
    src = SCRIPT.read_text()
    for gone in ("--jackpot-limit", "--ticket-out-limit", "jackpot_limit",
                 "ticket_out_limit"):
        assert gone not in src, gone


def test_the_amount_is_reported_but_never_classifies(tmp_path):
    """It is useful context for whoever reads the row, and it must not be
    what decides the row is there."""
    import ast
    tree = ast.parse(SCRIPT.read_text())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "find_unwanted")
    assert "amount" not in ast.dump(fn)


def test_it_never_opens_the_serial_port():
    src = SCRIPT.read_text()
    for forbidden in ("open_serial_port", "SASTransport", "SASClient"):
        assert forbidden not in src
    assert "mode=ro" in src


# --- the two false positives from the first real run --------------------

def test_a_ZERO_second_gap_is_not_evidence_of_a_sequence(tmp_path):
    """Both findings on the first run against real data read "locked up 0s
    later", and both were wrong.

    The poller drains a cycle's events in one batch, and before 2026-11-06
    stamped them all with the cycle's own time. So a 0x57 and a 0x51
    captured in the same cycle share a timestamp and their real order is
    unknowable -- the SQL ordering is an accident of insert order.

    §7.8.1 makes this likely rather than rare: it REISSUES 0x51 every
    fifteen seconds until LP 0x1B reads the queue, so a handpay already
    waiting keeps firing and lands in the same batch as the next cashout's
    0x57."""
    _, conn = _db(tmp_path, [
        ("2026-10-26T19:12:51", CASHOUT_PRESSED),
        ("2026-10-26T19:13:02", VALIDATION_REQUEST),
        ("2026-10-26T19:13:02", HANDPAY_PENDING),      # same second
    ])
    assert find_unwanted(conn) == []


def test_a_ticket_ANYWHERE_in_the_window_clears_the_request(tmp_path):
    """The real shape of the first false positive: request, a same-second
    0x51 from an unrelated waiting handpay, then the ticket printing eleven
    seconds later. The cabinet printed, so the host answered, so this is not
    the defect -- whatever order the batch happened to be stored in."""
    _, conn = _db(tmp_path, [
        ("2026-10-26T19:12:51", CASHOUT_PRESSED),
        ("2026-10-26T19:13:02", VALIDATION_REQUEST),
        ("2026-10-26T19:13:05", HANDPAY_PENDING),
        ("2026-10-26T19:13:13", TICKET_PRINTED),       # it worked
    ])
    assert find_unwanted(conn) == []


def test_a_ticket_printed_AFTER_the_window_does_not_clear_it(tmp_path):
    """A cashout five minutes later is a different cashout. Clearing on it
    would hide every defect on a busy machine."""
    _, conn = _db(tmp_path, [
        ("2026-11-06T12:00:00", VALIDATION_REQUEST),
        ("2026-11-06T12:00:30", HANDPAY_PENDING),
        ("2026-11-06T12:05:00", TICKET_PRINTED),
    ])
    assert len(find_unwanted(conn)) == 1


def test_NO_VALIDATION_REQUEST_MEANS_A_LIMIT_even_with_an_AFT_request(tmp_path):
    """The operator, settling it: "if there's no system validation request,
    it is most definitely a limit issue, it probably exceeds machine's
    ticket out limit."

    This is the 2026-11-06T20:10:27 handpay, which he described at the time
    as "cashed out with handpay because AFT host didn't answer". The wire
    says otherwise, and the wire is right: the printer limit was still its
    OLD value at 20:10 -- he changed it at 20:11:07 -- so the cabinet had
    already decided it could not print that amount and never asked anybody.

    The 0x6A does not change that. Stage 1 of the ladder runs on every
    cashout whether or not an AFT host exists, so its presence says the
    cabinet tried AFT first -- which costs about eight seconds -- not that
    AFT is why this became a handpay. An earlier draft of this detector made
    it a separate cause and would have blamed the network for a correctly
    enforced limit."""
    from unwanted_handpays import UNWANTED, where_it_went_wrong
    _, conn = _db(tmp_path, [
        ("2026-11-06T20:10:20", CASHOUT_PRESSED),
        ("2026-11-06T20:10:25", 0x6A),                 # AFT host cashout
        ("2026-11-06T20:10:27", HANDPAY_PENDING),
    ])
    found = find_unwanted(conn)
    assert len(found) == 1
    assert found[0]["requested_at"] is None
    where = where_it_went_wrong(conn, found[0], 90.0)
    assert where == "machine-limit"
    assert where not in UNWANTED, "a limit doing its job is not a defect"


def test_a_limit_refusal_and_a_silent_host_are_told_apart(tmp_path):
    """The pair that matters, and the whole reason for reading event ORDER.

    Same button, same lockup, same absence of a ticket. The only difference
    is whether a 0x57 was ever raised -- and it is the difference between a
    limit working and a player waiting on a host that never answered."""
    from unwanted_handpays import UNWANTED, where_it_went_wrong

    _, limit = _db(tmp_path / "a", [
        ("2026-11-06T12:00:00", CASHOUT_PRESSED),
        ("2026-11-06T12:00:09", HANDPAY_PENDING),
    ])
    (tmp_path / "b").mkdir(exist_ok=True)
    _, silent = _db(tmp_path / "b", [
        ("2026-11-06T12:00:00", CASHOUT_PRESSED),
        ("2026-11-06T12:00:01", VALIDATION_REQUEST),
        ("2026-11-06T12:00:12", HANDPAY_PENDING),
    ])

    a = find_unwanted(limit)
    b = find_unwanted(silent)
    assert len(a) == len(b) == 1
    assert where_it_went_wrong(limit, a[0], 90.0) == "machine-limit"
    assert where_it_went_wrong(silent, b[0], 90.0) == "host-silent"
    assert where_it_went_wrong(limit, a[0], 90.0) not in UNWANTED
    assert where_it_went_wrong(silent, b[0], 90.0) in UNWANTED



def test_PAPER_LOW_is_advisory_and_must_not_excuse_a_host_failure(tmp_path):
    """0x74 fires on EGT cabinets with more than FIFTEEN tickets still
    loaded, and the operator's policy is to run to empty on purpose:
    "what are we going to do with the 15 tickets left, and if we pull them
    out, where are we going to use them."

    This tool reports a nearby printer fault as a reason the ticket "may
    have been unprintable whatever the host did". Counting an advisory as
    one would EXCUSE A GENUINE HOST FAILURE on a cabinet with plenty of
    paper -- exactly backwards for a scorecard whose job is attributing
    blame."""
    from unwanted_handpays import PRINTER_ADVISORIES, PRINTER_FAULTS
    assert 0x74 not in PRINTER_FAULTS
    assert 0x74 in PRINTER_ADVISORIES

    db, _ = _db(tmp_path, [
        ("2026-11-06T14:00:00", VALIDATION_REQUEST),
        ("2026-11-06T14:00:05", 0x74),            # paper LOW, not out
        ("2026-11-06T14:00:30", HANDPAY_PENDING),
    ])
    out = _run(db)
    assert out.returncode == 1
    # Assert the CONCLUSION, not the phrase. "printer fault" appears in the
    # tool's own "no printer fault is logged", so the obvious check matches
    # the sentence that proves the opposite. Seventh instance of a substring
    # guard matching its subject on this project -- and the first inside a
    # test written by the person who had just documented the other six.
    assert "unprintable whatever the host did" not in out.stdout, (
        "a paper-LOW advisory was treated as a reason the ticket could not "
        "print, excusing the host")
    assert "host-silent" in out.stdout


def test_PAPER_OUT_is_still_a_real_fault(tmp_path):
    """0x61 is the one that matters: the machine locks and cannot print."""
    from unwanted_handpays import PRINTER_FAULTS
    assert 0x61 in PRINTER_FAULTS
    db, _ = _db(tmp_path, [
        ("2026-11-06T14:00:00", VALIDATION_REQUEST),
        ("2026-11-06T14:00:05", 0x61),
        ("2026-11-06T14:00:30", HANDPAY_PENDING),
    ])
    out = _run(db)
    assert "unprintable whatever the host did" in out.stdout


# --- the gateway was not there to fail ----------------------------------
#
# Operator, 2026-11-08, about a lockup during a deliberate gap between
# soaks: "the earlier handpay doesn't count as undesired one. because
# everything was in norms. gateway wasn't running. don't add to the
# statistics."


def test_a_lockup_the_gateway_SLEPT_THROUGH_is_not_charged_to_the_host(tmp_path):
    """The whole sequence, but reported on reconnect.

    source='startup-backlog' is the poller's own record that the cabinet
    had queued this while nothing was listening. No host was late; there
    was no host.
    """
    _, conn = _db(tmp_path, [
        ("2026-11-08T18:18:00", CASHOUT_PRESSED, "startup-backlog"),
        ("2026-11-08T18:18:01", VALIDATION_REQUEST, "startup-backlog"),
        ("2026-11-08T18:18:43", HANDPAY_PENDING, "startup-backlog"),
    ])
    found = find_unwanted(conn)
    assert len(found) == 1, "the sequence is still detected -- it happened"
    assert where_it_went_wrong(conn, found[0], 90.0) == "outage"


def test_THE_SAME_SEQUENCE_LIVE_IS_STILL_A_HOST_FAILURE(tmp_path):
    """The exclusion must not leak into a lockup with the poller running.

    This is the pair that matters: identical events, identical timings,
    and the ONLY difference is how they reached the gateway. If this ever
    returns 'outage' the scorecard has stopped counting real defects.
    """
    _, conn = _db(tmp_path, [
        ("2026-11-08T18:18:00", CASHOUT_PRESSED, "rte"),
        ("2026-11-08T18:18:01", VALIDATION_REQUEST, "rte"),
        ("2026-11-08T18:18:43", HANDPAY_PENDING, "rte"),
    ])
    found = find_unwanted(conn)
    assert len(found) == 1
    assert where_it_went_wrong(conn, found[0], 90.0) == "host-silent"


def test_the_excluded_lockup_is_PRINTED_not_silently_dropped(tmp_path):
    """Excluded from the count, kept in the report.

    The mechanism is exactly what a CMS outage does on a live floor, which
    is the migration's own argument. Deleting the evidence would throw
    that away to tidy a number.
    """
    db, _ = _db(tmp_path, [
        ("2026-11-08T18:18:00", CASHOUT_PRESSED, "startup-backlog"),
        ("2026-11-08T18:18:01", VALIDATION_REQUEST, "startup-backlog"),
        ("2026-11-08T18:18:43", HANDPAY_PENDING, "startup-backlog"),
    ])
    out = _run(db).stdout
    assert "excluded" in out.lower()
    assert "18:18:43" in out
    assert "0 UNWANTED" in out


def test_backlog_is_read_off_the_stored_source_not_guessed_from_a_gap(tmp_path):
    _, conn = _db(tmp_path, [
        ("2026-11-08T18:18:43", HANDPAY_PENDING, "startup-backlog"),
        ("2026-11-08T19:00:00", HANDPAY_PENDING, "rte-interleaved"),
    ])
    assert arrived_as_backlog(conn, "2026-11-08T18:18:43") is True
    assert arrived_as_backlog(conn, "2026-11-08T19:00:00") is False


# --- A cash out and its lockup inside ONE sweep -------------------------
#
# 2026-11-11, on the lab cabinet. A cash out, eight unanswered 0x6A and a
# 0x51 all landed in one 56-second sweep, so the poller stamped every one
# of them 22:26:54.099103. The gap was 0.0, `0 < gap` rejected it, and the
# tool reported "5 cash out(s) ended in a lockup" when there had been six.
#
# Not misclassified. NOT COUNTED. And the longer the sweep the likelier it
# is, so the scorecard went blind exactly when the host is most loaded --
# sweeps that night ran 37s to 131s.
#
# The 0x57 walk rejects a zero gap on purpose and keeps doing so: §7.8.1
# reissues 0x51 every fifteen seconds, so an OLD handpay can collide with
# a NEW 0x57 and did, twice. A cash out button press is not reissued, so
# in this walk a shared stamp is a sequence with a clock too coarse to
# show it, and the row id resolves it.

BATCH = "2026-11-11T22:26:54.099103+00:00"


def test_a_cashout_and_its_lockup_in_ONE_sweep_are_still_counted(tmp_path):
    _, conn = _db(tmp_path / "same", [
        (BATCH, CASHOUT_PRESSED),
        (BATCH, HANDPAY_PENDING),
    ])
    found = find_unwanted(conn)
    assert len(found) == 1, (
        "a lockup that shares its cash out's cycle stamp must still be "
        f"counted, got {found}")
    assert found[0]["requested_at"] is None, "no 0x57 was raised"
    assert found[0]["seconds"] == 0


def test_the_ORDER_inside_the_batch_still_has_to_be_right(tmp_path):
    """id is insert order and insert order is arrival order. A 0x51 filed
    BEFORE the button cannot have been caused by it -- that is the
    reissued-handpay case the 0x57 walk guards against, and this walk
    must not become the hole it closed."""
    _, conn = _db(tmp_path / "rev", [
        (BATCH, HANDPAY_PENDING),     # id 1 -- already pending
        (BATCH, CASHOUT_PRESSED),     # id 2 -- pressed afterwards
    ])
    assert find_unwanted(conn) == [], \
        "a handpay filed before the button must not pair with it"


def test_a_ticket_in_the_same_batch_still_clears_it(tmp_path):
    """The cabinet printed, so the host answered. Unchanged by the id
    tiebreak, and worth pinning: this is the difference between a lockup
    and a cashout that worked."""
    _, conn = _db(tmp_path / "tk", [
        (BATCH, CASHOUT_PRESSED),
        (BATCH, TICKET_PRINTED),
        (BATCH, HANDPAY_PENDING),
    ])
    assert find_unwanted(conn) == []


def test_the_0x57_walk_still_refuses_a_zero_gap(tmp_path):
    """The guard that stopped two false positives stays. A 0x57 and a 0x51
    in one batch are NOT evidence of a sequence, because the 0x51 may be
    §7.8.1 reissuing an older lockup every fifteen seconds."""
    _, conn = _db(tmp_path / "vr", [
        (BATCH, CASHOUT_PRESSED),
        (BATCH, VALIDATION_REQUEST),
        (BATCH, HANDPAY_PENDING),
    ])
    found = find_unwanted(conn)
    assert all(f["requested_at"] is None for f in found), (
        "a 0x51 sharing a batch with a 0x57 must not be charged to it: "
        f"{found}")
