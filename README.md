# NimaSAS-poller

A clean-room Python client for the GSA/IGT **SAS 6.02** slot accounting
system protocol — the SAS-side of the gateway software this project is
building, for stress-testing on real hardware and in your own lab
environments.

This repo is deliberately code-only: no protocol spec, no project
background, no vendor tooling. Full documentation and project context are
being shared with the team by email — this is the thing to actually run.

See **[MANUAL.md](MANUAL.md)** for the full user manual: a section per
tool below (simple explanation first, technical detail after), plus a
step-by-step guide to structuring a local SQL layer around a poller.

## What's here

- **`saspy/`** — the client itself: CRC-16, BCD/binary field codecs, frame
  building/parsing, a wakeup-bit-aware serial transport, a `SASClient`
  covering general poll plus the long polls listed below, and a
  `.ini`-based gateway config (`saspy/config.py`) so connection details
  don't need to be hardcoded.
- **`tests/`** — 265 tests, all against fake serial ports; run them with
  no hardware attached to confirm your environment is set up right before
  you touch real wiring.
- **`examples/connectivity_check.py`** — point this at a real port and
  address and it general-polls, then tries a few read-only long polls.
  Changes nothing on the machine by default. Start here for
  wiring/connectivity checks:
  ```
  python3 examples/connectivity_check.py /dev/ttyUSB0 --address 1
  ```
  `--test-shutdown` opts into round-tripping LP 0x01/0x02 (Shutdown
  then Startup) against the address — a real state change, unlike
  everything else this script does — with a confirmation prompt
  (`--yes` to skip it) and a Startup attempt no matter how the
  Shutdown call itself went, so a flaky exchange can't leave the
  machine disabled:
  ```
  python3 examples/connectivity_check.py /dev/ttyUSB0 --address 1 --test-shutdown
  ```
- **`examples/wire_diagnostics.py`** — run this first on a machine you
  have never polled before. Read-only: it answers three questions in the
  order they block you — does a plain general poll work, does a `0x80`
  sync byte before each poll help/hurt/do nothing (and does the gap after
  it matter), and do **long polls** work. That last one is separate on
  purpose: every long poll is a multi-byte frame, which exercises a
  parity transition a single-byte general poll never touches, so a bus
  that general-polls perfectly can still fail every long poll. Every
  condition runs the same counted harness, because a loop that stops at
  its first failure and one that counts failures are not comparable
  evidence.
  ```
  python3 examples/wire_diagnostics.py /dev/ttyS0 --address 1
  ```
- **`examples/wakeup_probe.py`** — read-only follow-up for when a bare
  general poll gets no answer but one preceded by a `0x80` does. Changing
  parity calls `tcsetattr` with `TCSANOW`, which reconfigures the port
  immediately, so a byte written straight afterwards may be the one that
  gets damaged — meaning a leading `0x80` can work by absorbing that
  damage rather than by being a sync. The two explanations lead to
  opposite fixes, so this measures which one holds: it polls with parity
  set once and never touched again, which only succeeds under the second.
  ```
  python3 examples/wakeup_probe.py /dev/ttyS0 --address 1
  python3 examples/wakeup_probe.py /dev/ttyS0 --address 1 --watch   # swing the slot door
  ```
- **`examples/preamble_probe.py`** — read-only. Once a preamble is known
  to be needed, this says *what it is*: whether any throwaway byte works
  (a link-layer fault — the far end needs the line driven before it reads)
  or only `0x80` does (protocol), and how long the line may be quiet
  before a bare poll stops being answered. Both answers feed straight into
  `SASTransport(wake_byte=..., wake_after_idle=...)`.
  ```
  python3 examples/preamble_probe.py /dev/ttyS0 --address 1
  ```
- **`examples/poll_test_tool.py`** — a bench tool for trying long polls
  against a real EGM and watching what comes back. Serves a small local
  web UI: one panel lists every long poll this client implements, tagged
  in plain language ("Total value of bills in"), built from
  `saspy.constants` so it can't drift from the code; the other is a
  free-form hex box for codes found in a forum post or vendor doc — it
  shows the exact bytes (CRC computed for you), sends, and decodes the
  reply as far as it structurally can, flagging whether the CRC checks
  out. When an unknown code turns out to work, that exchange is the
  test vector for adding it to `saspy` properly.
  Runs on Linux, Windows and macOS, either on a laptop with the adapter
  plugged into it, or on the gateway with a laptop's browser pointed at
  it. `--list-ports` finds the port name on any OS.
  ```
  python3 examples/poll_test_tool.py --list-ports                  # COM3? /dev/cu.usbserial-*?
  python3 examples/poll_test_tool.py /dev/ttyUSB0 --address 1      # then open localhost:8080
  python3 examples/poll_test_tool.py /dev/ttyUSB0 --bind 0.0.0.0   # on the gateway, browse from a laptop
  python3 examples/poll_test_tool.py --simulate                    # explore the UI, no hardware
  ```
  ⚠ **Lab gateways only — read
  [POLL_TEST_TOOL_WARNING.md](POLL_TEST_TOOL_WARNING.md) before running
  it**, and [POLL_TEST_TOOL_MANUAL.md](POLL_TEST_TOOL_MANUAL.md) for how
  to use it. It sends whatever you tell it to: SAS long polls are not
  all reads — some move funds (`0x72`), pay out a ticket (`0x71`), or
  take the machine out of service (`0x01`). Recovering a stuck machine
  often ends in a **RAM clear, which permanently zeroes that machine's
  meters**. This must never be installed on a gateway serving live
  machines, and must be erased and verified gone before a lab gateway is
  redeployed to a floor.

  Every command is classified read / state-change / custom, and anything
  that isn't a known-safe read needs a typed confirmation the
  **backend** enforces, not just the UI. `--lab-mode` drops that
  confirmation for a bench where a locked machine is a non-event — a
  deliberate startup flag, never the default.
- **`examples/exception_monitor.py`** — read-only. General-polls and names
  what comes back in plain language, so somebody can play the machine and
  watch events scroll: `0x7E` **game has started**, `0x7F` **game has
  ended**, doors, bills, cashout button, handpays. The full Appendix A
  table (114 codes) lives in `saspy.constants`, so a raw `0x84` reads as
  "self test or operator menu has been entered". On exit it summarises what
  was seen.
  ```
  python3 examples/exception_monitor.py /dev/ttyS0 --address 1
  ```
  **On RTE (real time event reporting):** codes for reel stops, cards held,
  draw-card requests and coin/credit wagered are sent *only* with RTE on,
  which the host enables with long poll `0x0E` (§12.1). **This library does
  not implement `0x0E`, deliberately** — §12.5 says that once RTE is on
  "gaming machines no longer report exceptions as single byte codes": every
  exception arrives as `address, FF, code, data…, CRC`, §12.3 lets an event
  pre-empt a long poll's response, and §12.4 requires the host to treat that
  as a NACK and re-queue. So enabling RTE breaks this client everywhere at
  once — `general_poll()` reads exactly one byte, and any long poll may come
  back as an event. `poll_test_tool.py` will happily send `0x0E 01`; if
  someone does, the machine looks dead and the fix is `0x0E 00` or a power
  cycle (the spec requires machines to default to non-RTE on power up).
  Game start/end need none of this.

  **§12.4 is handled.** Every long poll goes through one place in the client,
  so while RTE is on a reply whose second byte is the `FF` identifier is
  recognised as an event rather than decoded as meter data: the poll is
  reissued (§12.4 — "the host… considers the long poll NACKed and reinserts
  the long poll into its transmit queue") and the event is **kept**, not
  dropped. Discarding it would reintroduce the very exception loss RTE is
  being used to avoid. Collect them with `drain_rte_events()`.

  `--credits` adds the machine's credit meter to each GAME HAS ENDED line.
  Table 12.5.4 carries only the game win, so that is a separate LP `0x1A`
  read — and it is exactly the read §12.3 can pre-empt, which is why the
  above had to exist first.

  `--rte` enables it for one run and turns it back off on exit, inside a
  `finally` so a crash or Ctrl-C cannot leave the machine in a mode the
  ordinary poll loop mis-frames; if the restore itself fails it says so
  loudly, because nothing else will.
- **`examples/aft_push_credits.py`** — **moves money.** Puts cashable
  credits on a machine by AFT (Table 8.3d type `00`, in-house host→machine,
  full transfer only). Registration is not needed: Table 8.3a states
  "registration key (0 = registration not required)" and §8.1 that "a POS ID
  is never required for in-house transfers". It pre-checks the things that
  otherwise come back as a status code to look up — AFT and in-house enabled,
  transfer-to-machine currently available (all-zero is usually the operator
  menu or an open door, §8.3, and becomes status `87`), the amount against
  the machine's limit (`84`), the asset number read from the machine rather
  than guessed (`93`), and a transaction ID unique per run (`81`) — then
  reads Table C-7 meters `00A0`/`00A1` before and after, because the status
  byte is what the machine decided and the deltas are what happened.
  ```
  python3 examples/aft_push_credits.py /dev/ttyS0 --address 1 --amount 20.00
  ```
  A transfer answered with **pending** status `0x40` is not finished, and
  leaving it unacknowledged is not harmless: §8.8 requires the machine to
  issue exception `0x69` on completion and "reissue exception 69 **every 15
  seconds** until the host polls for and acknowledges the completion status".
  Measured on an EGT Premier at 15.2s, indefinitely — and `0x69` is a
  *priority* exception, so it is delivered ahead of anything queued. The tool
  therefore follows up with the Table 8.3b interrogation poll automatically.
  To clear a machine already stuck in that loop, without moving money:
  ```
  python3 examples/aft_push_credits.py /dev/ttyS0 --address 1 --acknowledge-only
  ```
- **`examples/poll_gap_probe.py`** — read-only. Finds what causes exception
  `0x70`, buffer overflow. Measured on an EGT Premier at turbo play: ~2.4
  exceptions/sec against a monitor polling 4.5/sec, with **zero** overflow —
  so a fast player does not outrun a loop that is actually polling, and
  raising the poll rate is not the fix. What matters is the **budget**: at
  2.4/sec a ~35-deep queue is about **15 seconds** of not polling. The usual
  culprit on a gateway is its own long polls — one general poll per cycle,
  then a meter sweep of dozens of exchanges during which nothing drains the
  queue. This times the real sweep (it imports `sql_poll_logger`'s own
  `_poll_all_meters` rather than imitating it) in each `--skip-*`
  configuration and converts each duration into exceptions accumulated.
  ```
  python3 examples/poll_gap_probe.py /dev/ttyS0 --address 1
  ```
- **`examples/exception_buffer_probe.py`** — read-only. Measures how many
  exceptions a machine really buffers. §2.2.1 requires "a first in/first out
  (FIFO) exception queue of **at least 20 elements** in non-volatile
  memory", discards the **oldest** on overrun, and requires exception `0x70`
  to report any loss — which contradicts the common belief that SAS holds
  only one pending exception. Since a general poll pulls exactly one
  exception, the queue can only be filled while nobody polls: the tool
  drains it, stops polling while you cycle the slot door a known number of
  times, then polls flat out and counts what returns.
  ```
  python3 examples/exception_buffer_probe.py /dev/ttyS0 --address 1 --cycles 15
  ```
  The diagnostic value is in `0x70`'s absence as much as its presence: a
  gateway losing exceptions that never sees `0x70` is not losing them to
  queue overrun, and the cause is elsewhere.
- **`examples/soak_report.py`** — read-only; reads a soak's SQLite database
  and says whether the link held. It buckets failures by hour and minute
  and reports the worst minute alongside the mean, because the failure mode
  that matters is a **bursty** one: 43 failures over four hours is 0.18/min
  — a "99.97% success rate" — and is an outage if 37 of them land in the
  same minute. A single percentage describes neither case. Also checks that
  no cumulative meter ever decreased, flags any stall in the snapshot
  cadence, and prints the current meters as dollars for comparison against
  the machine's own accounting screen.
  ```
  python3 examples/soak_report.py soak.db
  ```
- **`examples/aft_status.py`** — read-only AFT interrogation: long polls
  `0x73` and `0x74` with their query-only codes (`0xFF`, "report state,
  change nothing"). Answers whether a host is registered, what the machine's
  transfer limit is, and what credits are on it — before anyone sends
  `0x72`, which is the one command in this library that moves money and has
  never run against hardware.
  ```
  python3 examples/aft_status.py /dev/ttyS0 --address 1
  ```
- **`examples/commission_gateway.py`** — run this once when a gateway is
  attached to a new EGM: scans ports/addresses, confirms a find by
  querying the machine's SAS version and serial number, and writes a
  `gateway.ini` the other tools can load. Optional — a config file can
  always be hand-written.
- **`examples/sql_poll_logger.py`** — one poll loop, one SQLite database:
  a wide, deliberately redundant meter sweep (259+ columns across every
  meter long poll this client implements — core, ticket, last-accepted-
  bill info, ~39 single-meter polls, essentially the rest of Table C-7
  via chunked extended-meter reads, and per-validation-type meters;
  `--skip-full-meter-sweep`/`--skip-table-c7-sweep`/
  `--skip-validation-meters-sweep`/`--skip-last-accepted-bill-poll` to
  lighten or work around any of them), immediate re-poll on a failed general poll and a
  validation-pool age alert (Decisions Annex D-09/D-16), full
  ticket-out history (buffer backfill at startup plus live capture),
  read-only ticket-in capture plus a `ticket_in_history` view
  correlating each insertion with how its redemption cycle ended, and
  gateway-local cashout validation
  against a seeded local pool, all from a single general-poll stream
  (deliberately one program — see the module docstring for why). A
  runnable starting point for stress-testing over time or prototyping a
  persistence layer — see MANUAL.md §4/§6 for the schema and reasoning.

  **An unsupported meter does not abort a cycle.** §4.4 requires a machine
  that doesn't support a long poll to ignore it silently, so a single-meter
  poll returns a timeout for "unsupported" and a timeout for "no answer" —
  the same bytes for two different facts. Treating both as fatal meant a
  cashless machine (no hopper, so LP `0x2C` never answers) abandoned **every**
  cycle's meter write, which is what the first EGT Premier did: zero meter
  rows and one `poll_errors` row per cycle, indefinitely. The distinction is
  now made over time — a poll that has *never* answered is taken as
  unsupported and its column left NULL, while one that answered before and
  then goes silent is still a real failure and still aborts. A
  permanently-NULL column doesn't misrepresent "as of `polled_at`" (the
  machine has no such meter); one populated yesterday and missing today
  does. Only a timeout qualifies — a checksum error is a link fault whatever
  the machine supports.
- **`legacy/`** — an earlier Python 2 implementation, kept for reference.
  **It has known, confirmed bugs** (wrong byte offsets, a fabricated
  field on a fund-transfer command, crashes on certain inputs) — don't
  use it as a second source of truth, and don't build on it. It's here
  purely as command-coverage history. `legacy/pre_pollcode_expansion/`
  is unrelated: a frozen snapshot of `saspy/constants.py`, `client.py`,
  and `models.py` from just before LP 2F/6F/4C/4D/7B were added, kept
  as a fast rollback path during live floor testing — see its own
  `README.md`.

## Setup

```
pip install -e ".[dev]"
pytest
```

Requires Python 3.10+ and `pyserial`. Works on Linux, and should work
unmodified on Windows/macOS too — the serial handling goes entirely
through pyserial's cross-platform API, nothing Linux-specific.

## Long polls implemented

This covers every data-returning long poll in the SAS 6.02 spec's own
Appendix B (Table B-1) — the complete, authoritative command list — that
is realistically relevant to a TITO/AFT/meters gateway. See
`saspy/client.py` and `saspy/constants.py` — every method and constant
cites the spec table it was built from, and `constants.py`'s module
docstring explains the scope boundary below.

- **General poll** (0x80/addr).

**Link note — the wake byte.** `SASTransport` sends a throwaway `0x80`
before each message by default (`wake_byte`, `wake_gap`,
`wake_after_idle`), and `wake_byte=None` switches it off.

It is there because on the first EGT machine tested, a general poll after
~0.5s of silence was answered **0/20** while the identical poll preceded
by `0x80` and 5 ms was answered **20/20** — the preamble's own reply
discarded, so the reply provably belonged to the real poll.

**But that is not a settled result.** A later run measured the same bare
poll at **13/20**, and bare polls at **20/20** across every silence from
5 ms to 1 s. Something on this link is intermittent, and until it is
characterised the wake byte stays on because it has never made anything
worse — not because it is known to be required. It is an empirical
workaround in any case, not SAS: the spec asks for nothing of the kind.

Two things follow. Do **not** raise `wake_after_idle` off a run that had
a preamble present throughout — that measures the gap a preamble works
across, not whether the line stays warm without one. And treat any
condition measured as a *block* of attempts with suspicion on this link:
`examples/preamble_probe.py` interleaves and rotates its conditions, and
reports how each one's successes split across the first and second half
of the run, precisely so drift cannot pose as a property of the byte.
- **Enable/disable** (0x01/0x02): remote shutdown ("lock out play") and
  startup, the only command/control long polls this project wants to
  send remotely — see "Deliberately not implemented" below for why
  every other one (sound, bill acceptor, maintenance mode, ...) isn't
  here. `send_shutdown()`/`send_startup()` raise `SASCommandNackedError`
  if the machine rejects the command (Table 7.4b's NACK) rather than
  returning silently — an ACK means only that the command was accepted,
  not that a shutdown has actually finished happening on the machine yet.
- **Meters**: core (0x0F, 0x19, 0x1C — see `constants.py` for why 0x1C
  isn't literally "11 through 15" despite an earlier misleading name),
  single-meter reads (0x10-0x18, 0x1A, 0x20, 0x2A-0x2C, bill/stacker
  meters 0x31-0x4A) via `send_meter()`, selected/extended meters (0x2F,
  0x6F, 0xAF — 0x6F/0xAF reach essentially the whole of Table C-7, 162
  codes, including ticket meters such as Cashable Tickets In that the
  core-meter polls cannot reach at all), per-game meters and
  configuration (0x52, 0x53), hand-paid cancelled credits (0x2D), total
  bill meters (0x1E), current hopper status (0x4F), last accepted bill
  information (0x48), per-validation-type validation meters (0x50).
- **Machine identity/config**: gaming machine ID (0x1F), SAS version and
  serial (0x54), enabled features (0xA0), token denomination (0xB3),
  selected/enabled game numbers (0x55, 0x56), games implemented (0x51),
  wager category info (0xB4), extended game info (0xB5), current
  date/time (0x7E), physical reel stops (0x8F).
- **AFT**: register (0x73), lock/status (0x74), transfer funds (0x72).
- **Real time event reporting** (0x0E): `enable_real_time_events()` plus
  `general_poll_rte()`, which returns a `RealTimeEvent` decoded per Tables
  12.5.1–12.5.8, or `None` for no activity (§12.6 makes silence the idle
  signal in this mode, the opposite of `general_poll()`). Table 12.5 has no
  length field, so `RTE_EVENT_DATA_BYTES` **is** the framing: a wrong entry
  desynchronises the stream rather than spoiling one field. The reason to
  turn it on is usually §12.5.3 — game start carries the total coin in meter
  *after* the wager, so a session's wager reconciles from its boundary events
  rather than from separate meter reads. §12.7's ROM-signature response
  (which omits the `FF` identifier) raises rather than being parsed as a
  bogus exception code.
- **Ticketing**: secure enhanced validation ID (0x4C), enhanced
  validation information / ticket-out history (0x4D), extended
  validation status (0x7B), ticket validation data (0x70), redeem ticket
  (0x71, plus the short-form, read-only status query), validation number
  (0x58), pending cashout info (0x57), cash-out ticket info (0x3D),
  handpay information (0x1B).

**Deliberately not implemented**, and why: every other command/control
poll (sound on/off, bill acceptor enable/disable, maintenance mode,
delay game, enable/disable game, remote handpay reset, jackpot-reset-
method/auto-rebet enable, receive-progressive/date-time/ticket-data
setters) — a scope decision, not an oversight: this project explicitly
does not want to override machine setup remotely beyond the enable/
disable pair above. Progressive jackpots,
tournament mode, legacy bonusing, and card/reel-stop data — out of this
project's stated TITO/AFT/meters scope; long poll 0x8B (multiplied
jackpots) specifically because the spec itself recommends against
implementing it. Component authentication (0x6E, Section 17) and the
multi-denom preamble (0xB0, a request-wrapping mechanism rather than a
standalone poll) — real gaps, not scoped out, just not yet built.

`MeterCode` (in `constants.py`) covers essentially the whole of Table
C-7 — 162 codes: the ticket meters, the core/extended/bill-denomination
range, SAS-validation-specific meters, and AFT-specific meters — every
assigned code except the table's own reserved gaps. All transcribed
directly from the spec's own table, not from any secondary source — an
earlier project planning document had the ticket-meter codes wrong (off
by more than a naming slip: 0x1A/0x1B are unrelated meters in the real
table), which is worth knowing if you're cross-checking against project
docs rather than the spec itself. `examples/sql_poll_logger.py` reads
essentially all of it every cycle by default (`--skip-table-c7-sweep`
to opt out) via `send_extended_meters()` (LP 0x6F), 12 codes per
exchange. The 12 validation-type meters at Table C-7 codes 0x80+ are
also independently readable one at a time via LP 0x50
(`send_validation_meters()`) — explicitly redundant with the Table C-7
sweep per the spec's own note, and read that way too by default
(`--skip-validation-meters-sweep` to opt out); no batching on 0x50, so
it's the most expensive of the sweeps per meter actually obtained.

## Verified against real hardware

`tests/test_hardware_vectors.py` is built from one EGT Premier (SAS version
602, serial 000106719, paytable MGDJE3), and each value in it was read over
SAS and then found on that machine's own Machine Accounting display, to the
cent:

| SAS field (LP `0x0F`) | read | machine's own screen |
|---|---|---|
| `total_coin_in` | 21387723 | Total Bet (Coin In) 213,877.23 USD |
| `total_coin_out` | 20718334 | Total Won 207,183.34 USD |
| `total_drop` | 6408958 | Total In 64,089.58 USD |
| `total_cancelled_credits` | 5739569 | Total Cancelled Credits 57,395.69 USD |
| `games_played` | 665524 | Total Games Played 665,524 |

Every other test here is derived from the spec tables, so it proves the code
matches somebody's reading of the document. These prove it matches a gaming
machine — which is a different claim, and the only one that catches a field
**order** slip: swapping two meters still decodes to plausible numbers and
still passes every spec-derived test.

One value is recorded as observed rather than verified. LP `0x1F` reported
`max_bet=255`, which is a legitimate figure and also `0xFF`, the saturation
value a too-narrow or unsupported field produces. Check it against the
machine's own max-bet setting before relying on it.

## Multi-version SAS (6.0 / 6.01 / 6.02)

The long polls implemented here are all from the command set that's been
stable across the whole 6.x line, and the protocol itself is designed for
mixed-version fleets: a machine that doesn't support a poll simply
doesn't answer it, and the intended way to discover what a given machine
supports is long poll 0xA0 (Send Enabled Features), not a version check
up front. One thing to know if your lab includes machines on meaningfully
different SAS revisions: `send_enabled_features()` currently assumes an
exact response length — if you hit a machine where that assumption
breaks, that's the first place to look.

## What this is for

Two things this repo is built to support:

1. **Connectivity and stress testing** — run this against real EGMs
   across whatever environments you've got, at whatever concurrency and
   duration makes sense for your lab. If something breaks, the client
   raises typed exceptions (`SASTimeoutError`, `SASChecksumError`,
   `SASAddressMismatchError`) rather than failing silently or crashing on
   a bad value — that's deliberate, and worth preserving in whatever you
   build on top.
2. **Architecture prototyping** — this library only does the wire
   protocol; it holds no state beyond one exchange and writes nothing to
   disk. That's the intended seam: plug your own persistence layer in
   above `SASClient` (a poll loop that calls its methods and writes
   results to SQL, run on the gateway) to prototype the actual gateway
   architecture.

## Reporting issues

Findings from stress testing — timeouts, unexpected responses, anything
that doesn't match a spec table — are genuinely useful upstream. Please
include the exact long poll, the raw bytes on the wire if you can capture
them, and what you expected instead.
