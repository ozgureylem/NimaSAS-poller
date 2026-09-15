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
how many long-poll exchanges (``meter_poll=X.XXXs/N polls``) — measured
wall-clock time against whatever this ran against, not a theoretical
number. That's the real answer to "does this many polls fit inside
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
import os
import random
import sqlite3
import sys
import time
from dataclasses import dataclass

from saspy.config import connect_from_config
from saspy.constants import SIMPLE_METER_WIDTH_BCD, ExceptionCode, LongPoll, MeterCode, ValidationType
from saspy.exceptions import SASError
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
DECREASE_CHECK_FIELDS = tuple(f for f in ALL_METER_FIELDS if f not in GAUGE_METER_FIELDS)

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
    {_METER_COLUMN_DDL}
);

CREATE TABLE IF NOT EXISTS meters_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    polled_at TEXT NOT NULL,
    {_METER_COLUMN_DDL}
);

CREATE TABLE IF NOT EXISTS poll_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    poll_name TEXT NOT NULL,
    error_type TEXT NOT NULL,
    message TEXT
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

CREATE TABLE IF NOT EXISTS validation_pool (
    validation_number INTEGER PRIMARY KEY,
    validation_system_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    issued_at TEXT,
    assigned_at TEXT,
    assigned_amount_cents INTEGER
);

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

DEFAULT_HISTORY_CAP = 200
DEFAULT_HISTORY_INTERVAL = 60.0
DEFAULT_BURST_COUNT = 10
DEFAULT_GENERAL_POLL_RETRIES = 3
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
    burst_remaining: int = 0


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


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
    for index in range(1, 32):
        try:
            record = client.send_enhanced_validation_information(function_code=index)
        except SASError as e:
            conn.execute(
                "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
                (now, "send_enhanced_validation_information(backfill)", type(e).__name__, str(e)),
            )
            _safe_commit(conn, now)
            continue
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

    row = conn.execute(
        "SELECT validation_number, validation_system_id FROM validation_pool "
        "WHERE status = 'available' ORDER BY validation_number LIMIT 1"
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


def _check_pool_age(conn: sqlite3.Connection, now: str, *, max_age_hours: float) -> None:
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
    if age is not None and age >= max_age_hours:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "validation_pool", "PoolStale", f"oldest available number is {age:.1f}h old (>= {max_age_hours}h)"),
        )
        _safe_commit(conn, now)
        print(
            f"[{now}] ALERT: validation_pool's oldest available number is {age:.1f}h old, at or above "
            f"--pool-age-alert-hours {max_age_hours} — likely no top-up reaching this gateway, or the "
            "machine isn't dispensing. Not a validity problem and not blocking dispensing (see D-16)."
        )


def _capture_ticket_in(client, conn: sqlite3.Connection, now: str) -> None:
    """Called on exception 0x67: read the ticket's validation data
    (read-only — see this module's docstring for why redeem_ticket() is
    deliberately never called here).
    """
    try:
        ticket = client.send_ticket_validation_data()
    except SASError as e:
        conn.execute(
            "INSERT INTO poll_errors (occurred_at, poll_name, error_type, message) VALUES (?, ?, ?, ?)",
            (now, "send_ticket_validation_data", type(e).__name__, str(e)),
        )
        _safe_commit(conn, now)
        return
    if not ticket.ticket_in_escrow:
        return  # exception fired, but nothing in escrow by the time we read it — rare, harmless race
    conn.execute(
        "INSERT INTO ticket_in_events (captured_at, amount_cents, parsing_code, validation_data_hex) "
        "VALUES (?, ?, ?, ?)",
        (now, ticket.amount_cents, ticket.parsing_code, ticket.validation_data.hex()),
    )
    _safe_commit(conn, now)
    print(f"[{now}] ticket-in captured: amount_cents={ticket.amount_cents} (not redeemed — see module docstring)")


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
    full_sweep: bool = True,
    table_c7_sweep: bool = True,
    validation_meters_sweep: bool = True,
    last_accepted_bill_poll: bool = True,
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

    def poll(name, fn, *args):
        try:
            return fn(*args)
        except SASError as e:
            raise _MeterPollFailure(name, e) from e

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
        bill = poll("send_last_accepted_bill_information", client.send_last_accepted_bill_information)
        values.update(zip(LP48_METER_COLUMNS, (bill.country_code, bill.denomination_code, bill.bill_meter)))
    else:
        for column in LP48_METER_COLUMNS:
            values[column] = None

    if full_sweep:
        for lp, column in SINGLE_METER_COLUMNS.items():
            values[column] = poll(f"send_meter({lp.name})", client.send_meter, lp)
    else:
        for column in SINGLE_METER_COLUMNS.values():
            values[column] = None

    if table_c7_sweep:
        for i in range(0, len(TABLE_C7_EXTENDED_CODES), TABLE_C7_CHUNK_SIZE):
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
            result = poll(f"send_validation_meters({vt.name})", client.send_validation_meters, vt)
            total_col, amount_col = VALIDATION_METER_COLUMNS[vt]
            values[total_col] = result.total_validations
            values[amount_col] = result.cumulative_amount_cents
    else:
        for column in VALIDATION_METER_COLUMNS_FLAT:
            values[column] = None

    return values


def _general_poll_with_retry(
    client, conn: sqlite3.Connection, now: str, *, max_attempts: int, sleep_fn=time.sleep
) -> tuple[int, int]:
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

    Returns (exception_code, attempt_number) on success. Raises the
    final SASError if every attempt fails. Every failed attempt is
    logged to poll_errors individually, numbered, so a flaky link shows
    up as a distinguishable run of attempts rather than one opaque
    failure.
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
            return client.general_poll(), attempt
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
    validation_meters_sweep: bool = True,
    last_accepted_bill_poll: bool = True,
    interval: float | None = None,
    general_poll_retries: int = DEFAULT_GENERAL_POLL_RETRIES,
    pool_age_alert_hours: float = DEFAULT_POOL_AGE_ALERT_HOURS,
    retry_sleep_fn=time.sleep,
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
    and how many long-poll exchanges that was (``meter_poll=X.XXXs/N
    polls``) — this is measured wall-clock time against whatever's on
    the other end of ``client``, not a theoretical figure, so it's the
    real answer to "does the meter sweep fit in --interval on this
    hardware." Pass ``interval`` (the caller's own poll-cycle interval,
    e.g. --interval) to also get a WARNING if the meter poll alone is at
    or above it — a sign this cycle's own meter sweep doesn't leave any
    slack for the general poll or the configured sleep, and
    --skip-full-meter-sweep, --skip-table-c7-sweep,
    --skip-validation-meters-sweep, or a larger --interval is worth
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

    _check_pool_age(conn, now, max_age_hours=pool_age_alert_hours)

    try:
        exception_code, attempt = _general_poll_with_retry(
            client, conn, now, max_attempts=general_poll_retries, sleep_fn=retry_sleep_fn
        )
    except SASError as e:
        print(
            f"[{now}] general poll failed after {general_poll_retries} consecutive attempt(s): "
            f"{type(e).__name__}: {e}"
        )
    else:
        if attempt > 1:
            print(f"[{now}] general poll recovered on attempt {attempt}/{general_poll_retries}")
        if exception_code == ExceptionCode.TICKET_INSERTED:
            _capture_ticket_in(client, conn, now)
        elif exception_code == ExceptionCode.TICKET_TRANSFER_COMPLETE:
            _capture_ticket_in_completion(client, conn, now)
        elif exception_code in (ExceptionCode.CASH_OUT_TICKET_PRINTED, ExceptionCode.HANDPAY_VALIDATED):
            _drain_ticket_out_history(client, conn, now)
        elif exception_code == ExceptionCode.SYSTEM_VALIDATION_REQUEST:
            _handle_cashout_request(client, conn, now)

    mono_now = monotonic_fn()

    try:
        values = _poll_all_meters(
            client,
            full_sweep=full_meter_sweep,
            table_c7_sweep=table_c7_sweep,
            validation_meters_sweep=validation_meters_sweep,
            last_accepted_bill_poll=last_accepted_bill_poll,
        )
    except _MeterPollFailure as failure:
        meter_poll_elapsed = monotonic_fn() - mono_now
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
        return

    meter_poll_elapsed = monotonic_fn() - mono_now
    meter_poll_count = (
        GROUPED_METER_POLL_COUNT
        + (1 if last_accepted_bill_poll else 0)
        + (len(SINGLE_METER_COLUMNS) if full_meter_sweep else 0)
        + (TABLE_C7_CHUNK_COUNT if table_c7_sweep else 0)
        + (len(VALIDATION_METER_TYPES) if validation_meters_sweep else 0)
    )
    if interval is not None and meter_poll_elapsed >= interval:
        print(
            f"[{now}] WARNING: meter poll took {meter_poll_elapsed:.3f}s across {meter_poll_count} long-poll "
            f"exchanges — at or above --interval {interval}s. This cycle's meter sweep alone doesn't leave "
            "room for the general poll or the configured sleep. Consider --skip-full-meter-sweep, "
            "--skip-table-c7-sweep, --skip-validation-meters-sweep, or a larger --interval."
        )

    columns = ", ".join(ALL_METER_FIELDS)
    qmarks = ", ".join("?" for _ in ALL_METER_FIELDS)
    bind = tuple(values[f] for f in ALL_METER_FIELDS)

    conn.execute(
        f"INSERT OR REPLACE INTO meters_current (id, polled_at, {columns}) VALUES (1, ?, {qmarks})",
        (now, *bind),
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
            f"INSERT INTO meters_history (polled_at, {columns}) VALUES (?, {qmarks})",
            (now, *bind),
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

    note = "history written" if write_history else "history skipped (cadence)"
    if decreased:
        note += ", meter decrease detected"
    elif anomaly:
        note += ", anomaly signaled"
    print(
        f"[{now}] coin_in={values['total_coin_in']} coin_out={values['total_coin_out']} "
        f"games_played={values['games_played']} ticket_in_cashable_cents={values['ticket_in_cashable_cents']} "
        f"ticket_out_cashable_cents={values['ticket_out_cashable_cents']} "
        f"meter_poll={meter_poll_elapsed:.3f}s/{meter_poll_count}polls — {note}"
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


def main() -> int:
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
    args = parser.parse_args()

    history = HistoryConfig(
        mode=args.mode,
        cap=args.history_cap,
        interval=args.history_interval,
        burst_count=args.burst_count,
    )
    state = PollState()

    client = connect_from_config(args.config)
    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
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
        f"Table C-7 sweep {'enabled' if not args.skip_table_c7_sweep else 'skipped'}, "
        f"validation meters sweep {'enabled' if not args.skip_validation_meters_sweep else 'skipped'}, "
        f"last-accepted-bill poll {'enabled' if not args.skip_last_accepted_bill_poll else 'skipped'})."
    )

    if not args.skip_ticket_out_backfill:
        print("Reading the existing ticket-out buffer (indices 1-31, non-destructive)...")
        found = backfill_ticket_out_history(client, conn)
        print(f"  {found} ticket-out record(s) found and stored.")

    if args.seed_validation_pool:
        added = seed_validation_pool(conn, args.seed_validation_pool)
        available = conn.execute("SELECT COUNT(*) FROM validation_pool WHERE status = 'available'").fetchone()[0]
        print(f"  validation_pool: added {added} test number(s), {available} available (lab numbers, not server-issued).")

    print("Ctrl-C to stop." if args.cycles == 0 else f"Will stop after {args.cycles} cycle(s).")

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
                validation_meters_sweep=not args.skip_validation_meters_sweep,
                last_accepted_bill_poll=not args.skip_last_accepted_bill_poll,
                interval=args.interval,
                general_poll_retries=args.general_poll_retries,
                pool_age_alert_hours=args.pool_age_alert_hours,
            )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
