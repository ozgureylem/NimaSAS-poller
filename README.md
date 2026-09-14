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
- **`tests/`** — 157 tests, all against fake serial ports; run them with
  no hardware attached to confirm your environment is set up right before
  you touch real wiring.
- **`examples/connectivity_check.py`** — point this at a real port and
  address and it general-polls, then tries a few read-only long polls.
  Changes nothing on the machine. Start here for wiring/connectivity
  checks:
  ```
  python3 examples/connectivity_check.py /dev/ttyUSB0 --address 1
  ```
- **`examples/commission_gateway.py`** — run this once when a gateway is
  attached to a new EGM: scans ports/addresses, confirms a find by
  querying the machine's SAS version and serial number, and writes a
  `gateway.ini` the other tools can load. Optional — a config file can
  always be hand-written.
- **`examples/sql_poll_logger.py`** — one poll loop, one SQLite database:
  a wide, deliberately redundant meter sweep (~80 columns across every
  meter long poll this client implements — core, ticket, and ~39
  single-meter polls, `--skip-full-meter-sweep` to lighten it), full
  ticket-out history (buffer backfill at startup plus live capture),
  read-only ticket-in capture, and gateway-local cashout validation
  against a seeded local pool, all from a single general-poll stream
  (deliberately one program — see the module docstring for why). A
  runnable starting point for stress-testing over time or prototyping a
  persistence layer — see MANUAL.md §4/§6 for the schema and reasoning.
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
- **Meters**: core (0x0F, 0x19, 0x1C — see `constants.py` for why 0x1C
  isn't literally "11 through 15" despite an earlier misleading name),
  single-meter reads (0x10-0x18, 0x1A, 0x20, 0x2A-0x2C, bill/stacker
  meters 0x31-0x4A) via `send_meter()`, selected/extended meters (0x2F,
  0x6F, 0xAF — the only way to reach ticket meters such as Cashable
  Tickets In, since the core-meter polls cannot), per-game meters and
  configuration (0x52, 0x53), hand-paid cancelled credits (0x2D), total
  bill meters (0x1E), current hopper status (0x4F).
- **Machine identity/config**: gaming machine ID (0x1F), SAS version and
  serial (0x54), enabled features (0xA0), token denomination (0xB3),
  selected/enabled game numbers (0x55, 0x56), games implemented (0x51),
  wager category info (0xB4), extended game info (0xB5), current
  date/time (0x7E), physical reel stops (0x8F).
- **AFT**: register (0x73), lock/status (0x74), transfer funds (0x72).
- **Ticketing**: secure enhanced validation ID (0x4C), enhanced
  validation information / ticket-out history (0x4D), extended
  validation status (0x7B), ticket validation data (0x70), redeem ticket
  (0x71, plus the short-form, read-only status query), validation number
  (0x58), pending cashout info (0x57), cash-out ticket info (0x3D),
  handpay information (0x1B).

**Deliberately not implemented**, and why: pure command/control polls
with no data to pull (shutdown, sound on/off, bill acceptor enable/
disable, maintenance mode, delay game, enable/disable game, remote
handpay reset, jackpot-reset-method/auto-rebet enable, receive-progressive/
date-time/ticket-data setters) — a distinct, lower-priority piece of work
from "can't pull some data" if you need it later. Progressive jackpots,
tournament mode, legacy bonusing, and card/reel-stop data — out of this
project's stated TITO/AFT/meters scope; long poll 0x8B (multiplied
jackpots) specifically because the spec itself recommends against
implementing it. Component authentication (0x6E, Section 17) and the
multi-denom preamble (0xB0, a request-wrapping mechanism rather than a
standalone poll) — real gaps, not scoped out, just not yet built.

Meter codes for 0x2F/0x6F are transcribed directly from the spec's own
Table C-7, not from any secondary source — an earlier project planning
document had the ticket-meter codes wrong (off by more than a naming
slip: 0x1A/0x1B are unrelated meters in the real table), which is worth
knowing if you're cross-checking against project docs rather than the
spec itself.

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
