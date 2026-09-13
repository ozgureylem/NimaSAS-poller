# NimaSAS-poller

A clean-room Python client for the GSA/IGT **SAS 6.02** slot accounting
system protocol — the SAS-side of the gateway software this project is
building, for stress-testing on real hardware and in your own lab
environments.

This repo is deliberately code-only: no protocol spec, no project
background, no vendor tooling. Full documentation and project context are
being shared with the team by email — this is the thing to actually run.

## What's here

- **`saspy/`** — the client itself: CRC-16, BCD/binary field codecs, frame
  building/parsing, a wakeup-bit-aware serial transport, and a `SASClient`
  covering general poll plus the long polls listed below.
- **`tests/`** — 40 tests, all against fake serial ports; run them with
  no hardware attached to confirm your environment is set up right before
  you touch real wiring.
- **`examples/connectivity_check.py`** — point this at a real port and
  address and it general-polls, then tries a few read-only long polls.
  Changes nothing on the machine. Start here for wiring/connectivity
  checks:
  ```
  python3 examples/connectivity_check.py /dev/ttyUSB0 --address 1
  ```
- **`legacy/`** — an earlier Python 2 implementation, kept for reference.
  **It has known, confirmed bugs** (wrong byte offsets, a fabricated
  field on a fund-transfer command, crashes on certain inputs) — don't
  use it as a second source of truth, and don't build on it. It's here
  purely as command-coverage history.

## Setup

```
pip install -e ".[dev]"
pytest
```

Requires Python 3.10+ and `pyserial`. Works on Linux, and should work
unmodified on Windows/macOS too — the serial handling goes entirely
through pyserial's cross-platform API, nothing Linux-specific.

## Long polls implemented

General poll; meters (0x0F, 0x1C); gaming machine ID (0x1F); enabled
features (0xA0); AFT register (0x73), lock/status (0x74), transfer funds
(0x72); ticket validation data (0x70), redeem ticket (0x71); validation
number (0x58). See `saspy/client.py` — every method cites the spec table
it was built from.

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
