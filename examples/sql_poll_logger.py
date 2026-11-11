#!/usr/bin/env python3
"""One poll loop, one SQLite database: meters, ticket-out history,
ticket-in capture, and gateway-local cashout validation, all from a
single continuous general-poll cycle.

This is a worked, runnable example of the "local SQL layer on top of
SASClient" pattern described in MANUAL.md — a starting point for
prototyping gateway architecture, not a production drain/sync service.
Every poll failure is logged rather than raised, so one bad exchange
never stops the loop.

It is deliberately one program, not several. Only one process can safely
own the serial port's poll loop, and ticket-in/ticket-out capture both
depend on seeing the *same* continuous general-poll stream that meters
share the connection with — splitting these into separate programs would
mean two loops competing over one shared serial line, and would break
the exception-draining guarantee a real gateway depends on (an
unread exception can be overwritten by the next one if nothing drains it
fast enough — see the SAS spec's exception-queue behavior, §2.2.1).

What each cycle does, in order:

0. A validation_pool age check (see "Gateway-local cashout validation"
   below and --pool-age-alert-hours) — cheap, always run, independent of
   whether a cashout is even pending this cycle, since a stale pool is a
   condition that can otherwise go unnoticed until someone actually
   tries to spend from it.
1. A general poll, re-polled immediately (not deferred to the next
   --interval cycle) up to --general-poll-retries consecutive attempts
   on failure — see _general_poll_with_retry()'s docstring for why: SAS
   delivers one pending exception per poll, and an undrained one can be
   silently overwritten by the next, with no trail at all. If it returns
   exception 0x67 (ticket inserted), read the ticket's validation data
   (LP 70) and log it to ticket_in_events — read-only; see "What this
   does NOT do" below. If it returns 0x68 (ticket transfer complete —
   the redemption cycle finished, stacked or rejected, not which),
   read the completion status (the safe, read-only LP 71/FF status
   query) and log it to ticket_in_completions — see "What this does
   NOT do" below for how this stays read-only too. (The two are
   correlated into one row per ticket by the ticket_in_history view;
   see "Ticket-in history" below.) If it returns 0x3D
   or 0x3E (a ticket-out record is ready), drain every currently-unread
   ticket-out record (LP 4D,
   function code 0x00) into ticket_out_history. If it returns 0x57
   (system validation request — the machine is ready to print a cashout
   ticket and is waiting to be told what validation number to use),
   answer it locally from validation_pool: read the pending cashout
   amount (LP 57), take the next available number from the pool, and
   answer with it (LP 58) — see "Gateway-local cashout validation"
   below.
2. Meters — deliberately as many as this client can reach in one cycle,
   all in the same row, on the same cadence/ring-history/rollover logic
   (see the HistoryConfig docstring and MANUAL.md §4/§6): the six core
   meters (LP 0F), the eight cumulative ticket meters (LP 2F — the only
   way to reach these at all), LP 0x19/0x1C's own independent reads of
   several of the same core counters, games-since-power-up/door-closure
   (LP 0x18), total bill meters by denomination (LP 0x1E), hand-paid
   cancelled credits (LP 0x2D), current hopper status (LP 0x4F), and —
   unless --skip-full-meter-sweep is given — every other single-meter
   long poll this client knows how to read (LP 0x10-0x51/0x55, ~39
   polls). On top of that, unless --skip-table-c7-sweep is given,
   essentially the rest of Table C-7 (~154 more meter codes — every
   assigned code this client didn't already have a dedicated long poll
   for: per-denomination bill-acceptor counts, SAS-validation-specific
   meters, AFT transfer meters, and more) via LP 0x6F/0xAF in ~13
   chunked exchanges, 12 codes per exchange, self-describing size — far
   more wire-efficient per meter than the single-meter sweep, which is
   why it has its own opt-out rather than sharing
   --skip-full-meter-sweep's. Those chunks alternate between command
   codes 0x6F and 0xAF, which reach identical data: SAS implicitly ACKs
   a long poll only with a general poll or "a long poll with a different
   command byte" (Table 3.1), and a repeated identical poll is an
   implied NACK meaning "re-send what you just sent" (§3.2). 0xAF exists
   for precisely this (§7.21, and the spec's own 6.00 revision note
   "Added long poll AF as alternate 6F meter poll, to allow consecutive
   meter polls"); without alternating, a compliant machine could answer
   chunks 2..13 with chunk 1's data — wrong values in the right columns,
   with nothing raising an error.

   Two other places here issue the same command byte back-to-back, and
   SAS offers no alternate code for either: the validation-meters sweep
   (LP 0x50, 12 in a row, differing only in their data byte) and
   _drain_ticket_out_history() (LP 0x4D, function code 0x00, repeated
   until the buffer reports empty). Both are very probably fine. IGT's
   own tooling ships a meter script firing 41 consecutive LP 0x52 polls
   (sastest.ini [Meter Test]), which would be useless if machines
   re-sent the first response to every one of them; and LP 0x4D's
   "next unread, mark as read" is *designed* for repeated calls, so
   consecutive polls must advance or draining could never work at all.
   Neither is a spec guarantee, so both are worth an eye on real
   hardware — but neither is a known defect, and neither warrants
   restructuring the poll loop on inference.
   Several of these deliberately overlap: LP 0x19/0x1C, most of the
   single-meter sweep, and a good part of the Table C-7 sweep, report
   counters LP 0x0F already reports, through entirely independent
   request/response exchanges. That redundancy is the point, not an
   oversight — three long polls disagreeing about "total coin in" this
   cycle is a real finding a single poll can never surface, and a
   reference/stress-testing tool has no reason to economize on wire
   traffic the way a production gateway might. A poll failure anywhere
   in this group aborts that cycle's entire meter write, rather than
   saving a row that's fresh in some columns and stale or missing in
   others.

   "Poll failure" here means the exchange itself didn't succeed —
   SASTimeoutError, a checksum failure, anything the machine never
   coherently answered — not "the machine doesn't have this meter."
   Those are different outcomes and are handled differently. The Table
   C-7 sweep (LP 0x6F) is self-describing per meter (§7.21b): a code
   the machine doesn't implement comes back with size=0, which
   send_extended_meters() treats as a normal, present-but-absent
   result, not an error. This tool follows that: an unsupported meter
   writes NULL to just that one column and the cycle continues
   normally — it does NOT abort the write. Almost no real EGM
   implements literally all ~154 of these codes, so treating "doesn't
   have this meter" as equivalent to "communication failed" would make
   the sweep fail on essentially every real machine it's run against.

Every cycle's log line reports how long the meter poll took and across
how many exchanges (``meter_poll=X.XXXs/Nlp+Mgp``: N long polls and M
heartbeat general polls, which §2.3.3 counts against one budget) —
measured wall-clock time against whatever this ran against, not a
theoretical number. The counts are what the cycle actually sent, so a
C-7 sweep that was enabled but not due this cycle is not in them.
That's the real answer to "does this many polls fit inside
--interval on real hardware," and it comes with a WARNING if the meter
poll alone is at or above --interval, with no separate flag needed to
turn it on. --skip-full-meter-sweep is the lever if it doesn't fit.

On startup, the full ticket-out buffer (indices 1-31, non-destructive —
see send_enhanced_validation_information()'s docstring) is also read
once into ticket_out_history, so you get whatever the machine is already
holding, not just what happens from here forward.

Gateway-local cashout validation. Per the project's own Technical v3
§5.4: cashout is the one direction where the gateway IS meant to answer
without a server round-trip — the server pre-issues a batch of
validation numbers to the gateway in advance, and the gateway assigns
the next one itself when the machine asks. This is the opposite rule
from ticket-in (§5.8) below, not an inconsistency: a gateway may spend
from a pool it was already trusted with, but may never itself decide
whether someone else's ticket is genuine.

validation_pool here is a deliberately simplified stand-in for that
server-issued pool — a local SQLite table you seed (--seed-validation-
pool, or hand-insert real numbers yourself), with no protocol behind it
(no pool_epoch, no batch_uuid, no HMAC-authenticated top-up over the
network — that's real gateway<->server protocol machinery, out of scope
for a SAS wire-protocol reference client). If the pool runs out,
the cashout is left unanswered and the machine's own 10-second timeout
handles it, logged as a PoolExhausted row in poll_errors.

Separately from exhaustion, every cycle also checks how old the oldest
still-available number is (--pool-age-alert-hours, default 36 — per the
project's own Decisions Annex D-16) and raises an active, repeated
alert (a PoolStale row, plus an unmissable console line) if it's stale
— not because an old number is less valid (it isn't: age carries no
integrity meaning here, provenance is a signing concern this reference
tool doesn't implement, and consumption state is unaffected by age),
but because a pool that old almost always means either nothing is
being dispensed or nothing is topping this gateway up. This never
blocks dispensing on its own — see D-16's own reasoning for why an
age-based block would recreate, on a slower clock, exactly the
correlated-outage-time TITO disable the local pool exists to prevent.
Real order-gap detection (D-16's sharper signal, catching consumption
skipping or going non-contiguous) needs the actual server-issued
sequential/batch numbering named above as out of scope, so it isn't
attempted here either — age is the one signal available without it.

What this does NOT do: authorize or redeem tickets *coming in*. A
ticket-in event is read and logged (amount, validation data) but this
tool never calls redeem_ticket() — deciding whether to pay a ticket is
a real business/security decision (see the project's own Decisions
Annex on this), and a reference poller has no way to make that decision
correctly. Left unredeemed, the machine safely returns the ticket to
the player after its own 30-second timeout (spec-guaranteed), so
running this against a real machine does not risk paying out
incorrectly. ticket_in_events records that a ticket came in;
ticket_in_completions (exception 0x68) separately records how that
cycle ended — stacked or rejected, via the same safe, read-only status
query (redeem_ticket_status(), LP 71/FF) redeem_ticket() itself is
never used for — but "safe to observe" and "safe to decide" stay
different things throughout: this tool still never authorizes, rejects,
or influences a ticket-in outcome, only reads what already happened.

Ticket-in history. Those two tables are the raw, append-only record of
what each exception actually reported; the ticket_in_history VIEW joins
them into one row per ticket-in cycle (what went in, what it was worth,
how it ended). It is a view and not a table on purpose: SAS gives the
host no retroactive ticket-in buffer to re-read — unlike ticket-OUT,
where LP 4D genuinely can walk the machine's own buffer by index — so
this history exists only because both halves were caught live, and
correlating them is best-effort by nature. A 0x68 can arrive with
machine_status FF and no validation data at all (nothing to join on),
and a 0x67 can be missed outright if this tool started mid-cycle or the
machine's exception buffer overflowed. A view can be redefined when
that correlation logic improves; a guess written into a column at
capture time cannot be taken back out. See the view's own comments in
SCHEMA for the matching rule and MANUAL.md §6.3.
This is a different direction from cashout validation above, not a
contradiction of it — see §5.4 vs. §5.8 in the docstring paragraph above.

What this does NOT recover: ticket-IN history before this tool started,
or before the SAS 6.02 spec's own record — because there isn't any. LP
70/71 only ever expose the *current* redemption cycle; SAS has no
ticket-in buffer the way it has one for ticket-out (LP 4D). If you need
a full ticket-in audit trail, this tool has to be running continuously
from before the first ticket you care about.

What this does NOT cover, even with the full meter sweep: per-game
meters/configuration (LP 0x52/0x53). Those are indexed by game number,
which varies per machine, so they don't fit a fixed set of columns the
way every meter above does — a real gap, not scoped out on purpose, left
for a future child table keyed on game_number.

Nothing here ever deletes a ticket-in or ticket-out row to free space,
even under sustained disk pressure — that decision belongs to whatever
eventually syncs this data to a server (see synced_at, below), matching
the project's own Technical v3 §5.9: "Prune on confirmed ACK, never on
a timer." Instead: a write that actually fails (almost always a full
partition) is reported loudly rather than silently dropped — see
_safe_commit() — and --db-size-warning-mb gives you an early signal
before that happens, not just a fault report after.

Two history modes for meters, chosen with --mode (see MANUAL.md §4 and
§6 for the full reasoning):

- ``ring`` (default): a single current-value row plus a capped,
  decoupled-cadence history ring buffer. This is the flash-safe,
  production-shaped default — a growing per-poll log is a write-wear
  problem on flash-based hardware well before it's a space problem.
- ``append``: the original unbounded, every-poll history log. Right
  for lab and stress-testing runs on normal disks, where you want every
  sample and don't care about write volume.

Whichever mode is active, a meter *decrease*, a failed poll, or an
explicit anomaly signal (see poll_and_log()'s ``anomaly`` parameter, for
callers embedding this as a library function rather than running it as
a script) makes the next few polls log at full resolution regardless of
the normal cadence — that's where the diagnostic value actually is, and
it's cheap because it's rare. A handful of fields are gauges, not
cumulative counters (current hopper level/status, current credits,
selected game number) — a decrease there is normal, not anomalous, so
they're excluded from this check; see GAUGE_METER_FIELDS.

Usage:
    python3 examples/sql_poll_logger.py gateway.ini
    python3 examples/sql_poll_logger.py gateway.ini --db gateway.sqlite3 --interval 5
    python3 examples/sql_poll_logger.py gateway.ini --cycles 100   # stop after 100 cycles
    python3 examples/sql_poll_logger.py gateway.ini --mode append  # lab/stress-testing: log every meter poll, uncapped
    python3 examples/sql_poll_logger.py gateway.ini --skip-full-meter-sweep  # skip the ~39 single-meter polls, lighter per cycle

``gateway.ini`` is the file commission_gateway.py writes (see MANUAL.md),
or one you hand-wrote in the same format.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import signal
import random
import sqlite3
import sys
import time
from dataclasses import dataclass

from pathlib import Path

# Run from a clone without installing: examples are the first thing anyone
# tries at a machine, and "pip install -e ." is an easy step to skip.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.config import connect_from_config
from saspy.constants import (
    AFT_TRANSFER_CODE_FULL_ONLY,
    HANDPAY_RESET_CODES,
    SIMPLE_METER_WIDTH_BCD,
    AftTransferType,
    ExceptionCode,
    LongPoll,
    MeterCode,
    ValidationType,
    describe_aft_transfer_status,
    clarify_exception,
    describe_exception,
)
from saspy.exceptions import SASPreempted, SASError, SASPortCapabilityError, SASTimeoutError
from saspy.bcd import decode_bcd
from saspy.models import EnhancedValidationInfo

DEFAULT_VALIDATION_SYSTEM_ID = 1  # 0 means "deny" per Table 15.8a — never use it for a real pool entry

# --- Meter columns: every long poll this tool reads into meters_current/
#     meters_history, grouped by the poll that produces it. Defined before
#     SCHEMA so the schema's own column list is generated from these tuples
#     rather than hand-duplicated — at ~80 columns, keeping one source of
#     truth matters more than reading a literal CREATE TABLE end to end.

METER_FIELDS = (  # LP 0x0F (Table 7.1a/7.1b) — the six core meters
    "total_cancelled_credits",
    "total_coin_in",
    "total_coin_out",
    "total_drop",
    "total_jackpot",
    "games_played",
)

# Column name and MeterCode are paired by position — zip(TICKET_METER_COLUMNS,
# TICKET_METER_CODES) is the single source of truth for that mapping, used
# both to build the LP 2F request and to place its response into the row.
TICKET_METER_COLUMNS = (  # LP 0x2F — the only way to reach these at all
    "ticket_in_cashable_cents",
    "ticket_in_cashable_count",
    "ticket_in_restricted_cents",
    "ticket_in_restricted_count",
    "ticket_out_cashable_cents",
    "ticket_out_cashable_count",
    "ticket_out_restricted_cents",
    "ticket_out_restricted_count",
)
TICKET_METER_CODES = (
    MeterCode.CASHABLE_TICKET_IN_CENTS,
    MeterCode.CASHABLE_TICKET_IN_QUANTITY,
    MeterCode.RESTRICTED_TICKET_IN_CENTS,
    MeterCode.RESTRICTED_TICKET_IN_QUANTITY,
    MeterCode.CASHABLE_TICKET_OUT_CENTS,
    MeterCode.CASHABLE_TICKET_OUT_QUANTITY,
    MeterCode.RESTRICTED_TICKET_OUT_CENTS,
    MeterCode.RESTRICTED_TICKET_OUT_QUANTITY,
)  # all 8 fit in one LP 2F poll (max 10 codes per request, §7.3)

LP19_METER_COLUMNS = (  # LP 0x19 (Table 7.2b) — independent re-read of 5 of the 6 core meters
    "lp19_total_coin_in",
    "lp19_total_coin_out",
    "lp19_total_drop",
    "lp19_total_jackpot",
    "lp19_games_played",
)

LP1C_METER_COLUMNS = (  # LP 0x1C (Table 7.2c) — independent re-read of 5, plus 3 new fields
    "lp1c_total_coin_in",
    "lp1c_total_coin_out",
    "lp1c_total_drop",
    "lp1c_total_jackpot",
    "lp1c_games_played",
    "lp1c_games_won",
    "lp1c_slot_door_opened",
    "lp1c_power_reset",
)

LP18_METER_COLUMNS = (  # LP 0x18 (Table 7.7)
    "lp18_games_since_power_up",
    "lp18_games_since_door_closure",
)

LP1E_METER_COLUMNS = (  # LP 0x1E — 6 "bills in" count meters by denomination
    "lp1e_bills_1",
    "lp1e_bills_5",
    "lp1e_bills_10",
    "lp1e_bills_20",
    "lp1e_bills_50",
    "lp1e_bills_100",
)

LP2D_METER_COLUMNS = ("lp2d_total_hand_paid_cancelled_credits",)  # LP 0x2D, game_number=0 (all games)

LP4F_METER_COLUMNS = (  # LP 0x4F (Table 7.19a/7.19b) — gauges, not cumulative counters
    "lp4f_hopper_status",
    "lp4f_hopper_percent_full",
    "lp4f_hopper_level",
)

LP48_METER_COLUMNS = (  # LP 0x48 (Table 7.11) — snapshot of the single most-recently-accepted bill, not cumulative
    "lp48_last_bill_country_code",
    "lp48_last_bill_denomination_code",
    "lp48_last_bill_meter",
)

# 12 validation types (Table 15.13c) x 2 fields each, one LP 0x50 exchange per
# type (no batching, unlike LP 6F/2F) — see _poll_all_meters(). Column name
# derived from the ValidationType member's own name so it can't drift from
# constants.py. Explicitly redundant with MeterCode 0x80+ (Table C-7) per the
# spec's own note — deliberate, see this module's docstring on redundancy.
VALIDATION_METER_TYPES: tuple[ValidationType, ...] = tuple(ValidationType)
VALIDATION_METER_COLUMNS: dict[ValidationType, tuple[str, str]] = {
    vt: (f"lp50_{vt.name.lower()}_total_validations", f"lp50_{vt.name.lower()}_cumulative_amount_cents")
    for vt in VALIDATION_METER_TYPES
}
VALIDATION_METER_COLUMNS_FLAT: tuple[str, ...] = tuple(c for pair in VALIDATION_METER_COLUMNS.values() for c in pair)

# Every other single-meter long poll this client implements (SASClient.send_meter()),
# named "sm_" + a descriptive name so none can collide with a column above even
# where the underlying counter is the same one LP 0x0F/0x19/0x1C already report
# (e.g. sm_total_coin_in). Built from SIMPLE_METER_WIDTH_BCD so this list can
# never drift from what SASClient actually supports.
SINGLE_METER_COLUMNS: dict[LongPoll, str] = {
    LongPoll.SEND_TOTAL_CANCELLED_CREDITS_METER: "sm_total_cancelled_credits",
    LongPoll.SEND_TOTAL_COIN_IN_METER: "sm_total_coin_in",
    LongPoll.SEND_TOTAL_COIN_OUT_METER: "sm_total_coin_out",
    LongPoll.SEND_TOTAL_DROP_METER: "sm_total_drop",
    LongPoll.SEND_TOTAL_JACKPOT_METER: "sm_total_jackpot",
    LongPoll.SEND_GAMES_PLAYED_METER: "sm_games_played",
    LongPoll.SEND_GAMES_WON_METER: "sm_games_won",
    LongPoll.SEND_GAMES_LOST_METER: "sm_games_lost",
    LongPoll.SEND_CURRENT_CREDITS: "sm_current_credits",  # gauge: the player's current balance, not cumulative
    LongPoll.SEND_TOTAL_DOLLAR_VALUE_OF_BILLS: "sm_total_dollar_value_of_bills",
    LongPoll.SEND_TRUE_COIN_IN: "sm_true_coin_in",
    LongPoll.SEND_TRUE_COIN_OUT: "sm_true_coin_out",
    LongPoll.SEND_CURRENT_HOPPER_LEVEL: "sm_current_hopper_level",  # gauge
    LongPoll.SEND_BILLS_IN_METER_1: "sm_bills_in_1",
    LongPoll.SEND_BILLS_IN_METER_2: "sm_bills_in_2",
    LongPoll.SEND_BILLS_IN_METER_5: "sm_bills_in_5",
    LongPoll.SEND_BILLS_IN_METER_10: "sm_bills_in_10",
    LongPoll.SEND_BILLS_IN_METER_20: "sm_bills_in_20",
    LongPoll.SEND_BILLS_IN_METER_50: "sm_bills_in_50",
    LongPoll.SEND_BILLS_IN_METER_100: "sm_bills_in_100",
    LongPoll.SEND_BILLS_IN_METER_500: "sm_bills_in_500",
    LongPoll.SEND_BILLS_IN_METER_1000: "sm_bills_in_1000",
    LongPoll.SEND_BILLS_IN_METER_200: "sm_bills_in_200",
    LongPoll.SEND_BILLS_IN_METER_25: "sm_bills_in_25",
    LongPoll.SEND_BILLS_IN_METER_2000: "sm_bills_in_2000",
    LongPoll.SEND_BILLS_IN_METER_2500: "sm_bills_in_2500",
    LongPoll.SEND_BILLS_IN_METER_5000: "sm_bills_in_5000",
    LongPoll.SEND_BILLS_IN_METER_10000: "sm_bills_in_10000",
    LongPoll.SEND_BILLS_IN_METER_20000: "sm_bills_in_20000",
    LongPoll.SEND_BILLS_IN_METER_25000: "sm_bills_in_25000",
    LongPoll.SEND_BILLS_IN_METER_50000: "sm_bills_in_50000",
    LongPoll.SEND_BILLS_IN_METER_100000: "sm_bills_in_100000",
    LongPoll.SEND_BILLS_IN_METER_250: "sm_bills_in_250",
    LongPoll.SEND_CREDIT_AMOUNT_OF_ALL_BILLS_ACCEPTED: "sm_credit_amount_of_all_bills_accepted",
    LongPoll.SEND_COIN_AMOUNT_FROM_EXTERNAL_ACCEPTOR: "sm_coin_amount_from_external_acceptor",
    LongPoll.SEND_BILLS_IN_STACKER_COUNT: "sm_bills_in_stacker_count",
    LongPoll.SEND_BILLS_IN_STACKER_CREDIT_AMOUNT: "sm_bills_in_stacker_credit_amount",
    LongPoll.SEND_TOTAL_GAMES_IMPLEMENTED: "sm_total_games_implemented",  # config, not really a meter, kept for completeness
    LongPoll.SEND_SELECTED_GAME_NUMBER: "sm_selected_game_number",  # gauge: current game, not cumulative
}
assert set(SINGLE_METER_COLUMNS) == set(SIMPLE_METER_WIDTH_BCD), (
    "SINGLE_METER_COLUMNS must cover exactly what SASClient.send_meter() supports"
)

# Every Table C-7 meter code NOT already covered by TICKET_METER_COLUMNS above
# — roughly the rest of the table (~154 codes: core/extended/bill-denomination,
# SAS-validation-specific, AFT-specific; see MeterCode's own docstring for the
# exact ranges). Read via LP 0x6F (send_extended_meters()), 12 codes per
# exchange (self-describing size, unlike LP 2F) — see _poll_all_meters().
# Column name is "c7_" + the MeterCode member's own name, lowercased, so
# every one of these is traceable back to its exact spec entry with no
# separate name-mapping table to keep in sync (unlike SINGLE_METER_COLUMNS
# above, which is hand-mapped because LongPoll names don't read as column
# names directly). Built from MeterCode directly so this can never drift
# from what the enum actually contains.
TABLE_C7_EXTENDED_CODES: tuple[MeterCode, ...] = tuple(c for c in MeterCode if c not in TICKET_METER_CODES)
TABLE_C7_EXTENDED_COLUMNS: tuple[str, ...] = tuple(f"c7_{c.name.lower()}" for c in TABLE_C7_EXTENDED_CODES)
TABLE_C7_CHUNK_SIZE = 12  # LP 0x6F's own per-request limit (§7.21)
TABLE_C7_CHUNK_COUNT = -(-len(TABLE_C7_EXTENDED_CODES) // TABLE_C7_CHUNK_SIZE)  # ceil division

# Gauges: fields that go up AND down in normal operation, so a decrease here
# is not the diagnostic signal it is for a cumulative counter. Excluded from
# poll_and_log()'s burst-arming "decreased" check, not from the row itself.
GAUGE_METER_FIELDS = frozenset(
    {
        "lp4f_hopper_status",
        "lp4f_hopper_percent_full",
        "lp4f_hopper_level",  # also the one field here that can be None (Table 7.19a: no hopper-level sensor)
        "sm_current_credits",
        "sm_current_hopper_level",
        "sm_selected_game_number",
        "c7_current_credits",  # MeterCode.CURRENT_CREDITS (0x0C) — same gauge as sm_current_credits, different long poll
        "c7_current_restricted_credits",  # MeterCode.CURRENT_RESTRICTED_CREDITS (0x1B)
        "c7_number_of_bills_currently_in_stacker",  # fills/empties with normal operation, not cumulative
        "c7_total_value_of_bills_currently_in_stacker_credits",
        "c7_weighted_average_theoretical_payback_percentage",  # a percentage, not a counter
        "lp48_last_bill_country_code",  # identifies whichever bill was *last* accepted, not a running total
        "lp48_last_bill_denomination_code",
        "lp48_last_bill_meter",  # cumulative for that one denomination, but which denomination is "last" changes cycle to cycle
    }
)

# Column *order* only -- every one of these already exists in one of the
# group tuples above; this doesn't add a new poll or change what's read,
# just where it lands in the row. Front-loads the meters the business
# actually watches day to day, ahead of the long tail of redundant/
# secondary reads (LP 19/1C re-reads, the single-meter sweep, Table C-7,
# per-validation-type meters, etc.), which keep their existing relative
# order after it. "Ticket in"/"ticket out"/"promo ticket in"/"promo
# ticket out" are the cashable/restricted LP 2F pairs -- SAS calls
# restricted tickets "promotional" throughout (see ValidationType) --
# each kept as its natural (cents, count) pair, cents first, matching
# how TICKET_METER_COLUMNS itself already orders them.
PRIORITY_METER_COLUMNS = (
    "total_coin_in",
    "total_coin_out",
    "total_cancelled_credits",
    "total_jackpot",
    "sm_total_dollar_value_of_bills",
    "games_played",
    "ticket_in_cashable_cents",
    "ticket_in_cashable_count",
    "ticket_out_cashable_cents",
    "ticket_out_cashable_count",
    "ticket_in_restricted_cents",
    "ticket_in_restricted_count",
    "ticket_out_restricted_cents",
    "ticket_out_restricted_count",
)

_ALL_GROUPED_METER_COLUMNS = (
    METER_FIELDS
    + TICKET_METER_COLUMNS
    + LP19_METER_COLUMNS
    + LP1C_METER_COLUMNS
    + LP18_METER_COLUMNS
    + LP1E_METER_COLUMNS
    + LP2D_METER_COLUMNS
    + LP4F_METER_COLUMNS
    + LP48_METER_COLUMNS
    + tuple(SINGLE_METER_COLUMNS.values())
    + TABLE_C7_EXTENDED_COLUMNS
    + VALIDATION_METER_COLUMNS_FLAT
)
assert set(PRIORITY_METER_COLUMNS) <= set(_ALL_GROUPED_METER_COLUMNS), (
    "PRIORITY_METER_COLUMNS can only reorder columns that already exist above"
)
ALL_METER_FIELDS = PRIORITY_METER_COLUMNS + tuple(
    f for f in _ALL_GROUPED_METER_COLUMNS if f not in PRIORITY_METER_COLUMNS
)
# A third category, and the EGM proving it on 2026-10-28 is why it exists.
# These are not cumulative counters and not free-moving gauges: they count
# up from zero and are RESET BY A SPECIFIC EVENT. "Games since last power
# reset" going 13 -> 0 is not an anomaly, it is the power cycle. The lab
# cabinet lost power mid-soak, these dropped, and the run logged "meter
# decrease detected" for something that had behaved exactly as specified.
#
# The consequence outside this file is worse than a spurious burst. The
# rollover correction in the Linux migration spec §8.2 prepends an unbounded
# generation digit on a confirmed drop, so applying it here would turn a
# legitimate reset of 13 -> 0 into 100,000,000 -- permanently, since the
# corrected value is defined to be monotonic forever. Any host doing
# missing-digit correction has to exclude these by name.
#
# Note what is NOT here: lp1c_slot_door_opened and lp1c_power_reset are, per
# Table 7.2c, the "slot door opened meter" and "power reset meter" -- counts
# of how many times each has happened, which only ever rise.
RESET_ON_EVENT_METER_FIELDS = frozenset(
    {
        "lp18_games_since_power_up",        # Table 7.7
        "lp18_games_since_door_closure",    # Table 7.7
        "c7_games_since_last_power_reset",  # MeterCode 0x25
        "c7_games_since_slot_door_closure",  # MeterCode 0x26
    }
)

DECREASE_CHECK_FIELDS = tuple(
    f for f in ALL_METER_FIELDS
    if f not in GAUGE_METER_FIELDS and f not in RESET_ON_EVENT_METER_FIELDS)

# The 8 long-poll exchanges _poll_all_meters() always makes, regardless of
# --skip-full-meter-sweep/--skip-table-c7-sweep: send_meters_10_through_15,
# send_selected_meters (ticket meters), send_meters_11_through_15,
# send_extended_meters_group, send_games_since_power_up_and_door_closure,
# send_total_bill_meters, send_total_hand_paid_cancelled_credits,
# send_current_hopper_status. Used only to report how many exchanges a
# cycle's meter_poll timing covered.
GROUPED_METER_POLL_COUNT = 8

_METER_COLUMN_DDL = ",\n    ".join(f"{field} INTEGER" for field in ALL_METER_FIELDS)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meters_current (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    polled_at TEXT NOT NULL,
    epoch_id INTEGER,
    {_METER_COLUMN_DDL}
);

CREATE TABLE IF NOT EXISTS meters_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    polled_at TEXT NOT NULL,
    epoch_id INTEGER,
    {_METER_COLUMN_DDL}
);

CREATE TABLE IF NOT EXISTS machine_epochs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    reason TEXT NOT NULL,          -- 'first_seen' | 'ram_clear' | 'machine_changed'
    serial_number TEXT,
    asset_number INTEGER,
    sas_version TEXT,
    detail TEXT,
    acknowledged_at TEXT           -- set by the CMS, never by the gateway
);

CREATE INDEX IF NOT EXISTS idx_machine_epochs_unack
    ON machine_epochs (acknowledged_at);

CREATE TABLE IF NOT EXISTS credit_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at TEXT NOT NULL,
    credits INTEGER NOT NULL,
    -- How many general polls had been spent on this sweep when the sample
    -- was taken. Non-decreasing within a cycle; a large step between two
    -- samples means the sampler was suppressing itself, which is the
    -- design working rather than a gap in the data.
    heartbeats INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_credit_samples_at ON credit_samples (sampled_at);

CREATE TABLE IF NOT EXISTS machine_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    exception_code INTEGER NOT NULL,
    description TEXT NOT NULL,
    -- 'cycle', 'heartbeat', 'rte' (the event a general poll returned under
    -- real time event mode) or 'rte-interleaved' (an event that arrived in
    -- place of a LONG poll's response, per 12.3, and was collected by the
    -- client rather than discarded).
    source TEXT NOT NULL,
    -- The decoded Table 12.5 data, as JSON, for events that carry any.
    -- NULL under ordinary polling, where an exception is a bare code with
    -- no payload at all -- which is the whole reason RTE exists.
    rte_fields TEXT,
    synced_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_machine_events_at ON machine_events (occurred_at);

-- What a full interrogation found, on first connect and after a comms gap
-- long enough for someone to have moved the cable.
--
-- The trigger is a PHYSICAL-ACT threshold, not a data-loss one. Events are
-- already gone after ~8 seconds of silence on a played machine (§2.2.1's
-- 20-element queue against ~2.4 exceptions/sec), and no sweep brings them
-- back. What a sweep CAN establish is whether the thing now answering is
-- the same machine, configured the same way, as the thing that was
-- answering before -- and unplugging a cable, walking to another cabinet
-- and plugging in takes far longer than 30 seconds.
CREATE TABLE IF NOT EXISTS interrogations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    reason TEXT NOT NULL,            -- 'first_connect' | 'comms_restored' | 'options_changed'
    gap_seconds REAL,                -- NULL on first connect
    epoch_id INTEGER,
    serial_number TEXT,
    asset_number INTEGER,
    sas_version TEXT,
    -- Set when this interrogation found a DIFFERENT machine than the one
    -- the previous epoch recorded. The whole point of the exercise.
    machine_changed INTEGER NOT NULL DEFAULT 0,
    identity_note TEXT,
    enabled_features TEXT,           -- JSON, LP 0xA0 Tables 7.14c-e
    games_found INTEGER,
    ticket_out_records INTEGER,
    last_redeemed TEXT,              -- JSON, LP 0x71/FF
    duration_seconds REAL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS interrogation_games (
    interrogation_id INTEGER NOT NULL REFERENCES interrogations(id) ON DELETE CASCADE,
    game_number INTEGER NOT NULL,
    -- LP 0x53
    game_id TEXT,
    additional_id TEXT,
    denomination INTEGER,
    max_bet INTEGER,
    progressive_group INTEGER,
    game_options INTEGER,
    paytable_id TEXT,
    base_percentage TEXT,
    -- LP 0xB5, which carries the human-readable names 0x53 does not
    game_name TEXT,
    paytable_name TEXT,
    progressive_levels INTEGER,
    wager_categories INTEGER,
    PRIMARY KEY (interrogation_id, game_number)
);

CREATE TABLE IF NOT EXISTS poll_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    poll_name TEXT NOT NULL,
    error_type TEXT NOT NULL,
    message TEXT
);

-- WHY A RUN ENDED, written to the DATABASE rather than to stdout.
--
-- 2026-11-08: the operator asked why the previous soak stopped. The poller
-- prints its reason on every exit path -- "Stopped (signal 15)", a
-- traceback, a serial-port message -- and prints it to the nohup log, which
-- was gone. So a six-day run ended for a reason that existed, was written
-- down, and could not be recovered. The database survived; the one line
-- that mattered did not.
--
-- THE ABSENCE OF A ROW IS THE FINDING. A clean stop, a caught signal, an
-- unhandled exception and a serial failure can all write 'stopped'. SIGKILL
-- cannot -- not an OOM kill, not `kill -9`, not a power cut. So a 'started'
-- row with no matching 'stopped' is positive evidence that the process was
-- ended by something it could not catch, which is exactly the distinction
-- nobody could make about 2026-11-08T17:16.
CREATE TABLE IF NOT EXISTS poller_lifecycle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    event TEXT NOT NULL,          -- 'started' | 'stopped'
    reason TEXT,                  -- 'signal 15', an exception repr, 'end of run'
    pid INTEGER,
    argv TEXT
);

CREATE TABLE IF NOT EXISTS ticket_out_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    buffer_index INTEGER,
    validation_type INTEGER,
    ticket_date TEXT,
    ticket_time TEXT,
    validation_number INTEGER NOT NULL,
    amount_cents INTEGER,
    ticket_number INTEGER,
    validation_system_id INTEGER,
    expiration TEXT,
    pool_id INTEGER,
    synced_at TEXT,
    UNIQUE(validation_number, ticket_date, ticket_time)
);

CREATE TABLE IF NOT EXISTS ticket_in_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    amount_cents INTEGER,
    parsing_code INTEGER,
    validation_data_hex TEXT,
    -- NULL unless --lab-ticket-authority answered this one. What the
    -- gateway told the machine, and what the machine said back: the
    -- decision is not recoverable from the meters afterwards, because a
    -- denied ticket moves nothing at all.
    authority_decision TEXT,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS ticket_in_completions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    machine_status INTEGER,
    amount_cents INTEGER,
    parsing_code INTEGER,
    validation_data_hex TEXT,
    synced_at TEXT
);

-- The most recent reading that actually ran the Table C-7 sweep.
--
-- With --table-c7-interval the c7_* columns are populated on some cycles and
-- NULL on the rest, NULL meaning "not read this cycle" exactly as it does for
-- an unsupported meter. That is the honest representation and an awkward one
-- to query, so this view hides the awkwardness without hiding the fact: it
-- returns the newest row where the sweep genuinely ran, carrying its own
-- polled_at, so a consumer can see how old the reading is instead of assuming
-- it is current.
--
-- c7_total_coin_in_credits is the probe column because code 0000 is the first
-- meter in Table C-7 and every machine implements it: a machine that answered
-- the sweep at all answered that one. Both tables are searched because
-- meters_current holds one row and ring mode writes history at its own
-- cadence, so either may hold the newest sweep.
CREATE VIEW IF NOT EXISTS latest_c7_meters AS
    SELECT * FROM (
        SELECT * FROM meters_current WHERE c7_total_coin_in_credits IS NOT NULL
        UNION ALL
        SELECT * FROM meters_history WHERE c7_total_coin_in_credits IS NOT NULL
    )
    ORDER BY polled_at DESC, id DESC
    LIMIT 1;

-- Exception 0x69, AFT transfer complete, and what the acknowledging
-- interrogation found. §8.8 makes acknowledging MANDATORY rather than
-- optional: "if the host does not respond with a proper interrogation poll,
-- the gaming machine must reissue exception 69 every 15 seconds until the
-- host polls for and acknowledges the completion status" -- measured at 15.2s
-- on an EGT Premier. And 0x69 is a PRIORITY exception, so it bypasses the
-- §2.2.1 queue and is delivered ahead of everything waiting: an
-- unacknowledged transfer puts a permanent 15-second beat at the FRONT of the
-- stream and pushes real events further back.
--
-- The amounts are recorded as the machine reported them, not as this tool
-- asked for them, because a partial transfer is the case where the two differ
-- and the difference is the whole point of reading it back.
CREATE TABLE IF NOT EXISTS aft_transfer_completions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    buffer_position INTEGER,
    transfer_status INTEGER,
    receipt_status INTEGER,
    transfer_type INTEGER,
    cashable_amount INTEGER,
    restricted_amount INTEGER,
    nonrestricted_amount INTEGER,
    transaction_id TEXT,
    transaction_date TEXT,
    transaction_time TEXT,
    asset_number INTEGER,
    note TEXT,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS handpay_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    progressive_group INTEGER,
    level INTEGER,
    amount INTEGER,
    partial_pay INTEGER,
    reset_id INTEGER,
    all_zeros INTEGER NOT NULL,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS stream_gaps (
    -- EVERY TIME THE EVENT STREAM FAILED ITS OWN ARITHMETIC.
    --
    -- §12.5.3 puts the coin-in meter INSIDE every game-start event, after
    -- the wager. So two consecutive starts must differ by exactly the later
    -- one's wager, and when they do not, a game start went missing and the
    -- excess says how much was wagered in it.
    --
    -- This needs no meters at all, which is the point: it is the one check
    -- that can run on every single event and tell the operator, while he is
    -- standing there, that RTE and the meters have diverged. It found nine
    -- lost game starts on 2026-11-06 -- three hours after they happened,
    -- because nothing ran it until somebody asked by hand.
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    previous_at TEXT,
    previous_coin_in INTEGER,
    this_coin_in INTEGER,
    this_wager INTEGER,
    unexplained_credits INTEGER NOT NULL,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS lab_auto_actions (
    -- LAB ONLY. Every time this poller moved money or cleared a lockup by
    -- itself. A gateway that does either without a human is a cash-dispensing
    -- defect on a floor; in a lab running overnight it is the only way to keep
    -- a cabinet spinning. The row is the audit trail that says which it was.
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    action TEXT NOT NULL,          -- 'handpay_reset' | 'aft_credit'
    detail TEXT,
    outcome TEXT,
    synced_at TEXT
);
CREATE TABLE IF NOT EXISTS validation_pool (
    validation_number INTEGER PRIMARY KEY,
    validation_system_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    issued_at TEXT,
    assigned_at TEXT,
    assigned_amount_cents INTEGER
);

-- THE INBOUND COMMAND CHANNEL. Every other table in this schema is
-- OUTBOUND -- the gateway writes, a server drains. validation_pool is
-- inbound but it is DATA, not a command: nobody is waiting on it.
--
-- WfA ("Waiting for Amount") is the flow that needs the other direction. The
-- server holds a ticket's validation number from the pool but not its value,
-- because the dispense-detail drain never arrived -- the micro-PC-to-server
-- LAN was down when the ticket printed, and dispense is deliberately
-- local-only so it keeps working when that link is down. A cashier then
-- scans the ticket at the cage and somebody has to ask the EGM what it was
-- worth, right now, with a customer waiting.
--
-- So: a server INSERTs a row here and polls it for an answer. The poller
-- owns the serial port and is the only process that can speak to the
-- cabinet (one port, one master), so the request has to come to it.
--
-- STATES. pending -> claimed -> done | failed | expired. A request is never
-- deleted by the poller; the server reads its own answer and is responsible
-- for retention.
--
-- WHY A TABLE AND NOT A SOCKET. The database is already WAL, already
-- durable across a power cut, already the thing the server drains, and
-- already has a busy timeout. A socket would need its own lifecycle, its
-- own restart semantics and its own audit trail. A request that survives a
-- gateway reboot and can be read afterwards is worth more than a few
-- milliseconds.
CREATE TABLE IF NOT EXISTS host_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- When the SERVER wrote the row. The end-to-end budget is measured from
    -- here, not from when the poller noticed.
    requested_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    validation_number INTEGER,
    -- The hard budget for this request in milliseconds. Past it the poller
    -- stops spending poll slots and marks the row expired: a cashier who has
    -- already given up must not still be costing the event stream.
    deadline_ms INTEGER NOT NULL DEFAULT 1000,
    state TEXT NOT NULL DEFAULT 'pending',
    claimed_at TEXT,
    completed_at TEXT,
    -- 'local'      -- answered from ticket_out_history, ZERO poll slots
    -- 'buffer-walk'-- answered by walking LP 0x4D, one slot per index
    -- 'cache'      -- a repeat inside the 10 s pull cap, served from the
    --                 previous answer rather than pulling again
    served_from TEXT,
    -- What this request COST the event stream. The number that decides
    -- whether on-demand pulls are affordable on a busy floor.
    polls_spent INTEGER NOT NULL DEFAULT 0,
    -- Measured, so p95 is a measurement and not a claim.
    latency_ms INTEGER,
    amount_cents INTEGER,
    result_json TEXT,
    error TEXT
);

-- The poller asks "is there anything pending" at every poll-slot boundary,
-- so that lookup must never scan.
CREATE INDEX IF NOT EXISTS idx_host_requests_pending
    ON host_requests (state, id);

-- Ticket-in history: the two raw capture tables above, correlated into one
-- row per ticket-in cycle. DROP+CREATE rather than IF NOT EXISTS because a
-- view holds no data -- recreating it costs nothing and guarantees an older
-- database gets the current definition, which is exactly the migration
-- problem a *table* would have (see MANUAL.md 6.3 on column order).
--
-- Deliberately a view, not a table: correlation here is best-effort by
-- nature (a 0x68 can arrive with machine_status FF and empty validation
-- data, leaving nothing to join on; a 0x67 can be missed entirely if this
-- tool started mid-cycle or the machine's exception buffer overflowed).
-- The raw tables stay the append-only record of what the wire actually
-- said; a wrong guess here can be redefined, a wrong guess written into a
-- column cannot.
DROP VIEW IF EXISTS ticket_in_history;
CREATE VIEW ticket_in_history AS
    -- Every completion, joined to the most recent *preceding* insert
    -- carrying the same validation number. Driving from completions keeps
    -- this one-to-one: a ticket rejected and re-inserted (same validation
    -- number twice) would otherwise be claimed by both inserts.
    SELECT
        e.captured_at AS inserted_at,
        COALESCE(NULLIF(c.validation_data_hex, ''), e.validation_data_hex) AS validation_number,
        COALESCE(e.amount_cents, c.amount_cents) AS amount_cents,
        c.captured_at AS completed_at,
        c.machine_status AS machine_status,
        CASE c.machine_status >> 5
            WHEN 0 THEN 'redeemed'
            WHEN 1 THEN 'waiting for long poll 71'
            WHEN 2 THEN 'redemption pending'
            WHEN 4 THEN 'rejected'
            WHEN 6 THEN 'incompatible with current cycle'
            WHEN 7 THEN 'no validation information'
            ELSE 'unknown'
        END AS outcome,
        e.id AS event_id,
        c.id AS completion_id
    FROM ticket_in_completions c
    LEFT JOIN ticket_in_events e
        ON e.id = (
            -- Ordered by captured_at, NOT by id: the two tables have
            -- independent AUTOINCREMENT sequences, so event id 1 and
            -- completion id 1 say nothing about which happened first.
            -- captured_at is ISO-8601 UTC, so string order is time order.
            SELECT e2.id FROM ticket_in_events e2
            WHERE e2.validation_data_hex = c.validation_data_hex
              AND c.validation_data_hex <> ''
              AND e2.captured_at <= c.captured_at
            ORDER BY e2.captured_at DESC, e2.id DESC
            LIMIT 1
        )
    UNION ALL
    -- Inserts that no completion claimed: still in escrow, rejected before
    -- a redemption cycle started, or superseded by a later insert of the
    -- same ticket. Kept visible rather than dropped -- a history that
    -- silently omits a ticket that went in is worse than one that says
    -- "went in, never saw it finish".
    SELECT
        e.captured_at, e.validation_data_hex, e.amount_cents,
        NULL, NULL, 'awaiting completion', e.id, NULL
    FROM ticket_in_events e
    WHERE NOT EXISTS (
        SELECT 1 FROM ticket_in_completions c2
        WHERE c2.validation_data_hex = e.validation_data_hex
          AND c2.validation_data_hex <> ''
          AND c2.captured_at >= e.captured_at
          AND e.id = (
              SELECT e3.id FROM ticket_in_events e3
              WHERE e3.validation_data_hex = c2.validation_data_hex
                AND e3.captured_at <= c2.captured_at
              ORDER BY e3.captured_at DESC, e3.id DESC
              LIMIT 1
          )
    );
"""
# meters_current always holds exactly one row (id=1, INSERT OR REPLACE) —
# most consumers only ever want the latest value, and a one-row table
# makes that a trivial read instead of an ORDER BY over a growing log.
#
# meters_history is the diagnostic time series. In ring mode it's capped
# at HistoryConfig.cap rows (oldest evicted) and written at most once per
# HistoryConfig.interval seconds, decoupled from --interval (the poll
# rate) — except during a burst (see burst_count), when it's written
# every cycle at full resolution. In append mode it's unbounded and
# written every cycle, unconditionally: the original behavior, still the
# right one for a lab/stress run on ordinary disk.
#
# Both tables carry ~80 meter columns, generated from ALL_METER_FIELDS
# (see above) rather than hand-typed here — deliberately redundant by
# design, not an accident: LP 0x0F/0x19/0x1C, and most of the "sm_"
# single-meter sweep, independently report several of the same
# underlying counters through entirely different request/response
# exchanges. Three long polls disagreeing about "total coin in" this
# cycle is a real finding; a schema that only kept one of them couldn't
# surface it. ticket_in_*/ticket_out_* (LP 2F) are a different thing
# again from ticket_in_events/ticket_out_history below: those record
# individual transactions (one row per ticket); these columns are
# running totals the machine itself maintains, useful for reconciling
# "does the machine's own count agree with what we captured per-ticket"
# without summing either table. Being ordinary BCD meters, all of these
# roll over exactly like the core six (§8.2) — decode_bcd() has no
# notion of "value decreased," so a wrapped meter is just a normal,
# smaller read, treated as diagnostic burst signal, not an error (except
# the handful of true gauges in GAUGE_METER_FIELDS, where a decrease is
# just normal operation, not a rollover).
#
# ticket_out_history is deduplicated on (validation_number, ticket_date,
# ticket_time) — the startup backfill (non-destructive, by buffer index)
# and the live exception-driven drain (destructive, by "next unread") can
# both observe the same physical ticket, and a ring buffer position gets
# reused over time, so buffer_index alone is not a stable identity.
#
# Neither meters table carries a synced_at column — they're a local
# diagnostic buffer, not a queue, and there's nothing for a sync column
# to mean on a table that's never drained (see §6.6's synced_at
# discussion). ticket_out_history, ticket_in_events and
# ticket_in_completions are different: per Technical v3 §5.9, ticket
# events ARE the event stream ("sas_events — append-only, drained on
# invitation, pruned on confirmed ACK"), so they carry synced_at even
# though this reference tool has no real drain process to ever set it.
# The column exists so a future one can, and so the correct future
# cleanup query is obvious and safe: `DELETE ... WHERE synced_at IS NOT
# NULL` — never a row-count cap, and never anything keyed on age or
# local disk pressure alone. This tool does not implement that deletion
# itself; see the module docstring's note on why sustained disk
# pressure is reported loudly instead of resolved by deleting unsynced
# rows.
#
# ticket_in_completions is a separate table from ticket_in_events, not
# extra columns on it, because nothing in SAS correlates a completion
# (exception 0x68) back to the specific insertion (exception 0x67) that
# started its cycle — no shared ID exists to join on. Both are captured
# as their own append-only log; matching one to the other, if ever
# needed, is a job for whoever consumes this data (by timestamp
# proximity and validation_data_hex), not this tool inventing a
# correlation key SAS itself doesn't provide.
#
# validation_pool is different in kind from every other table here: it's
# not something this tool observes, it's something this tool spends
# from. A row starts 'available' (seeded by --seed-validation-pool, or
# hand-inserted with real numbers, with issued_at set to when it entered
# the pool) and moves to 'assigned' the moment the machine acknowledges
# it (status 0x00 on LP 58) — never reused, and never removed, so
# validation_pool doubles as its own audit trail of what this gateway
# has ever handed out. issued_at backs the pool-age check below (the
# Decisions Annex's D-16): a real server tracks when it minted a batch,
# and issued_at is this lab stand-in's equivalent for locally-seeded
# numbers — see _pool_age_hours() and --pool-age-alert-hours.

# How often the Table C-7 sweep runs, in seconds. 0 means every cycle.
#
# Every cycle is what this used to do, and on an EGT Premier it costs 13 extra
# chunked exchanges -- enough to push a 5-second cycle over its own interval
# ceiling (§2.3.3 caps the poll interval at 5.0s, so a longer interval is not
# available as a remedy). The practical consequence was that the only way to
# stay inside the budget was --skip-table-c7-sweep, which left all ~154 c7_*
# columns NULL forever: the meters were never logged at all.
#
# A cadence is the fix. C-7's extended meters are cumulative lifetime
# counters; nothing in them needs sub-minute resolution, while the core
# meters that DO move fast are still read every cycle. The default is
# therefore a minute rather than every cycle -- strictly more data than
# skipping and strictly less traffic than before.
DEFAULT_TABLE_C7_INTERVAL = 60.0

DEFAULT_HISTORY_CAP = 200
DEFAULT_HISTORY_INTERVAL = 60.0
DEFAULT_BURST_COUNT = 10
DEFAULT_GENERAL_POLL_RETRIES = 3
# How many polls the startup drain will spend clearing a pre-existing queue.
# §2.2.1 guarantees at least 20 elements; machines hold more (the lab
# cabinet's is about 35), and a long unattended spell can leave the queue
# full, so this is generous rather than exact. Capped because a machine
# emitting faster than we drain would otherwise never let the run start.
STARTUP_DRAIN_MAX = 64

# How long the link must be down before a recovery is treated as "someone
# may have moved this cable" rather than "a poll was missed".
#
# This is deliberately NOT the data-loss threshold. By §2.2.1's own arithmetic
# the exception queue is gone after about 8 seconds of silence on a machine
# being played, and no amount of sweeping afterwards recovers it. 30 seconds
# is a PHYSICAL-ACT threshold: the time it takes to unplug a cable, walk to
# another cabinet and plug it in. Nobody does that in less.
DEFAULT_COMMS_GAP_SECONDS = 30.0
DEFAULT_POOL_AGE_ALERT_HOURS = 36.0

# SAS 6.02 §2.3.3 ("Polling Rate"): "The host may not issue general polls
# or long polls to any single gaming machine at a rate faster than once
# per 200 ms. The slowest allowable polling rate is 5000 ms." This is a
# protocol requirement, not a tuning knob -- it bounds --interval (see
# main()'s validation) and paces the immediate general-poll retries in
# _general_poll_with_retry() below, which would otherwise fire back-to-
# back with no delay between them.
SAS_MIN_POLL_INTERVAL_S = 0.2
SAS_MAX_POLL_INTERVAL_S = 5.0


@dataclass
class HistoryConfig:
    mode: str = "ring"  # "ring" or "append"
    cap: int = DEFAULT_HISTORY_CAP  # ring mode only; ignored in append mode
    interval: float = DEFAULT_HISTORY_INTERVAL  # ring mode only; ignored in append mode
    burst_count: int = DEFAULT_BURST_COUNT

    def __post_init__(self) -> None:
        if self.mode not in ("ring", "append"):
            raise ValueError(f"mode must be 'ring' or 'append', got {self.mode!r}")


@dataclass
class PollState:
    """Mutable state carried between poll_and_log() calls across one run."""

    last_meters: dict | None = None
    last_history_write: float | None = None  # None: never written yet, forces a write on the first cycle
    # Same shape, for the Table C-7 sweep: None forces one on the first
    # cycle, so a run always captures C-7 at least once however long it lives.
    last_c7_sweep: float | None = None
    burst_remaining: int = 0
    # DRAIN PRIORITY. When the cabinet is producing events faster than the
    # sweep leaves room to collect, the optional meter polls are deferred
    # and the time is spent on general polls instead. See _should_drain().
    draining: bool = False
    drain_cycles: int = 0          # how many cycles this run spent draining
    # What inbound WfA pulls cost the event stream this run, in poll
    # slots. The number that decides whether on-demand pulls are
    # affordable on a busy floor -- reported, not assumed.
    wfa_polls_spent: int = 0
    last_event_rate: float = 0.0   # events/s measured over the last sweep
    last_capacity: float = 0.0     # polls/s §2.3.3 pacing allows
    # Continuity. epoch_id is the epoch every meter row written this cycle
    # belongs to; see "Continuity: epochs" above.
    epoch_id: int | None = None
    identity: dict | None = None
    cycles_since_identity: int = 0
    # Consecutive cycles whose general poll failed outright, and when the
    # first of them was. Together they are how long the link has been down,
    # which is the only thing that distinguishes a flaky byte from somebody
    # walking off with the cable.
    consecutive_poll_failures: int = 0
    gap_started_mono: float | None = None
    interrogation_pending: float | None = None  # gap seconds, once recovered
    # --rte was asked for, and whether the machine has actually been put into
    # it. They are not the same thing, and the gap between them is where a
    # whole soak went: see poll_and_log()'s retry.
    rte_wanted: bool = False
    rte_enabled: bool = False
    # The pool-age alert is one CONDITION, not one event per cycle. These
    # track the episode so poll_errors gets a record of it rather than a flood
    # -- see _check_pool_age().
    # Exception 0x3C seen; the re-read is deferred like the comms-restored
    # interrogation, for the same reason -- see poll_and_log().
    options_change_pending: str | None = None
    # Off unless --decline-aft-cashout is passed. §8.7's stage 1 costs ~8s
    # on every cashout when nothing answers it; declining skips straight to
    # the ticket path. Per-model: on a cabinet with no printer "its own
    # means" is an attendant, so this would make things worse there.
    decline_aft_cashout: bool = False
    # §12.5.3's coin-in meter, from the last game-start event seen. The
    # live continuity check compares each new one against it.
    last_game_start_coin_in: int | None = None
    last_game_start_at: str | None = None
    stream_gaps: int = 0
    stream_credits_lost: int = 0
    # Headroom: events collected and the window they arrived in.
    events_this_cycle: int = 0
    # SEEN, not filed. events_this_cycle is incremented in
    # _dispatch_exception, and the heartbeat QUEUES most codes into `seen`
    # to dispatch after the sweep -- so mid-sweep that counter is still
    # near zero however hard the cabinet is flooding. The live drain probe
    # read it anyway and therefore could never fire, which is why
    # --drain-priority did nothing on its first burst (2026-11-09T00:18).
    events_seen_live: int = 0
    # LOSS THAT ACTUALLY HAPPENED, this cycle. Two independent witnesses:
    # the cabinet's own 0x70, and §12.5.3's arithmetic on the coin-in
    # carried inside each game start. Neither is a model of the queue --
    # both are evidence that something is already gone.
    overflows_this_cycle: int = 0
    gaps_this_cycle: int = 0
    # FAST POLLING UNDER PRESSURE. None until the machine has been asked;
    # True only if LP 0xA0 Table 7.14e bit 0 says it supports 40 ms. Never
    # assumed: a cabinet that does not advertise it never gets polled fast,
    # whatever the flags say.
    fast_poll_supported: bool | None = None
    normal_poll_interval: float = 0.0   # what to restore when drain ends
    # THE DECISION MUST NOT MOVE WHEN THE RATE MOVES.
    #
    # last_capacity is 1/current interval, so switching to 40 ms multiplies
    # it by 7.5 -- and the SAME event rate then reads as a seventh of the
    # budget and drain mode turns straight back off, which restores 300 ms,
    # which makes it look busy again. Measured 2026-11-09T18:46: ON at 3.3
    # ev/s "against a 3.3/s budget", OFF two seconds later at 2.59 "against
    # a 25.0/s budget", repeatedly. The hysteresis cannot help: it guards
    # the numerator and this moves the DENOMINATOR.
    #
    # So the decision uses the BASELINE budget -- what the configured
    # interval could collect -- which does not change when we temporarily
    # speed up. The question is "is the cabinet out-running our normal
    # rate", and the answer must not depend on our answer to it.
    baseline_capacity: float = 0.0
    fast_poll_active: bool = False
    fast_poll_fallback_announced: bool = False
    pool_stale_since: str | None = None
    pool_stale_last_row: float | None = None
    pool_stale_last_print: float | None = None
    clear_candidate_cycles: int = 0
    clear_first_seen_at: str | None = None
    # LAB ONLY, and None unless --lab-ticket-authority was given. See
    # _capture_ticket_in() for why this tool is otherwise read-only on
    # ticket-in, and why a file is what lifts that rather than a flag alone.
    ticket_authority: dict | None = None
    ticket_authority_binding: object | None = None  # ticket_lab_authority.MachineBinding
    ticket_authority_path: str | None = None
    # LAB ONLY, and only ever set when --lab-ticket-authority is also in
    # effect. See LabAutoConfig for why that file is the gate.
    #
    # handpay_pending_since is monotonic, not wall clock: it is measuring how
    # long a lockup has gone unattended, and a clock that can step backwards
    # over NTP would make that negative.
    handpay_pending_since: float | None = None
    handpay_resets: int = 0
    aft_pushes: int = 0
    aft_cents_pushed: int = 0
    aft_last_push_mono: float | None = None
    # The meters as they stood BEFORE the first zeroed reading. Confirmation
    # has to compare against this, not against the previous cycle: once the
    # first zero is in, last_meters is zero too, nothing can "fall to zero"
    # again, and a check against the previous cycle would decide the meters
    # had recovered on the very next poll.
    clear_baseline: dict | None = None
    # Set when the machine itself announces a meter reset (CLEAR_ANNOUNCED_BY).
    # Consumed by the next maintain_epoch() call, which needs no further
    # evidence: the machine has already said so.
    clear_announced_at: str | None = None


# --- Continuity: epochs -----------------------------------------------------
#
# A gateway on a floor does not stay pointed at one unchanging machine. The
# two events that break a meter history are both routine:
#
#   RAM CLEAR -- on EGT especially, a progressive change or a game package
#   change requires one, and every meter goes to zero. It is scheduled
#   maintenance, not a fault.
#
#   MACHINE SWAP -- the gateway is moved to a different cabinet, which needs
#   new HMAC keys and has an entirely unrelated meter history.
#
# Neither may stop the poller. A technician on a floor has no screen and no
# keyboard, and a gateway that needs attention after every RAM clear is a
# gateway that generates callouts. So the poller records the discontinuity and
# keeps going; resolving it is the CMS's job, which is where the history to
# resolve it with lives.
#
# Silence would be worse than stopping, though. Writing post-clear meters into
# the same history as pre-clear ones produces a record that looks continuous
# and is not -- every total computed across the boundary is wrong, and nothing
# says so. Hence epochs: each row of meter history belongs to one, and a new
# epoch is an explicit, queryable statement that nothing before it reconciles
# against anything after it.
#
# acknowledged_at is for the CMS and the gateway never sets it, so
# "SELECT * FROM machine_epochs WHERE acknowledged_at IS NULL" is the
# complete list of discontinuities nobody has dealt with yet.

# How many consecutive cycles must agree before a mass-zero reading is called
# a RAM clear. A single sweep can come back wrong -- a cable nudged, a machine
# mid-reset, a decode fault -- and declaring an epoch on one bad read would
# fabricate a discontinuity that never happened, which is the same corruption
# this exists to prevent, just in the other direction. Three cycles of the
# same answer is cheap; the meters are not going anywhere.
CLEAR_CONFIRM_CYCLES = 3

# Exceptions by which a machine announces, in SAS's own words, that its meters
# are gone. Captured on an EGT Premier during a technician's RAM clear:
#
#   0x7A  gaming machine soft (lifetime-to-date) meters reset to zero
#   0x32  CMOS RAM error (no data recovered from EEPROM)
#   0x3A  memory error reset (operator used self test switch)
#
# This beats inferring a clear from the meters. The meter heuristic reads a
# consequence and needs three cycles to be sure; 0x7A is the machine stating
# the fact, and one of them is worth more than any amount of the other. The
# heuristic stays as the backstop for the case no exception can cover -- a
# clear performed while the gateway was not running, where the queue has long
# since been drained by someone else or the event predates this database.
#
# 0x7B (bill validator period totals reset) is deliberately NOT here: an
# attendant resets those during a normal drop, with the lifetime meters
# untouched, so treating it as a clear would open an epoch on every drop.
CLEAR_ANNOUNCED_BY = {0x7A, 0x32, 0x3A}

# A mass-zero is this fraction of the cumulative meters that previously held a
# non-zero value. Not all of them: a machine can legitimately report a few
# meters it no longer populates, and demanding 100% would miss real clears.
CLEAR_FRACTION = 0.8

# Identity is 2-3 extra long polls, so it is not read every cycle. It IS read
# whenever the meters look cleared, because that is the one moment it matters:
# a RAM clear and a machine swap look identical in the meters and are told
# apart only by who is answering.
IDENTITY_CHECK_CYCLES = 120


# How long a second writer waits for the poller's write lock before giving up.
# The poller's own cycle is 5-7s at 300ms pacing, so anything near SQLite's
# default (and Python's 5s) loses the race on a busy gateway. §5.4's "replace
# the pool without restarting anything" is only true if the replacement is
# willing to wait out one cycle.
GATEWAY_DB_BUSY_TIMEOUT_MS = 30_000


def open_gateway_db(path: str, *, busy_timeout_ms: int = GATEWAY_DB_BUSY_TIMEOUT_MS):
    """Open the gateway database the way every writer to it must.

    TWO PRAGMAS, AND BOTH WERE MISSING. Three documents in this repository
    asserted "SQLite is in WAL mode, so a second writer is fine"; nothing
    anywhere ever set it. The database was in the default rollback-journal
    mode, where a writer takes an EXCLUSIVE lock that blocks readers too --
    the opposite of what the claim promised, and nobody had run the two tools
    against each other to find out.

    It surfaced as `sqlite3.OperationalError: database is locked` from
    pool_replace.py's BEGIN IMMEDIATE while a soak was running, i.e. in
    exactly the live-replacement case §5.4 describes a CMS performing.

    journal_mode is persistent: set once, it stays in the file. busy_timeout
    is per-connection and must be set by every process that writes.
    """
    conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000)
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def read_identity(client) -> dict:
    """Who is answering. Every field is optional -- section 4.4 lets a machine
    ignore a long poll it does not support, and an unreadable identity is not
    evidence that the machine changed.
    """
    identity: dict = {"serial_number": None, "asset_number": None,
                      "sas_version": None}
    try:
        version = client.send_sas_version_and_serial()
        identity["serial_number"] = version.serial_number.strip()
        identity["sas_version"] = version.sas_version.strip()
    except SASError:
        pass
    try:
        identity["asset_number"] = client.aft_game_lock_and_status_request(
            lock_code=0xFF).asset_number
    except SASError:
        pass
    return identity


def ticket_authority_binding_verdict(binding, identity: dict | None, *,
                                     allow_unverified: bool) -> tuple[str, str] | None:
    """Whether a run holding a bound ticket file may talk to this cabinet.

    Returns None to proceed, or (reason, detail). Three cases that look alike
    and are not:

    MISMATCHED -- the machine identified itself and it is the wrong one.
    Always fatal. No flag overrides it, because an operator cannot know better
    than the cabinet's own identifiers.

    UNVERIFIABLE -- nothing comparable. §4.4 lets a machine ignore LP 0x54 and
    the lab EGM does, so a file bound only by serial cannot be checked against
    it at all. Fatal by default (an unenforceable binding is not a binding),
    overridable by an operator standing at the machine.

    UNBOUND -- the file names no cabinet. Permitted, because whoever wrote the
    file chose that, but it is said loudly.

    SERIAL WINS WHERE BOTH EXIST, AND AN ASSET MISMATCH DOES NOT VETO IT. A
    RAM clear wipes the asset number while the serial survives, so refusing a
    cabinet whose serial matches because its asset was retyped would block a
    legitimate machine after every clear. The fallback runs only where the
    serial cannot be compared at all.

    The original check was ``if seen and seen.strip() != bound``, which
    silently treated UNVERIFIABLE as fine. Replacing that with a bare refusal
    treated it as MISMATCHED and killed a running soak on a command line that
    had worked minutes before. Both failures came from one conflation.
    """
    if binding is None or not binding.is_bound():
        return ("unbound", "the ticket file names no cabinet at all")

    seen_serial = usable_serial(identity)
    if binding.serial is not None and seen_serial is not None:
        if seen_serial != binding.serial.strip():
            return ("mismatch",
                    f"ticket file is for serial {binding.serial!r} but this "
                    f"cabinet is {seen_serial!r}")
        return None  # Serial is authoritative; the asset cannot overrule it.

    seen_asset = usable_asset(identity)
    if binding.asset is not None and seen_asset is not None:
        if seen_asset != binding.asset:
            return ("mismatch",
                    f"ticket file is for asset {binding.asset} but this "
                    f"cabinet is asset {seen_asset}")
        return None

    if allow_unverified:
        return None
    return ("unverifiable",
            f"this cabinet reports {machine_key(identity) or 'no identity at all'} "
            f"and the ticket file is bound to {binding.describe()}, so the "
            "binding cannot be checked")


# Imported, never copied: two placeholder lists drift, and the floor lessons
# that built this one (an empty serial, all zeros, "n/a", and an IGT cabinet
# reporting asset 7 with serial "7") were paid for once already in
# survey_store.py. The sibling directory is on the path for the same reason
# ticket_lab_authority is imported rather than reimplemented.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from saspy.constants import is_placeholder_identifier  # noqa: E402


# Table A-1 defines exception 0x00 ("no activity") and then jumps to 0x11, so
# 0x01-0x10 are entirely unassigned. A general poll answering with one of those
# AND with this client's own address is unambiguously the Table 12.5 address
# byte being read as an exception code -- real time event mode left enabled in
# the cabinet by an unclean stop.
#
# The detection only holds for addresses 1-16. At address 20 a returned 0x14
# is both the address and a real code ("reel 1 ... "), and nothing can tell
# them apart from one byte; this project's cabinets are on address 1.
RTE_STUCK_DETECTABLE_BELOW = 0x11


def rte_looks_stuck(code: int, address: int) -> bool:
    """Whether one general-poll byte says the cabinet is still in RTE."""
    return (int(code) == int(address)
            and 0 < int(address) < RTE_STUCK_DETECTABLE_BELOW)


def recover_stuck_rte(client, address: int | None = None) -> tuple[str, int | None]:
    """Clear real time event mode left on by an unclean stop, before anything
    else reads the wire.

    Returns (what happened, the exception code the probe consumed or None).

    WHY THIS EXISTS. The teardown that disables RTE is not guaranteed to
    succeed: LP 0x0E's acknowledgement can itself be pre-empted by a queued
    event (§12.3), and on 2026-11-02 that is exactly what happened -- a soak
    was stopped with SIGTERM, the service started, and its interrogation
    logged "exception 0x01 during interrogation: unknown/unassigned code 0x01"
    three times. The service unit's ExecStartPre, which exists for this, had
    also failed.

    Each of those mis-read polls is worse than a wrong log line: it consumes
    ONE BYTE of a multi-byte Table 12.5 frame and leaves the rest in the
    buffer, so the long polls that follow are misframed. The identity and
    configuration an interrogation reads in that state cannot be trusted.

    The transport resets the input buffer before every exchange, so the
    half-frame left by the probe is discarded by the disable poll itself.
    """
    # The poller gets its address from the .ini through connect_from_config,
    # not from argparse, so the client is the only thing that knows it.
    if address is None:
        address = getattr(client, "address", None)
    if address is None:
        return ("skipped: this client does not expose its address", None)
    try:
        code = client.general_poll()
    except SASError as e:
        return (f"could not probe the cabinet's framing ({type(e).__name__}: {e})",
                None)
    if not rte_looks_stuck(code, address):
        return ("ordinary framing", int(code))
    try:
        client.enable_real_time_events(False)
    except SASError as e:
        return (f"the cabinet answered general polls with its own address "
                f"(0x{int(code):02X}), so real time event mode was still on, "
                f"and disabling it FAILED ({type(e).__name__}: {e}). Readings "
                "from this run's interrogation are suspect.", None)
    try:
        again = client.general_poll()
    except SASError as e:
        return (f"disabled a stuck real time event mode, but the confirming "
                f"poll failed ({type(e).__name__}: {e})", None)
    if rte_looks_stuck(again, address):
        return (f"real time event mode is STILL on after LP 0x0E "
                f"(0x{int(again):02X}); power-cycle the cabinet -- §12 requires "
                "the ordinary mode on power up", None)
    return (f"real time event mode had been left ON (general poll answered "
            f"0x{int(code):02X}, this machine's own address); disabled it, and "
            f"the next poll answered 0x{int(again):02X}", int(again))


def usable_serial(identity: dict | None) -> str | None:
    """The serial number, or None if the machine did not really give one.

    §4.4 lets a cabinet ignore LP 0x54 entirely, and this project's own lab
    EGM does. Others answer with a placeholder -- an empty string, all zeros,
    "n/a" -- which passes a `is not None` test and is not an identity.
    survey_store.py learned this on the floor on 2026-10-27, where an IGT
    cabinet reported asset 7 and serial "7": non-empty, not all zeros, not
    "n/a", and reads like a real serial in a report. The placeholder list is
    imported from there rather than copied so the two cannot drift.
    """
    if not identity:
        return None
    serial = (identity.get("serial_number") or "").strip()
    if is_placeholder_identifier(serial):
        return None
    # A "serial" that is just the asset number re-typed identifies nothing
    # the asset number does not already identify.
    asset = identity.get("asset_number")
    if asset not in (None, 0) and serial == str(asset):
        return None
    return serial


def usable_asset(identity: dict | None) -> int | None:
    """The asset number, or None if it was never assigned.

    Zero is not an asset number: it is the field's unset value. SAS carries
    it as a 4-byte little-endian BINARY integer (decode_binary_le), so a
    letter is not representable in it at all -- the operator's "we always use
    numbers, never letters" convention is satisfied by the protocol itself,
    and there is nothing here to validate.
    """
    if not identity:
        return None
    asset = identity.get("asset_number")
    return None if asset in (None, 0) else int(asset)


def machine_key(identity: dict | None) -> tuple[str, str] | None:
    """Which identifier this cabinet can actually be known by, and which one
    it is. ('serial', '000106719'), ('asset', '246'), or None.

    THE FALLBACK IS AN OPERATIONAL REQUIREMENT, not a convenience. Not every
    machine announces a serial over SAS; on the Belize and Trinidad floors the
    asset number is the identifier that is always present, never reissued, and
    -- in Trinidad -- unique across all three properties, so it is a sound key
    where the serial is absent.

    It is a WEAKER key and the caller is told which it got. A serial is burned
    in and survives a RAM clear; an asset number is typed in by an operator and
    a RAM clear wipes it. An epoch keyed on an asset number is evidence about
    a machine's identity that a RAM clear can erase.
    """
    serial = usable_serial(identity)
    if serial is not None:
        return ("serial", serial)
    asset = usable_asset(identity)
    if asset is not None:
        return ("asset", str(asset))
    return None


def identity_changed(previous: dict | None, current: dict) -> tuple[str, str] | None:
    """(kind, description) of what changed, or None.

    The two identifiers are not equivalent and must not be treated as one.
    But NOT for the reason this comment gave until 2026-11-05, which was that
    the serial is "burned into the machine and survives a RAM clear". IT IS
    NOT. On the cabinets this project works with, the serial is TYPED IN AT
    CONFIGURATION, during the RAM clear, exactly like the asset number. A
    technician can leave it blank or mistype it, and on this project's own lab
    machine one did -- which is what produced a ticket run refused for a
    serial mismatch, and which explains the cabinets that "do not report a
    serial" (the field is empty, not ignored per §4.4).

    The serial is still the better key, for a different reason: entered
    correctly it is a manufacturer number that is unique everywhere, while an
    asset number is unique only by house policy -- and §5a of the handover
    records a floor where that policy does not hold. So a different serial
    still means a different cabinet, and an asset that changes while the
    serial holds is still a reconfiguration. What changes is that neither
    identifier is self-verifying: both are only as good as the data entry, and
    a cleared machine can come back with a serial that is blank, mistyped, or
    somebody else's.

    WITH NO SERIAL ON EITHER SIDE THAT REASONING INVERTS, and this used to get
    it backwards. "The asset changed but the serial held" is only reassuring
    when there IS a serial holding. On a cabinet that reports none -- §4.4
    permits it, and the lab EGM does it -- the asset number is the only
    identity there is, and on these floors asset numbers are never reissued.
    So a different asset number with nothing anchoring it is the best
    available evidence of a DIFFERENT MACHINE, not of the same one
    reconfigured. Reporting it as a reconfiguration would quietly carry one
    cabinet's meter history onto another.

    Only fields BOTH readings carry are compared. A machine that stops
    answering 0x54 has not become a different machine, and treating a None as
    a change would open an epoch every time a poll was missed.
    """
    if not previous:
        return None
    was, now = usable_serial(previous), usable_serial(current)
    if was is not None and now is not None and was != now:
        return "machine_changed", f"serial_number {was!r} -> {now!r}"
    had_serial = was is not None or now is not None
    was_asset, now_asset = usable_asset(previous), usable_asset(current)
    if was_asset is not None and now_asset is not None and was_asset != now_asset:
        if had_serial:
            return "asset_changed", f"asset_number {was_asset!r} -> {now_asset!r}"
        return ("machine_changed",
                f"asset_number {was_asset!r} -> {now_asset!r} and neither "
                "reading carries a serial number, so the asset number is the "
                "only identity this cabinet has (§4.4) -- on these floors it "
                "is never reissued, so a different one is a different machine")
    return None


def looks_cleared(previous: dict | None, current: dict) -> tuple[bool, int, int]:
    """Whether the cumulative meters just went to zero en masse.

    Returns (verdict, how many fell to zero, how many were populated), so a
    caller can report the evidence rather than just the conclusion.
    """
    if not previous:
        return False, 0, 0
    populated = zeroed = 0
    for field in DECREASE_CHECK_FIELDS:
        was, now = previous.get(field), current.get(field)
        if was is None or not was > 0:
            continue
        populated += 1
        if now == 0:
            zeroed += 1
    if not populated:
        return False, 0, 0
    return zeroed >= max(5, populated * CLEAR_FRACTION), zeroed, populated


def load_persisted_meters(conn: sqlite3.Connection) -> dict | None:
    """The last reading this database holds, as a {field: value} dict.

    meters_current is one row by construction, so this is cheap and is the
    only memory a restarted gateway has of what the machine looked like
    before it stopped.
    """
    try:
        row = conn.execute(
            f"SELECT {', '.join(ALL_METER_FIELDS)} FROM meters_current "
            "WHERE id = 1").fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return dict(zip(ALL_METER_FIELDS, row))


# Fields of interrogation_games worth comparing after exception 0x3C. Not all
# of them: game_name and paytable_name are labels, and a vendor is free to
# re-word one without changing anything that affects accounting.
GAME_CONFIG_FIELDS = ("denomination", "max_bet", "progressive_group",
                      "game_options", "paytable_id", "base_percentage")

# A change in any of these invalidates comparisons across it, so it opens a new
# epoch rather than only being noted.
#
# DENOMINATION is the one that matters and the reason this exists. SAS meters
# are counted in credits (or in cents, per LP 0xA0's meter_model), so a
# denomination change silently rescales every credit figure in the database:
# 1000 credits at $0.01 and 1000 credits at $0.25 are the same meter reading
# and twenty-four dollars apart. Without a boundary there is no way to know
# which rows mean which.
#
# PAYTABLE_ID and BASE_PERCENTAGE change what the machine is, for an auditor,
# even though they do not rescale a meter. Treated the same way: the epoch is
# cheap and a mislabelled window is not.
EPOCH_WORTHY_CONFIG_FIELDS = ("denomination", "paytable_id", "base_percentage")


def game_config_diff(conn: sqlite3.Connection, previous_id: int,
                     current_id: int) -> list[tuple[int, str, object, object]]:
    """What changed between two interrogations' game configurations.

    Returns (game_number, field, was, now) per difference. Games that appear
    or disappear are reported as a change on the fields they carry, because an
    operator enabling a game is exactly the kind of thing exception 0x3C
    announces.

    Only fields BOTH interrogations carry are compared: §4.4 lets a machine
    refuse LP 0x53 for a game, and a refusal is not a change.
    """
    def rows(interrogation_id):
        out = {}
        for row in conn.execute(
                f"SELECT game_number, {', '.join(GAME_CONFIG_FIELDS)} "
                "FROM interrogation_games WHERE interrogation_id = ?",
                (interrogation_id,)):
            out[row[0]] = dict(zip(GAME_CONFIG_FIELDS, row[1:]))
        return out

    was_games, now_games = rows(previous_id), rows(current_id)
    changes: list[tuple[int, str, object, object]] = []
    for game in sorted(set(was_games) | set(now_games)):
        was, now = was_games.get(game), now_games.get(game)
        if was is None:
            changes.append((game, "game", "absent", "present"))
            continue
        if now is None:
            changes.append((game, "game", "present", "absent"))
            continue
        for field in GAME_CONFIG_FIELDS:
            a, b = was.get(field), now.get(field)
            if a is not None and b is not None and a != b:
                changes.append((game, field, a, b))
    return changes


def open_epoch(conn, now: str, reason: str, identity: dict, detail: str) -> int:
    """Close the open epoch, start a new one, return its id."""
    conn.execute(
        "UPDATE machine_epochs SET ended_at = ? WHERE ended_at IS NULL", (now,))
    cursor = conn.execute(
        "INSERT INTO machine_epochs (started_at, reason, serial_number, "
        "asset_number, sas_version, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (now, reason, identity.get("serial_number"), identity.get("asset_number"),
         identity.get("sas_version"), detail))
    return int(cursor.lastrowid)


def migrate_epoch_columns(conn: sqlite3.Connection) -> None:
    """Add epoch_id to meter tables created before epochs existed.

    CREATE TABLE IF NOT EXISTS is a no-op on a table that already exists, so
    a database from an earlier run keeps its old shape and every INSERT naming
    epoch_id fails. Soak databases are the point of this tool and are days
    long; silently refusing to open them, or quietly dropping their history,
    would both be worse than a one-line ALTER.

    Existing rows get NULL, which is correct and honest: those readings were
    taken before continuity was tracked, so which epoch they belong to is
    genuinely unknown rather than epoch 1.
    """
    for table in ("meters_current", "meters_history"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if columns and "epoch_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN epoch_id INTEGER")
            print(f"migrated: added epoch_id to {table} "
                  "(existing rows keep NULL -- they predate epoch tracking)")
    # Same reasoning for the RTE payload column: a database from a run before
    # --rte existed keeps its old machine_events shape, and every insert
    # naming rte_fields would fail on it.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ticket_in_events)")}
    if columns and "authority_decision" not in columns:
        conn.execute("ALTER TABLE ticket_in_events ADD COLUMN authority_decision TEXT")
        print("migrated: added authority_decision to ticket_in_events "
              "(existing rows keep NULL -- nothing answered them)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(machine_events)")}
    if columns and "rte_fields" not in columns:
        conn.execute("ALTER TABLE machine_events ADD COLUMN rte_fields TEXT")
        print("migrated: added rte_fields to machine_events "
              "(existing rows keep NULL -- they predate real time events)")
    conn.commit()


def maintain_epoch(conn, client, state, values: dict, now: str) -> None:
    """Keep state.epoch_id pointing at the right epoch, opening a new one when
    the machine's history has demonstrably broken. Never raises, never exits.
    """
    if state.epoch_id is None:
        # First cycle of this run. Adopt the open epoch if the database has
        # one -- a restarted gateway polling the same machine has not had a
        # discontinuity, and inventing one on every restart would bury the
        # real ones among the noise.
        row = conn.execute(
            "SELECT id, serial_number, asset_number, sas_version FROM machine_epochs "
            "WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        identity = read_identity(client)
        state.identity = identity
        state.cycles_since_identity = 0
        if row is None:
            state.epoch_id = open_epoch(
                conn, now, "first_seen", identity,
                "first run against this database")
            print(f"[{now}] epoch {state.epoch_id} opened (first_seen): "
                  f"serial {identity.get('serial_number')} "
                  f"asset {identity.get('asset_number')}")
            return
        previous = {"serial_number": row[1], "asset_number": row[2],
                    "sas_version": row[3]}
        state.epoch_id = int(row[0])
        changed = identity_changed(previous, identity)

        # A clear or a swap that happened while the gateway was STOPPED is the
        # case in-memory state cannot see: last_meters is None on the first
        # cycle, so nothing has fallen and nothing looks cleared. The last
        # persisted reading is the baseline that closes that gap -- without it,
        # powering a machine down, clearing it, and starting the gateway again
        # files the new machine's meters under the old machine's history.
        persisted = load_persisted_meters(conn)
        cleared, zeroed, populated = looks_cleared(persisted, values)

        if changed and changed[0] == "machine_changed":
            state.epoch_id = open_epoch(
                conn, now, "machine_changed", identity, f"on restart, {changed[1]}")
            print(f"[{now}] MACHINE CHANGED while stopped: {changed[1]}")
            print(f"[{now}] epoch {state.epoch_id} opened (machine_changed). "
                  "New HMAC/registration keys are the CMS's to issue; polling "
                  "continues meanwhile.")
        elif cleared:
            # Same cabinet, meters gone. A RAM clear also wipes the operator's
            # asset number, so an asset that changed alongside this is evidence
            # FOR a clear, not evidence of a swap.
            detail = (f"{zeroed} of {populated} populated cumulative meters "
                      "zeroed while the gateway was stopped")
            if changed:
                detail += f"; {changed[1]} (a RAM clear wipes the asset number)"
            state.epoch_id = open_epoch(conn, now, "ram_clear", identity, detail)
            # The machine queues its meter-reset announcement in a NON-VOLATILE
            # buffer (§2.2.1), so a clear performed while the gateway was
            # stopped is still waiting to be drained when it starts. Both
            # detectors then fire for the SAME event: the meter comparison on
            # the first cycle, the announcement on the next, and two epochs are
            # opened for one clear. Observed on hardware -- epochs 2 and 3,
            # eleven seconds apart, both ram_clear, both real.
            #
            # The announcement is evidence FOR the clear just recorded, not a
            # second one, so it is consumed here.
            if state.clear_announced_at:
                print(f"[{now}] (the machine's own meter-reset announcement from "
                      f"{state.clear_announced_at} refers to this same clear, "
                      "and is not a second one)")
                state.clear_announced_at = None
            print(f"[{now}] RAM CLEAR while stopped: {detail}")
            print(f"[{now}] epoch {state.epoch_id} opened (ram_clear). Polling "
                  "continues; nothing before this reconciles against anything "
                  "after.")
        elif changed:
            # Asset number changed with the meters intact: a reconfiguration,
            # not a discontinuity. Still an epoch, because AFT transfers quote
            # the asset number and the CMS has re-registration to do -- the
            # detail says plainly that the meter history is unbroken.
            state.epoch_id = open_epoch(
                conn, now, "asset_changed", identity,
                f"on restart, {changed[1]}; meters are CONTINUOUS across this")
            print(f"[{now}] ASSET NUMBER CHANGED while stopped: {changed[1]}")
            print(f"[{now}] epoch {state.epoch_id} opened (asset_changed). "
                  "Meters are continuous; AFT re-registration is the CMS's.")
        return

    state.cycles_since_identity += 1

    # The machine said it cleared its own meters. Nothing here is better
    # evidence than that, so it is acted on at once rather than waiting for
    # three cycles of a heuristic to agree with it.
    if state.clear_announced_at:
        announced = state.clear_announced_at
        state.clear_announced_at = None
        state.clear_candidate_cycles = 0
        state.clear_first_seen_at = None
        state.clear_baseline = None
        identity = read_identity(client)
        state.identity = identity
        state.cycles_since_identity = 0
        state.epoch_id = open_epoch(
            conn, now, "ram_clear", identity,
            f"announced by the machine at {announced} "
            f"(exception {', '.join(f'0x{c:02X}' for c in sorted(CLEAR_ANNOUNCED_BY))} "
            "are meter-reset announcements)")
        state.burst_remaining = max(state.burst_remaining, CLEAR_CONFIRM_CYCLES)
        print(f"[{now}] RAM CLEAR announced by the machine at {announced}.")
        print(f"[{now}] epoch {state.epoch_id} opened (ram_clear). Polling "
              "continues; nothing before this reconciles against anything after.")
        return

    # While a clear is pending, every confirmation is measured against the
    # pre-clear baseline, so "still zero" keeps counting as evidence.
    baseline = state.clear_baseline if state.clear_candidate_cycles else state.last_meters
    cleared, zeroed, populated = looks_cleared(baseline, values)

    # Identity is read on a slow schedule, and immediately whenever the meters
    # look cleared -- a RAM clear and a machine swap are indistinguishable in
    # the meters, and only the identity tells them apart.
    if cleared or state.cycles_since_identity >= IDENTITY_CHECK_CYCLES:
        identity = read_identity(client)
        state.cycles_since_identity = 0
        changed = identity_changed(state.identity, identity)
        state.identity = identity
        if changed and changed[0] == "machine_changed":
            state.clear_candidate_cycles = 0
            state.clear_first_seen_at = None
            state.clear_baseline = None
            state.epoch_id = open_epoch(
                conn, now, "machine_changed", identity, changed[1])
            state.burst_remaining = max(state.burst_remaining, 5)
            print(f"[{now}] MACHINE CHANGED: {changed[1]}")
            print(f"[{now}] epoch {state.epoch_id} opened (machine_changed). "
                  "Polling continues; the CMS owns re-keying and reconciliation.")
            return
        if changed and not cleared:
            # Asset reconfigured mid-run with the meters intact.
            state.epoch_id = open_epoch(
                conn, now, "asset_changed", identity,
                f"{changed[1]}; meters are CONTINUOUS across this")
            print(f"[{now}] ASSET NUMBER CHANGED: {changed[1]}")
            print(f"[{now}] epoch {state.epoch_id} opened (asset_changed). "
                  "Meters are continuous; AFT re-registration is the CMS's.")
            return

    if not cleared:
        if state.clear_candidate_cycles:
            # The meters came back. That was a bad read, not a clear, and
            # saying so matters: it is the evidence that the confirmation
            # requirement earned its keep.
            print(f"[{now}] meters recovered after "
                  f"{state.clear_candidate_cycles} zeroed cycle(s) -- a read "
                  "fault, not a RAM clear. No epoch opened.")
            conn.execute(
                "INSERT INTO poll_errors (occurred_at, poll_name, error_type, "
                "message) VALUES (?, ?, ?, ?)",
                (now, "meter_continuity", "TransientZeroes",
                 f"{state.clear_candidate_cycles} cycle(s) read as zeroed, "
                 "then recovered"))
        state.clear_candidate_cycles = 0
        state.clear_first_seen_at = None
        state.clear_baseline = None
        return

    if not state.clear_candidate_cycles:
        state.clear_baseline = state.last_meters
    state.clear_candidate_cycles += 1
    state.burst_remaining = max(state.burst_remaining, CLEAR_CONFIRM_CYCLES)
    if state.clear_first_seen_at is None:
        state.clear_first_seen_at = now

    if state.clear_candidate_cycles < CLEAR_CONFIRM_CYCLES:
        print(f"[{now}] {zeroed}/{populated} cumulative meters read as zero "
              f"({state.clear_candidate_cycles}/{CLEAR_CONFIRM_CYCLES}) -- not "
              "declaring a RAM clear until it is confirmed.")
        return

    detail = (f"{zeroed} of {populated} populated cumulative meters zeroed, "
              f"confirmed over {CLEAR_CONFIRM_CYCLES} cycles; first seen "
              f"{state.clear_first_seen_at}")
    state.epoch_id = open_epoch(conn, now, "ram_clear", state.identity or {}, detail)
    state.clear_candidate_cycles = 0
    state.clear_first_seen_at = None
    state.clear_baseline = None
    print(f"[{now}] RAM CLEAR confirmed: {detail}")
    print(f"[{now}] epoch {state.epoch_id} opened (ram_clear). Polling "
          "continues; nothing before this reconciles against anything after.")


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# Enter drain mode at this fraction of the pacing budget, leave at the
# lower one. Hysteresis, because a threshold with no gap oscillates: one
# busy cycle would defer the sweep, the quiet cycle that follows would
# resume it, and the sweep would never finish while play stayed near the
# line.
# §2.2.1 guarantees AT LEAST 20 elements. This cabinet was measured at 67
# (handover §2.2.1 work), and the floor figure is what a FLOOR design must
# use -- but the warning below is about THIS cabinet, so it uses what this
# cabinet was measured to hold. examples/queue_safety.py carries the same
# constant for the same reason and reports both.
MEASURED_LAB_QUEUE_DEPTH = 67

# §2.2.1's own announcement that the queue overran and the oldest entries
# are gone. The cabinet raising this is the only direct report of loss a
# host ever gets; everything else is inference.
BUFFER_OVERFLOW_EXCEPTION = 0x70

# §12.2: under real time events the rate "can be increased to 40 ms in
# order to better approximate real time", and Table 7.14e bit 0 is where a
# machine advertises that it supports it. This is the floor -- never below.
SAS_FAST_POLL_INTERVAL_S = 0.04

DRAIN_ENTER_FRACTION = 0.50
DRAIN_LEAVE_FRACTION = 0.30


def _set_poll_interval(client, seconds: float) -> bool:
    """Change the transport's §2.3.3 pacing floor, if there is one to change.

    Written through the transport because that is where pacing is enforced
    on every message -- the same place the headroom figure reads it from.
    A copy held anywhere else could drift from the one actually in force.
    """
    transport = getattr(client, "transport", None)
    if transport is None or not hasattr(transport, "_min_poll_interval"):
        return False
    transport._min_poll_interval = seconds
    return True


def _apply_drain_poll_rate(client, state: "PollState", now: str, *,
                           want_fast: bool,
                           fast_interval: float = SAS_FAST_POLL_INTERVAL_S,
                           fallback_interval: float = 0.0) -> None:
    """Poll faster while draining, and ONLY while draining.

    The operator's instruction, 2026-11-09: "Normal polling will be 300ms
    and under drain priority; it will be set to max_polling_rate. But only
    during drain priority, it will be set back to 300ms as soon as drain
    priority is off."

    WHY IT IS WORTH DOING, and why it is not simply "poll faster". One
    poll drains exactly one event, so the drain ceiling is 1 /
    poll_interval. At 300 ms that is 3.3 events/s against the 11.5/s a
    button burst produced on this cabinet -- a race that cannot be won, and
    the queue only decides how many seconds before loss starts. At 40 ms it
    is 25/s, which EXCEEDS the burst, so the queue drains back to empty and
    nothing is lost. The faster rate buys retention, not just latency, and
    this project's own 2026-11-02 note said otherwise (handover §31).

    THREE THINGS IT WILL NOT DO.

    It will not poll a machine that has not said it can. Table 7.14e bit 0
    is read during interrogation and cached on the state; None means the
    question was never answered, and None is not yes.

    It will not go below 40 ms. §12.2 names that number; this project has
    already polled at 14 ms once and recorded it as a bug.

    It will not stay fast. The rate goes back the moment drain priority
    ends, and the teardown restores it as well, so a run that dies mid-burst
    does not hand the next tool a cabinet it has been hammering.
    """
    if want_fast == state.fast_poll_active:
        return
    if want_fast:
        advertised = state.fast_poll_supported is True
        if advertised:
            interval = max(fast_interval, SAS_FAST_POLL_INTERVAL_S)
        elif fallback_interval and fallback_interval > 0:
            # THE OPERATOR'S CALL, 2026-11-09: "If a cabinet doesn't
            # advertise; we can still try 100ms during drain priority."
            #
            # Not advertising is not a refusal -- §4.4 lets a machine ignore
            # a long poll it does not implement, so an old cabinet that
            # never answered LP 0xA0 has said nothing either way. A rate
            # between the §2.3.3 floor and §12.2's 40 ms is a middle
            # position: still faster than the conservative reading, still
            # well short of what the advertised machines get.
            interval = max(fallback_interval, SAS_FAST_POLL_INTERVAL_S)
            if not state.fast_poll_fallback_announced:
                state.fast_poll_fallback_announced = True
                print(f"[{now}] the machine has NOT advertised Table 7.14e "
                      f"bit 0 (40 ms). Using the fallback "
                      f"{interval:.3f}s while draining rather than the "
                      f"advertised rate — set --drain-fast-poll-fallback 0 "
                      f"to stay at {state.normal_poll_interval:.3f}s.")
        else:
            if not state.fast_poll_fallback_announced:
                state.fast_poll_fallback_announced = True
                print(f"[{now}] fast polling NOT used: the machine has not "
                      f"advertised Table 7.14e bit 0 (40 ms support) and no "
                      f"fallback is configured. Staying at "
                      f"{state.normal_poll_interval:.3f}s.")
            return
        if _set_poll_interval(client, interval):
            state.fast_poll_active = True
            why = ("the machine advertises 40 ms in LP 0xA0"
                   if advertised else "fallback: the machine never said")
            print(f"[{now}] poll rate {state.normal_poll_interval:.3f}s -> "
                  f"{interval:.3f}s for the duration of drain priority "
                  f"(§12.2; {why}). Drain ceiling {1/interval:.0f}/s "
                  f"instead of {1/state.normal_poll_interval:.1f}/s.")
    else:
        if _set_poll_interval(client, state.normal_poll_interval):
            state.fast_poll_active = False
            print(f"[{now}] poll rate back to "
                  f"{state.normal_poll_interval:.3f}s (drain priority off).")


def _never_give_up() -> bool:
    """The default: finish the sweep whatever happens, as it always did."""
    return False


def _live_drain_probe(state: "PollState", started: float, *,
                      monotonic_fn=time.monotonic,
                      min_seconds: float = 5.0,
                      enter: float = DRAIN_ENTER_FRACTION,
                      on_fire=None):
    """Abandon the rest of the optional sweep WHILE it is running.

    WHY MID-SWEEP AND NOT NEXT CYCLE. The first hardware run of drain
    priority, 2026-11-08T23:51, measured 74% of the pacing budget and then
    ran the full 56.9-second sweep anyway, because the decision was taken
    from the PREVIOUS cycle's rate. Drain mode engaged 59 seconds later,
    by which time the burst was over.

    A 59-second reaction to a ten-second event is no reaction at all. The
    operator's customers mash for a few seconds; the thing being defended
    against is shorter than one cycle, so the decision has to live inside
    the cycle.

    `min_seconds` exists because a rate over a fraction of a second is
    noise: one event in the first 50 ms reads as 20/s and would abandon
    every sweep on this cabinet.
    """
    def probe() -> bool:
        elapsed = monotonic_fn() - started
        if elapsed < min_seconds:
            return False
        capacity = state.baseline_capacity or state.last_capacity
        if not capacity:
            return False
        if (state.events_seen_live / elapsed) < enter * capacity:
            return False
        # FIRE EVERYTHING AT ONCE, not just the sweep.
        #
        # The operator, 2026-11-09, reading a table of mine: "Isn't it
        # supposed to start faster polling immediately?" It was not. The
        # probe abandoned the optional polls mid-sweep but left the poll
        # RATE alone, because the rate was flipped at the top of a cycle
        # from state.draining -- which this probe does not set until the
        # next one. So a burst caught mid-sweep got the short sweep at once
        # and the fast rate a cycle later, which is the whole latency the
        # probe exists to remove, reintroduced one layer down.
        #
        # Setting state.draining here also makes the inter-cycle gap drain
        # instead of sleep, which was subject to the same lag.
        if not state.draining:
            state.draining = True
            if on_fire is not None:
                on_fire()
        return True
    return probe


def _should_drain(state: "PollState", enter: float = DRAIN_ENTER_FRACTION,
                  leave: float = DRAIN_LEAVE_FRACTION) -> bool:
    """Is the cabinet out-running the room the sweep leaves for collecting?

    THE ARITHMETIC THIS EXISTS FOR. One poll drains exactly one event --
    a general poll returns one Table 12.5 message, a pre-empted long poll
    returns one event instead of its reply -- and §2.3.3 caps any
    conforming host at one poll per 200 ms. So 5 events/s is the ceiling
    for ANY SAS host, and nothing can change that.

    What an implementation controls is how much of its own budget it
    spends on draining. Measured 2026-11-08 under a deliberate button
    burst: 170 exchanges in one 60-second sweep, of which only 97 were
    general polls. **A long poll that is not pre-empted drains nothing**,
    so 43% of the slots did no draining at all -- 1.6 events/s collected
    against a 3.3/s budget, while the cabinet produced 11.51/s. Six game
    starts were lost and the cabinet raised 24 buffer overflows.

    Deferring the optional sweeps turns those wasted slots into drains.
    It cannot beat the protocol ceiling; it roughly triples how close we
    get to it, which is the difference between losing a real customer's
    bets after seven seconds of mashing and after eleven.

    The operator authorised the trade explicitly, 2026-11-08: "suspending
    meter sweep is something I can exchange with accurate customer result
    reading." Meters are cumulative, so a deferred sweep loses nothing;
    an event is gone for good.
    """
    capacity = state.baseline_capacity or state.last_capacity
    if not capacity:
        return state.draining
    used = state.last_event_rate / capacity
    if state.draining:
        return used > leave
    return used >= enter


def _drain_sleep(client, conn, state: "PollState", seconds: float, *,
                 rte: bool, monotonic_fn=time.monotonic,
                 sleep_fn=time.sleep, now_fn=utc_now) -> int:
    """Spend the gap between cycles collecting events instead of sleeping.

    An ordinary cycle ends with `time.sleep(interval)`, which is correct
    when nothing is happening and exactly wrong when the cabinet is
    flooding: the queue keeps filling and nothing drains. Under drain
    priority the wait becomes general polls instead, paced by the
    transport's own §2.3.3 floor.

    SOURCE LABEL: these are filed as 'rte-heartbeat', which is what they
    are -- a general poll issued outside the meter sweep to keep the
    queue short. A new label would be more descriptive and would also be
    SILENTLY DROPPED by rte_reconcile.py's RTE_SOURCES whitelist, which
    has already happened once on this project ("'rte-heartbeat' was
    missing here for exactly one run"). How much drain mode collected is
    reported in the cycle line and counted in state.drain_cycles, which
    needs no new label.

    Returns the number of events collected, for that line.
    """
    if seconds <= 0:
        return 0
    if not state.draining:
        sleep_fn(seconds)
        return 0

    deadline = monotonic_fn() + seconds
    drained = 0
    while monotonic_fn() < deadline:
        try:
            if rte:
                event = client.general_poll_rte()
                code = None if event is None else int(event.code)
                fields = (event.fields or None) if event is not None else None
            else:
                code, fields = client.general_poll(), None
        except SASError:
            # A dead link is the cycle's own problem to diagnose and
            # report; this is best-effort draining, exactly like the
            # heartbeat. Fall back to waiting out the rest.
            remaining = deadline - monotonic_fn()
            if remaining > 0:
                sleep_fn(remaining)
            break
        if code is None or code == ExceptionCode.NONE:
            # Queue empty. Stop polling a quiet machine at full rate --
            # that is pacing budget spent for nothing, and §2.3.3's floor
            # is a minimum interval, not a target.
            remaining = deadline - monotonic_fn()
            if remaining > 0:
                sleep_fn(remaining)
            break
        _dispatch_exception(client, conn, now_fn(), code, state=state,
                            source="rte-heartbeat" if rte else "heartbeat",
                            fields=fields)
        drained += 1
    return drained


def _record_lifecycle(conn, event: str, reason: str | None) -> None:
    """Write one row saying this run started or stopped, and why.

    Deliberately never raises: a failure to record WHY a run ended must not
    become the reason it ends. A bare except is right here and nowhere
    else in this file.
    """
    try:
        conn.execute(
            "INSERT INTO poller_lifecycle (at, event, reason, pid, argv) "
            "VALUES (?, ?, ?, ?, ?)",
            (utc_now(), event, reason, os.getpid(), " ".join(sys.argv[1:])))
        conn.commit()
    except Exception as e:  # noqa: BLE001 - see docstring
        print(f"WARNING: could not record lifecycle '{event}': "
              f"{type(e).__name__}: {e}", file=sys.stderr)


def _safe_commit(conn: sqlite3.Connection, now: str) -> bool:
    """Commit, or print a loud, impossible-to-miss fault to stderr and
    return False rather than raising or failing silently. A commit
    failure here is almost always SQLITE_FULL — the partition holding
    the database file is out of space, most likely during an extended
    network outage with nothing draining this data. Per this project's
    own Technical v3 §5.9: "If the event-write path does not handle
    this explicitly, the gateway silently stops buffering and the
    original no-buffer defect is back. This must fail loudly." This
    never deletes anything to make room — see the module docstring.
    """
    try:
        conn.commit()
        return True
    except sqlite3.OperationalError as e:
        print(
            f"[{now}] FAULT: database write failed ({e}) — most likely the partition "
            "holding the database file is full. This row was not saved. This tool will "
            "never delete existing rows to make room; free space on the partition or "
            "move the database file.",
            file=sys.stderr,
        )
        return False


def _db_file_size_bytes(conn: sqlite3.Connection) -> int | None:
    """Best-effort size, in bytes, of the main database file backing
    ``conn`` — or None if it can't be determined (an in-memory database,
    used throughout this project's own test suite, has no file to size).
    """
    try:
        for _seq, name, file in conn.execute("PRAGMA database_list"):
            if name == "main" and file:
                return os.path.getsize(file)
    except (sqlite3.Error, OSError):
        pass
    return None


def _is_empty_ticket_out_record(record: EnhancedValidationInfo) -> bool:
    """Per §15.10: "If no unread records are in the buffer, all fields in
    the long poll 4D response will be zero" — same for an unused/invalid
    buffer index. Checked on three fields together rather than just
    validation_number, since a genuinely-zero value in any single field
    is far more plausible than all three being zero at once.
    """
    return record.validation_number == 0 and record.amount_cents == 0 and record.ticket_number == 0


def _insert_ticket_out_record(conn: sqlite3.Connection, record: EnhancedValidationInfo, captured_at: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO ticket_out_history "
        "(captured_at, buffer_index, validation_type, ticket_date, ticket_time, validation_number, "
        " amount_cents, ticket_number, validation_system_id, expiration, pool_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            captured_at,
            record.index_number,
            record.validation_type,
            record.date,
            record.time,
            record.validation_number,
            record.amount_cents,
            record.ticket_number,
            record.validation_system_id,
            record.expiration,
            record.pool_id,
        ),
    )
    _safe_commit(conn, captured_at)


def backfill_ticket_out_history(client, conn: sqlite3.Connection, *, now_fn=utc_now) -> int:
    """Read every buffer position (1-31, the spec's maximum — §15.6) once,
    non-destructively (indexed reads don't mark anything as read), so
    ticket_out_history starts with whatever the machine is already
    holding rather than only what happens from here forward. Safe to call
    more than once — inserts are deduplicated. Returns the number of new
    rows found.
    """
    now = now_fn()
    found = 0
    consecutive_timeouts = 0
    for index in range(1, 32):
        try:
            record = client.send_enhanced_validation_information(function_code=index)
        except SASTimeoutError as e:
            # A machine with no printer has no ticket-out buffer to read, so
            # it ignores every index (section 4.4). On the first EGT Premier
            # tested that was 31 timeouts at every startup -- about 31
            # seconds of it, and 31 poll_errors rows that then dominated the
            # soak report's failure count and hid a real cluster elsewhere.
            #
            # If index 1 and 2 are silent, 3 through 31 will be too: this is
            # a feature the machine does not have, not a buffer that happens
            # to start empty. Record it once and stop.
            consecutive_timeouts += 1
            if consecutive_timeouts == 1:
                conn.execute(
                    "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) "
                    "VALUES (?, ?, ?, ?)",
                    (now, "send_enhanced_validation_information(backfill)", type(e).__name__,
                     f"{e} -- no response at buffer index {index}"),
                )
                _safe_commit(conn, now)
            if consecutive_timeouts >= 2:
                print(f"[{now}] ticket-out backfill: no response at indices 1-{index}; this "
                      f"machine has no readable ticket-out buffer (no printer configured). "
                      f"Skipping the remaining {31 - index} indices.")
                break
            continue
        except SASError as e:
            # Not a timeout: a checksum or framing fault is a real problem
            # whatever the machine supports, and worth a row of its own.
            consecutive_timeouts = 0
            conn.execute(
                "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
                (now, "send_enhanced_validation_information(backfill)", type(e).__name__, str(e)),
            )
            _safe_commit(conn, now)
            continue
        consecutive_timeouts = 0
        if _is_empty_ticket_out_record(record):
            continue
        before = conn.total_changes
        _insert_ticket_out_record(conn, record, now)
        if conn.total_changes > before:
            found += 1
    return found


def _drain_ticket_out_history(client, conn: sqlite3.Connection, now: str) -> None:
    """Called on exception 0x3D/0x3E: read every currently-unread
    ticket-out record (function code 0x00 — "next unread, mark as read")
    until the buffer reports empty. Unlike the startup backfill, this
    drains the FIFO "unread" pointer rather than reading by absolute
    index, which is what lets it see every new record even if several
    tickets printed between poll cycles.
    """
    for _ in range(31):  # hard cap — the buffer can never hold more than this
        try:
            record = client.send_enhanced_validation_information(function_code=0x00)
        except SASError as e:
            conn.execute(
                "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
                (now, "send_enhanced_validation_information(drain)", type(e).__name__, str(e)),
            )
            _safe_commit(conn, now)
            return
        if _is_empty_ticket_out_record(record):
            return
        _insert_ticket_out_record(conn, record, now)
        print(f"[{now}] ticket-out captured: validation_number={record.validation_number} amount_cents={record.amount_cents}")


def seed_validation_pool(
    conn: sqlite3.Connection,
    target_available: int,
    *,
    validation_system_id: int = DEFAULT_VALIDATION_SYSTEM_ID,
    random_fn=random.getrandbits,
    now_fn=utc_now,
) -> int:
    """Top up validation_pool to at least ``target_available`` 'available'
    rows, generating random 16-digit numbers (not real server-issued
    ones — see the module docstring). Safe to call every run: it only
    adds what's missing, and a random collision with an existing number
    just retries. Returns how many rows were actually added.

    Each new row's ``issued_at`` is set to ``now_fn()`` — this lab
    stand-in's equivalent of a real server recording when it minted a
    batch, which _pool_age_hours() reads to back the pool-age check
    (Decisions Annex D-16).
    """
    if target_available <= 0:
        return 0
    now = now_fn()
    existing = conn.execute("SELECT COUNT(*) FROM validation_pool WHERE status = 'available'").fetchone()[0]
    added = 0
    while existing + added < target_available:
        candidate = random_fn(53) % 10**16
        try:
            conn.execute(
                "INSERT INTO validation_pool (validation_number, validation_system_id, status, issued_at) "
                "VALUES (?, ?, 'available', ?)",
                (candidate, validation_system_id, now),
            )
        except sqlite3.IntegrityError:
            continue  # collided with an existing validation_number (primary key) — try another
        added += 1
    _safe_commit(conn, now)
    return added


# How long an 'assigned' validation number may still be considered in flight.
# Section 15.12's own clocks are in seconds -- ten from insertion to long poll
# 70, thirty to long poll 71 -- and a cashout that has not produced a ticket
# in two minutes is not going to.
IN_FLIGHT_WINDOW_S = 120


def _iso_seconds_ago(now: str, seconds: int) -> str:
    """``now`` minus ``seconds``, as the same ISO text the rows are stored
    with, so the comparison is a plain string comparison on a sortable format.
    A malformed timestamp returns something that matches nothing rather than
    raising: a bad clock must not take the poll loop down.
    """
    try:
        parsed = datetime.datetime.fromisoformat(now)
    except ValueError:
        return "9999-12-31T23:59:59+00:00"
    return (parsed - datetime.timedelta(seconds=seconds)).isoformat()


def _handle_cashout_request(client, conn: sqlite3.Connection, now: str) -> None:
    """Called on exception 0x57 (system validation request): read the
    pending cashout amount (LP 57), take the next available number from
    validation_pool, and answer with it (LP 58) — the gateway-local
    cashout-validation flow described in the module docstring (§5.4).
    """
    try:
        info = client.send_pending_cashout_info()
    except SASError as e:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "send_pending_cashout_info", type(e).__name__, str(e)),
        )
        _safe_commit(conn, now)
        return
    if info.cashout_type == 0x80:
        return  # "not waiting for system validation" (Table 15.7b) — the exception fired, but the race is over

    # A machine reissues exception 57 until the host answers in a way it
    # accepts, so one cashout produces several. Answering each with a FRESH
    # number burns the pool and, worse, marks numbers 'assigned' that never
    # reach paper: one real ticket on the bench consumed six numbers, five of
    # them phantom issues that a CMS reconciling issued-against-printed would
    # have to explain. Section 15.8 expects the same answer within the same
    # cashout -- "a long poll 58 that specifies a valid validation number must
    # be preceded by a valid long poll 57 within the same cashout" -- so the
    # correct behaviour is idempotent, not sequential.
    #
    # An 'assigned' number that has not yet appeared in ticket_out_history is
    # one still in flight. During the reissue burst that is exactly the state,
    # because the ticket-out record is only drained after the ticket prints.
    # Bounded in time, because "assigned but not yet printed" is only a
    # reliable in-flight signal for the seconds a cashout actually takes. Any
    # number that never reached paper -- a cashout the player abandoned, a
    # ticket the machine refused, or the five phantoms the pre-fix handler
    # burned on a single cashout -- stays 'assigned' and absent from
    # ticket_out_history for good. Without a window, the next cashout for a
    # matching amount would be answered with one of those stale numbers, which
    # is the old defect wearing the fix's clothes.
    in_flight = conn.execute(
        "SELECT validation_number, validation_system_id, assigned_amount_cents "
        "FROM validation_pool WHERE status = 'assigned' AND validation_number "
        "NOT IN (SELECT validation_number FROM ticket_out_history) "
        "AND assigned_at >= ? "
        "ORDER BY assigned_at DESC LIMIT 1",
        (_iso_seconds_ago(now, IN_FLIGHT_WINDOW_S),)
    ).fetchone()
    if in_flight is not None and in_flight[2] == info.amount_cents:
        try:
            status = client.send_validation_number(
                validation_system_id=in_flight[1], validation_number=in_flight[0])
        except SASError as e:
            conn.execute(
                "INSERT INTO poll_errors (occurred_at, poll_name, error_type, "
                "message) VALUES (?, ?, ?, ?)",
                (now, "send_validation_number", type(e).__name__, str(e)))
            _safe_commit(conn, now)
            return
        print(f"[{now}] cashout re-answered with the SAME number "
              f"{in_flight[0]} (reissued 0x57 for the same "
              f"{info.amount_cents} cashout; no new number consumed)")
        return

    # OLDEST ISSUED FIRST, by AGE, not by number.
    #
    # Operator, 2026-11-10: "ORDER BY validation_number isn't a correct
    # approach, because we always look at the age of the validation number.
    # So for the lab it's not a problem, but for the floor it definitely
    # needs to be ordered by the age of the validation numbers and oldest
    # must be issued."
    #
    # Spec V2 §7.1 says only "the next available number" and does not fix the
    # order, so this is the decision that settles it. Number order and issue
    # order coincide only while every batch happens to be minted ascending;
    # a second validation system id, a gap-fill or a re-mint breaks that, and
    # the failure is silent -- a newer number handed out while an older one
    # ages in the pool, which is the opposite of what the ticket's own
    # validity window wants.
    #
    # NULLS LAST, deliberately. A NULL issued_at is a hand-inserted number
    # with no batch info (see the pool-age check) -- its age is UNKNOWN, and
    # unknown is not evidence of being oldest. SQLite's plain ORDER BY sorts
    # NULLs FIRST, which would hand out the one number whose age nobody can
    # vouch for, first.
    #
    # validation_number is the final tiebreak, which keeps the lab
    # deterministic: --seed-validation-pool stamps every row with the same
    # issued_at, so seeded pools behave exactly as they did before this
    # change. That is why the old ordering was never wrong in the lab and
    # would have been wrong on the floor.
    row = conn.execute(
        "SELECT validation_number, validation_system_id FROM validation_pool "
        "WHERE status = 'available' "
        "ORDER BY issued_at IS NULL, issued_at, validation_number LIMIT 1"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "validation_pool", "PoolExhausted", f"no available validation number for amount_cents={info.amount_cents}"),
        )
        _safe_commit(conn, now)
        print(f"[{now}] cashout pending (amount_cents={info.amount_cents}) but validation_pool is empty — left unanswered")
        return

    validation_number, validation_system_id = row
    try:
        status = client.send_validation_number(
            validation_system_id=validation_system_id, validation_number=validation_number
        )
    except SASError as e:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "send_validation_number", type(e).__name__, str(e)),
        )
        _safe_commit(conn, now)
        return

    if status == 0x00:
        conn.execute(
            "UPDATE validation_pool SET status = 'assigned', assigned_at = ?, assigned_amount_cents = ? "
            "WHERE validation_number = ?",
            (now, info.amount_cents, validation_number),
        )
        _safe_commit(conn, now)
        print(f"[{now}] cashout answered: validation_number={validation_number} amount_cents={info.amount_cents}")
    else:
        # 0x80 not in cashout, 0x81 improper validation rejected (Table 15.8b) — the number was never
        # actually consumed, so leave it 'available' for the next attempt rather than burning it.
        print(f"[{now}] validation number rejected by machine (status=0x{status:02X}); left available for reuse")


def _pool_age_hours(conn: sqlite3.Connection, now: str) -> float | None:
    """Hours since the oldest still-'available' row entered the pool, or
    None if the pool currently has no available rows at all (that's
    PoolExhausted's condition, a different one — see _handle_cashout_request()).
    A NULL issued_at (a hand-inserted real number with no batch info
    recorded) is treated as unknown age, not zero or infinite — excluded
    from the MIN() rather than guessed at.
    """
    row = conn.execute(
        "SELECT MIN(issued_at) FROM validation_pool WHERE status = 'available' AND issued_at IS NOT NULL"
    ).fetchone()
    oldest = row[0] if row else None
    if oldest is None:
        return None
    return (datetime.datetime.fromisoformat(now) - datetime.datetime.fromisoformat(oldest)).total_seconds() / 3600.0


# A stale pool is a standing condition. Record it when it starts, once an
# hour while it lasts (so it has a duration), and when it clears -- not once
# per cycle, which produced 1302 rows in one hour on the lab database.
POOL_STALE_ROW_INTERVAL_S = 3600.0
POOL_STALE_PRINT_INTERVAL_S = 900.0


def _check_pool_age(conn: sqlite3.Connection, now: str, *, max_age_hours: float,
                    state: "PollState | None" = None,
                    monotonic_fn=time.monotonic) -> None:
    """Per the Decisions Annex D-16: a pool older than ~36 hours is
    "genuinely abnormal... either the machine has dispensed nothing in a
    day and a half, or the gateway has been unable to reach the server
    to take a top-up" — raised as an active, repeated alert (the same
    treatment as the existing --db-size-warning-mb check below), never
    used to block dispensing. Age is not an integrity signal (an old
    validation number is exactly as valid as a new one — see D-15/D-16),
    so this is a health signal for a technician, not a validity check;
    nothing here ever refuses or withholds a number on account of age.
    Real order-gap detection (D-16's "sharper signal") needs the actual
    server-issued sequential/batch numbering this reference tool
    deliberately doesn't implement (see the module docstring's note on
    pool_epoch/batch_uuid) — out of scope here for the same reason.
    """
    if max_age_hours <= 0:
        return
    age = _pool_age_hours(conn, now)
    stale = age is not None and age >= max_age_hours

    if not stale:
        # Clearing is worth exactly one row: it is the record that a top-up or
        # a pool replacement actually reached this gateway, which is the thing
        # the alert exists to ask for.
        if state is not None and state.pool_stale_since is not None:
            conn.execute(
                "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) "
                "VALUES (?, ?, ?, ?)",
                (now, "validation_pool", "PoolStaleCleared",
                 f"stale since {state.pool_stale_since}; the oldest available "
                 f"number is now {age if age is None else round(age, 1)}h old"),
            )
            _safe_commit(conn, now)
            print(f"[{now}] validation_pool is no longer stale (was flagged "
                  f"from {state.pool_stale_since}).")
            state.pool_stale_since = None
            state.pool_stale_last_row = None
            state.pool_stale_last_print = None
        return

    # ONE CONDITION, NOT ONE ROW PER CYCLE.
    #
    # This used to INSERT a PoolStale row and commit on EVERY cycle the pool
    # was stale. Measured on the lab database: 1302 rows in one hour at the
    # early 2.8s cadence, then ~271/hour for four days -- tens of thousands of
    # rows for a condition that never changed. poll_errors is the table a
    # human reads to find poll FAILURES, and it was swamped: the real signal,
    # ~90 core-meter timeouts an hour, sat underneath a flood of advisory
    # rows that all said the same thing.
    #
    # It was also a write plus a commit every cycle, on a gateway whose whole
    # --mode ring design exists to spare the flash exactly that.
    #
    # So: a row when the condition STARTS, one an hour while it lasts (which
    # is what gives it a duration), and one when it clears. The console line
    # repeats every fifteen minutes rather than every cycle -- often enough
    # that somebody tailing the log cannot miss it, rarely enough that the
    # log stays readable.
    message = f"oldest available number is {age:.1f}h old (>= {max_age_hours}h)"
    if state is None:
        # No state to track an episode with: keep the old behaviour rather
        # than silently dropping the alert.
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "validation_pool", "PoolStale", message))
        _safe_commit(conn, now)
        print(f"[{now}] ALERT: validation_pool's oldest available number is "
              f"{age:.1f}h old, at or above --pool-age-alert-hours "
              f"{max_age_hours} — likely no top-up reaching this gateway, or "
              "the machine isn't dispensing. Not a validity problem and not "
              "blocking dispensing (see D-16).")
        return

    mono = monotonic_fn()
    first = state.pool_stale_since is None
    if first:
        state.pool_stale_since = now
    if first or state.pool_stale_last_row is None or (
            mono - state.pool_stale_last_row) >= POOL_STALE_ROW_INTERVAL_S:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) "
            "VALUES (?, ?, ?, ?)",
            (now, "validation_pool", "PoolStale",
             message if first else f"{message}; still stale since {state.pool_stale_since}"))
        _safe_commit(conn, now)
        state.pool_stale_last_row = mono
    if first or state.pool_stale_last_print is None or (
            mono - state.pool_stale_last_print) >= POOL_STALE_PRINT_INTERVAL_S:
        print(
            f"[{now}] ALERT: validation_pool's oldest available number is {age:.1f}h old, at or above "
            f"--pool-age-alert-hours {max_age_hours} — likely no top-up reaching this gateway, or the "
            "machine isn't dispensing. Not a validity problem and not blocking dispensing (see D-16)."
        )
        state.pool_stale_last_print = mono


# How many times to re-read LP 0x70 after exception 0x67 before concluding
# there is genuinely nothing in escrow. Each read is paced by the transport
# (--min-poll-interval), so 4 spans about a second at the 300ms default --
# nothing against §15.12's thirty-second window, and enough for a validator
# that has raised the exception on insertion but not yet finished moving the
# ticket into the escrow position.
TICKET_ESCROW_READS = 4


def _capture_ticket_in(client, conn: sqlite3.Connection, now: str,
                       state: "PollState | None" = None,
                       escrow_reads: int = TICKET_ESCROW_READS) -> None:
    """Called on exception 0x67: read the ticket's validation data, and
    answer it ONLY if a lab ticket file was handed to this run.

    Without --lab-ticket-authority this stays exactly as read-only as it
    has always been: the ticket is recorded and never redeemed, the machine
    returns it after its own thirty-second timeout (§15.12), and nothing
    here decides what a ticket is worth. That refusal is deliberate and is
    the right default for a gateway — a poller that pays tickets by itself
    is a cash-dispensing defect.

    What lifts it is a FILE, not a flag: the operator has to hand this run
    a specific list of validation numbers bound to a specific cabinet
    serial. That is the same boundary ticket_lab_authority.py draws, and
    the lookup, the transfer codes and the expiry date are imported from it
    rather than copied, so the two cannot drift apart.

    The reason to have it here at all is the serial port: one port, one
    master. ticket_lab_authority.py cannot run while a soak holds
    /dev/ttyS0, so ticket-in was the one path a soak could never exercise
    — which is why §3 of every rte_reconcile run so far has printed
    [NO DATA].
    """
    # ONE READ USED TO BE ENOUGH, AND IT WAS NOT.
    #
    # This read a single LP 0x70, and if the machine said nothing was in
    # escrow it returned -- no row, no log line, no error. The comment called
    # that "a rare, harmless race". It is neither.
    #
    # The cabinet raises 0x67 when the validator takes the ticket, which is
    # BEFORE the ticket reaches the escrow position. A poller that answers
    # within the same cycle can therefore read 0x70 too early, see no escrow,
    # and silently decline to answer at all -- so the machine waits out
    # §15.12's thirty seconds and hands the ticket back. From the player's
    # side: inserted, rejected, for no visible reason.
    #
    # Observed twice on 2026-11-01 and reproducible: two 0x67 exceptions at
    # 22:06 produced NO ticket_in_events rows at all, and the ticket was only
    # accepted on a third attempt a minute later (id 9, 22:07:30). "Spin
    # once more and it goes in" is this, not the machine being fussy.
    #
    # So: re-read, and when the escrow really is empty, WRITE IT DOWN. A
    # silent return is what made this invisible for weeks -- the one failure
    # that left no trace anywhere, in a table whose whole purpose is that a
    # denied ticket moves no meter and is otherwise unrecoverable.
    ticket = None
    for attempt in range(1, max(1, escrow_reads) + 1):
        try:
            ticket = client.send_ticket_validation_data()
        except SASError as e:
            conn.execute(
                "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
                (now, "send_ticket_validation_data", type(e).__name__, str(e)),
            )
            _safe_commit(conn, now)
            return
        if ticket.ticket_in_escrow:
            if attempt > 1:
                print(f"[{now}] ticket reached escrow on read {attempt}/"
                      f"{escrow_reads} — exception 0x67 fires on insertion, "
                      "before the validator has finished moving it")
            break
    if ticket is None or not ticket.ticket_in_escrow:
        note = (f"NOT ANSWERED: exception 0x67 fired but LP 0x70 reported no "
                f"ticket in escrow across {escrow_reads} read(s). The machine "
                "will hold it for §15.12's thirty seconds and hand it back.")
        conn.execute(
            "INSERT INTO ticket_in_events (captured_at, amount_cents, parsing_code, "
            "validation_data_hex, authority_decision) VALUES (?, ?, ?, ?, ?)",
            (now, getattr(ticket, "amount_cents", None),
             getattr(ticket, "parsing_code", None),
             ticket.validation_data.hex() if ticket is not None else None, note),
        )
        _safe_commit(conn, now)
        print(f"[{now}] {note}")
        return
    decision = None
    if state is not None and state.ticket_authority is not None:
        decision = _answer_ticket_in(client, conn, ticket, state, now)

    conn.execute(
        "INSERT INTO ticket_in_events (captured_at, amount_cents, parsing_code, "
        "validation_data_hex, authority_decision) VALUES (?, ?, ?, ?, ?)",
        (now, ticket.amount_cents, ticket.parsing_code,
         ticket.validation_data.hex(), decision),
    )
    _safe_commit(conn, now)
    if decision is None:
        print(f"[{now}] ticket-in captured: amount_cents={ticket.amount_cents} "
              "(not redeemed — see module docstring)")
    else:
        print(f"[{now}] ticket-in captured: amount_cents={ticket.amount_cents} "
              f"-> {decision}")


def _pool_issued_ticket(conn: sqlite3.Connection, number: int):
    """Did THIS gateway print the ticket now being inserted?

    The two halves of ticket handling use different books. Ticket-OUT draws
    a number from validation_pool and marks it 'assigned' with the amount
    once the machine acknowledges the print; ticket-IN looks numbers up in
    lab_tickets.json. Nothing joined them, so a ticket the cabinet printed
    thirty seconds earlier came back as DENIED 0x82 "not in system" -- the
    gateway refusing to recognise its own issuance.

    Honouring one is a weaker claim than the lab file makes, not a stronger
    one: the host is not deciding what an unknown ticket is worth, it is
    reading back an amount it recorded when it authorised the print. That
    is what a real CMS does. It stays behind the same lab gate only because
    it still moves money.

    Returns the (status, amount_cents) row, or None if this gateway never
    issued that number.
    """
    return conn.execute(
        "SELECT status, assigned_amount_cents FROM validation_pool "
        "WHERE validation_number = ?", (number,)).fetchone()


def _answer_ticket_in(client, conn: sqlite3.Connection, ticket,
                      state: "PollState", now: str) -> str:
    """LAB ONLY. Look the ticket up in the file this run was given and
    answer LP 0x71 with it.

    Returns what happened, as one line, for ticket_in_events. A denied
    ticket moves no meter at all, so if this is not written down the
    decision is unrecoverable afterwards.

    The answer is the only thing here that changes machine state, and the
    file is what authorises it. A number that is not in the file is denied
    with 0x82, "not in system" — never silently ignored, which would leave
    the machine holding the ticket for its full thirty seconds and tell the
    player nothing.
    """
    # From the LIBRARY, not from examples/ticket_lab_authority.py.
    #
    # These are Table 15.12 and 15.12c -- spec tables, not lab logic. Taking
    # them from the lab module made the product depend at runtime on the
    # scaffolding that stands in for a server, so the public mirror could not
    # drop the scaffolding without breaking the gateway. Validation numbers
    # are always issued by the server; nothing here may need that module.
    from saspy.constants import (  # noqa: E402
        TICKET_NOT_IN_SYSTEM as NOT_IN_SYSTEM,
        TICKET_TRANSFER_CODES as TRANSFER_CODES,
        describe_redemption_status as describe,
    )

    # Table 15.11b: parsing code 00 means the 9 bytes are BCD, an 18-digit
    # decimal number (2-digit system ID + 16). Any other shape is not
    # something this file knows how to look up, and guessing at it would
    # mean answering a ticket we have not actually identified.
    if ticket.parsing_code != 0x00 or len(ticket.validation_data) != 9:
        return (f"NOT ANSWERED: parsing code 0x{ticket.parsing_code:02X} with "
                f"{len(ticket.validation_data)} bytes is not Table 15.11b's 00 "
                "with 9 BCD bytes")

    number = decode_bcd(ticket.validation_data)
    digits = f"{number:018d}"
    spec = state.ticket_authority.get(digits)

    # A ticket this gateway printed itself is not in the lab file and never
    # will be. Recognise it from validation_pool instead -- see
    # _pool_issued_ticket() for why that is the weaker claim, not the
    # stronger one.
    reserved_number = None
    deny_reason = "not in file"
    if spec is None:
        row = _pool_issued_ticket(conn, number)
        if row is not None and row[0] == "assigned" and row[1]:
            # RESERVE IT BEFORE SENDING LP 0x71, not after. If this process
            # dies between the poll and the update, 'redeeming' blocks a
            # second redemption of the same ticket -- which is the failure
            # that costs real money. It is released again only on a clean
            # refusal from the machine, where we know nothing moved.
            conn.execute(
                "UPDATE validation_pool SET status = 'redeeming' "
                "WHERE validation_number = ? AND status = 'assigned'", (number,))
            _safe_commit(conn, now)
            reserved_number = number
            spec = {"kind": "cashable", "cents": int(row[1]),
                    "expiration": 0, "pool_id": 0}
        elif row is not None and row[0] in ("redeemed", "redeeming"):
            deny_reason = (f"this gateway issued ...{digits[-6:]} but it is "
                           f"already '{row[0]}' -- a ticket is redeemable once")
        elif row is not None:
            # 'available' or 'retired': the number exists in the pool but was
            # never put on a ticket, so there is nothing to pay.
            deny_reason = (f"...{digits[-6:]} is '{row[0]}' in the pool, never "
                           "issued on a ticket")

    if spec is None:
        # NEVER an early return. Silence leaves the machine holding the ticket
        # for §15.12's full thirty seconds and tells the player nothing; that
        # was the single most visible defect this file ever had.
        code, amount = NOT_IN_SYSTEM, 0
        note = f"DENIED 0x{NOT_IN_SYSTEM:02X} {deny_reason}"
    else:
        code = TRANSFER_CODES[spec["kind"]]
        amount = spec["cents"]
        note = f"honoured as {spec['kind']} for {amount} cents"
        if ticket.amount_cents and ticket.amount_cents != amount:
            note += f" (machine reported {ticket.amount_cents})"

    try:
        result = client.redeem_ticket(
            transfer_code=code,
            transfer_amount_cents=amount,
            validation_number=number,
            restricted_expiration=(spec["expiration"]
                                   if spec and spec["kind"] == "restricted" else 0),
            pool_id=(spec["pool_id"]
                     if spec and spec["kind"] == "restricted" else 0),
            parsing_code=ticket.parsing_code,
        )
    except SASError as e:
        # Deliberately leaves a reserved number in 'redeeming'. The exchange
        # may have failed after the machine took the ticket, and those two
        # look identical from here; blocking a second redemption is the safe
        # direction, and the row is visible for a human afterwards.
        return f"{note}; LP 0x71 failed: {type(e).__name__}: {e}"
    if reserved_number is not None:
        # The outcome is the CATEGORY (status >> 5), not a single code: 0x01
        # is "restricted promotional ticket redeemed" and is a success. Three
        # outcomes, not two --
        #   SUCCESS (0b000)            -> redeemed, the money moved
        #   FAILED/incompatible/none   -> back to assigned; the player is
        #                                 still holding a spendable ticket
        #   waiting/pending (0b001/010) -> LEAVE IT RESERVED. The machine has
        #                                 not said yet, and releasing a ticket
        #                                 that then completes pays it twice.
        category = result.machine_status >> 5
        settled = {0b000: "redeemed"}.get(category)
        if settled is None and category not in (0b001, 0b010):
            settled = "assigned"
        if settled is not None:
            conn.execute(
                "UPDATE validation_pool SET status = ? WHERE validation_number = ?",
                (settled, reserved_number))
            _safe_commit(conn, now)
        note += f" (issued by this gateway; now {settled or 'redeeming'})"
    return f"{note}; machine status 0x{result.machine_status:02X} {describe(result.machine_status)}"


def _acknowledge_aft_transfer(client, conn: sqlite3.Connection, now: str) -> None:
    """Called on exception 0x69: read the completion status and ACKNOWLEDGE it.

    THE ACKNOWLEDGEMENT IS NOT OPTIONAL. §8.8: a transfer answered with
    pending status 0x40 completes with exception 0x69, and "if the host does
    not respond with a proper interrogation poll, the gaming machine must
    reissue exception 69 every 15 seconds until the host polls for and
    acknowledges the completion status". Measured on an EGT Premier: 15.2
    seconds, indefinitely.

    0x69 is a PRIORITY exception, so it bypasses §2.2.1's queue and arrives
    ahead of everything waiting. An unacknowledged transfer therefore does not
    merely leave a loose end -- it installs a permanent 15-second beat at the
    FRONT of the exception stream and pushes real events behind it.

    Until 2026-11-03 this poller did not handle 0x69 at all.
    `grep 0x69 examples/sql_poll_logger.py` returned nothing, while
    aft_push_credits.py had done it correctly all along. That mattered because
    the poller is the process that will perform transfers once the bill-
    validator bridge exists (docs/project/AFT-BRIDGE-DESIGN.md).

    transfer code FF is the acknowledging form; FE reads the same status
    WITHOUT acknowledging and does not stop the reissue, so it is not what
    this wants.
    """
    try:
        result = client.aft_interrogate_transfer(0x00, acknowledge=True)
    except SASError as e:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) "
            "VALUES (?, ?, ?, ?)",
            (now, "aft_interrogate_transfer", type(e).__name__, str(e)))
        _safe_commit(conn, now)
        print(f"[{now}] exception 0x69 (AFT transfer complete) could NOT be "
              f"acknowledged: {type(e).__name__}: {e}. §8.8 means the machine "
              "will reissue it every 15 seconds, as a priority exception, "
              "until it is.")
        return

    note = (f"transfer status 0x{int(result.transfer_status):02X}, "
            f"receipt status 0x{int(result.receipt_status):02X}")
    conn.execute(
        "INSERT INTO aft_transfer_completions (captured_at, buffer_position, "
        "transfer_status, receipt_status, transfer_type, cashable_amount, "
        "restricted_amount, nonrestricted_amount, transaction_id, "
        "transaction_date, transaction_time, asset_number, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now, result.transaction_buffer_position, result.transfer_status,
         result.receipt_status, result.transfer_type, result.cashable_amount,
         result.restricted_amount, result.nonrestricted_amount,
         result.transaction_id, result.transaction_date,
         result.transaction_time, result.asset_number, note))
    _safe_commit(conn, now)
    print(f"[{now}] AFT transfer complete (0x69) acknowledged: {note}; "
          f"cashable={result.cashable_amount} restricted={result.restricted_amount} "
          f"nonrestricted={result.nonrestricted_amount} "
          f"id={result.transaction_id!r}")


def _capture_ticket_in_completion(client, conn: sqlite3.Connection, now: str) -> None:
    """Called on exception 0x68: the machine's redemption cycle finished
    — stacked or rejected, not which one (Appendix A of the project's own
    Technical v3: "Announces that the cycle finished, not that it
    succeeded"). Reads the outcome via redeem_ticket_status() — the safe,
    read-only LP 71/FF status query (transfer code FF, §15.12b), never
    redeem_ticket() itself, so this stays inside the same read-only
    boundary as _capture_ticket_in(): this tool observes what happened to
    a ticket, it never decides what should happen to one.

    machine_status 0xFF means no completed cycle since the machine was
    last polled — the exception fired, but the race is already over by
    the time this read landed. Logged as-is rather than filtered out:
    that itself is informative (a fast, easily-missed cycle), not an
    error.
    """
    try:
        result = client.redeem_ticket_status()
    except SASError as e:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "redeem_ticket_status", type(e).__name__, str(e)),
        )
        _safe_commit(conn, now)
        return
    conn.execute(
        "INSERT INTO ticket_in_completions (captured_at, machine_status, amount_cents, parsing_code, validation_data_hex) "
        "VALUES (?, ?, ?, ?, ?)",
        (now, result.machine_status, result.amount_cents, result.parsing_code, result.validation_data.hex()),
    )
    _safe_commit(conn, now)
    print(f"[{now}] ticket-in completion captured: machine_status=0x{result.machine_status:02X} amount_cents={result.amount_cents}")


def _capture_handpay(client, conn: sqlite3.Connection, now: str,
                     state: "PollState | None" = None,
                     monotonic_fn=time.monotonic) -> None:
    """Called on exception 0x51. Issues the long poll 0x1B that §7.8.1
    REQUIRES, and without which the machine never stops shouting.

    Section 7.8.1 is explicit, and it is about a queue rather than a lock:
    "When a handpay occurs, the gaming machine stores all pertinent data
    required for the 1B long poll in the handpay queue and sends exception
    51 ... When long poll 1B is received, the oldest unreported entry in the
    queue is sent to the host. When the gaming machine's response is
    acknowledged, this entry is marked as reported or removed from the
    queue."

    And the consequence of not reading it: "If the 1B long poll is not
    received from the host within fifteen seconds after exception 51 has
    been sent and acknowledged, the gaming machine will re-issue exception
    51 every fifteen seconds as long as the entry is in the queue."

    This poller used to log 0x51 and read nothing, which produced exactly
    that on an EGT Premier: 0x51 every fifteen seconds for minutes on end,
    on a machine that was NOT locked and was being played normally
    throughout. The exception was reporting an unreported QUEUE ENTRY, not a
    live handpay condition -- a distinction the exception's own text
    ("handpay is pending") actively hides.

    The stakes are higher than noise. The queue holds a minimum of five
    entries and §7.8.1 says "if the handpay queue is already full, the
    oldest handpay record will be lost". A host that never reads 1B
    therefore loses handpay records -- money events -- once the sixth one
    arrives. Reading is not optional politeness; it is how the queue drains.

    All zeros is a normal answer, not a failure: §7.8.1 says "long poll 1B
    returns all zeros if a handpay record has been reported and
    acknowledged, and a subsequent exception 51 has not been issued and
    acknowledged". So a second read of the same entry reads zero, and that
    is recorded as such rather than as a handpay of 0.00.

    This reads and records. It does NOT reset the handpay -- that is long
    poll 0x94, it moves money, and it belongs to a human running
    examples/handpay_clear.py, not to a logger.

    The one exception is an explicitly licensed lab run: with
    --lab-auto-handpay-reset the clock started here is what
    _lab_auto_handpay_reset() later measures against. Starting it is free
    and unconditional -- the decision to act on it is not made in this
    function, and is refused outright unless a bound lab ticket file is
    also in effect.
    """
    try:
        info = client.send_handpay_information()
    except SASError as e:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "send_handpay_information", type(e).__name__, str(e)),
        )
        _safe_commit(conn, now)
        return
    all_zeros = not (info.amount or info.partial_pay or info.level
                     or info.progressive_group)
    conn.execute(
        "INSERT INTO handpay_queue (captured_at, progressive_group, level, amount, "
        "partial_pay, reset_id, all_zeros) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now, info.progressive_group, info.level, info.amount, info.partial_pay,
         info.reset_id, int(all_zeros)),
    )
    _safe_commit(conn, now)
    if state is not None and not all_zeros:
        # An all-zeros read is a re-read of an already-reported entry, not a
        # new lockup, so it must not restart the clock -- otherwise a 0x51
        # reissue would keep pushing the deadline out forever and the
        # auto-reset would never fire.
        if state.handpay_pending_since is None:
            state.handpay_pending_since = monotonic_fn()
    if all_zeros:
        print(f"[{now}] handpay queue read (0x1B): all zeros -- the entry was "
              f"already reported and acknowledged (§7.8.1)")
    else:
        print(f"[{now}] handpay queue entry read (0x1B): level=0x{info.level:02X} "
              f"amount={info.amount} partial_pay={info.partial_pay} "
              f"reset_id=0x{info.reset_id:02X}; the queue entry is now reported, "
              f"so the 15-second 0x51 reissue should stop")
        # Only for CANCELLED CREDITS (0x80). A jackpot is the cabinet paying
        # a win and its composition is not in question; a cashout that fell
        # back is the player's own balance leaving as cash, and that is the
        # one an attendant has to judge with nothing to judge it on.
        if info.level == HANDPAY_LEVEL_CANCELLED_CREDITS:
            for line in handpay_composition(conn):
                print(line)


# A24: WHAT A HANDPAY IS MADE OF, AND WHAT CANNOT BE KNOWN
#
# Measured 2026-11-11. Two cashouts on the lab cabinet, 31 seconds apart:
# 7,167.17 cashable printed a Cashable Voucher, and 2,761.41 derived from a
# Promotional Voucher became a handpay -- 2.6x SMALLER, under the same
# 20,000 printer limit. Credit type, not amount.
#
# The operator's objection, which is the reason this function exists:
#
#   "when it locks up for handpay, attendant has no way to know if it's a
#    promo credit or cash credit. And handpays are paid in cash, so
#    indirectly we're letting the customer turn a promo ticket into cash
#    even without visiting cashdesk and without anyone's consent."
#
# He is right, and SAS cannot fix it. The cabinet keeps exactly two balance
# meters -- c7_current_credits and c7_current_restricted_credits -- so
# RESTRICTED is separable and NONRESTRICTED PROMOTIONAL is not. The machine
# is not withholding it; it stopped distinguishing the moment the credits
# landed, because by its own rules nonrestricted promotional IS cash.
#
# So this reports three things and is careful about which is which: the
# restricted portion, which is READ; the most recent promotional money seen
# arriving, which is a POINTER and an upper bound, not a measurement; and
# the limitation itself, because an attendant told "no promo detected" would
# be worse off than one told nothing.
#
# The real control is upstream and is recorded in A24: promotions issued as
# RESTRICTED print "Playable Only" and never reach this path at all.
def handpay_composition(conn: sqlite3.Connection) -> list[str]:
    """What the host can say about the money in a cancelled-credits handpay.

    Best effort from meters already stored. Returns display lines, empty if
    nothing is known -- silence is correct when there is nothing to add.
    """
    out: list[str] = []
    try:
        row = conn.execute(
            "SELECT c7_current_credits, c7_current_restricted_credits "
            "FROM meters_history "
            "WHERE c7_current_restricted_credits IS NOT NULL "
            "ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return out
    if row and row[1] is not None:
        total, restricted = row[0], row[1]
        if restricted:
            out.append(f"    RESTRICTED portion of the balance: {restricted} "
                       f"(read from c7_current_restricted_credits)")
        else:
            out.append("    restricted portion: 0 -- so none of this is "
                       "'Playable Only' money")
        if total is not None:
            out.append(f"    balance at that reading: {total}")
    try:
        promo = conn.execute(
            "SELECT polled_at, c7_nonrestricted_ticket_in_cents FROM meters_history "
            "WHERE c7_nonrestricted_ticket_in_cents IS NOT NULL "
            "ORDER BY id DESC LIMIT 2").fetchall()
    except sqlite3.Error:
        promo = []
    if len(promo) == 2 and None not in (promo[0][1], promo[1][1]):
        moved = promo[0][1] - promo[1][1]
        if moved > 0:
            out.append(f"    ** NONRESTRICTED PROMOTIONAL money arrived "
                       f"recently: {moved} cents, by {promo[0][0]}. It is "
                       f"indistinguishable from cash in the balance, so it "
                       f"MAY be part of this handpay. An upper bound, not a "
                       f"measurement.")
    if out:
        out.append("    Nonrestricted promotional credits cannot be separated "
                   "from cashable in any SAS balance meter (A24). Absence of "
                   "the line above is not evidence that none is present.")
    return out


# ---------------------------------------------------------------------------
# LAB ONLY: the poller acting on money by itself
# ---------------------------------------------------------------------------
#
# READ THIS BEFORE ENABLING EITHER OF THE TWO FLAGS BELOW.
#
# Everything else in this file is a gateway: it observes, records and
# reconciles, and the one thing it refuses to do is decide what money is
# worth. Two features break that deliberately, for one reason -- an
# unattended overnight soak on a lab cabinet stops dead the first time a
# jackpot locks it up or the credits run out, and a soak that stops at 1am
# measures nothing after 1am.
#
# They must never run on a floor. A poller that clears handpays by itself
# records a jackpot as paid when nobody paid it; a poller that pushes credits
# by itself is a money printer with a cron job. Neither failure is subtle and
# neither is recoverable from the meters, because the meters will agree with
# the gateway -- they will both be wrong in the same direction.
#
# THE GATE. Both require --lab-ticket-authority to be in effect, and that is
# not decoration. The lab ticket file is the project's existing statement of
# "this is the lab cabinet": it binds to one machine by serial (falling back
# to asset number, §4.4), it carries a hard 2026-11-30 expiry, and the run
# exits rather than act against any other cabinet. Reusing it means these two
# features inherit that binding and that expiry for free and cannot be
# switched on against a floor machine by a stray flag in a unit file.
# deploy/nimasas-poller.service carries none of the three.

# LP 0x1B (§7.8, Table 7.8) Level. MANUAL.md §7.4 and amendment A18:
# 0x80 is cancelled credits -- a cashout that fell back to a handpay --
# and 0x00 is a win the cabinet wants an attendant to pay. The two need
# different handling, because only the first has a composition anyone can
# argue about (A24).
HANDPAY_LEVEL_JACKPOT = 0x00
HANDPAY_LEVEL_CANCELLED_CREDITS = 0x80

LAB_AUTO_HANDPAY_MIN_SECONDS = 60.0
# Below a minute you are not clearing an unattended lockup, you are racing the
# attendant walking towards the machine.

LAB_AUTO_AFT_MAX_PUSHES = 40
LAB_AUTO_AFT_MAX_TOTAL_CENTS = 200_000
LAB_AUTO_AFT_MIN_INTERVAL_S = 120.0
# Three independent ceilings, because the failure mode that matters is a loop:
# a credit read that always comes back zero (a machine that stops answering LP
# 0x74 reads as broke, not as silent) would otherwise push every cycle until
# morning. Whichever ceiling is hit first stops the feature for the rest of
# the run -- it does not pause and retry, because a run that has pushed forty
# times has something wrong with it that another push will not fix.


@dataclass
class LabAutoConfig:
    """LAB ONLY. Off unless explicitly constructed from the lab flags."""

    handpay_reset_after_s: float = 0.0     # 0 = off
    aft_credit_cents: int = 0              # 0 = off
    aft_floor_cents: int = 0
    aft_pool_id: int = 0

    def any_enabled(self) -> bool:
        return bool(self.handpay_reset_after_s or self.aft_credit_cents)


def _record_lab_action(conn: sqlite3.Connection, now: str, action: str,
                       detail: str, outcome: str) -> None:
    conn.execute(
        "INSERT INTO lab_auto_actions (occurred_at, action, detail, outcome) "
        "VALUES (?, ?, ?, ?)", (now, action, detail, outcome))
    _safe_commit(conn, now)
    print(f"[{now}] LAB AUTO {action}: {detail} -> {outcome}")


def _lab_auto_handpay_reset(client, conn: sqlite3.Connection, now: str,
                            state: "PollState", *, after_seconds: float,
                            monotonic_fn=time.monotonic) -> bool:
    """LP 0x94 on a lockup nobody has come to clear. Returns True if sent.

    §7.9 makes two outcomes possible and the reset code does not tell them
    apart. Where the W2-G Reset To Credit Meter function has been enabled
    (LP 0xA8, which this tool does not send), the money goes back onto the
    machine as credits; otherwise the handpay is recorded as paid by an
    attendant who was never there. Code 0x00 is returned either way.

    THAT AMBIGUITY IS THE WHOLE REASON --lab-auto-aft-credit EXISTS. On a
    cabinet without reset-to-credit-meter the lockup clears and the credit
    meter stays at zero, so the soak stops spinning anyway unless something
    puts credits back. The handpay information read at 0x51 carries
    reset_id, which says which kind of reset this machine offers; it is
    recorded in handpay_queue every time, so the morning's reconciliation
    can say which outcome actually happened rather than guessing.
    """
    if after_seconds <= 0 or state.handpay_pending_since is None:
        return False
    waited = monotonic_fn() - state.handpay_pending_since
    if waited < after_seconds:
        return False
    try:
        code = client.reset_handpay()
    except SASError as e:
        # §7.9: a machine not configured for remote handpay reset "must
        # ignore long poll 94", and ignoring means silence. So a timeout
        # here says the feature is off at the cabinet -- a different fact
        # from code 0x01, which says the feature is on and the machine
        # declined. Do not retry it every cycle; stop the clock and let a
        # human find the row in the morning.
        state.handpay_pending_since = None
        _record_lab_action(
            conn, now, "handpay_reset", f"after {waited:.0f}s",
            f"NO ANSWER ({type(e).__name__}: {e}) -- §7.9 says a machine not "
            "configured for remote handpay reset ignores LP 0x94 entirely, so "
            "this most likely means the feature is off at the cabinet. Not "
            "retried; clear it by hand.")
        return False
    outcome = HANDPAY_RESET_CODES.get(code, f"unknown reset code 0x{code:02X}")
    state.handpay_resets += 1
    if code == 0x00:
        state.handpay_pending_since = None
    else:
        # 0x01 "unable to reset" and 0x02 "not currently in a handpay
        # condition" both mean nothing changed. Stop the clock either way:
        # 0x02 means the lockup is already gone and our clock was stale,
        # and 0x01 retried every cycle is a machine being nagged.
        state.handpay_pending_since = None
    _record_lab_action(
        conn, now, "handpay_reset",
        f"LP 0x94 after {waited:.0f}s unattended (reset #{state.handpay_resets})",
        f"code 0x{code:02X} {outcome}")
    return True


def _lab_auto_aft_credit(client, conn: sqlite3.Connection, now: str,
                         state: "PollState", *, amount_cents: int,
                         floor_cents: int, pool_id: int = 0,
                         monotonic_fn=time.monotonic,
                         sleep_fn=time.sleep) -> bool:
    """Push credits when the cabinet has run dry. Returns True if it did.

    Reads the three balances with LP 0x74 (lock code 0xFF, status only, no
    lock) rather than trusting a meter column: under --skip-full-meter-sweep
    every sm_ column is NULL, and c7_current_credits only refreshes on the
    C-7 sweep, which routine runs throttle to once every five minutes. A
    five-minute-old credit figure is not a basis for moving money.
    """
    if amount_cents <= 0:
        return False
    if state.aft_pushes >= LAB_AUTO_AFT_MAX_PUSHES:
        return False
    if state.aft_cents_pushed >= LAB_AUTO_AFT_MAX_TOTAL_CENTS:
        return False
    if (state.aft_last_push_mono is not None
            and monotonic_fn() - state.aft_last_push_mono < LAB_AUTO_AFT_MIN_INTERVAL_S):
        return False
    try:
        lock = client.aft_game_lock_and_status_request(lock_code=0xFF)
    except SASError as e:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) "
            "VALUES (?, ?, ?, ?)",
            (now, "aft_game_lock_and_status_request(lab auto credit)",
             type(e).__name__, str(e)))
        _safe_commit(conn, now)
        # Silence is NOT zero credits. Refusing to push on a failed read is
        # the whole difference between this and a money printer.
        return False
    on_machine = (int(lock.current_cashable_amount or 0)
                  + int(lock.current_restricted_amount or 0)
                  + int(lock.current_non_restricted_amount or 0))
    if on_machine > floor_cents:
        return False
    transaction_id = f"LABAUTO{int(time.time())}"
    try:
        result = client.aft_transfer_funds(
            transfer_code=AFT_TRANSFER_CODE_FULL_ONLY,
            transaction_index=0x00,
            transfer_type=int(AftTransferType.IN_HOUSE_TO_GAMING_MACHINE),
            cashable_amount=amount_cents,
            restricted_amount=0,
            nonrestricted_amount=0,
            pool_id=pool_id,
            expiration=0,
            asset_number=lock.asset_number,
            registration_key=bytes(20),
            transaction_id=transaction_id,
        )
    except SASError as e:
        # An exchange that fails AFTER the machine acted looks identical to
        # one that failed before. Do not retry with a fresh transaction ID --
        # that is how a cabinet gets paid twice. Count it against the cap and
        # let the morning reconcile it from the meters.
        state.aft_pushes += 1
        state.aft_last_push_mono = monotonic_fn()
        _record_lab_action(
            conn, now, "aft_credit", f"{amount_cents / 100:,.2f} as {transaction_id}",
            f"FAILED ({type(e).__name__}: {e}) -- it may or may not have "
            "landed. Not retried: reusing the ID risks status 0x81, a fresh "
            "one risks paying twice. Reconcile from the meters.")
        return False
    state.aft_pushes += 1
    state.aft_cents_pushed += amount_cents
    state.aft_last_push_mono = monotonic_fn()
    category, _ = describe_aft_transfer_status(result.transfer_status)
    if category == "PENDING":
        # §8.8, and not optional: an unacknowledged completion makes the
        # machine reissue exception 0x69 every fifteen seconds forever, as a
        # PRIORITY exception delivered ahead of anything queued. Left alone
        # overnight that is a permanent beat in the event stream.
        for _ in range(12):
            sleep_fn(1.0)
            try:
                final = client.aft_interrogate_transfer(0x00, acknowledge=True)
            except SASError:
                break
            category, _ = describe_aft_transfer_status(final.transfer_status)
            if category != "PENDING":
                result = final
                break
    _record_lab_action(
        conn, now, "aft_credit",
        f"{amount_cents / 100:,.2f} cashable, machine had "
        f"{on_machine / 100:,.2f} (floor {floor_cents / 100:,.2f}), "
        f"push #{state.aft_pushes}/{LAB_AUTO_AFT_MAX_PUSHES}",
        f"status 0x{result.transfer_status:02X} {category}")
    return True


class _MeterSupport:
    """Which optional meter polls this machine has ever answered.

    Section 4.4 requires a machine that does not support a long poll to
    ignore it silently, so for a single-meter poll there is no way to tell
    "unsupported" from "no answer" in one exchange: both are a timeout.
    Treating either as a cycle-aborting failure means a machine missing any
    one optional meter can never write a meter row at all -- and that is not
    hypothetical. A cashless machine has no hopper, so LP 0x2C
    (SEND_CURRENT_HOPPER_LEVEL) never answers, and on the first EGT Premier
    tested it aborted every cycle before a single row was written.

    The distinction is therefore made over time rather than per exchange: a
    poll that has NEVER answered is taken as unsupported and its column left
    NULL, while a poll that HAS answered before and now times out is a real
    failure and still aborts the cycle. A column that is permanently NULL
    does not misrepresent "as of polled_at" -- the machine has no such meter.
    One that was populated yesterday and is missing today does, which is why
    the two are not treated alike.
    """

    def __init__(self):
        self.answered: set[str] = set()
        self.unsupported: set[str] = set()

    def record_success(self, name: str) -> None:
        self.answered.add(name)
        self.unsupported.discard(name)

    def is_known_good(self, name: str) -> bool:
        return name in self.answered

    def record_unsupported(self, name: str) -> None:
        self.unsupported.add(name)


METER_SUPPORT = _MeterSupport()


def _poll_optional(support: _MeterSupport, name: str, fn, *args):
    """A meter poll whose absence is a per-column NULL rather than a fault.

    Only a timeout is taken as "unsupported", because silence is the one
    thing section 4.4 specifies for an unsupported long poll. A checksum or
    address error is a real fault whatever the machine supports, and still
    aborts the cycle. And a timeout from a poll that has answered before is
    a real failure too -- that is the whole point of keeping the ledger.
    """
    try:
        result = fn(*args)
    except SASTimeoutError as e:
        if support.is_known_good(name):
            raise _MeterPollFailure(name, e) from e
        support.record_unsupported(name)
        return None
    except SASError as e:
        raise _MeterPollFailure(name, e) from e
    support.record_success(name)
    return result


class _MeterPollFailure(Exception):
    """Internal signal: one meter poll failed. Carries which poll and the
    original SASError so the caller can log a precise poll_errors row.
    Any failure anywhere in _poll_all_meters() aborts the whole cycle's
    meter write — see poll_and_log() and the module docstring for why:
    a row that's fresh in some meter columns and stale or missing in
    others would misrepresent what "as of polled_at" means.
    """

    def __init__(self, poll_name: str, original: SASError):
        super().__init__(f"{poll_name}: {original}")
        self.poll_name = poll_name
        self.original = original


def _poll_all_meters(
    client,
    *,
    now: str = "",
    full_sweep: bool = True,
    table_c7_sweep: bool = True,
    validation_meters_sweep: bool = True,
    last_accepted_bill_poll: bool = True,
    heartbeat=None,
    heartbeat_every: int = 8,
    give_up_optional=None,
) -> dict:
    """Poll every meter this tool knows how to read, in one pass, and
    return {column: value} covering every name in ALL_METER_FIELDS.
    Raises _MeterPollFailure on the first failure, naming exactly which
    poll failed. When ``full_sweep`` is False, the ~39 SINGLE_METER_COLUMNS
    are skipped and left as None (NULL) in the returned row rather than
    polled — a lighter-weight cycle for hardware where that much extra
    wire traffic per cycle isn't affordable; see --skip-full-meter-sweep.
    When ``table_c7_sweep`` is False, the ~154 TABLE_C7_EXTENDED_COLUMNS
    are likewise skipped and left None; see --skip-table-c7-sweep. The
    two sweeps are independent: LP 0x6F (this one) is far more
    wire-efficient per meter than the single-meter LP sweep (12 meters
    per exchange instead of 1), so it's worth keeping on even where the
    single-meter sweep isn't. When ``validation_meters_sweep`` is False,
    the 24 VALIDATION_METER_COLUMNS are likewise skipped and left None;
    see --skip-validation-meters-sweep. LP 0x50 has no batching (unlike
    LP 6F), so this sweep costs one exchange per validation type — 12
    exchanges for meters this project already reads via Table C-7 (codes
    0x80+, per the spec's own note); it's on by default anyway because
    the redundancy is deliberate (see this module's docstring), but it's
    the priciest of the three sweeps per meter actually obtained, so the
    opt-out exists for constrained links. When ``last_accepted_bill_poll``
    is False, LP48_METER_COLUMNS are likewise skipped and left None; see
    --skip-last-accepted-bill-poll. This one's opt-out exists for a
    different reason than the sweeps: §7.11 explicitly warns some older
    machines don't support LP 0x48 at all, and unlike LP 0x6F's per-meter
    size=0 signal, that failure mode is a genuine SASError on the whole
    exchange — so on such a machine this poll, left on, would abort every
    single cycle's row write, not just leave one field NULL.
    """
    values: dict = {}
    exchanges = 0

    def beat():
        nonlocal exchanges
        exchanges += 1
        if heartbeat is not None and exchanges % heartbeat_every == 0:
            heartbeat()

    if give_up_optional is None:
        give_up_optional = _never_give_up

    def poll(name, fn, *args):
        """One long poll, with §12.4's requeue rather than a failure.

        §12.4: "When the host receives an event response to a long poll, it
        considers the long poll NACKed and REINSERTS THE LONG POLL INTO ITS
        TRANSMIT QUEUE." Not "gives up on it", which is what this did until
        2026-11-06.

        The difference is the whole of acceptance row P7. A customer
        pressing CHANGE and CASHOUT repeatedly -- which the operator has
        watched customers do on purpose, believing it affects the system --
        raises a burst of events, every one of which can pre-empt a long
        poll. Exhaust the transport's reissue budget and the meter read was
        simply lost, and recorded as a fault.

        It is not a fault. Every pre-emption CARRIED A REAL EVENT AWAY: the
        queue is shorter for it and nothing was dropped. So the poll is put
        back and sent again after a heartbeat, and a heartbeat is the right
        thing to do first because a general poll drains one event for one
        byte, where reissuing this long poll drains one for seventy.

        Only when the second attempt is pre-empted too does it become a
        failure -- and even then it is recorded under its own error type,
        so a mashing customer never again looks like a dying cable.
        """
        try:
            result = fn(*args)
        except SASPreempted as first:
            # §12.4's requeue. Drain cheaply, then send it again.
            beat()
            # `now` IS A PARAMETER, and was not until 2026-11-08.
            #
            # This line read `f"[{now}] ..."` with no `now` anywhere in
            # this function's scope. It is on the §12.4 requeue path, which
            # only runs when a long poll is pre-empted -- so it never
            # executed in any test or any ordinary cycle, and raised
            # NameError the first time a real customer-style button burst
            # drove it. That ENDED TWO SOAKS: 2026-11-08T17:16 (unexplained
            # for a day) and 2026-11-08T22:51, the second one deliberately
            # reproduced with the log attached. See handover §29.
            print(f"[{now}] {name} was pre-empted "
                  f"{first.events_collected} time(s) by real time events "
                  f"(§12.3); requeued per §12.4 after a heartbeat")
            try:
                result = fn(*args)
            except SASPreempted as second:
                raise _MeterPollFailure(name, second) from second
            except SASError as e:
                raise _MeterPollFailure(name, e) from e
        except SASError as e:
            raise _MeterPollFailure(name, e) from e
        METER_SUPPORT.record_success(name)
        beat()
        return result

    def poll_optional(name, fn, *args):
        result = _poll_optional(METER_SUPPORT, name, fn, *args)
        beat()
        return result

    meters = poll("send_meters_10_through_15", client.send_meters_10_through_15)
    values.update({f: getattr(meters, f) for f in METER_FIELDS})

    ticket_meters = poll("send_selected_meters(ticket_meters)", client.send_selected_meters, list(TICKET_METER_CODES))
    values.update((c, ticket_meters.meters[code]) for c, code in zip(TICKET_METER_COLUMNS, TICKET_METER_CODES))

    m19 = poll("send_meters_11_through_15", client.send_meters_11_through_15)
    values.update(zip(LP19_METER_COLUMNS, (m19.total_coin_in, m19.total_coin_out, m19.total_drop, m19.total_jackpot, m19.games_played)))

    m1c = poll("send_extended_meters_group", client.send_extended_meters_group)
    values.update(
        zip(
            LP1C_METER_COLUMNS,
            (
                m1c.total_coin_in,
                m1c.total_coin_out,
                m1c.total_drop,
                m1c.total_jackpot,
                m1c.games_played,
                m1c.games_won,
                m1c.slot_door_opened,
                m1c.power_reset,
            ),
        )
    )

    m18 = poll("send_games_since_power_up_and_door_closure", client.send_games_since_power_up_and_door_closure)
    values.update(zip(LP18_METER_COLUMNS, (m18.games_since_power_up, m18.games_since_door_closure)))

    bills = poll("send_total_bill_meters", client.send_total_bill_meters)
    values.update(zip(LP1E_METER_COLUMNS, (bills.bills_1, bills.bills_5, bills.bills_10, bills.bills_20, bills.bills_50, bills.bills_100)))

    values[LP2D_METER_COLUMNS[0]] = poll(
        "send_total_hand_paid_cancelled_credits", client.send_total_hand_paid_cancelled_credits
    )

    hopper = poll("send_current_hopper_status", client.send_current_hopper_status)
    values.update(zip(LP4F_METER_COLUMNS, (hopper.status, hopper.percent_full, hopper.level)))

    if last_accepted_bill_poll:
        # Its own flag, not folded into the always-on group above: unlike
        # LP 0x6F's per-meter size=0 signal, a machine that doesn't support
        # LP 0x48 at all (§7.11 explicitly warns some older ones don't)
        # fails the *whole exchange* -- a genuine SASError/timeout, not a
        # soft per-field skip. Folding it in unconditionally would abort
        # every cycle's row write on such a machine; see --skip-last-
        # accepted-bill-poll.
        bill = poll_optional(
            "send_last_accepted_bill_information", client.send_last_accepted_bill_information
        )
        if bill is None:
            # The docstring above anticipated exactly this: machines that do
            # not implement LP 0x48 ignore it, and aborting on that would end
            # every cycle. NULL the three columns instead.
            for column in LP48_METER_COLUMNS:
                values[column] = None
        else:
            values.update(
                zip(LP48_METER_COLUMNS, (bill.country_code, bill.denomination_code, bill.bill_meter))
            )
    else:
        for column in LP48_METER_COLUMNS:
            values[column] = None

    if full_sweep:
        for lp, column in SINGLE_METER_COLUMNS.items():
            if give_up_optional():
                # Stop HERE, mid-sweep. Everything still unread is left
                # None, which is what a skipped sweep already means.
                for remaining in SINGLE_METER_COLUMNS.values():
                    values.setdefault(remaining, None)
                break
            values[column] = poll_optional(f"send_meter({lp.name})", client.send_meter, lp)
        for column in SINGLE_METER_COLUMNS.values():
            values.setdefault(column, None)
    else:
        for column in SINGLE_METER_COLUMNS.values():
            values[column] = None

    if table_c7_sweep:
        for i in range(0, len(TABLE_C7_EXTENDED_CODES), TABLE_C7_CHUNK_SIZE):
            if give_up_optional():
                for remaining in TABLE_C7_EXTENDED_COLUMNS:
                    values.setdefault(remaining, None)
                break
            chunk_codes = TABLE_C7_EXTENDED_CODES[i:i + TABLE_C7_CHUNK_SIZE]
            chunk_columns = TABLE_C7_EXTENDED_COLUMNS[i:i + TABLE_C7_CHUNK_SIZE]
            chunk_num = i // TABLE_C7_CHUNK_SIZE + 1
            # Alternate 0x6F / 0xAF between consecutive chunks. Both codes
            # read the exact same meter data; the spec provides the second
            # one for precisely this reason (§7.21: "to allow a host to
            # perform consecutive meter polls and still provide a proper
            # implied acknowledgement in accordance with Section 3.1", and
            # the 6.00 revision note "Added long poll AF as alternate 6F
            # meter poll, to allow consecutive meter polls").
            #
            # Why bother: per Table 3.1 a long poll is implicitly ACKed only
            # by a general poll or "a long poll with a different command
            # byte", and §3.2 makes a repeated identical poll an implied
            # NACK meaning "re-send the information you just sent".
            #
            # Calibrate that risk honestly, though. IGT's own test tool ships
            # a canned meter script (sastest.ini [Meter Test]) that fires 41
            # consecutive LP 0x52 polls differing only in their game-number
            # data -- so real machines evidently tolerate same-command-byte
            # runs, or that script would return game 0's meters 41 times and
            # be useless. The honest reading is that 0xAF exists because 0x6F
            # is the one poll *designed* for automated back-to-back use (12
            # meters per poll, ~13 polls to cover Table C-7), not because
            # consecutive polls are broadly unsafe.
            #
            # Alternating costs nothing and is exactly what the spec provides
            # 0xAF for, so it stays -- but treat it as cheap insurance, not
            # as a bug being patched.
            alternate = chunk_num % 2 == 0
            fn = client.send_extended_meters_alternate if alternate else client.send_extended_meters
            result = poll(
                f"send_extended_meters{'_alternate' if alternate else ''}"
                f"(chunk {chunk_num}/{TABLE_C7_CHUNK_COUNT})",
                fn,
                list(chunk_codes),
            )
            # .get(), not [] -- a code absent from result.meters means the
            # machine answered normally but reported that specific meter as
            # unsupported (size=0, §7.21b), not a failure. That's a known
            # absence, correctly NULL, and must not raise: send_extended_meters()
            # silently drops unsupported codes rather than erroring on them
            # (see its own docstring), and almost no real EGM implements every
            # one of Table C-7's ~154 codes, so treating an unsupported single
            # meter as a whole-chunk failure would make this sweep unusable on
            # real hardware -- only a genuine SASError (timeout, checksum
            # failure, garbled response) aborts the cycle, same as everywhere
            # else in this function.
            values.update((column, result.meters.get(code)) for column, code in zip(chunk_columns, chunk_codes))
    else:
        for column in TABLE_C7_EXTENDED_COLUMNS:
            values[column] = None

    if validation_meters_sweep:
        for vt in VALIDATION_METER_TYPES:
            if give_up_optional():
                for column in VALIDATION_METER_COLUMNS_FLAT:
                    values.setdefault(column, None)
                break
            result = poll(f"send_validation_meters({vt.name})", client.send_validation_meters, vt)
            total_col, amount_col = VALIDATION_METER_COLUMNS[vt]
            values[total_col] = result.total_validations
            values[amount_col] = result.cumulative_amount_cents
    else:
        for column in VALIDATION_METER_COLUMNS_FLAT:
            values[column] = None

    return values


AFT_CASHOUT_REQUEST = 0x6A       # §8.7, AFT request for host cashout
AFT_WIN_CASHOUT_REQUEST = 0x6B   # §8.7, AFT request for host to cash out a win


def _decline_aft_cashout(client, conn: sqlite3.Connection, now: str,
                         exception_code: int) -> None:
    """Answer §8.7's 0x6A with an all-zeros LP 0x72: "not by AFT, use your
    own means."

    WHAT THIS IS WORTH, measured on the wire 2026-10-28 with nothing
    answering. Cashout on this cabinet is a THREE-STAGE LADDER and only the
    middle stage is in the migration spec:

        0x6A  AFT request for host cashout     8 repeats over   7.652 s
        0x57  system validation request       10 repeats over   9.796 s
        0x51  handpay is pending              then every      15.248 s
        button press -> handpay                               17.884 s

    So the machine falls through to the ticket path on its own -- stage 1 is
    not a dead end -- but EVERY CASHOUT BURNS ABOUT EIGHT SECONDS THERE,
    with a fully stocked validation pool and nothing wrong. A player presses
    cash out and watches a dead screen before the ticket even begins. They
    press again, and then they call somebody.

    §8.7 permits exactly this answer: "all amounts may be set to zero to
    instruct the gaming machine to perform the cashout by whatever other
    means are available."

    IT MOVES NO MONEY. Every amount is zero; this is a decline, not a
    transfer. That matters here because this poller is otherwise forbidden
    from touching money at all.

    AND IT ADDS NO DEPENDENCY, which is the real argument. If the gateway is
    down, stage 1 times out after ~8 s and the machine proceeds exactly as
    it does today. That is the opposite of stage 2, where a missing answer
    CAUSES a handpay. So this is upside when the gateway is up and costs
    nothing when it is not.

    OFF BY DEFAULT, and per-model. examples/host_cashout.py measured a
    denied cashout on an EGT Premier WITH NO PRINTER producing a handpay
    inside two minutes: with no printer, "whatever other means are
    available" is an attendant. On such a cabinet this makes things worse,
    faster. The ladder above is this cabinet's; check it per model before
    enabling anywhere.
    """
    try:
        result = client.aft_transfer_funds(
            transfer_code=AFT_TRANSFER_CODE_FULL_ONLY,
            transaction_index=0x00,
            transfer_type=int(AftTransferType.IN_HOUSE_FROM_GAMING_MACHINE_TO_HOST),
            cashable_amount=0,
            restricted_amount=0,
            nonrestricted_amount=0,
            transaction_id=f"DECLINE{int(time.time())}",
        )
    except SASError as e:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) "
            "VALUES (?, ?, ?, ?)",
            (now, f"decline_aft_cashout(0x{exception_code:02X})",
             type(e).__name__, str(e)))
        _safe_commit(conn, now)
        print(f"[{now}] could not decline the AFT cashout request: {e}. "
              f"The machine will time out stage 1 on its own in ~8s and "
              f"carry on, so this is slow rather than broken.")
        return
    conn.execute(
        "INSERT INTO lab_auto_actions (occurred_at, action, detail, outcome) "
        "VALUES (?, ?, ?, ?)",
        (now, "decline_aft_cashout",
         f"0x{exception_code:02X}: all-zeros LP 0x72 (§8.7)",
         f"status 0x{int(getattr(result, 'transfer_status', 0) or 0):02X}"))
    _safe_commit(conn, now)
    print(f"[{now}] declined the AFT cashout request (§8.7 all-zeros LP "
          f"0x72). The machine goes straight to its own cashout means "
          f"instead of burning ~8s waiting for a host that is not there.")


GAME_STARTED = 0x7E


def _check_stream_continuity(conn: sqlite3.Connection, now: str,
                             state: "PollState", fields: dict | None) -> None:
    """§12.5.3's arithmetic, on every game start, while it is happening.

    THE ONE CHECK THAT NEEDS NO METERS. §12.5.3 places the coin-in meter
    inside the game-start event, AFTER the wager, so two consecutive starts
    must differ by exactly the later one's wager:

        this.total_coin_in - previous.total_coin_in == this.wager_cents

    When they differ by more, a game start went missing in between, and the
    excess is how much was wagered in it. The stream audits itself.

    WHY THIS RUNS LIVE RATHER THAN IN A REPORT. rte_reconcile.py has had
    this arithmetic since it was written, and on 2026-11-06 it found nine
    lost game starts in the operator's session -- THREE HOURS LATE, because
    nothing ran it until he asked by hand. The soak report showed the
    start/end gap climbing and nothing that explained it.

    The operator's requirement is that extended meters and RTE must match
    unless the gateway lost communication. This is how a gateway knows the
    moment they stop matching, instead of finding out from a post-mortem.

    A NOTE ON WHAT IT CANNOT SEE. A gateway restart or a dead cable breaks
    the chain legitimately -- play continues while nobody is polling -- and
    the next start after the gap will look like a loss. The caller resets
    the chain on a comms gap for exactly that reason, so a run that spans
    one does not cry wolf.
    """
    if not fields:
        return
    coin_in = fields.get("total_coin_in")
    wager = fields.get("wager_cents")
    if coin_in is None:
        return

    previous, previous_at = state.last_game_start_coin_in, state.last_game_start_at
    state.last_game_start_coin_in = coin_in
    state.last_game_start_at = now
    if previous is None or wager is None:
        return

    # §12.5.3's meter only ever rises. A fall is a RAM clear or a bad read,
    # not a lost event, and reporting it as one would be a false alarm on
    # the day a technician clears the machine.
    if coin_in < previous:
        state.last_game_start_coin_in = coin_in
        return

    unexplained = (coin_in - previous) - wager
    if unexplained <= 0:
        return

    state.stream_gaps += 1
    state.gaps_this_cycle += 1
    state.stream_credits_lost += unexplained
    conn.execute(
        "INSERT INTO stream_gaps (detected_at, previous_at, previous_coin_in, "
        "this_coin_in, this_wager, unexplained_credits) VALUES (?,?,?,?,?,?)",
        (now, previous_at, previous, coin_in, wager, unexplained))
    _safe_commit(conn, now)
    # SPINS, NOT CREDITS. The operator, 2026-11-09: "I would like to know
    # how many spins were undetected." A credit figure is the money, which
    # the meters already have; the SPIN count is what is missing from the
    # player's history, and it is the thing a CMS cannot reconstruct.
    #
    # Exact only while the bet is unchanged across the gap -- the missing
    # spins are missing, so their wagers are unknown and this cabinet's
    # own last wager is the only divisor available. Labelled as an
    # estimate rather than dressed up as a count.
    spins = (unexplained / wager) if wager else 0
    spin_note = (f" ~{spins:.0f} spin(s) at {wager}/spin"
                 + ("" if spins == int(spins) else
                    " — NOT a whole number, so the bet changed inside the gap")
                 if wager else "")
    print(f"[{now}] ** STREAM GAP: coin in rose {coin_in - previous} but this "
          f"game wagered {wager}; {unexplained} credits unaccounted for,"
          f"{spin_note}. "
          f"A game start went missing between {previous_at} and now. "
          f"({state.stream_gaps} gap(s), {state.stream_credits_lost} credits "
          f"this run.)")


def _dispatch_exception(
    client, conn: sqlite3.Connection, now: str, exception_code: int, *,
    source: str = "cycle", state: "PollState | None" = None,
    fields: dict | None = None
) -> None:
    """Record one general-poll exception, then act on it if it is actionable.

    Extracted so that a heartbeat poll taken mid-sweep is handled exactly
    like the cycle's own poll -- an exception found while draining the queue
    is not a lesser exception, and a second copy of this dispatch would be a
    place for the two to drift apart.

    EVERY exception is recorded, not only the five this tool acts on. The
    reason is concrete: an 18-hour soak produced a cluster of 81 core-meter
    timeouts and a 116-second stall, and nothing in the database explained
    either. The explanation was in the machine's own event log, read off its
    screen afterwards -- a technician had the slot door open for most of the
    run and entered the attendant menu twice. The poller had been told all of
    that over the wire, as exceptions 0x11, 0x12, 0x82 and 0x83, and had
    thrown it away because it only cared about tickets and cashouts.

    A failure log without the machine's own state beside it cannot be read.
    Recording the rest costs one row per event and turns "the loop stalled,
    investigate" into "the door was open".
    """
    if exception_code != ExceptionCode.NONE:
        if state is not None:
            # The numerator for the headroom figure below. Counted here
            # rather than at the drain so that a heartbeat's own events and
            # an interleaved one are weighed the same -- they cost the same
            # poll slot, which is the resource being measured.
            state.events_this_cycle += 1
            if int(exception_code) == BUFFER_OVERFLOW_EXCEPTION:
                state.overflows_this_cycle += 1
        conn.execute(
            "INSERT INTO machine_events (occurred_at, exception_code, description, "
            "source, rte_fields) VALUES (?, ?, ?, ?, ?)",
            (now, int(exception_code), describe_exception(int(exception_code)), source,
             json.dumps(fields, sort_keys=True) if fields else None),
        )
        _safe_commit(conn, now)

    # A meter-reset announcement is noted for maintain_epoch() rather than
    # acted on here: this runs inside the general poll, before the cycle's
    # meters have been read, and an epoch opened now would be the epoch the
    # pre-clear readings land in.
    if state is not None and int(exception_code) in CLEAR_ANNOUNCED_BY:
        state.clear_announced_at = now
        print(f"[{now}] the machine announced a meter reset "
              f"(0x{int(exception_code):02X}); a new epoch opens with this "
              "cycle's meters.")

    if int(exception_code) == GAME_STARTED and state is not None:
        # Before anything else: the stream's own arithmetic, on every start.
        _check_stream_continuity(conn, now, state, fields)

    if exception_code == ExceptionCode.TICKET_INSERTED:
        _capture_ticket_in(client, conn, now, state)
    elif exception_code == ExceptionCode.TICKET_TRANSFER_COMPLETE:
        _capture_ticket_in_completion(client, conn, now)
    elif exception_code in (ExceptionCode.CASH_OUT_TICKET_PRINTED, ExceptionCode.HANDPAY_VALIDATED):
        _drain_ticket_out_history(client, conn, now)
    elif exception_code == ExceptionCode.SYSTEM_VALIDATION_REQUEST:
        _handle_cashout_request(client, conn, now)
    elif int(exception_code) == 0x69:
        _acknowledge_aft_transfer(client, conn, now)
    elif (int(exception_code) in (AFT_CASHOUT_REQUEST, AFT_WIN_CASHOUT_REQUEST)
          and state is not None and state.decline_aft_cashout):
        _decline_aft_cashout(client, conn, now, int(exception_code))
    elif int(exception_code) == 0x3C:
        # "Operator changed options" -- the ONLY in-band signal that this
        # cabinet's configuration has moved, a denomination among the things
        # it covers. It was recognised in the description table and dispatched
        # nowhere, which meant a denomination change could happen with nothing
        # in the database marking when: every credit figure after it wrong by
        # a factor, and no way to find the boundary afterwards.
        #
        # Deferred rather than acted on here, exactly like the comms-restored
        # interrogation: re-reading the configuration is ~150 long polls and
        # this dispatcher can be running from inside a meter sweep.
        if state is not None:
            state.options_change_pending = now
            print(f"[{now}] exception 0x3C: the operator changed options. "
                  "Re-reading the machine's configuration after this cycle's "
                  "sweep -- a denomination change makes every credit figure "
                  "before and after it incomparable.")
    elif exception_code == ExceptionCode.HANDPAY_PENDING:
        _capture_handpay(client, conn, now, state)
    elif exception_code == ExceptionCode.HANDPAY_RESET:
        # Somebody turned the key, or our own LP 0x94 landed. Either way the
        # lockup is gone and the clock must stop, or the next cycle would
        # "reset" a handpay that no longer exists.
        if state is not None:
            state.handpay_pending_since = None


def interrogate(client, conn: sqlite3.Connection, state: "PollState", now: str,
                *, reason: str, gap_seconds: float | None = None,
                heartbeat=None, monotonic_fn=time.monotonic) -> int:
    """Everything the machine will tell us about itself, in one pass.

    Run on first connect and whenever the link has been down long enough for
    somebody to have moved the cable (DEFAULT_COMMS_GAP_SECONDS). The
    question it answers is not "what did we miss" -- events lost to a gap are
    gone and §2.2.1 says so -- but "is this the same machine, configured the
    same way, as the one that was answering before".

    IT MUST BE HEARTBEAT-DRIVEN, and that is not a detail. On the lab cabinet
    this pass is ~150 long polls at the 88 ms each a 73-poll sweep measured,
    which is thirteen seconds of not draining the exception queue -- longer
    than the queue takes to overflow on a machine being played. An
    interrogation that ran without draining would cause exactly the loss it
    exists to detect, and would do it silently, at the moment its output is
    most trusted.

    Every poll is individually optional. §4.4 lets a machine ignore a long
    poll it does not implement, and a cabinet that answers nothing here has
    still been interrogated -- the row records what it refused, which is
    itself a fact about the machine.
    """
    started = monotonic_fn()
    identity = read_identity(client)
    state.identity = identity
    state.cycles_since_identity = 0

    previous = conn.execute(
        "SELECT serial_number, asset_number, sas_version FROM machine_epochs "
        "WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    changed = None
    if previous is not None:
        changed = identity_changed(
            {"serial_number": previous[0], "asset_number": previous[1],
             "sas_version": previous[2]}, identity)

    if previous is None:
        # Nothing to compare against yet. The baseline has to be written here
        # or the NEXT interrogation has nothing to detect a swap against --
        # which is the whole purpose, and was this function's first bug.
        state.epoch_id = open_epoch(
            conn, now, "first_seen", identity,
            f"first interrogation ({reason})")
        print(f"[{now}] epoch {state.epoch_id} opened (first_seen): "
              f"serial {identity.get('serial_number')} "
              f"asset {identity.get('asset_number')}")
    elif changed and changed[0] == "machine_changed":
        state.epoch_id = open_epoch(
            conn, now, "machine_changed", identity,
            f"after {reason}: {changed[1]}")
        print(f"[{now}] *** DIFFERENT MACHINE after {reason}: {changed[1]}")
        print(f"[{now}] epoch {state.epoch_id} opened. Meters from here belong "
              "to the new cabinet; nothing before this line does.")
    elif reason == "comms_restored":
        state.epoch_id = open_epoch(
            conn, now, "comms_restored", identity,
            f"link down {gap_seconds:.0f}s; same machine answered on recovery"
            if gap_seconds else "link restored")
        print(f"[{now}] epoch {state.epoch_id} opened (comms_restored): same "
              f"machine, but {gap_seconds:.0f}s of events are unrecoverable "
              "(§2.2.1) so continuity is broken here.")

    if heartbeat:
        heartbeat()

    features = None
    try:
        f = client.send_enabled_features()
        features = {k: v for k, v in vars(f).items()}
        # Table 7.14e bit 0. Cached because the fast-poll decision is taken
        # every time drain priority flips, and re-reading LP 0xA0 there
        # would cost a long poll in the one condition where poll slots are
        # the scarce resource.
        state.fast_poll_supported = bool(
            getattr(f, "max_polling_rate_40ms", False))
    except SASError as e:
        features = {"error": f"{type(e).__name__}: {e}"}
    if heartbeat:
        heartbeat()

    cur = conn.execute(
        "INSERT INTO interrogations (started_at, reason, gap_seconds, epoch_id, "
        "serial_number, asset_number, sas_version, machine_changed, "
        "identity_note, enabled_features) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now, reason, gap_seconds, state.epoch_id,
         identity.get("serial_number"), identity.get("asset_number"),
         identity.get("sas_version"),
         1 if (changed and changed[0] == "machine_changed") else 0,
         changed[1] if changed else None,
         json.dumps(features, sort_keys=True, default=str)))
    interrogation_id = cur.lastrowid
    _safe_commit(conn, now)

    games = _interrogate_games(client, conn, interrogation_id, heartbeat)
    redeemed = _interrogate_last_redeemed(client, heartbeat)
    tickets = backfill_ticket_out_history(client, conn)

    conn.execute(
        "UPDATE interrogations SET games_found = ?, ticket_out_records = ?, "
        "last_redeemed = ?, duration_seconds = ? WHERE id = ?",
        (games, tickets,
         json.dumps(redeemed, sort_keys=True, default=str) if redeemed else None,
         monotonic_fn() - started, interrogation_id))
    _safe_commit(conn, now)
    print(f"[{now}] interrogation {interrogation_id} ({reason}): "
          f"{games} game(s), {tickets} ticket-out record(s), "
          f"{monotonic_fn() - started:.1f}s")
    return interrogation_id


def _interrogate_games(client, conn, interrogation_id: int, heartbeat) -> int:
    """Every game the machine implements, by both routes.

    LP 0x53 carries the configuration -- denomination, max bet, paytable id,
    base percentage -- and LP 0xB5 carries the human-readable game and
    paytable NAMES that 0x53 does not. Neither is a superset, so both are
    read, and a game answering only one still gets a row.

    Game numbering starts at 1. Game 0 is "the machine as a whole" in most
    polls and is not a game, so it is not enumerated here.
    """
    # LP 0x51 is the count. If the machine will not give it, LP 0x54's list
    # of ENABLED games is a floor rather than the full set -- a disabled game
    # is still implemented and still has a configuration worth recording --
    # so it is only the fallback.
    implemented = None
    try:
        implemented = client.send_meter(LongPoll.SEND_TOTAL_GAMES_IMPLEMENTED)
    except SASError:
        pass
    if not implemented:
        try:
            enabled = client.send_enabled_game_numbers()
            implemented = max(enabled) if enabled else 0
        except SASError:
            implemented = 0
    if not implemented:
        return 0

    found = 0
    for number in range(1, int(implemented) + 1):
        config = extended = None
        try:
            config = client.send_game_n_configuration(number)
        except SASError:
            pass
        if heartbeat:
            heartbeat()
        try:
            extended = client.send_extended_game_n_information(number)
        except SASError:
            pass
        if heartbeat:
            heartbeat()
        if config is None and extended is None:
            continue
        found += 1
        conn.execute(
            "INSERT INTO interrogation_games (interrogation_id, game_number, "
            "game_id, additional_id, denomination, max_bet, progressive_group, "
            "game_options, paytable_id, base_percentage, game_name, "
            "paytable_name, progressive_levels, wager_categories) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (interrogation_id, game_number) DO NOTHING",
            (interrogation_id, number,
             getattr(config, "game_id", None),
             getattr(config, "additional_id", None),
             getattr(config, "denomination", None),
             getattr(config, "max_bet", None) if config else getattr(extended, "max_bet", None),
             getattr(config, "progressive_group", None) if config
             else getattr(extended, "progressive_group", None),
             getattr(config, "game_options", None),
             getattr(config, "paytable_id", None),
             getattr(config, "base_percentage", None),
             getattr(extended, "game_name", None),
             getattr(extended, "paytable_name", None),
             getattr(extended, "progressive_levels", None),
             getattr(extended, "wager_categories", None)))
    return found


def _interrogate_last_redeemed(client, heartbeat) -> dict | None:
    """LP 0x71/FF, the read-only status query -- the only SAS-side record of
    the last ticket this machine was asked to redeem, including a refusal.
    One cycle deep (§15.12), so an interrogation is the last chance to see
    whatever is still in it."""
    try:
        result = client.redeem_ticket_status()
    except (SASError, AttributeError):
        return None
    finally:
        if heartbeat:
            heartbeat()
    return {k: v for k, v in vars(result).items()} if result else None


# Exceptions whose deadline is shorter than a meter sweep, so a heartbeat
# must act on them where it finds them instead of queueing them for the end.
#
#   0x67  ticket inserted            §15.12 allows 30 s
#   0x57  system validation request  §15.7 allows TEN
#
# Everything else waits: handling a cashout or a config re-read mid-sweep is
# the "long polls from inside a long poll loop" the deferral was built to
# avoid, and nothing else here has a deadline a sweep can miss.
# How many general polls one heartbeat may spend draining before it gives
# the sweep its turn back.
#
# Twenty is a whole §2.2.1 queue: a heartbeat that drains twenty and still
# finds more has met a machine generating faster than the host may poll,
# which no cap fixes and §12.2's relaxation is the only lever for. Below
# that it empties the queue and the sweep resumes with nothing waiting.
HEARTBEAT_DRAIN_CAP = 20

LATENCY_CRITICAL_EXCEPTIONS = frozenset({0x67, 0x57})


def _make_credit_sampler(client, conn: sqlite3.Connection, every: int):
    """LP 0x1A on an EMPTY queue only, so the credit gauge costs no event.

    WHY THIS EXISTS. Detector 7 in rte_reconcile.py is the operator's
    ledger -- credits before, minus wagers, plus wins, equals credits after
    -- and it is the only check on this project that can score an
    individual game END. Its resolution is set by how often the gauge is
    read. Today that is once per cycle, about 25 games, so a one-game fault
    is far below one interval's tolerance and only the signed total over
    hundreds of intervals can see it. One reading between consecutive games
    makes the per-game chain exact.

    THE CONSTRAINT IS THE OPERATOR'S, AND IT DECIDES THE DESIGN:

        "The cost is important. Button bursts shouldn't make the events
        lost. All our aim is gathering correct data without jeopardizing
        the correct polling."

    So this is NOT a timer. It fires only in the one moment when a poll
    provably cannot displace an event: immediately after a heartbeat has
    drained the queue to EMPTY (§18). A poll issued against an empty queue
    has no event to carry, so the slot it spends was not carrying one.

    Under a button burst the queue is never empty -- the heartbeat hits
    HEARTBEAT_DRAIN_CAP instead of returning on ExceptionCode.NONE -- and
    the sampler never runs. **It suppresses itself exactly when the machine
    is busiest, which is precisely when event collection must not be
    competed with**, and resumes with no intervention when the burst ends.
    The gauge is a convenience; the event stream is the product.

    ONE HONEST CAVEAT. Between the heartbeat reading empty and this poll
    going out, an event can arrive -- §12.3 then lets the machine answer
    THIS long poll with that event instead of the credit value. Nothing is
    lost: the event is collected by the normal pre-emption path and only
    the credit sample is missed. SASPreempted is therefore skipped in
    silence, not recorded as a fault.
    """
    def sample() -> None:
        sample.beats += 1
        if every <= 0 or sample.beats % every:
            return
        try:
            credits = client.send_meter(LongPoll.SEND_CURRENT_CREDITS)
        except SASPreempted:
            # An event beat us to the slot. It was collected; this reading
            # was not. Not a fault, and not worth a row.
            sample.preempted += 1
            return
        except SASError:
            sample.failed += 1
            return
        try:
            conn.execute(
                "INSERT INTO credit_samples (sampled_at, credits, heartbeats) "
                "VALUES (?, ?, ?)", (utc_now(), int(credits), sample.beats))
        except sqlite3.Error:
            # A sample is diagnostics. It must never abort a cycle.
            sample.failed += 1
            return
        sample.taken += 1

    sample.beats = 0
    sample.taken = 0
    sample.preempted = 0
    sample.failed = 0
    return sample


# WfA ON-DEMAND PULLS. The spec's three numbers, and what each one costs:
#
#   500 ms p95, 1 s hard, end to end     -- a cashier is at the window
#   repeat pulls >= 10 s apart            -- the abuse control
#   "an on-demand pull takes the NEXT poll slot and never interrupts an
#    in-flight poll -- a naive bypass here would reintroduce exactly the
#    mid-poll gap this project exists to remove"
#
# The third is the one with teeth, and it is satisfied structurally rather
# than by care: the servicer is only ever called from the heartbeat, which by
# construction runs BETWEEN exchanges. There is no code path from here into
# the middle of a frame, so "never interrupts an in-flight poll" is not a
# rule somebody has to remember.
#
# THE LATENCY BUDGET IS MET BY NOT GOING TO THE WIRE. A full LP 0x4D walk is
# 31 exchanges; at the ~83 ms per long poll this gateway actually measures
# that is 2.6 seconds, which blows the 1 s hard budget on its own. No amount
# of scheduling fixes that.
#
# What fixes it is that the gateway usually already knows. It drains LP 0x4D
# continuously into ticket_out_history and backfills the whole buffer at
# startup, so the ticket a cashier is scanning is normally a row we already
# have. That path costs ZERO poll slots and answers in under a millisecond.
# The wire walk is the fallback for the case the local record cannot cover --
# the gateway was down when the ticket printed, or its database was replaced.
#
# WHERE THIS DIFFERS FROM THE CREDIT SAMPLER, deliberately. That one fires
# only on an EMPTY queue, so a button burst suppresses it completely: the
# gauge is a convenience and the event stream is the product. A WfA pull
# cannot take that rule, because the thing waiting on it is a person at a
# cage window, not a detector's resolution. So the wire walk DOES run under
# load -- but it is capped per visit, it stops at its deadline, and every
# request records what it cost in poll slots. The trade is made visible
# rather than argued about.
WFA_POLLS_PER_VISIT = 4
WFA_PULL_RATE_CAP_S = 10.0
WFA_BUFFER_INDICES = 31
WFA_DEFAULT_DEADLINE_MS = 1000


def _lookup_ticket_out_local(conn: sqlite3.Connection, validation_number: int):
    """The fast path: has this gateway already seen the ticket print?

    Indexed by ticket_out_history's UNIQUE(validation_number, ...), whose
    leading column is the one asked for here. Most recent first, because a
    validation number should be unique but a pool that was re-seeded by hand
    is exactly the kind of thing that turns out not to be.
    """
    row = conn.execute(
        "SELECT amount_cents, validation_type, ticket_date, ticket_time, "
        "       ticket_number, captured_at "
        "  FROM ticket_out_history WHERE validation_number = ? "
        " ORDER BY id DESC LIMIT 1", (int(validation_number),)).fetchone()
    if row is None or row[0] is None:
        return None
    return {"amount_cents": row[0], "validation_type": row[1],
            "ticket_date": row[2], "ticket_time": row[3],
            "ticket_number": row[4], "captured_at": row[5]}


def _make_request_servicer(client, conn: sqlite3.Connection, state=None, *,
                           now_fn=utc_now, clock=time.monotonic,
                           polls_per_visit: int = WFA_POLLS_PER_VISIT,
                           rate_cap_s: float = WFA_PULL_RATE_CAP_S):
    """Serve inbound host_requests at poll-slot boundaries.

    Returns a callable taking no arguments. Call it from the heartbeat and
    nowhere else -- see the note above on why that placement IS the
    never-interrupt rule.

    One request is worked at a time. A walk that does not finish inside one
    visit keeps its position and resumes on the next, so a request never
    holds the sweep: it takes `polls_per_visit` slots, yields, and comes
    back.
    """
    walk = {}            # in-flight buffer walk, if any: id -> next index

    def _finish(req_id, started_at, *, state_name, served_from=None,
                amount=None, payload=None, error=None, polls=0):
        now = now_fn()
        latency = None
        if started_at is not None:
            latency = int(max(0.0, (clock() - started_at)) * 1000)
        conn.execute(
            "UPDATE host_requests SET state = ?, completed_at = ?, "
            "       served_from = ?, amount_cents = ?, result_json = ?, "
            "       error = ?, latency_ms = ?, "
            "       polls_spent = polls_spent + ? "
            " WHERE id = ?",
            (state_name, now, served_from, amount,
             json.dumps(payload) if payload is not None else None,
             error, latency, int(polls), req_id))
        _safe_commit(conn, now)
        walk.pop(req_id, None)

    def _elapsed_ms(requested_at: str) -> float:
        """How long the SERVER has been waiting. The budget is end to end,
        so it runs from the row's own requested_at and not from the moment
        this gateway noticed it."""
        try:
            started = datetime.datetime.fromisoformat(requested_at)
        except (TypeError, ValueError):
            return 0.0
        if started.tzinfo is None:
            started = started.replace(tzinfo=datetime.timezone.utc)
        delta = datetime.datetime.now(datetime.timezone.utc) - started
        return delta.total_seconds() * 1000.0

    def service() -> None:
        try:
            row = conn.execute(
                "SELECT id, kind, validation_number, deadline_ms, "
                "       requested_at, state "
                "  FROM host_requests "
                " WHERE state IN ('pending','claimed') ORDER BY id LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return
        if row is None:
            return
        req_id, kind, number, deadline_ms, requested_at, req_state = row
        started = clock()

        if kind != "wfa_pull":
            _finish(req_id, None, state_name="failed",
                    error=f"unknown request kind {kind!r}")
            return
        if number is None:
            _finish(req_id, None, state_name="failed",
                    error="wfa_pull with no validation_number")
            return

        if req_state == "pending":
            conn.execute(
                "UPDATE host_requests SET state = 'claimed', claimed_at = ? "
                " WHERE id = ?", (now_fn(), req_id))
            _safe_commit(conn, now_fn())

        # THE FAST PATH, and the reason the budget is achievable at all.
        local = _lookup_ticket_out_local(conn, number)
        if local is not None:
            _finish(req_id, started, state_name="done", served_from="local",
                    amount=local["amount_cents"], payload=local, polls=0)
            return

        # Past the hard deadline: stop spending the event stream on somebody
        # who is no longer waiting. Expired is not failed -- the request was
        # well formed and the answer simply did not arrive in time.
        budget = deadline_ms if deadline_ms else WFA_DEFAULT_DEADLINE_MS
        if _elapsed_ms(requested_at) > budget:
            _finish(req_id, started, state_name="expired",
                    error=f"deadline {budget} ms exceeded before an answer",
                    polls=0)
            return

        # THE 10-SECOND PULL CAP, and the case it actually protects.
        #
        # The first version of this guarded a repeat of a ticket we had
        # already FOUND, which is close to dead code: a successful walk
        # inserts the record into ticket_out_history, so the next scan of
        # the same ticket is answered by the fast path above at zero cost
        # and never reaches here. A test caught it on the day it was
        # written.
        #
        # The expensive repeat is the one that finds NOTHING. A validation
        # number the cabinet's buffer does not hold costs a full 31-index
        # walk EVERY time it is scanned, and a cashier with a bad ticket
        # will scan it more than once. That is the abuse the cap is for, so
        # the lookup below deliberately does NOT require an amount: a
        # remembered "not in the buffer" is exactly what must be reused.
        cached = conn.execute(
            "SELECT amount_cents, result_json, completed_at FROM host_requests "
            " WHERE validation_number = ? AND state = 'done' AND id <> ? "
            " ORDER BY id DESC LIMIT 1", (int(number), req_id)).fetchone()
        if cached is not None and _elapsed_ms(cached[2]) < rate_cap_s * 1000:
            _finish(req_id, started, state_name="done", served_from="cache",
                    amount=cached[0],
                    payload=json.loads(cached[1]) if cached[1] else None,
                    polls=0)
            return

        # THE SLOW PATH. One index per poll slot, `polls_per_visit` of them,
        # then yield back to the sweep and resume on the next heartbeat.
        index = walk.get(req_id, 1)
        spent = 0
        while spent < polls_per_visit and index <= WFA_BUFFER_INDICES:
            try:
                record = client.send_enhanced_validation_information(
                    function_code=index)
            except SASError:
                # A machine with no printer ignores every index (§4.4), and a
                # single timeout is not evidence either way. Count the slot,
                # move on; the deadline is what ends this, not one failure.
                spent += 1
                index += 1
                continue
            spent += 1
            index += 1
            if _is_empty_ticket_out_record(record):
                continue
            _insert_ticket_out_record(conn, record, now_fn())
            if int(record.validation_number) == int(number):
                _finish(req_id, started, state_name="done",
                        served_from="buffer-walk",
                        amount=record.amount_cents,
                        payload={"amount_cents": record.amount_cents,
                                 "validation_type": record.validation_type,
                                 "ticket_date": record.date,
                                 "ticket_time": record.time,
                                 "ticket_number": record.ticket_number,
                                 "buffer_index": record.index_number},
                        polls=spent)
                return

        conn.execute(
            "UPDATE host_requests SET polls_spent = polls_spent + ? WHERE id = ?",
            (spent, req_id))
        _safe_commit(conn, now_fn())
        service.polls_spent += spent
        if state is not None:
            state.wfa_polls_spent += spent

        if index > WFA_BUFFER_INDICES:
            # The whole buffer, and the number is not in it. That is an
            # ANSWER, not a failure: §15.10's buffer holds 31 records and a
            # busy cabinet wraps it. The server's fallback is the physical
            # visit, and it needs to know it has reached that point.
            _finish(req_id, started, state_name="done",
                    served_from="buffer-walk", amount=None,
                    payload={"found": False,
                             "searched_indices": WFA_BUFFER_INDICES},
                    polls=0)
            return
        walk[req_id] = index

    service.polls_spent = 0
    return service


def _make_heartbeat(client, conn: sqlite3.Connection, now: str, seen: list,
                    *, rte: bool = False, state: "PollState | None" = None,
                    credit_sampler=None, request_servicer=None):
    """A general poll to run between long polls, so the machine's exception
    queue is drained while the meter sweep is in progress.

    Without this the sweep is one long silence. Measured on an EGT Premier:
    73 long-poll exchanges taking 6.07s, and with the cycle's sleep a gap of
    7.12s between general polls -- against a queue about 35 deep which, at
    the 2.4 exceptions/sec a single player generates in turbo mode, fills in
    roughly 15 seconds. That is half the queue spent on every cycle, for no
    reason other than ordering, and the margin vanishes with a second event
    source or a slower disk. Exception 0x70, buffer overflow, is what the
    machine reports once it has gone.

    Codes are collected rather than acted on here, and dispatched after the
    sweep: handling a ticket or cashout mid-sweep would mean issuing long
    polls from inside a long poll loop. One sweep of latency is well within
    the 30 seconds section 15.12 allows for a validation response.

    THE INTERLEAVED BUFFER IS DRAINED FIRST, and that ordering is the whole
    reason this function touches it at all. Under RTE an event can arrive in
    place of a LONG poll's reply (12.3); the client keeps it, and the cycle
    used to write those only at the end -- after every heartbeat code. So an
    event that arrived DURING the long poll immediately before a heartbeat
    was stored AFTER the heartbeat's own event, and the row ids ran backwards
    against arrival.

    On the 41-hour soak that inverted two game starts in one cycle: ids 2883
    and 2884 carried total_coin_in 281,750 and 281,725, in that order. Since
    12.5.3's total_coin_in only ever rises, rte_reconcile read the inversion
    as a 50-credit jump with a 25-credit wager and reported 300 credits
    "unaccounted for" across twelve gaps -- in adjacent pairs, which is the
    signature of a swap rather than a loss. Nothing had been lost. Draining
    here puts both sources into one list in true arrival order.

    Entries are (code, fields, source) because the two sources keep their own
    labels: an event that pre-empted a long poll is still 'rte-interleaved',
    not something the heartbeat found.

    UNDER RTE THIS MUST USE THE RTE POLL, and getting that wrong is not a
    cosmetic bug. ``general_poll()`` returns ``response[0]``, the first byte
    of the reply. In the ordinary mode that byte IS the exception code; under
    real time event reporting the reply is Table 12.5's "address, FF, code,
    data..., CRC", so the first byte is the gaming machine's ADDRESS. On
    address 1 every heartbeat then reports a phantom "exception 0x01", and
    worse, it consumes one byte of a multi-byte frame and leaves the rest in
    the buffer for the next read to misframe.

    Found on an idle lab cabinet within a minute of the first --rte run: ten
    or eleven "exception 0x01 drained during the meter sweep: unknown/
    unassigned code 0x01" per cycle, followed by a burst of interleaved
    events whose codes were partly frame fragments. The cycle's own general
    poll had been converted; this one had not.
    """

    def note(code: int, fields: dict | None, source: str) -> None:
        """Queue the code, or act on it now if its deadline is short.

        THE DEFERRAL MISSES THE DEADLINE, and the lab proved it. A 0x67
        collected by a heartbeat used to wait for the whole sweep: 5.4 s on an
        ordinary cycle, 16.8 s on a Table C-7 cycle, and **69.4 s** during the
        startup interrogation measured on 2026-11-02. §15.12 gives thirty
        seconds, so the machine had returned the ticket long before we read
        LP 0x70 -- which is why four ticket_in_events rows that day say
        "exception 0x67 fired but LP 0x70 reported no ticket in escrow".

        §15.7 gives a system validation request only TEN seconds, so 0x57 is
        worse: a C-7 cycle alone overruns it, and a 0x57 that goes unanswered
        becomes a handpay.

        The objection to dispatching here was "issuing long polls from inside a
        long poll loop". SAS has no rule against that -- a sweep IS a sequence
        of long polls, and Table 3.1's implied-ACK concern is about REPEATED
        IDENTICAL polls, which these are not.
        """
        if state is not None:
            # Every code the heartbeat sees costs one poll slot, which is
            # the resource the drain probe is measuring. Counted here,
            # before the latency-critical branch, so both paths weigh the
            # same.
            state.events_seen_live += 1
        if int(code) in LATENCY_CRITICAL_EXCEPTIONS:
            try:
                _dispatch_exception(client, conn, now, int(code),
                                    source=source, state=state, fields=fields)
            except SASError:
                # Best-effort, exactly like the heartbeat itself: the sweep
                # must not abort because a ticket could not be answered.
                pass
            return
        seen.append((int(code), fields, source))

    def _drain_interleaved() -> None:
        """Anything that pre-empted a long poll arrived BEFORE whatever this
        heartbeat is about to read. Draining it first is what keeps row ids
        in arrival order -- see the inversion described above."""
        if not rte:
            return
        try:
            for event in client.drain_rte_events():
                note(int(event.code), event.fields or None, "rte-interleaved")
        except SASError:
            pass

    def heartbeat() -> None:
        heartbeat.count += 1
        _drain_interleaved()

        # THE WfA SLOT. This is the "next poll slot" the scheduling rule
        # names: we are between exchanges, nothing is in flight, and a
        # request serviced here cannot open a mid-poll gap.
        #
        # It sits AFTER the interleaved drain and is followed by another,
        # because the servicer may issue LP 0x4D and §12.3 lets an event
        # arrive in its place. Without the second drain those events would be
        # filed after this heartbeat's own and the ids would run backwards --
        # the same inversion that once produced twelve phantom "lost" game
        # starts in adjacent pairs.
        if request_servicer is not None:
            try:
                request_servicer()
            except sqlite3.Error:
                # An inbound request must never be able to stop the sweep.
                # The row stays claimed and the next visit picks it up.
                pass
            _drain_interleaved()
        # DRAIN TO EMPTY, not once.
        #
        # A heartbeat used to take exactly one general poll and return, so a
        # sweep alternated long poll / general poll blindly. That drains ONE
        # event per two slots while events can arrive faster, so under a
        # burst the queue falls further behind with every pair.
        #
        # The queue is what decides whether anything is lost, and the
        # inequality is:
        #
        #     sweep duration  <  queue depth / event rate
        #
        # At 300 ms, 60 long polls is 18 s of sweep. Against this cabinet's
        # measured 67-deep queue and a 3.2 events/s burst the limit is 20.9 s,
        # so it survived with 14% to spare. Against §2.2.1's guaranteed 20 the
        # limit is 6.2 s, and the same sweep is THREE TIMES OVER. That is the
        # floor risk, not a bench curiosity.
        #
        # Draining to empty removes the sweep's duration from the inequality
        # altogether: the queue can only grow by what arrives between two
        # checks, however long the whole sweep takes. A 73-poll sweep becomes
        # safe on a cabinet that guarantees only 20.
        #
        # THE CAP IS NOT OPTIONAL. A machine firing events continuously would
        # otherwise hold the sweep forever, and a meter row that never gets
        # written is worse than a late one. On the cap the sweep resumes and
        # the next heartbeat carries on draining -- nothing is dropped, it
        # just takes longer.
        drained = 0
        while drained < HEARTBEAT_DRAIN_CAP:
            try:
                if rte:
                    event = client.general_poll_rte()
                    # §12.6: silence is no activity, not a failure.
                    if event is None or int(event.code) == ExceptionCode.NONE:
                        # THE QUEUE IS EMPTY. This is the only moment a
                        # credit poll cannot cost an event, so it is the
                        # only moment one is issued. See
                        # _make_credit_sampler.
                        if credit_sampler is not None:
                            credit_sampler()
                        return
                    note(int(event.code), event.fields or None, "rte-heartbeat")
                else:
                    code = client.general_poll()
                    if code == ExceptionCode.NONE:
                        if credit_sampler is not None:
                            credit_sampler()
                        return
                    note(int(code), None, "heartbeat")
            except SASError:
                # A failed heartbeat is not this cycle's problem to solve --
                # the cycle's own general poll has its own retry path.
                # Draining is best-effort; the sweep must not abort over it.
                return
            drained += 1
            heartbeat.count += 1
        heartbeat.capped += 1

    # How many general polls this sweep actually spent. Read by the caller
    # for the meter_poll=.../N label: the heartbeats share the §2.3.3 budget
    # with the long polls, so a label that counts only long polls understates
    # what the cycle put on the wire.
    heartbeat.count = 0
    # How many heartbeats hit the drain cap. Non-zero means events were
    # arriving faster than one heartbeat could clear, which is the signal
    # that the machine is under a burst -- worth seeing rather than
    # inferring from cycle length afterwards.
    heartbeat.capped = 0
    return heartbeat


def _general_poll_with_retry(
    client, conn: sqlite3.Connection, now: str, *, max_attempts: int,
    sleep_fn=time.sleep, rte: bool = False
) -> tuple[int, int, dict | None]:
    """SAS delivers exactly one pending exception per poll (§2.2.1); if
    the host doesn't drain it before the next one arrives, the earlier
    exception is overwritten with no trail at all — a real event, gone,
    undetectable afterwards. Per the Decisions Annex D-09, "the mirror
    of ghost redemption": a failed or garbled general poll is therefore
    re-polled immediately here, not deferred to the next --interval
    cycle, for up to ``max_attempts`` consecutive tries — capped so one
    unresponsive machine can't consume the whole cycle budget chasing a
    dead link (D-09's own reasoning is about a round-robin across many
    machines; this tool only ever talks to one, but the underlying
    exception-overwrite risk is identical).

    "Immediate" is paced, not truly zero-delay: SAS 6.02 §2.3.3 forbids
    polling a single machine faster than once per 200 ms
    (SAS_MIN_POLL_INTERVAL_S) — a retry loop with no pacing would
    violate that floor whenever an attempt fails fast (a checksum
    error, say, rather than a timeout that already ran out the clock).
    Each attempt after the first waits out whatever's left of that
    200 ms window since the previous attempt started, never more.

    Returns (exception_code, attempt_number, rte_fields) on success.
    Raises the final SASError if every attempt fails. Every failed
    attempt is logged to poll_errors individually, numbered, so a flaky
    link shows up as a distinguishable run of attempts rather than one
    opaque failure.

    Under ``rte`` the poll is LP 0x0E's Table 12.5 message rather than a
    single byte, and silence means something different: 12.6 says a
    machine in real time event mode reports no activity by sending
    NOTHING, so a None here is the idle it looks like, not a timeout to
    retry. Retrying it would spend the whole attempt budget every quiet
    cycle. (The lab cabinet sends an explicit code-00 event anyway, in
    defiance of 12.6; both shapes land on ExceptionCode.NONE here.)
    """
    last_error: SASError | None = None
    attempt_started: float | None = None
    for attempt in range(1, max_attempts + 1):
        if attempt_started is not None:
            remaining = SAS_MIN_POLL_INTERVAL_S - (time.monotonic() - attempt_started)
            if remaining > 0:
                sleep_fn(remaining)
        attempt_started = time.monotonic()
        try:
            if not rte:
                return client.general_poll(), attempt, None
            event = client.general_poll_rte()
            if event is None:
                return int(ExceptionCode.NONE), attempt, None
            return int(event.code), attempt, (event.fields or None)
        except SASError as e:
            last_error = e
            conn.execute(
                "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
                (now, f"general_poll(attempt {attempt}/{max_attempts})", type(e).__name__, str(e)),
            )
            _safe_commit(conn, now)
    assert last_error is not None  # max_attempts >= 1 is enforced by argparse/callers
    raise last_error


def poll_and_log(
    client,
    conn: sqlite3.Connection,
    state: PollState,
    history: HistoryConfig,
    *,
    anomaly: bool = False,
    now_fn=utc_now,
    monotonic_fn=time.monotonic,
    db_size_warning_mb: int = 0,
    db_size_fn=_db_file_size_bytes,
    full_meter_sweep: bool = True,
    table_c7_sweep: bool = True,
    table_c7_interval: float = DEFAULT_TABLE_C7_INTERVAL,
    validation_meters_sweep: bool = True,
    heartbeat_every: int = 8,
    last_accepted_bill_poll: bool = True,
    interval: float | None = None,
    general_poll_retries: int = DEFAULT_GENERAL_POLL_RETRIES,
    pool_age_alert_hours: float = DEFAULT_POOL_AGE_ALERT_HOURS,
    retry_sleep_fn=time.sleep,
    rte: bool = False,
    credit_poll_every: int = 0,
    serve_host_requests: bool = False,
    comms_gap_seconds: float = DEFAULT_COMMS_GAP_SECONDS,
    lab_auto: "LabAutoConfig | None" = None,
    drain_priority: bool = False,
    drain_fast_poll: bool = False,
    drain_fast_poll_interval: float = SAS_FAST_POLL_INTERVAL_S,
    drain_fast_poll_fallback: float = 0.0,
) -> None:
    """Run one full cycle: a general poll (retried immediately, up to
    ``general_poll_retries`` consecutive attempts, on failure — see
    _general_poll_with_retry()'s docstring for why — dispatching to
    ticket-in/ticket-out capture on the relevant exception codes), a
    validation_pool age check (see _check_pool_age()), then the full
    meter poll (see _poll_all_meters()) — refreshing meters_current every
    cycle, and writing to meters_history according to ``history``'s
    mode/cadence/cap, or immediately (for the next ``history.burst_count``
    cycles) on a meter decrease, a failed poll, or an ``anomaly=True``
    signal from the caller. A failure in ANY meter poll aborts that
    cycle's entire meter write — see _poll_all_meters()'s docstring.
    ``db_size_warning_mb`` (0 = disabled) prints a warning every cycle
    the database file is at or above that size — an early signal ahead
    of an actual full-partition write failure, not a substitute for
    _safe_commit()'s own fault reporting when one happens anyway.
    ``pool_age_alert_hours`` (0 or negative = disabled) is the same kind
    of early, repeated, non-blocking signal for validation_pool.

    Every cycle's log line reports how long the meter poll itself took
    and how many exchanges that was (``meter_poll=X.XXXs/Nlp+Mgp``: long
    polls and heartbeat general polls, counted as sent rather than as
    enabled) — this is measured wall-clock time against whatever's on
    the other end of ``client``, not a theoretical figure, so it's the
    real answer to "does the meter sweep fit in --interval on this
    hardware." Pass ``interval`` (the caller's own poll-cycle interval,
    e.g. --interval) to also get a WARNING if the meter poll alone is at
    or above it — a sign this cycle's own meter sweep doesn't leave any
    slack for the general poll or the configured sleep, and
    --skip-full-meter-sweep, --skip-table-c7-sweep,
    --skip-validation-meters-sweep is worth
    considering. ``interval=None`` (the default) skips that comparison.
    """
    now = now_fn()

    if db_size_warning_mb:
        size = db_size_fn(conn)
        if size is not None and size >= db_size_warning_mb * 1024 * 1024:
            print(
                f"[{now}] WARNING: database file is {size / (1024 * 1024):.1f} MB, at or above "
                f"--db-size-warning-mb {db_size_warning_mb} — check available space on its partition. "
                "This tool never deletes ticket/event rows to free space; see the module docstring."
            )

    _check_pool_age(conn, now, max_age_hours=pool_age_alert_hours,
                    state=state, monotonic_fn=monotonic_fn)

    # LAB ONLY, and off unless the lab flags built a LabAutoConfig. Placed
    # here, before the general poll, on purpose: it then runs on every cycle
    # including ones whose meter sweep aborts, and a one-cycle lag against a
    # threshold measured in minutes is not worth a second code path. See the
    # block comment above LabAutoConfig for why these exist at all.
    if lab_auto is not None:
        try:
            _lab_auto_handpay_reset(
                client, conn, now, state,
                after_seconds=lab_auto.handpay_reset_after_s,
                monotonic_fn=monotonic_fn)
            _lab_auto_aft_credit(
                client, conn, now, state,
                amount_cents=lab_auto.aft_credit_cents,
                floor_cents=lab_auto.aft_floor_cents,
                pool_id=lab_auto.aft_pool_id,
                monotonic_fn=monotonic_fn)
        except SASError as e:
            # Never let a lab convenience take the soak down with it.
            conn.execute(
                "INSERT INTO poll_errors (occurred_at, poll_name, error_type, "
                "message) VALUES (?, ?, ?, ?)",
                (now, "lab_auto", type(e).__name__, str(e)))
            _safe_commit(conn, now)

    try:
        exception_code, attempt, rte_fields = _general_poll_with_retry(
            client, conn, now, max_attempts=general_poll_retries,
            sleep_fn=retry_sleep_fn, rte=rte,
        )
    except SASError as e:
        # A down link is measured, not just logged. One failed poll is a
        # missed byte; a run of them long enough for somebody to walk to
        # another cabinet is a different event entirely, and only the clock
        # tells them apart.
        poll_mono = monotonic_fn()
        if state.gap_started_mono is None:
            state.gap_started_mono = poll_mono
        state.consecutive_poll_failures += 1
        print(
            f"[{now}] general poll failed after {general_poll_retries} consecutive attempt(s): "
            f"{type(e).__name__}: {e}"
            f" (link down {poll_mono - state.gap_started_mono:.0f}s, "
            f"{state.consecutive_poll_failures} cycle(s))"
        )
    else:
        if state.gap_started_mono is not None:
            gap = monotonic_fn() - state.gap_started_mono
            print(f"[{now}] link restored after {gap:.0f}s and "
                  f"{state.consecutive_poll_failures} failed cycle(s)")
            if gap >= comms_gap_seconds:
                # Deferred, not run here: the interrogation needs the
                # heartbeat that only exists once the meter sweep is set up,
                # and running ~150 long polls from inside the general-poll
                # handler would be a sweep nested in a sweep.
                state.interrogation_pending = gap
            state.gap_started_mono = None
            state.consecutive_poll_failures = 0
        if attempt > 1:
            print(f"[{now}] general poll recovered on attempt {attempt}/{general_poll_retries}")
        _dispatch_exception(client, conn, now, exception_code, state=state,
                            source="rte" if rte else "cycle", fields=rte_fields)

    mono_now = monotonic_fn()

    # Is the Table C-7 sweep due this cycle? None means it has never run in
    # this process, which forces one -- a run that polls for an hour and
    # never captures C-7 because its first cycle was not "due" would be a
    # silly way to lose the data this cadence exists to collect.
    #
    # A skipped sweep leaves the c7_* columns NULL rather than carrying the
    # previous reading forward. NULL already means "not read this cycle"
    # everywhere else in this schema (§4.4 makes unsupported and unanswered
    # indistinguishable), and copying a stale value into a fresh row would
    # make a ten-minute-old meter indistinguishable from one read a second
    # ago. The latest_c7_meters view below is how you ask for the last real
    # sweep without caring which rows those were.
    # A FAILED RTE ENABLE MUST NOT BE PERMANENT, and it used to be.
    #
    # main() enabled real time event reporting once, at startup, and on
    # SASError printed "Continuing in the ordinary polling mode" and never
    # tried again. After the power cut of 2026-11-01 the gateway was back and
    # polling in about thirty seconds; the EGM takes minutes to boot. The
    # poller came up, found nothing answering, fell back, and would have run
    # the entire soak without the real time events it was started for --
    # announced in one line at startup and invisible afterwards.
    #
    # That is the ordinary case on a floor, not an unlucky one: a gateway
    # micro-PC boots faster than a cabinet, so every mains event produces it.
    # A supervisor does not help, because the process does not exit; it runs
    # happily in the wrong mode.
    #
    # One long poll per cycle, only while it is still failing -- and while it
    # is failing the machine is not answering anything else either.
    if state.rte_wanted and not state.rte_enabled:
        try:
            client.enable_real_time_events(True)
            state.rte_enabled = True
            print(f"[{now}] RTE ENABLED (LP 0x0E) on retry: the machine is "
                  "answering now. Events arrive as Table 12.5 messages from "
                  "here on; anything before this line was collected in the "
                  "ordinary single-byte mode.")
        except SASError:
            # Not logged per cycle: the general poll below reports the same
            # silence, and two lines a cycle for one dead link is noise.
            pass
    if state.rte_wanted:
        rte = state.rte_enabled

    c7_due = table_c7_sweep and (
        table_c7_interval <= 0
        or state.last_c7_sweep is None
        or (mono_now - state.last_c7_sweep) >= table_c7_interval
    )

    heartbeat_codes: list = []
    # Only the main sweep's heartbeat samples credits. The interrogation
    # heartbeats deliberately do not: an interrogation is already ~150 long
    # polls and the one thing it must not do is get longer.
    credit_sampler = (_make_credit_sampler(client, conn, credit_poll_every)
                      if credit_poll_every > 0 else None)
    request_servicer = (_make_request_servicer(client, conn, state)
                        if serve_host_requests else None)
    heartbeat = _make_heartbeat(client, conn, now, heartbeat_codes,
                                rte=rte, state=state,
                                credit_sampler=credit_sampler,
                                request_servicer=request_servicer)

    # DRAIN PRIORITY. Decided from the LAST cycle's measured rate, because
    # this cycle's is not known until its sweep has run -- and the sweep is
    # the thing being decided about.
    #
    # The 8 grouped polls are never deferred. They carry coin in, coin out
    # and games played, which every reconciliation window anchors to, and
    # they are 8 exchanges against the 64 that are deferred. A burst must
    # not leave the window with no meter sample at all; it may leave it
    # with a coarser one.
    draining = drain_priority and _should_drain(state)
    if draining != state.draining:
        print(f"[{now}] drain priority {'ON' if draining else 'OFF'}: "
              f"{state.last_event_rate:.2f} events/s against the "
              f"{(state.baseline_capacity or state.last_capacity):.1f}/s "
              f"BASELINE budget. "
              + ("Deferring the optional meter sweeps; coin in, coin out and "
                 "games played still read every cycle."
                 if draining else "Resuming the full sweep."))
    if draining != state.draining and drain_fast_poll:
        _apply_drain_poll_rate(client, state, now, want_fast=draining,
                               fast_interval=drain_fast_poll_interval,
                               fallback_interval=drain_fast_poll_fallback)
    state.draining = draining
    if draining:
        state.drain_cycles += 1

    try:
        values = _poll_all_meters(
            client,
            now=now,
            give_up_optional=(
                _live_drain_probe(
                    state, monotonic_fn(), monotonic_fn=monotonic_fn,
                    on_fire=((lambda: _apply_drain_poll_rate(
                        client, state, now, want_fast=True,
                        fast_interval=drain_fast_poll_interval,
                        fallback_interval=drain_fast_poll_fallback))
                        if drain_fast_poll else None))
                if drain_priority else None),
            heartbeat=heartbeat,
            heartbeat_every=heartbeat_every,
            full_sweep=full_meter_sweep and not draining,
            table_c7_sweep=c7_due and not draining,
            validation_meters_sweep=validation_meters_sweep and not draining,
            last_accepted_bill_poll=last_accepted_bill_poll and not draining,
        )
    except _MeterPollFailure as failure:
        meter_poll_elapsed = monotonic_fn() - mono_now
        # THE RATE MUST BE UPDATED HERE TOO.
        #
        # This path returns early, before the headroom block that records
        # last_event_rate / last_capacity. So on 2026-11-09T00:18 a burst
        # failed three sweeps in a row and _should_drain() kept reading the
        # last SUCCESSFUL cycle's 25% -- the condition that should trigger
        # drain mode was the condition preventing the trigger from being
        # computed. A pre-empted sweep is the strongest evidence of a burst
        # there is, and it was the one the decision could not see.
        _pace = getattr(getattr(client, "transport", None),
                        "_min_poll_interval", 0.0) or 0.0
        state.last_capacity = (1.0 / _pace) if _pace > 0 else 0.0
        if meter_poll_elapsed > 0:
            state.last_event_rate = max(
                state.events_seen_live, state.events_this_cycle
            ) / meter_poll_elapsed
        # A 24-REISSUE PRE-EMPTION IS A BURST. Act on it here, not next
        # cycle.
        #
        # Operator, 2026-11-09: "it moved almost 30 seconds after button
        # burst." The burst hit send_meters_10_through_15 -- one of the 8
        # GROUPED polls, which run first and carry no give_up_optional()
        # probe because they are never deferred. So the cycle died 20.4s
        # into that poll's reissues, before any probe point existed, and
        # drain mode waited for the next cycle's decision.
        #
        # SASPreempted after the full retry budget is not ambiguous: 24
        # real time events arrived in place of one reply. Entering drain
        # mode on it costs nothing if the burst is over -- the next cycle
        # measures a quiet machine and leaves again -- and saves ~30
        # seconds when it is not. It also makes the NEXT pre-emption's
        # reissues run at 40 ms instead of 300 ms, which is where most of
        # those 20.4 seconds went.
        if drain_priority and isinstance(failure.original, SASPreempted):
            if not state.draining:
                state.draining = True
                print(f"[{now}] drain priority ON (a long poll was "
                      f"pre-empted {failure.original.events_collected} "
                      f"time(s) — that is a burst, not a decision to defer "
                      f"to the next cycle).")
            if drain_fast_poll:
                _apply_drain_poll_rate(
                    client, state, now, want_fast=True,
                    fast_interval=drain_fast_poll_interval,
                    fallback_interval=drain_fast_poll_fallback)
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, failure.poll_name, type(failure.original).__name__, str(failure.original)),
        )
        _safe_commit(conn, now)
        state.burst_remaining = max(state.burst_remaining, history.burst_count)
        print(
            f"[{now}] meters poll failed after {meter_poll_elapsed:.3f}s ({failure.poll_name}): "
            f"{type(failure.original).__name__}: {failure.original}"
        )
        # DRAIN BEFORE RETURNING, which this did not do until 2026-11-06.
        #
        # A failed sweep used to return straight out of the cycle, past the
        # drain_rte_events() call near the end. The events were not lost --
        # the client holds them in its pending list until somebody drains it
        # -- but they sat there until the NEXT cycle and were written with
        # that cycle's timestamp, up to a full sweep later.
        #
        # The failure that skips the drain is a §12.3 pre-emption burst, and
        # a pre-emption burst is by definition the moment the machine has
        # the most queued. So the one cycle that collected the most events
        # was the one that filed them last and timed them worst, and the
        # operator's button-pressing at 21:02 is exactly that case: two
        # polls each burned all 24 reissues, and every event either of them
        # carried off the machine waited for the following cycle.
        #
        # Nothing here depends on that being the start/end gap's cause -- it
        # is not proven to be -- but a drain that runs only on the happy
        # path is wrong on its own terms.
        if rte:
            stranded = client.drain_rte_events()
            for event in stranded:
                _dispatch_exception(client, conn, event.arrived_at or now,
                                    int(event.code), state=state,
                                    source="rte-interleaved",
                                    fields=(event.fields or None))
            if stranded:
                print(f"[{now}] {len(stranded)} event(s) collected during the "
                      f"failed sweep were still filed")
        return

    # Only a cycle that actually READ C-7 consumes the cadence. Two ways a
    # due sweep does not happen, and both must leave the cadence alone:
    #
    # A FAILED POLL. On a flapping link the next cycle would be "not due",
    # and C-7 could go unread indefinitely while the log filled with
    # failures that each looked individually harmless. Handled by the early
    # return above, which never reaches this line.
    #
    # A DRAIN. The call site reads `c7_due and not draining` -- under drain
    # priority the sweep is deferred, not performed -- while this line read
    # `c7_due` alone and stamped it as done. C-7 then waited another FULL
    # interval: 300s on the floor unit. The operator's floor gateways run
    # --drain-priority permanently, because it is what keeps real time
    # events from being lost, so every busy spell silently cost a C-7 read
    # and the next one was 5 minutes behind a machine that was mid-play.
    # Worse, drains cluster: the cabinet is busiest exactly when C-7 keeps
    # coming due, so the starvation repeats rather than averaging out.
    #
    # THE RULE THIS FILE KEEPS RELEARNING, now three times (the "22polls"
    # label, the "60lp" label, and this): a cadence or a count must be
    # computed from THE SAME EXPRESSION THE CALL SITE USES, never from a
    # subset of its terms. `c7_due` is the decision to sweep; `c7_due and
    # not draining` is the sweep. Only the second one happened.
    if c7_due and not draining:
        state.last_c7_sweep = mono_now

    # A gap long enough for the cable to have moved. Run here, after the
    # sweep, so the heartbeat is available to keep the exception queue
    # draining through ~150 long polls of interrogation.
    if state.interrogation_pending is not None:
        gap = state.interrogation_pending
        state.interrogation_pending = None
        print(f"[{now}] link was down {gap:.0f}s (>= {comms_gap_seconds:.0f}s): "
              "interrogating the machine before trusting anything else")
        try:
            # The continuity chain cannot survive a gap: play carried on
            # while nobody was polling, so the next game start will show a
            # coin-in jump that is NOT a lost event. Forgetting the previous
            # reading is what stops this crying wolf after every reboot and
            # every pulled cable -- which is precisely the exemption the
            # operator stated: "unless gateway lost communication due to a
            # reboot or faulty sas cable."
            if state.last_game_start_coin_in is not None:
                print(f"[{now}] continuity chain reset across the comms gap: "
                      f"play during it is not a lost event")
            state.last_game_start_coin_in = None
            state.last_game_start_at = None
            interrogate(client, conn, state, now, reason="comms_restored",
                        gap_seconds=gap,
                        heartbeat=_make_heartbeat(client, conn, now,
                                                  heartbeat_codes, rte=rte,
                                                  state=state),
                        monotonic_fn=monotonic_fn)
        except SASError as e:
            conn.execute(
                "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) "
                "VALUES (?, ?, ?, ?)",
                (now, "interrogate(comms_restored)", type(e).__name__, str(e)))
            _safe_commit(conn, now)
            print(f"[{now}] interrogation failed: {type(e).__name__}: {e}")

    # Anything the heartbeats drained is handled now -- see _make_heartbeat()
    # for why it is deferred rather than acted on inside the sweep.
    #
    # AFTER the comms-restored interrogation above and BEFORE the options
    # re-read below, deliberately: a 0x3C found by a heartbeat in this sweep
    # should be acted on in this cycle, not the next one.
    for code, hb_fields, hb_source in heartbeat_codes:
        print(f"[{now}] exception 0x{code:02X} drained during the meter sweep: "
              f"{describe_exception(code)}")
        # Table A-1's own name for a handful of codes misleads a reader badly
        # enough to have cost this project days. The spec name is what gets
        # logged; this is what a person needs beside it.
        _clarify = clarify_exception(code)
        if _clarify:
            print(f"    ^ {_clarify}")
        _dispatch_exception(client, conn, now, code, source=hb_source,
                            state=state, fields=hb_fields)

    # Exception 0x3C said the operator changed options. Re-read the
    # configuration and find out WHAT, because the exception does not say.
    #
    # Deferred to here for the same reason the comms-restored interrogation
    # is: it is ~150 long polls, and the dispatcher that set the flag can
    # have been running from inside the sweep above.
    if state.options_change_pending is not None:
        seen_at = state.options_change_pending
        state.options_change_pending = None
        previous = conn.execute(
            "SELECT id FROM interrogations ORDER BY id DESC LIMIT 1").fetchone()
        print(f"[{now}] re-reading the configuration after the 0x3C seen at "
              f"{seen_at}")
        try:
            interrogate(client, conn, state, now, reason="options_changed",
                        heartbeat=_make_heartbeat(client, conn, now,
                                                  heartbeat_codes, rte=rte,
                                                  state=state),
                        monotonic_fn=monotonic_fn)
        except SASError as e:
            conn.execute(
                "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) "
                "VALUES (?, ?, ?, ?)",
                (now, "interrogate(options_changed)", type(e).__name__, str(e)))
            _safe_commit(conn, now)
            print(f"[{now}] configuration re-read failed: {type(e).__name__}: {e}")
        else:
            current = conn.execute(
                "SELECT id FROM interrogations ORDER BY id DESC LIMIT 1").fetchone()
            if previous is None or current is None or previous[0] == current[0]:
                print(f"[{now}] 0x3C: nothing to compare against -- this is the "
                      "first configuration reading in this database.")
            else:
                changes = game_config_diff(conn, previous[0], current[0])
                if not changes:
                    # Not nothing: 0x3C covers options this tool cannot read
                    # (bill acceptor settings, attendant menus). Saying so is
                    # better than implying the exception was spurious.
                    note = ("0x3C with no change in any game configuration "
                            "field this tool reads -- the option that changed "
                            "is one LP 0x53/0xB5 does not report")
                    print(f"[{now}] {note}")
                    conn.execute("UPDATE interrogations SET note = ? WHERE id = ?",
                                 (note, current[0]))
                    _safe_commit(conn, now)
                else:
                    detail = "; ".join(
                        f"game {g}: {f} {a!r} -> {b!r}" for g, f, a, b in changes)
                    print(f"[{now}] 0x3C changed: {detail}")
                    conn.execute("UPDATE interrogations SET note = ? WHERE id = ?",
                                 (f"0x3C: {detail}", current[0]))
                    material = [c for c in changes
                                if c[1] in EPOCH_WORTHY_CONFIG_FIELDS]
                    if material:
                        why = "; ".join(
                            f"game {g}: {f} {a!r} -> {b!r}" for g, f, a, b in material)
                        epoch_id = open_epoch(
                            conn, now, "options_changed", state.identity or {},
                            f"operator changed options (0x3C seen {seen_at}): {why}")
                        state.epoch_id = epoch_id
                        print(f"[{now}] EPOCH {epoch_id}: {why}. Meter readings "
                              "either side of this boundary are not comparable "
                              "-- SAS counts credits, so a denomination change "
                              "rescales every figure in the database.")
                    _safe_commit(conn, now)

    meter_poll_elapsed = monotonic_fn() - mono_now
    # What this cycle actually put on the wire, not what the flags allow.
    #
    # This used to read ``table_c7_sweep`` -- the flag saying C-7 sweeps are
    # enabled at all -- where it meant ``c7_due``, the decision this cycle
    # made. With --table-c7-interval 300 that is wrong on 149 cycles out of
    # every 150: the label read "22polls" for a cycle that had made 9, and
    # the over-count landed in the one message a human reads when deciding
    # whether the sweep is too heavy. A 2.4x exaggeration there argues for
    # cutting a sweep that was never the problem.
    #
    # AND THE SAME MISTAKE RECURRED on 2026-11-08 with drain priority: the
    # line read "60lp" for a cycle that made 8, because `draining` was
    # missing from every term below while the call site had it. 8 long
    # polls plus 8 heartbeats at the 0.3 s floor is 4.8 s, and the cycle
    # took 4.796 s -- the arithmetic was the only thing that said so. The
    # rule this keeps failing: **this count must be computed from the same
    # expressions the call site uses, never from the flags they are
    # derived from.**
    meter_poll_count = (
        GROUPED_METER_POLL_COUNT
        + (1 if last_accepted_bill_poll and not draining else 0)
        + (len(SINGLE_METER_COLUMNS) if full_meter_sweep and not draining else 0)
        + (TABLE_C7_CHUNK_COUNT if c7_due and not draining else 0)
        + (len(VALIDATION_METER_TYPES)
           if validation_meters_sweep and not draining else 0)
    )
    # The heartbeats are general polls, and §2.3.3 counts general polls and
    # long polls against ONE budget. Reporting them separately keeps both
    # facts visible: how much work the sweep is, and how much of the 200 ms
    # floor the whole cycle consumes.
    heartbeat_count = heartbeat.count
    # THE COST, ON EVERY CYCLE LINE, because a knob whose price is invisible
    # gets left on. `supp` is the headline: heartbeats that found the queue
    # NOT empty, where the sampler stood down. A burst drives it to the whole
    # count, which is the design working.
    if credit_sampler is None:
        credit_note = ""
    else:
        due = credit_sampler.beats // max(1, credit_poll_every)
        supp = due - (credit_sampler.taken + credit_sampler.preempted
                      + credit_sampler.failed)
        credit_note = (f" credits={credit_sampler.taken}"
                       f"/{credit_sampler.beats}gp")
        if credit_sampler.preempted:
            credit_note += f" preempted={credit_sampler.preempted}"
        if credit_sampler.failed:
            credit_note += f" failed={credit_sampler.failed}"
        if supp > 0:
            credit_note += f" stood-down={supp}"
    # Decide which epoch these readings belong to BEFORE writing them, so a
    # clear or a swap never lands rows in the epoch it ended.
    #
    # Both exception dispatchers run above this, so an announcement seen in
    # this cycle's general poll or drained by a heartbeat mid-sweep is acted
    # on here, with this cycle's meters. When the announcement arrives DURING
    # the sweep those meters are a mix of before and after, which no ordering
    # can fix -- so they go to the new epoch, because under-crediting the old
    # history is recoverable and contaminating it is not. burst_remaining is
    # raised at the same time, so the next, unambiguous cycle is sampled
    # immediately rather than at the usual history interval.
    try:
        maintain_epoch(conn, client, state, values, now)
    except Exception as e:  # noqa: BLE001
        # Continuity bookkeeping must never be the reason a gateway stops
        # logging. A missed epoch boundary is recoverable at the CMS; a poller
        # that died on a floor at 3am is not.
        print(f"[{now}] WARNING: epoch bookkeeping failed ({type(e).__name__}: "
              f"{e}); continuing to log into epoch {state.epoch_id}.",
              file=sys.stderr)

    columns = ", ".join(ALL_METER_FIELDS)
    qmarks = ", ".join("?" for _ in ALL_METER_FIELDS)
    bind = tuple(values[f] for f in ALL_METER_FIELDS)

    conn.execute(
        f"INSERT OR REPLACE INTO meters_current (id, polled_at, epoch_id, {columns}) "
        f"VALUES (1, ?, ?, {qmarks})",
        (now, state.epoch_id, *bind),
    )

    decreased = state.last_meters is not None and any(
        values[f] is not None and state.last_meters.get(f) is not None and values[f] < state.last_meters[f]
        for f in DECREASE_CHECK_FIELDS
    )
    if decreased or anomaly:
        state.burst_remaining = history.burst_count

    if history.mode == "append":
        write_history = True
    elif state.burst_remaining > 0:
        write_history = True
        state.burst_remaining -= 1
    else:
        write_history = (
            state.last_history_write is None
            or (mono_now - state.last_history_write) >= history.interval
        )

    if write_history:
        conn.execute(
            f"INSERT INTO meters_history (polled_at, epoch_id, {columns}) "
            f"VALUES (?, ?, {qmarks})",
            (now, state.epoch_id, *bind),
        )
        state.last_history_write = mono_now
        if history.mode == "ring" and history.cap:
            conn.execute(
                "DELETE FROM meters_history WHERE id NOT IN "
                "(SELECT id FROM meters_history ORDER BY id DESC LIMIT ?)",
                (history.cap,),
            )

    _safe_commit(conn, now)
    state.last_meters = values

    # Events that arrived in place of a LONG poll's response rather than a
    # general poll's. Section 12.3 allows a machine in real time event mode
    # to pre-empt any long poll reply with an event; the client reissues the
    # long poll and keeps the event instead of discarding it, and this is
    # where those get recorded.
    #
    # Draining EVERY cycle is not optional. The client's pending list has no
    # cap, so a caller that enables RTE and never drains grows it without
    # bound for as long as the run lasts -- which on a multi-hour soak with a
    # ~70-long-poll sweep is the one memory leak this tool could have.
    if rte:
        interleaved = client.drain_rte_events()
        for event in interleaved:
            # event.arrived_at, NOT `now`.
            #
            # These are drained once per cycle, so `now` is the end of the
            # sweep and is IDENTICAL for every event in the batch. Stamping
            # forty-odd events with one time throws away when each landed,
            # and that is the only thing that can distinguish a poller which
            # stalled for a minute from one which was sweeping and
            # collecting the whole time.
            #
            # It is not a cosmetic difference. A queue-safety check read "no
            # event arrived inside this 58-second gap" off cycle-stamped
            # rows and reported six stalls during a soak that was running
            # perfectly -- the absence was guaranteed by the stamping, not
            # by anything the gateway did.
            _dispatch_exception(client, conn, event.arrived_at or now,
                                int(event.code), state=state,
                                source="rte-interleaved",
                                fields=(event.fields or None))
        if interleaved:
            print(f"[{now}] {len(interleaved)} event(s) arrived interleaved with "
                  f"long polls: "
                  f"{', '.join(f'0x{e.code:02X}' for e in interleaved)}")

    # HEADROOM, not occupancy.
    #
    # "42 events per cycle against a queue of 67" was the wrong measure and
    # this project used it for days: §12.3 drains the queue on EVERY message,
    # so a cycle's event count says nothing about depth. What matters is the
    # RATE against what §2.3.3's pacing allows us to collect:
    #
    #     capacity = 1 / min_poll_interval     events per second
    #
    # On 2026-11-06 the operator was at 3.2 events/s against a capacity of
    # 3.3 -- 97% -- and nothing said so. Autoplay alone is 25%; sixty
    # seconds of pressing CHANGE and CASHOUT took it to the ceiling, and the
    # queue built because a stalled long poll is dead air where nothing is
    # collected.
    # Read off the transport rather than taken as an argument: the pacing
    # floor is enforced there, on every write, and a copy passed down here
    # could drift from the one actually in force. The thing being reported
    # must be the thing being obeyed.
    pace = getattr(getattr(client, "transport", None),
                   "_min_poll_interval", 0.0) or 0.0
    capacity = (1.0 / pace) if pace > 0 else 0.0
    collected = state.events_this_cycle
    rate = (collected / meter_poll_elapsed) if meter_poll_elapsed > 0 else 0.0
    used = (100.0 * rate / capacity) if capacity else 0.0
    headroom = f"{rate:.2f}/{capacity:.1f}ev/s={used:.0f}%"
    # What _should_drain() reads on the next cycle.
    state.last_event_rate, state.last_capacity = rate, capacity

    # WHAT CAN ACTUALLY BE MEASURED, AND WHAT CANNOT.
    #
    # This line has been wrong twice, in two different ways, and the second
    # is worth keeping written down because it was subtler and it shipped.
    #
    # FIRST: warned when the sweep ran longer than --interval. That is the
    # sleep between cycles and says nothing about whether events survive,
    # so it fired on every full sweep and became background. The operator
    # objected, correctly.
    #
    # SECOND (2026-11-09, mine): warned when the sweep ran longer than
    # `queue_depth / rate`, calling that "the time the queue survives".
    # **The arithmetic does not hold.** `rate` is events_this_cycle /
    # elapsed -- the rate this poller COLLECTED events. A collected event
    # is not in the queue. So it divided the queue depth by the rate the
    # queue is being EMPTIED and called the answer a time-to-overflow. It
    # fired at 00:41:43 on a cycle collecting 1.66 events/s while making
    # 102 general polls in 59.7s -- draining 1.71/s, slightly faster than
    # it collected, with the queue stable and nothing lost.
    #
    # THE ARRIVAL RATE IS NOT OBSERVABLE. A host sees what it collected and
    # what the cabinet reports losing. It never sees what the cabinet
    # produced. Any warning resting on arrival rate rests on a number we
    # do not have, however reasonable the formula looks.
    #
    # So this warns on the two things that ARE measured: evidence of loss
    # that already happened, and saturation of the poll budget (the EVENT
    # HEADROOM line below, which was right all along).
    lost_now = state.overflows_this_cycle + state.gaps_this_cycle
    if lost_now:
        print(
            f"[{now}] WARNING: EVENTS WERE LOST IN THIS CYCLE — "
            f"{state.overflows_this_cycle} buffer overflow(s) reported by "
            f"the cabinet, {state.gaps_this_cycle} stream gap(s) from "
            f"§12.5.3's own arithmetic. The sweep took "
            f"{meter_poll_elapsed:.1f}s across {meter_poll_count} long "
            f"polls and {heartbeat_count} general polls, collecting "
            f"{rate:.2f} of a {capacity:.1f}/s budget. Shorten the sweep: "
            f"--drain-priority (defers the optional polls automatically "
            f"under load), --skip-validation-meters-sweep, "
            f"--table-c7-interval 300, or --skip-full-meter-sweep. A larger "
            f"--interval is NOT the remedy — it is the sleep between "
            f"cycles, and {SAS_MAX_POLL_INTERVAL_S}s is the §2.3.3 ceiling."
        )
    elif interval is not None and meter_poll_elapsed >= interval:
        # A long sweep that loses nothing is a COST, not a fault: it buys
        # 154 Table C-7 meters and 39 single ones. Said once, quietly.
        print(f"[{now}] note: sweep {meter_poll_elapsed:.1f}s over "
              f"--interval {interval}s, collecting {rate:.2f}/{capacity:.1f} "
              f"events/s with nothing reported lost")

    if used >= 70.0:
        print(f"[{now}] ** EVENT HEADROOM {used:.0f}% -- {rate:.2f} events/s "
              f"against the {capacity:.1f}/s that §2.3.3 pacing allows us to "
              f"collect. Above this the queue builds, and a stalled long poll "
              f"is dead air where nothing drains. Shorten the sweep or widen "
              f"--min-poll-interval's budget.")
    state.events_this_cycle = 0
    state.events_seen_live = 0
    state.overflows_this_cycle = 0
    state.gaps_this_cycle = 0

    note = "history written" if write_history else "history skipped (cadence)"
    if decreased:
        note += ", meter decrease detected"
    elif anomaly:
        note += ", anomaly signaled"
    print(
        f"[{now}] {headroom} coin_in={values['total_coin_in']} coin_out={values['total_coin_out']} "
        f"games_played={values['games_played']} ticket_in_cashable_cents={values['ticket_in_cashable_cents']} "
        f"ticket_out_cashable_cents={values['ticket_out_cashable_cents']} "
        f"meter_poll={meter_poll_elapsed:.3f}s/{meter_poll_count}lp+{heartbeat_count}gp"
        f"{credit_note} — {note}"
    )


def _sas_poll_interval(value: str) -> float:
    """argparse type= for --interval: SAS 6.02 §2.3.3 ("Polling Rate")
    is explicit that a host "may not" poll a single machine faster than
    once per 200 ms, nor slower than once per 5000 ms — a protocol
    requirement, not a tuning preference. Enforced here rather than
    just documented, because a --interval outside this range doesn't
    fail loudly on its own: it risks the EGM misbehaving (or, on the
    slow side, the machine's own link-down detection tripping) in a way
    that would look like a bug in whatever's being tested against this
    tool, not a misconfigured poll rate.
    """
    parsed = float(value)
    if not SAS_MIN_POLL_INTERVAL_S <= parsed <= SAS_MAX_POLL_INTERVAL_S:
        raise argparse.ArgumentTypeError(
            f"--interval must be between {SAS_MIN_POLL_INTERVAL_S}s and {SAS_MAX_POLL_INTERVAL_S}s "
            f"(SAS 6.02 §2.3.3's own polling-rate bounds for a single machine), got {parsed}s"
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """The command line, built separately so it can be inspected without
    running the poller.

    Split out on 2026-11-10 so a test could format every help string. They
    had been unformattable for a day: a drain-priority f-string produced a
    literal "50%% of", argparse read "%% o" as a conversion and `--help`
    raised TypeError for the whole tool. A help string nobody can print is
    worse than no help string, and nothing could have caught it while the
    parser existed only inside main().
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", help="gateway .ini file (see saspy/config.py or MANUAL.md)")
    parser.add_argument("--db", default="gateway.sqlite3", help="SQLite file to write to")
    parser.add_argument(
        "--interval",
        type=_sas_poll_interval,
        default=SAS_MAX_POLL_INTERVAL_S,
        help=f"seconds between poll cycles ({SAS_MIN_POLL_INTERVAL_S}-{SAS_MAX_POLL_INTERVAL_S}, "
        "SAS 6.02 §2.3.3's own bounds for polling a single machine)",
    )
    parser.add_argument("--cycles", type=int, default=0, help="stop after N cycles (default: run until Ctrl-C)")
    parser.add_argument(
        "--mode",
        choices=("ring", "append"),
        default="ring",
        help="'ring' (default): capped meters_history at a decoupled write cadence — the flash-safe default. "
        "'append': unbounded meters_history written every cycle — the original lab/stress-testing behavior. "
        "Ticket tables are always deduplicated, not capped — see the module docstring.",
    )
    parser.add_argument(
        "--history-cap",
        type=int,
        default=DEFAULT_HISTORY_CAP,
        help="max rows kept in meters_history in ring mode (default: %(default)s); ignored in append mode",
    )
    parser.add_argument(
        "--history-interval",
        type=float,
        default=DEFAULT_HISTORY_INTERVAL,
        help="minimum seconds between meters_history writes in ring mode, outside a burst "
        "(default: %(default)s); ignored in append mode",
    )
    parser.add_argument(
        "--burst-count",
        type=int,
        default=DEFAULT_BURST_COUNT,
        help="consecutive polls logged at full resolution after a meter decrease or failed poll "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--skip-ticket-out-backfill",
        action="store_true",
        help="skip the one-time startup read of the full ticket-out buffer (indices 1-31)",
    )
    parser.add_argument(
        "--seed-validation-pool",
        type=int,
        default=0,
        metavar="N",
        help="top up validation_pool to at least N 'available' rows with random test numbers "
        "(default: 0, don't seed — hand-insert real numbers yourself if you have them)",
    )
    parser.add_argument(
        "--db-size-warning-mb",
        type=int,
        default=0,
        metavar="MB",
        help="print a warning every cycle the database file is at or above this size, as an early "
        "signal before a full partition actually fails a write (default: 0, disabled)",
    )
    parser.add_argument(
        "--skip-full-meter-sweep",
        action="store_true",
        help=f"skip the ~{len(SINGLE_METER_COLUMNS)} individual single-meter long polls each cycle "
        "(sm_* columns are left NULL) — lighter per-cycle wire traffic, for hardware where that "
        "much extra polling isn't affordable. The six core meters, eight ticket meters, and the "
        "other grouped meter polls (LP 0x18/0x19/0x1C/0x1E/0x2D/0x4F) still run either way.",
    )
    parser.add_argument(
        "--skip-table-c7-sweep",
        action="store_true",
        help=f"skip the {len(TABLE_C7_EXTENDED_COLUMNS)} extra meters read via LP 0x6F in "
        f"{TABLE_C7_CHUNK_COUNT} chunked exchanges (c7_* columns are left NULL) — nearly the rest "
        "of Table C-7 beyond the core/ticket meters (per-denomination bill counts, AFT transfer "
        "meters, SAS-validation-specific meters, and more). Independent of "
        "--skip-full-meter-sweep: this poll is far more wire-efficient per meter (12 meters per "
        "exchange, self-describing size) than the single-meter sweep, so it's worth keeping on "
        "even where that one isn't affordable.",
    )
    parser.add_argument(
        "--table-c7-interval",
        type=float,
        default=DEFAULT_TABLE_C7_INTERVAL,
        metavar="SECONDS",
        help=f"how often to run the Table C-7 sweep (default {DEFAULT_TABLE_C7_INTERVAL:.0f}s; "
        "0 = every cycle). C-7's extended meters are cumulative lifetime counters, so a "
        "cadence costs nothing in resolution and keeps the cycle inside its interval. "
        "Ignored when --skip-table-c7-sweep is given",
    )
    parser.add_argument(
        "--no-drain-priority",
        dest="drain_priority",
        action="store_false",
        help="turn OFF the deferral of optional meter sweeps under load. "
             "Only for measuring what the deferral is worth, by running a "
             "window without it. A floor gateway should not use this.",
    )
    parser.add_argument(
        "--drain-priority",
        dest="drain_priority",
        action="store_true",
        default=True,
        help="when the cabinet produces events faster than the sweep leaves "
             # argparse runs help through %-formatting, so every literal
             # percent sign must be doubled -- including the ones an
             # f-string computes. ".0%" produces a bare "%", which argparse
             # then read as a conversion: "50% of" parsed as "% o" and
             # raised "%o format: an integer is required, not dict". That
             # broke --help for the WHOLE tool, not just this flag, from
             # 2026-11-09 until it was found on 2026-11-10.
             f"room to collect ({DRAIN_ENTER_FRACTION * 100:.0f}%% of the "
             f"§2.3.3 budget), defer the optional meter sweeps and spend "
             f"the time on general polls instead; resume below "
             f"{DRAIN_LEAVE_FRACTION * 100:.0f}%%. Coin in, coin out and games "
             "played are still read every cycle -- only the 39 single "
             "meters, 13 Table C-7 chunks and 12 validation polls are "
             "deferred, and those are cumulative so nothing is lost by "
             "reading them later. Measured on 2026-11-08: a button burst "
             "put 43%% of poll slots into long polls that drain nothing, "
             "collecting 1.6 events/s against a 3.3/s budget while the "
             "cabinet produced 11.5/s. **ON BY DEFAULT since 2026-11-11**, "
             "at the operator's instruction: \"drain priority must be on by "
             "default, not only during soak. It really helps losing less of "
             "rte.\" Kept as an explicit flag so a unit file can say so out "
             "loud; --no-drain-priority turns it off",
    )
    parser.add_argument(
        "--drain-fast-poll",
        action="store_true",
        help="while --drain-priority is ON, poll at the machine's "
             f"advertised {SAS_FAST_POLL_INTERVAL_S*1000:.0f} ms rate "
             "instead of --min-poll-interval, and go back the moment it "
             "ends. §12.2 allows the rate to be increased under real time "
             "events; Table 7.14e bit 0 is where the machine says it "
             "supports it, and a machine that has not said so is never "
             "polled fast. One poll drains one event, so this raises the "
             "drain ceiling from 3.3/s to 25/s — above the 11.5/s a button "
             "burst produced on the lab cabinet. Requires --drain-priority. "
             "OFF by default: it changes what goes on the wire",
    )
    parser.add_argument(
        "--drain-fast-poll-interval",
        type=float,
        default=SAS_FAST_POLL_INTERVAL_S,
        metavar="SECONDS",
        help=f"the rate --drain-fast-poll uses (default "
             f"{SAS_FAST_POLL_INTERVAL_S}). Clamped at "
             f"{SAS_FAST_POLL_INTERVAL_S} — §12.2 names that number, and "
             "this project has polled below it once and recorded it as a bug",
    )
    parser.add_argument(
        "--drain-fast-poll-fallback",
        type=float,
        default=0.10,
        metavar="SECONDS",
        help="the rate to use while draining on a machine that has NOT "
             "advertised Table 7.14e bit 0 (default 0.10). §4.4 lets a "
             "machine ignore a long poll it does not implement, so silence "
             "is not a refusal — it is no answer. 0 disables the fallback "
             "and such a machine stays at --min-poll-interval",
    )
    parser.add_argument(
        "--skip-validation-meters-sweep",
        action="store_true",
        help=f"skip the {len(VALIDATION_METER_TYPES)} LP 0x50 exchanges, one per validation type "
        "(lp50_* columns are left NULL). These are explicitly redundant with the Table C-7 "
        "validation meters (codes 0x80+) that --skip-table-c7-sweep already covers -- deliberate "
        "cross-check, not waste, but LP 0x50 has no batching (unlike LP 6F), so it's the priciest "
        "of the three sweeps per meter actually obtained.",
    )
    parser.add_argument(
        "--skip-last-accepted-bill-poll",
        action="store_true",
        help="skip LP 0x48 (lp48_* columns are left NULL). Its own flag, not "
        "--skip-full-meter-sweep's: unlike every other meter poll here, §7.11 explicitly warns "
        "some older gaming machines don't support LP 0x48 at all, and that failure is a genuine "
        "SASError on the whole exchange (not a soft per-field skip like LP 0x6F's), which would "
        "otherwise abort every cycle's row write on such a machine.",
    )
    parser.add_argument(
        "--lab-ticket-authority",
        metavar="PATH",
        default=None,
        help="LAB ONLY. Answer ticket-in (exception 0x67 / LP 0x70 / LP 0x71) from "
        "this file of validation numbers, instead of only recording the insertion. "
        "Without it this tool never calls redeem_ticket() and the machine returns "
        "every ticket after its own 30-second timeout, which is the correct default "
        "for a gateway. The file is the authorisation, not the flag: it binds to one "
        "cabinet by serial and the run refuses any other. Same format, same expiry "
        "date and same lookup as examples/ticket_lab_authority.py, imported from it "
        "rather than copied. A gateway that decides what tickets are worth has no "
        "place on a floor -- delete both before one goes near one.",
    )
    parser.add_argument(
        "--decline-aft-cashout",
        action="store_true",
        help="answer §8.7's AFT host-cashout request (0x6A/0x6B) with an "
        "all-zeros LP 0x72, which the spec defines as 'cash out by whatever "
        "other means are available'. MOVES NO MONEY -- every amount is zero, "
        "so it is a decline, not a transfer. Measured on this cabinet: "
        "cashout is a three-stage ladder and stage 1 burns ~8s before the "
        "ticket path is even tried, on every cashout, with a full validation "
        "pool. Declining skips it. OFF BY DEFAULT and PER-MODEL: on a "
        "cabinet with no printer 'other means' is an attendant, so there "
        "this makes a handpay arrive sooner rather than a ticket.")
    parser.add_argument(
        "--lab-auto-handpay-reset",
        type=float,
        metavar="SECONDS",
        default=0.0,
        help="LAB ONLY, REQUIRES --lab-ticket-authority. Send LP 0x94 (remote "
        "handpay reset) when a lockup has gone this many seconds with nobody "
        "clearing it, so an unattended overnight soak does not stop at the "
        f"first jackpot. Minimum {LAB_AUTO_HANDPAY_MIN_SECONDS:.0f}s. On a "
        "floor this records a jackpot as PAID when nobody paid it, and the "
        "meters will agree with the gateway because both are wrong the same "
        "way. §7.9 also means the financial outcome is ambiguous: with W2-G "
        "Reset To Credit Meter enabled the money returns as credits, "
        "otherwise it is recorded as handed to a player, and reset code 0x00 "
        "comes back either way. Every reset is written to lab_auto_actions.",
    )
    parser.add_argument(
        "--lab-auto-aft-credit",
        type=float,
        metavar="AMOUNT",
        default=0.0,
        help="LAB ONLY, REQUIRES --lab-ticket-authority. Push this many "
        "dollars of cashable credit by AFT whenever the cabinet's balance "
        "falls to --lab-aft-credit-floor or below, so a soak keeps spinning "
        "after it cashes out or a handpay clears without returning credits. "
        f"Hard ceilings: {LAB_AUTO_AFT_MAX_PUSHES} pushes per run, "
        f"{LAB_AUTO_AFT_MAX_TOTAL_CENTS / 100:,.0f} total, one every "
        f"{LAB_AUTO_AFT_MIN_INTERVAL_S:.0f}s. Balances are read with LP 0x74 "
        "rather than a meter column, and a FAILED read never counts as zero "
        "credits -- that distinction is what keeps this from being a money "
        "printer with a cron job. Every push is written to lab_auto_actions.",
    )
    parser.add_argument(
        "--lab-aft-credit-floor",
        type=float,
        metavar="AMOUNT",
        default=1.0,
        help="The balance at or below which --lab-auto-aft-credit tops up. "
        "Default 1.00.",
    )
    parser.add_argument(
        "--ticket-authority-unverified-serial",
        action="store_true",
        help="Run --lab-ticket-authority against a cabinet that will not report "
        "its serial number. §4.4 lets a machine ignore LP 0x54, and on such a "
        "cabinet the ticket file's serial binding cannot be checked at all -- so "
        "the default is to refuse, because an unenforceable binding is not a "
        "binding. This says the operator is at the machine and knows which one it "
        "is. It does NOT weaken the mismatch check: a cabinet that reports the "
        "WRONG serial is still refused, flag or no flag.",
    )
    parser.add_argument(
        "--min-poll-interval",
        type=float,
        default=0.2,
        metavar="SECONDS",
        help="§2.3.3's floor, enforced between EVERY message to the machine -- "
        "general polls and long polls together, since the specification counts "
        "them against one budget: 'The host may not issue general polls or long "
        "polls to any single gaming machine at a rate faster than once per 200 "
        "ms.' (default: %(default)s). §12.2 permits 40 ms while real time events "
        "are enabled, which is a permission and not a target: §12.3 delivers at "
        "most ONE event per poll, so 200 ms already allows five a second against "
        "a measured play rate near 0.85. Raising it is the interesting direction "
        "-- the ceiling is 5000 ms and the general poll must stay inside it, so "
        "this times (--heartbeat-every + 1) has to remain under 5 s. 0 disables "
        "pacing and is for simulated ports only.",
    )
    parser.add_argument(
        "--serve-host-requests",
        action="store_true",
        help="serve inbound WfA pulls from the host_requests table. A server "
        "INSERTs a row naming a validation number and polls it for an "
        "answer; the poller services it at a poll-slot boundary, which is "
        "where the scheduling rule puts it -- an on-demand pull takes the "
        "NEXT slot and never interrupts an in-flight poll. Most requests "
        "are answered from ticket_out_history at zero poll cost; only a "
        "ticket this gateway never saw print falls back to walking LP 0x4D, "
        "and that walk is capped per visit, stops at the request's "
        "deadline, and records what it spent. Off by default: it is the "
        "only inbound command channel this gateway has, and a gateway "
        "nobody is asking should not be listening.",
    )
    parser.add_argument(
        "--credit-poll-every",
        type=int,
        default=0,
        metavar="N",
        help="read the current-credits meter (LP 0x1A) every N heartbeats, "
        "ON AN EMPTY QUEUE ONLY (default: 0, off). This feeds detector 7 in "
        "rte_reconcile.py -- the credit ledger, credits before minus wagers "
        "plus wins equals credits after -- which is the only check that "
        "scores an individual game END. Its resolution is set by how often "
        "the gauge is read: once per cycle today, about 25 games, which is "
        "coarser than a one-game fault. "
        "THE COST IS BOUNDED BY CONSTRUCTION, not by this number. The poll "
        "is issued only in the moment a heartbeat has just drained the queue "
        "to EMPTY, so there was no event for that slot to carry. Under a "
        "button burst the queue is never empty, the heartbeat hits its drain "
        "cap instead, and the sampler does not run at all -- it gets out of "
        "the way exactly when event collection matters most, and resumes by "
        "itself afterwards. Try 8 on a cabinet playing 1,500 games/hour: "
        "that is roughly one reading every few seconds against a game every "
        "2.4s. Measure before trusting it; this project has been wrong about "
        "polling cost from first principles before.",
    )
    parser.add_argument(
        "--heartbeat-every",
        type=int,
        default=8,
        metavar="N",
        help="drain the exception queue with a general poll every N long-poll "
        "exchanges during the meter sweep (default: 8). SAS delivers ONE "
        "exception per general poll, so this sets the drain rate, and the "
        "queue overflows when a machine produces exceptions faster than it. "
        "During active play each game is at least two (0x7E started, 0x7F "
        "ended), so a player spinning every two seconds needs better than one "
        "drain per second just to keep level: a 60-exchange sweep at the "
        "default drains eight times in roughly five seconds, which is thin, "
        "and a 73-exchange sweep with the Table C-7 sweep on is thinner. Lower "
        "it to 2 or 3 on a machine that is being played; 0x70 (exception "
        "buffer overflow) in machine_events is the symptom that it is too high.",
    )
    parser.add_argument(
        "--general-poll-retries",
        type=int,
        default=DEFAULT_GENERAL_POLL_RETRIES,
        metavar="N",
        help="consecutive immediate re-poll attempts on a failed general poll, before waiting for the "
        "next --interval cycle (default: %(default)s). A queued SAS exception can be silently "
        "overwritten by the next one before it's ever read if the host doesn't drain fast enough "
        "(Decisions Annex D-09) — this re-polls right away rather than losing that cycle. N=1 disables retrying.",
    )
    parser.add_argument(
        "--pool-age-alert-hours",
        type=float,
        default=DEFAULT_POOL_AGE_ALERT_HOURS,
        metavar="HOURS",
        help="print an active, repeated alert (and log a PoolStale row to poll_errors) every cycle the "
        "oldest available validation_pool number is at or above this age (default: %(default)s, per "
        "Decisions Annex D-16). Age is not an integrity signal and this never blocks dispensing -- "
        "it's a health signal that a top-up isn't reaching this gateway, or the machine isn't dispensing. "
        "0 or negative disables it.",
    )
    parser.add_argument(
        "--comms-gap-seconds",
        type=float,
        default=DEFAULT_COMMS_GAP_SECONDS,
        metavar="SECONDS",
        help="how long the link must be down before a recovery triggers a full "
        f"interrogation (default: %(default)s). NOT a data-loss threshold: by "
        "§2.2.1's arithmetic the exception queue is gone after roughly 8 seconds "
        "of silence on a played machine and no sweep recovers it. This is the "
        "time it takes somebody to unplug a cable, walk to another cabinet and "
        "plug it in -- which is what the interrogation detects. 0 disables it.",
    )
    parser.add_argument(
        "--no-interrogate-on-connect",
        dest="interrogate_on_connect",
        action="store_false",
        help="skip the startup interrogation. It is on by default because the "
        "first connect is the one moment with nothing to lose -- no cycle has "
        "run, so the ~150 long polls cost no exception queue -- and it is what "
        "later reconnects are compared against.",
    )
    parser.add_argument(
        "--rte",
        action="store_true",
        help="enable real time event reporting (LP 0x0E) for the run. Exceptions "
        "then arrive as Table 12.5 event messages carrying DATA -- a game's wager "
        "and win, a bill's denomination, a door's identity -- instead of a bare "
        "code, and the decoded fields are stored as JSON in machine_events."
        "rte_fields. Section 12.3 also lets the machine answer a LONG poll with an "
        "event instead of the reply; those are collected and stored with source "
        "'rte-interleaved' rather than discarded. RTE is turned off again on exit, "
        "including on Ctrl-C; §12 requires machines to power up in the ordinary "
        "mode, so a power cycle also clears it.",
    )
    return parser


def main() -> int:
    # Line-buffer stdout. Python block-buffers it when it is not a terminal,
    # so under `nohup ... > soak.log` nothing appears until ~8KB has built up
    # -- about four minutes of cycles at ~250 bytes each. A long-running
    # process whose log stays empty for minutes after it starts is
    # indistinguishable from one that failed to start, and the first thing
    # anyone does is tail the log. Costs nothing: one flush per cycle.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass  # not a reconfigurable stream; the log is merely less prompt

    parser = build_parser()
    args = parser.parse_args()

    history = HistoryConfig(
        mode=args.mode,
        cap=args.history_cap,
        interval=args.history_interval,
        burst_count=args.burst_count,
    )
    state = PollState()
    state.decline_aft_cashout = bool(args.decline_aft_cashout)
    if state.decline_aft_cashout:
        print("§8.7 AFT host-cashout requests will be DECLINED with an "
              "all-zeros LP 0x72, so a cashout goes straight to the ticket "
              "path instead of burning ~8s in stage 1. This moves no money.")

    # Checked BEFORE the port is opened. A refusal that first takes /dev/ttyS0
    # cannot even be read while a soak holds it, and an argument that needs no
    # hardware to validate has no business grabbing hardware to be rejected.
    # THE GATE on the two features that let this poller act on money by
    # itself. The lab ticket file is the authorisation, exactly as it is for
    # ticket-in: it binds to one cabinet, it expires, and the run exits
    # rather than act against any other machine. A flag alone is too easy to
    # leave in a unit file.
    lab_auto = None
    if args.drain_fast_poll and not args.drain_priority:
        print("--drain-fast-poll does nothing without --drain-priority: it "
              "only changes the rate WHILE draining, and without it nothing "
              "ever drains.", file=sys.stderr)
        return 2
    if args.drain_fast_poll_interval < SAS_FAST_POLL_INTERVAL_S:
        print(f"--drain-fast-poll-interval {args.drain_fast_poll_interval} is "
              f"below §12.2's {SAS_FAST_POLL_INTERVAL_S}s. This project has "
              f"polled below the floor once and recorded it as a bug; it is "
              f"not a knob.", file=sys.stderr)
        return 2
    if args.lab_auto_handpay_reset or args.lab_auto_aft_credit:
        if not args.lab_ticket_authority:
            print("--lab-auto-handpay-reset and --lab-auto-aft-credit both "
                  "require --lab-ticket-authority.\n"
                  "  Not bureaucracy: that file is what binds this run to one "
                  "cabinet by serial (or asset, §4.4) and carries the expiry "
                  "date.\n"
                  "  Clearing handpays and pushing credits without a human are "
                  "cash-dispensing defects on a floor, and an unbound flag is "
                  "how one gets there.", file=sys.stderr)
            return 2
        if (args.lab_auto_handpay_reset
                and args.lab_auto_handpay_reset < LAB_AUTO_HANDPAY_MIN_SECONDS):
            print(f"--lab-auto-handpay-reset {args.lab_auto_handpay_reset:.0f} is "
                  f"below the {LAB_AUTO_HANDPAY_MIN_SECONDS:.0f}s minimum. Below a "
                  "minute you are not clearing an unattended lockup, you are "
                  "racing the attendant walking towards the machine.",
                  file=sys.stderr)
            return 2
        if args.lab_auto_aft_credit and args.lab_auto_aft_credit <= 0:
            print("--lab-auto-aft-credit must be positive.", file=sys.stderr)
            return 2
        lab_auto = LabAutoConfig(
            handpay_reset_after_s=args.lab_auto_handpay_reset,
            aft_credit_cents=round(args.lab_auto_aft_credit * 100),
            aft_floor_cents=round(args.lab_aft_credit_floor * 100),
        )


    try:
        client = connect_from_config(args.config, min_poll_interval=args.min_poll_interval)
    except SASPortCapabilityError as e:
        # Worth failing loudly at startup rather than logging endless
        # timeouts into the database: the adapter cannot carry SAS's
        # wakeup bit, so no poll will ever succeed on this port.
        print(f"\n{e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - a bad port/config must not print a traceback
        print(f"\nCould not open the serial port from {args.config}: {e}", file=sys.stderr)
        print(
            "\nCheck the port name in the config file, that the adapter is plugged in, "
            "and that nothing else already holds the port (this client opens it "
            "exclusively, so a second poller or poll_test_tool.py will block it).",
            file=sys.stderr,
        )
        return 2
    conn = open_gateway_db(args.db)
    conn.executescript(SCHEMA)
    migrate_epoch_columns(conn)
    conn.commit()

    print(f"Polling {args.config} every {args.interval}s, writing to {args.db} (mode={args.mode}).")
    if args.mode == "ring":
        print(
            f"  meters_history: capped at {args.history_cap} rows, written every {args.history_interval}s, "
            f"or every cycle for {args.burst_count} poll(s) after a meter decrease or failed poll."
        )
    else:
        print("  meters_history: unbounded, written every cycle (lab/stress-testing mode).")
    print(
        f"  meters: {len(ALL_METER_FIELDS)} columns per row "
        f"(single-meter sweep {'enabled' if not args.skip_full_meter_sweep else 'skipped'}, "
        f"Table C-7 sweep {'skipped' if args.skip_table_c7_sweep else ('every cycle' if args.table_c7_interval <= 0 else f'every {args.table_c7_interval:.0f}s')}, "
        f"validation meters sweep {'enabled' if not args.skip_validation_meters_sweep else 'skipped'}, "
        f"last-accepted-bill poll {'enabled' if not args.skip_last_accepted_bill_poll else 'skipped'})."
    )
    # A default that changes what goes on the wire has to announce itself.
    # Nobody reads a unit file to find out why a sweep got shorter.
    if args.drain_priority:
        print(f"  drain priority ON (default): above "
              f"{DRAIN_ENTER_FRACTION * 100:.0f}% of the collectable event "
              f"budget the optional sweeps are deferred and the slots spent "
              f"on general polls; resumes below "
              f"{DRAIN_LEAVE_FRACTION * 100:.0f}%. Coin in, coin out and "
              f"games played are read every cycle regardless.")
    else:
        print("  drain priority OFF (--no-drain-priority): the full sweep "
              "runs however busy the cabinet gets. Events the queue drops "
              "while it runs are gone -- no meter carries them.")

    if args.lab_ticket_authority:
        import datetime as _dt
        from pathlib import Path as _Path
        try:
            from ticket_lab_authority import LAB_AUTHORITY_EXPIRES, load_tickets
        except ImportError:
            print("--lab-ticket-authority needs examples/ticket_lab_authority.py, "
                  "which is not in this checkout. It is meant to be deleted before "
                  "a gateway goes near a floor, so this is very likely correct.",
                  file=sys.stderr)
            return 2
        today = _dt.date.today()
        if today > LAB_AUTHORITY_EXPIRES:
            print(f"The lab ticket authority expired on {LAB_AUTHORITY_EXPIRES}. "
                  "That date is deliberate. Refusing to answer tickets.",
                  file=sys.stderr)
            return 2
        try:
            tickets, binding = load_tickets(_Path(args.lab_ticket_authority))
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"could not read {args.lab_ticket_authority}: {e}", file=sys.stderr)
            return 2
        state.ticket_authority = tickets
        state.ticket_authority_binding = binding
        state.ticket_authority_path = args.lab_ticket_authority
        print("=" * 70)
        print("  LAB TICKET AUTHORITY ACTIVE. This run will REDEEM tickets.")
        print(f"  {len(tickets)} number(s) from {args.lab_ticket_authority}, "
              f"bound to {binding.describe()}")
        print(f"  Expires {LAB_AUTHORITY_EXPIRES}. Delete the file and "
              "examples/ticket_lab_authority.py when the bench work is done.")
        if lab_auto is not None and lab_auto.any_enabled():
            print("-" * 70)
            print("  THIS RUN ALSO ACTS ON MONEY BY ITSELF:")
            if lab_auto.handpay_reset_after_s:
                print(f"    LP 0x94 remote handpay reset after "
                      f"{lab_auto.handpay_reset_after_s:.0f}s unattended.")
                print("    §7.9: with W2-G Reset To Credit Meter off, this "
                      "records the")
                print("    jackpot as PAID TO A PLAYER who was never there.")
            if lab_auto.aft_credit_cents:
                print(f"    AFT push {lab_auto.aft_credit_cents / 100:,.2f} "
                      f"cashable whenever the balance is at or below "
                      f"{lab_auto.aft_floor_cents / 100:,.2f},")
                print(f"    capped at {LAB_AUTO_AFT_MAX_PUSHES} pushes and "
                      f"{LAB_AUTO_AFT_MAX_TOTAL_CENTS / 100:,.0f} total "
                      f"for this run.")
            print("    Every action lands in the lab_auto_actions table.")
        print("=" * 70)

    # BEFORE ANYTHING READS THE WIRE. A cabinet left in real time event mode
    # by an unclean stop answers every general poll with its own address byte,
    # and each mis-read poll eats one byte of a Table 12.5 frame and misframes
    # the long polls after it. An interrogation run in that state produces
    # identity and configuration readings that look fine and are not.
    framing, probed_code = recover_stuck_rte(client)
    print(f"  framing check: {framing}")
    if probed_code is not None and probed_code != ExceptionCode.NONE:
        # The probe consumed a real exception. It predates this run's
        # reconciliation window, same as the startup drain below.
        _dispatch_exception(client, conn, utc_now(), probed_code,
                            source="startup-backlog", state=state)
        _safe_commit(conn, utc_now())

    if args.interrogate_on_connect:
        print("Interrogating the machine (identity, features, every game, "
              "ticket history)...")
        # THE HEARTBEAT IS NOT OPTIONAL HERE, and this call used to omit it.
        #
        # interrogate()'s own docstring says so in as many words: ~150 long
        # polls, "thirteen seconds of not draining the exception queue --
        # longer than the queue takes to overflow on a machine being played.
        # An interrogation that ran without draining would cause exactly the
        # loss it exists to detect, and would do it silently, at the moment
        # its output is most trusted." The comms_restored caller passed one;
        # this one did not, so every run began with a blind window.
        #
        # Pacing made it worse rather than better. At the 88 ms a sweep
        # measured, 150 polls is ~13s; at --min-poll-interval 0.3 it is ~45s,
        # against a 20-deep queue (§2.2.1) that a played cabinet fills in
        # roughly eight. Every soak since pacing landed therefore started by
        # overflowing the queue and losing whatever was in it -- the first
        # mechanism found here that DESTROYS events rather than merely
        # delaying them.
        #
        # rte=False is correct: real time event reporting is enabled further
        # down, after this returns, so the replies here are ordinary single
        # bytes and general_poll_rte() would mis-frame them.
        startup_codes: list = []
        startup_heartbeat = _make_heartbeat(client, conn, utc_now(),
                                            startup_codes, rte=False,
                                            state=state)
        try:
            interrogate(client, conn, state, utc_now(), reason="first_connect",
                        heartbeat=startup_heartbeat)
        except SASError as e:
            print(f"  interrogation failed: {type(e).__name__}: {e}", file=sys.stderr)
            print("  Continuing; the cycle's own identity check still runs.",
                  file=sys.stderr)
        # Whatever the heartbeats drained predates the first meter baseline,
        # so it carries the same source label as the startup backlog: real,
        # recorded, and explicitly outside this run's reconciliation window.
        if startup_codes:
            drain_now = utc_now()
            print(f"  {len(startup_codes)} event(s) drained by the interrogation's "
                  "heartbeats; stored as source 'startup-backlog'.")
            for code, hb_fields, _hb_source in startup_codes:
                print(f"  exception 0x{code:02X} during interrogation: "
                      f"{describe_exception(code)}")
                _clarify = clarify_exception(code)
                if _clarify:
                    print(f"      ^ {_clarify}")
                _dispatch_exception(client, conn, drain_now, code,
                                    source="startup-backlog", state=state,
                                    fields=hb_fields)

    # Checked OUTSIDE the interrogation branch, because it used to live
    # inside it: --no-interrogate-on-connect then silently disabled the one
    # check that stops this file being used against the wrong cabinet. The
    # binding is the authorisation; a flag about startup chatter must not be
    # able to switch it off.
    #
    # THE UNREADABLE CASE IS A SEPARATE DECISION FROM THE MISMATCHED ONE, and
    # conflating them cost a soak. The original check read
    # ``if seen and seen.strip() != bound``, which fails OPEN: a cabinet whose
    # LP 0x54 does not answer -- section 4.4 lets it refuse -- had no serial to
    # compare, so the binding was not enforced and nothing said so. Replacing
    # that with a bare refusal fixed the silence and broke every such cabinet,
    # including this project's own lab EGM, which exits 2 at startup on a
    # command line that worked ten minutes earlier.
    #
    # So: a MISMATCH is always fatal, because the machine has identified
    # itself and it is the wrong one. An UNREADABLE serial is fatal by
    # default, because an unenforceable binding is not a binding -- but the
    # operator standing at the cabinet can overrule that with
    # --ticket-authority-unverified-serial, which puts the decision in the
    # command line and in the log instead of nowhere.
    if state.ticket_authority is not None:
        # Only worth a long poll if nothing has asked yet. An interrogation
        # that ran and got None was already refused by the machine; asking
        # again spends an exchange to be told the same thing.
        if state.identity is None:
            try:
                state.identity = read_identity(client)
            except SASError as e:
                print(f"  could not read this cabinet's identity "
                      f"({type(e).__name__}: {e})", file=sys.stderr)
        verdict = ticket_authority_binding_verdict(
            state.ticket_authority_binding, state.identity,
            allow_unverified=args.ticket_authority_unverified_serial)
        if verdict is not None:
            reason, detail = verdict
            if reason == "mismatch":
                print(f"\n  WRONG CABINET: {detail}.", file=sys.stderr)
                print("  Refusing to redeem tickets against it. No flag "
                      "overrides this.", file=sys.stderr)
                return 2
            if reason == "unverifiable":
                print(f"\n  CANNOT VERIFY WHICH CABINET THIS IS: {detail}.",
                      file=sys.stderr)
                print(f"  That binding is the only thing stopping "
                      f"{args.lab_ticket_authority} redeeming tickets against "
                      "a cabinet it was not issued for.", file=sys.stderr)
                print("  Add machine_asset to the file if this cabinet reports "
                      "an asset number — that is enforceable where a serial is "
                      "not. Or, if you are at the cabinet and know it is the "
                      "right one, say so deliberately:", file=sys.stderr)
                print("    --ticket-authority-unverified-serial", file=sys.stderr)
                return 2
            if reason == "unbound":
                print("\n  WARNING: the ticket file names no cabinet, so it "
                      "will redeem against ANY machine this run talks to.",
                      file=sys.stderr)
            else:
                print(f"\n  WARNING: {detail}; the binding is NOT being "
                      "enforced. --ticket-authority-unverified-serial was "
                      "given, so the run continues on the operator's word.",
                      file=sys.stderr)

    if not args.interrogate_on_connect and not args.skip_ticket_out_backfill:
        print("Reading the existing ticket-out buffer (indices 1-31, non-destructive)...")
        found = backfill_ticket_out_history(client, conn)
        print(f"  {found} ticket-out record(s) found and stored.")

    if args.seed_validation_pool:
        added = seed_validation_pool(conn, args.seed_validation_pool)
        available = conn.execute("SELECT COUNT(*) FROM validation_pool WHERE status = 'available'").fetchone()[0]
        print(f"  validation_pool: added {added} test number(s), {available} available (lab numbers, not server-issued).")

    # SIGTERM and SIGHUP must reach the teardown, not bypass it.
    #
    # Python does not raise on either by default: the process simply dies and
    # no finally block runs. For an unattended overnight run that is the
    # difference between a machine handed back in the ordinary polling mode
    # and one left in real time event reporting -- where every other tool in
    # this repository reads the Table 12.5 address byte as an exception code
    # and sees a flood of phantom 0x01s, until someone power cycles the
    # cabinet or runs exception_monitor.py --disable-rte against it.
    #
    # SIGHUP matters as much as SIGTERM here: it is what a dropped SSH
    # session sends, which is exactly how a soak started from a terminal
    # tends to end.
    def _stop(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")

    for _sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if _sig is None:
            continue
        try:
            # An INHERITED SIG_IGN is a deliberate instruction from whoever
            # started us, and installing a handler over it silently revokes
            # their protection. nohup sets exactly this for SIGHUP, so the
            # first version of this block turned `nohup ... &` from "survives
            # a dropped SSH session" into "stops cleanly on one" -- the exact
            # opposite of why the handler was added, and invisible until an
            # overnight run came back shorter than it should have been.
            if signal.getsignal(_sig) == signal.SIG_IGN:
                continue
            signal.signal(_sig, _stop)
        except (ValueError, OSError):
            pass  # not the main thread, or a platform without it

    rte_on = False
    if args.rte:
        # Enabling is the easy half. Guaranteeing it is turned off again is
        # the half that matters: a machine left in real time event mode
        # answers every later general poll with a Table 12.5 message, which a
        # client expecting a single byte cannot frame at all -- so a crash
        # here would leave the cabinet unreadable by this tool's own default
        # mode until someone power-cycled it.
        state.rte_wanted = True
        try:
            client.enable_real_time_events(True)
            rte_on = True
            state.rte_enabled = True
            print("  RTE ENABLED (LP 0x0E): exceptions arrive as Table 12.5 events "
                  "with data, stored in machine_events.rte_fields.")
        except SASError as e:
            print(f"  Could not enable RTE: {type(e).__name__}: {e}", file=sys.stderr)
            print("  Starting in the ordinary polling mode and RETRYING every "
                  "cycle. A cabinet that is still booting -- the usual case "
                  "after a power cut, since the gateway comes back first -- "
                  "will be switched over as soon as it answers.", file=sys.stderr)

    # Drain whatever the machine had queued BEFORE this run, so the first
    # meters_history row is a clean baseline.
    #
    # §2.2.1's queue is non-volatile and the machine fills it whether or not
    # a host is listening. A cabinet played for ten minutes with no gateway
    # attached hands the first poll a full queue -- and, if it overflowed
    # while nobody was polling, a pending exception 0x70 that has nothing to
    # do with this run's polling rate.
    #
    # Without this drain those events land in the first cycle, stamped with
    # the first cycle's timestamp, and are then reconciled against a meter
    # delta that begins after them. Measured on the lab cabinet: 13 games'
    # worth of events arrived in cycle 1 against a window in which the meters
    # moved 11 games, and the reconciliation reported 13 phantom "lost"
    # events in a run that had lost nothing.
    drained = 0
    if rte_on or True:  # the queue predates the run in either mode
        start_now = utc_now()
        for _ in range(STARTUP_DRAIN_MAX):
            try:
                if rte_on:
                    event = client.general_poll_rte()
                    if event is None or int(event.code) == ExceptionCode.NONE:
                        break
                    code, fields = int(event.code), (event.fields or None)
                else:
                    code, fields = client.general_poll(), None
                    if code == ExceptionCode.NONE:
                        break
            except SASError:
                break
            drained += 1
            _dispatch_exception(client, conn, start_now, code,
                                source="startup-backlog", state=state, fields=fields)
        if rte_on:
            for event in client.drain_rte_events():
                drained += 1
                _dispatch_exception(client, conn, start_now, int(event.code),
                                    source="startup-backlog", state=state,
                                    fields=(event.fields or None))
        if drained:
            print(f"  drained {drained} event(s) the machine had queued before this "
                  f"run started; they are stored as source 'startup-backlog' and "
                  f"are NOT part of this run's reconciliation window.")
            if drained >= STARTUP_DRAIN_MAX:
                print(f"  (hit the {STARTUP_DRAIN_MAX}-poll drain cap -- the machine "
                      f"still had more queued. Anything left will arrive in cycle 1.)")

    print("Ctrl-C to stop." if args.cycles == 0 else f"Will stop after {args.cycles} cycle(s).")

    # The run announces itself to the DATABASE, not just to stdout. See the
    # poller_lifecycle comment in SCHEMA: a 'started' with no 'stopped' is
    # what proves the process was killed rather than asked to stop.
    _record_lifecycle(conn, "started", reason=None)
    stop_reason = "end of run"
    # The rate to go back to. Read off the transport, which is where pacing
    # is enforced, rather than from args -- the thing restored must be the
    # thing that was in force.
    state.normal_poll_interval = getattr(
        getattr(client, "transport", None), "_min_poll_interval", 0.0) or 0.0
    state.baseline_capacity = (
        1.0 / state.normal_poll_interval if state.normal_poll_interval else 0.0)

    cycle = 0
    try:
        while args.cycles == 0 or cycle < args.cycles:
            cycle += 1
            poll_and_log(
                client,
                conn,
                state,
                history,
                db_size_warning_mb=args.db_size_warning_mb,
                full_meter_sweep=not args.skip_full_meter_sweep,
                table_c7_sweep=not args.skip_table_c7_sweep,
            table_c7_interval=args.table_c7_interval,
                validation_meters_sweep=not args.skip_validation_meters_sweep,
                heartbeat_every=args.heartbeat_every,
                credit_poll_every=args.credit_poll_every,
                serve_host_requests=args.serve_host_requests,
                last_accepted_bill_poll=not args.skip_last_accepted_bill_poll,
                interval=args.interval,
                general_poll_retries=args.general_poll_retries,
                pool_age_alert_hours=args.pool_age_alert_hours,
                rte=rte_on,
                comms_gap_seconds=args.comms_gap_seconds,
                lab_auto=lab_auto,
                drain_priority=args.drain_priority,
                drain_fast_poll=args.drain_fast_poll,
                drain_fast_poll_interval=args.drain_fast_poll_interval,
                drain_fast_poll_fallback=args.drain_fast_poll_fallback,
            )
            # Under drain priority this is general polls, not sleep: the
            # inter-cycle gap is the largest uninterrupted block of time in
            # the loop, and during a burst it is the worst possible moment
            # to stop collecting.
            _drain_sleep(client, conn, state, args.interval, rte=rte_on)
    except KeyboardInterrupt as e:
        reason = f" ({e})" if str(e) else ""
        # "signal 15" when logind, a supervisor or soak_cycle.sh asked;
        # bare KeyboardInterrupt when a person pressed Ctrl-C. Different
        # causes, and until now they were distinguishable only in a log
        # file that outlived nothing.
        stop_reason = str(e) or "KeyboardInterrupt (Ctrl-C)"
        print(f"\nStopped{reason}.")
    except Exception as e:  # noqa: BLE001 - see below; must not end as a traceback
        # A USB-serial adapter that re-enumerates mid-run (unplugged,
        # power-saved, or a flaky cable) raises out of pyserial, not as a
        # SASError, so it lands here rather than being absorbed as a
        # failed poll. There is deliberately no reconnect loop: silently
        # reopening a port hides a physical fault that someone should be
        # told about, and reopening the WRONG device after a re-enumeration
        # (ttyUSB0 coming back as ttyUSB1, with another machine's adapter
        # now on ttyUSB0) would log one machine's meters under another's
        # name. Exiting non-zero hands recovery to a process supervisor,
        # which restarts from the config and so re-resolves the port by
        # name. Everything already committed to SQLite is safe; the
        # finally-block close below still runs.
        stop_reason = f"{type(e).__name__}: {e}"
        print(f"\nPolling stopped: {type(e).__name__}: {e}", file=sys.stderr)
        print(
            "\nThe serial port failed mid-run. The usual cause is the adapter "
            "disconnecting or re-enumerating. Data already written to the database "
            "is intact. Run this under a supervisor (systemd 'Restart=always') so a "
            "gateway recovers on its own — see MANUAL.md §4.6.",
            file=sys.stderr,
        )
        return 3
    finally:
        # state.rte_enabled, NOT the startup-time rte_on: a run that enabled
        # RTE on a later cycle (because the cabinet was still booting) must
        # still disable it on the way out, or it is left in Table 12.5
        # framing for the next tool to misread.
        # BEFORE the RTE teardown, which is itself a long poll: a run that
        # died mid-burst must not hand the cabinet back still being polled
        # at 40 ms, and must not send its last messages at that rate either.
        if state.fast_poll_active and state.normal_poll_interval > 0:
            if _set_poll_interval(client, state.normal_poll_interval):
                state.fast_poll_active = False
                print(f"poll rate restored to "
                      f"{state.normal_poll_interval:.3f}s on the way out.")
        if state.rte_enabled or rte_on:
            # Before the database closes, so a last interleaved event still
            # lands, and before the port does, so the machine is handed back
            # in the mode every other tool here expects.
            try:
                client.enable_real_time_events(False)
                print("RTE disabled; exceptions are single-byte codes again.")
            except SASError as e:
                print(f"WARNING: could not disable RTE ({type(e).__name__}: {e}). "
                      "This machine will keep answering general polls with Table "
                      "12.5 messages until it is power cycled or "
                      "'exception_monitor.py <port> --disable-rte' is run against "
                      "it. §12 requires a power-up default of ordinary polling.",
                      file=sys.stderr)
            for event in client.drain_rte_events():
                _dispatch_exception(client, conn, utc_now(), int(event.code),
                                    source="rte-interleaved",
                                    fields=(event.fields or None))
        # LAST, so it is written even when the RTE teardown above fails --
        # and so that a row recording "could not disable RTE" is impossible
        # to confuse with no row at all.
        _record_lifecycle(conn, "stopped", reason=stop_reason)
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
