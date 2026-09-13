# NimaSAS User Manual

This is the manual for everything runnable in this repository: the
`saspy` library itself, and the three example tools built on top of it
(`connectivity_check.py`, `commission_gateway.py`, `sql_poll_logger.py`).
Each section starts with a plain-language explanation of what the tool
is for and how to run it, then goes deeper into flags, internals, and
troubleshooting. Read as far as you need and stop — you don't have to
read the technical part of a section to use the tool.

The last part of this manual is a step-by-step guide to giving a poller
its own local SQL database, using `sql_poll_logger.py` as a worked
example.

If you just want to know "is this repo project's private planning
material in here" — see the licensing note in `README.md`. This manual
only covers the code.

---

## 1. What this repo is, in plain terms

A slot machine (an "EGM," electronic gaming machine) and a piece of
monitoring hardware ("the gateway") talk to each other over a serial
cable using a protocol called SAS — Slot Accounting System. The gateway
asks questions ("what are your meters right now?", "any new events?")
and the EGM answers. `saspy` is a Python library that speaks the
gateway's half of that conversation. The three example scripts are
small, complete programs built on top of it:

- **`connectivity_check.py`** — "is this cable/port/address actually
  talking to a machine?" Run this first, on a new setup, before
  anything else.
- **`commission_gateway.py`** — does the same kind of check, but
  automatically, across many ports and addresses at once, and — once it
  finds a live machine — writes a small config file so you don't have
  to type connection details in by hand again.
- **`sql_poll_logger.py`** — polls a machine on a timer and saves what
  it reads into a local SQLite database file, so you have a running
  history instead of a single snapshot.

You do not need to use all three. A lot of people will only ever run
`connectivity_check.py` once to confirm wiring, then write their own
program using the `saspy` library directly. The other two are there
because they solve problems that come up often enough to be worth
sharing.

### 1.1 Before you start

You need:

- Python 3.10 or newer.
- The `pyserial` package installed (`pip install pyserial`) — only
  needed for talking to a real serial port; the protocol code itself
  has no dependencies.
- A serial connection to an EGM (or a lab/simulator that behaves like
  one), and the SAS address configured on that machine (usually `1`
  unless you've been told otherwise).

From the repo root:

```
pip install pyserial
python3 -m pytest        # optional — confirms the library works in your environment
python3 examples/connectivity_check.py /dev/ttyUSB0 --address 1
```

If that prints "Address is live," you're wired up correctly and can
move on to whichever tool fits what you're doing next. If it doesn't,
read connectivity_check.py's own troubleshooting section (1.3) below
before touching anything else — nearly every problem at this stage is
wiring, baud rate, or address, not code.

---

## 2. `connectivity_check.py`

### 2.1 What it's for

Point it at a serial port and an address, and it tells you, plainly,
whether anything is listening and responding correctly. It doesn't
change any state on the machine — every poll it sends is read-only.
This is the tool to run first on any new piece of hardware, any new
cable, or any time something that used to work stops working and you
need to rule wiring in or out before debugging further up the stack.

### 2.2 Basic use

```
python3 examples/connectivity_check.py /dev/ttyUSB0 --address 1
```

You'll see either a confirmation that the address is live (followed by
a few example reads: gaming machine ID, meters, enabled features), or
a clear statement that nothing responded, with the most common cause
named directly (wrong address).

The exit code is `0` if the machine responded, `1` if it didn't — so if
you're scripting a sweep across several ports, you can check
`$?` after each run instead of parsing output.

### 2.3 All flags

| Flag | Default | Meaning |
|---|---|---|
| `port` (positional) | — | Serial device, e.g. `/dev/ttyUSB0`, `/dev/ttyS0`, or `COM3` on Windows. |
| `--address` | `1` | SAS address to poll (valid range 1–127). |
| `--baud` | `19200` | SAS is fixed at 19200 baud by the spec; you should not normally need to change this. |
| `--cycles` | `3` | How many general-poll attempts before giving up and reporting no response. |
| `--timeout` | `1.0` | Seconds to wait for a response before considering that attempt failed. |

### 2.4 What's actually happening (technical)

The script opens the port via `saspy.serial_port.open_serial_port`,
wraps it in a `SASTransport`, and constructs a `SASClient` for the
given address. It then calls `client.general_poll()` in a loop up to
`--cycles` times. A general poll is the SAS mechanism for "does anyone
answer at this address, and do they have anything to report" — it's a
single address byte with the wakeup bit set (see §5.2 for what that
means), and the machine answers with a one-byte exception/event code.
Getting any valid response at all — even exception code `0x00`, meaning
"nothing new" — is what "live" means here.

Once an address answers, the script tries three long polls that are
guaranteed not to change machine state: Send Gaming Machine ID (`0x1F`),
Send Meters 10–15 (`0x0F`), and Send Enabled Features (`0xA0`). Any of
these individually failing (e.g. a machine that doesn't support a given
poll) is reported but doesn't stop the script — the point is to show
you what does and doesn't work, not to require everything to work.

### 2.5 Troubleshooting

- **No response at all, any address**: check the physical cable and
  that you're pointed at the right device file. On Linux, `dmesg |
  tail` after plugging in a USB-serial adapter usually shows which
  `/dev/ttyUSB*` it became.
- **No response at this address specifically**: this is the single most
  common failure. Confirm the address configured in the EGM's own
  operator menu matches `--address`.
- **Response arrives but garbled / exception raised inside the read**:
  usually a baud rate mismatch, though SAS is fixed at 19200 so this is
  rare unless you're on unusual hardware. Can also indicate wiring with
  swapped TX/RX.
- **Works sometimes, not others**: try raising `--timeout`; a busy bus
  or slow adapter can cause borderline timing. If it's still
  inconsistent, this is worth reporting (see README.md) with details of
  your adapter.

---

## 3. `commission_gateway.py`

### 3.1 What it's for

This is the tool you run once, when a gateway (the small computer
running this software) is physically attached to a new EGM for the
first time. Instead of you having to know or guess the port and
address, it scans for you, confirms it found a real machine by asking
it for its SAS version and serial number, and writes all of that into
a config file (`gateway.ini` by default) that every other tool and any
poller you write can load without you re-typing connection details.

Using this tool is optional. If you already know your port and address,
you can write the config file by hand in two minutes — see §3.4 for the
exact format — and skip this tool entirely.

### 3.2 Basic use

```
python3 examples/commission_gateway.py
```

With no arguments, it auto-discovers serial ports on the machine,
scans addresses 1–32 on each, and — if it finds exactly one live
address — asks you to confirm before writing `gateway.ini` in the
current directory.

```
python3 examples/commission_gateway.py --output /etc/nimasas/gateway.ini --address-range 1-8 --yes
```

A more targeted, non-interactive run: only checks addresses 1–8, writes
straight to a specific path without asking for confirmation.

### 3.3 All flags

| Flag | Default | Meaning |
|---|---|---|
| `--output` | `gateway.ini` | Path to write the resulting config file to. |
| `--ports` | (auto-discover) | One or more specific ports to scan, e.g. `--ports /dev/ttyUSB0 /dev/ttyS0`. Skips auto-discovery when given. |
| `--baud` | `19200` | See §2.3 — you should not normally need this. |
| `--address-range` | `1-32` | Range of SAS addresses to try per port. Accepts `N-M` or a single `N`. |
| `--poll-timeout` | `0.3` | Seconds to wait per address during the scan. Lower = faster scan, higher = more tolerant of a slow/busy line. |
| `--yes` / `-y` | off | Skip the interactive confirmation before writing. Refuses to guess if the scan found more than one live address even with this set — see §3.5. |

### 3.4 The config file it writes

```ini
[connection]
port = /dev/ttyUSB0
baudrate = 19200
address = 1
timeout = 1.0

[protocol]
sas_version = 602

[machine]
serial_number = TROP-0042
game_id = 01
```

`[connection]` is the only required section — `port` and `address` are
the only required keys within it; `baudrate` and `timeout` default to
19200 and 1.0 if omitted. `[protocol]` and `[machine]` are metadata the
commissioning tool fills in as a record of what it found; nothing in
`saspy` reads `[machine]` back programmatically — it's there for
humans and for audit trail.

You can write this file by hand with any text editor if you'd rather
not run the scanner — that's a fully supported path, not a fallback.

### 3.5 What's actually happening (technical)

For each candidate port, `scan_port()` opens it once and then tries a
`general_poll()` at every address in the range in sequence, on the
theory that (per §5.2) only one address should answer per physical bus
in a correctly wired 1:1 gateway/EGM setup. Every address that
responds is collected as a `(port, address)` candidate. If the scan
finds exactly one candidate, it proceeds automatically; if it finds
more than one, it either asks you to choose interactively, or, with
`--yes`, refuses outright rather than silently picking one — the
reasoning being that a wrong guess here writes bad wiring information
into a config file that everything downstream will trust.

Once a candidate is chosen, `describe_machine()` calls
`send_sas_version_and_serial()` (long poll `0x54`) and
`send_gaming_machine_id()` (long poll `0x1F`). Either can fail
independently (some machines/firmware don't implement `0x54`, for
instance) without aborting the run — a missing field is written as
empty/`(unknown)` rather than stopping the tool, since the primary
goal (finding the right port and address) doesn't depend on these
succeeding.

### 3.6 Troubleshooting

- **"No serial ports found"**: pass `--ports` explicitly — some
  environments (containers, some USB-serial chipsets) don't show up
  under auto-discovery.
- **"Nothing responded"**: same causes as connectivity_check.py §2.5 —
  wiring, address, or an address range that doesn't include the
  machine's actual configured address.
- **Multiple candidates found**: this normally means either the address
  range is too wide (catching noise or another device on a shared bus)
  or there's genuinely more than one machine wired to this gateway,
  which isn't the topology this tool assumes. Narrow `--address-range`
  or `--ports` to isolate the one you actually want.
- **SAS version / serial number shows "(unknown)"**: the address itself
  is confirmed live (the general poll worked) but long poll `0x54`
  didn't answer — could be unsupported by that machine's SAS
  implementation. This doesn't block commissioning; it just means less
  metadata in `[machine]`.

---

## 4. `sql_poll_logger.py`

### 4.1 What it's for

This tool polls a machine on a repeating timer and writes what it reads
into a local SQLite database file, so instead of a single snapshot you
get a running history — useful for stress testing (does the connection
survive hours of continuous polling?), for basic monitoring, or as a
starting point for a real logging/sync service.

### 4.2 Basic use

```
python3 examples/sql_poll_logger.py gateway.ini
```

This reads connection details from `gateway.ini` (the file
`commission_gateway.py` writes — see §3.4), polls the meters every 5
seconds, and writes them into `gateway.sqlite3` in the current
directory. Press Ctrl-C to stop.

```
python3 examples/sql_poll_logger.py gateway.ini --db /var/log/nimasas/lab1.sqlite3 --interval 2 --cycles 500
```

Polls every 2 seconds, writes to a specific database path, and stops
automatically after 500 cycles — useful for an unattended, bounded
stress-test run.

### 4.3 All flags

| Flag | Default | Meaning |
|---|---|---|
| `config` (positional) | — | Path to a gateway `.ini` file (see §3.4). |
| `--db` | `gateway.sqlite3` | SQLite file to write to. Created automatically if it doesn't exist. |
| `--interval` | `5.0` | Seconds to sleep between poll cycles. |
| `--cycles` | `0` | Stop after this many cycles. `0` means run until Ctrl-C. |

### 4.4 What's actually happening (technical)

On startup, it loads the config with
`saspy.config.connect_from_config()` (opens the port, builds a
`SASTransport` and `SASClient` in one step) and creates two tables if
they don't already exist — see §6.3 for the schema and why it's shaped
this way. Each cycle calls `client.send_meters_10_through_15()`
(long poll `0x0F`); on success the six meter values are inserted into
`meter_snapshots`, on a `SASError` the exception's type and message are
inserted into `poll_errors` instead, and either way the loop continues
after `--interval` seconds. This is deliberate: one bad exchange —
a timeout, a checksum failure, anything — never stops the run. For
stress testing specifically, the failures are often the interesting
data, and a logger that dies on the first transient error defeats the
purpose.

### 4.5 Troubleshooting

- **Starts, then immediately exits with a `SASError`**: this is
  `connect_from_config()` failing to open the port — check the config
  file's `port` value and that nothing else has the port open.
- **Runs but every cycle logs to `poll_errors`**: the config's address
  or port is wrong, or the machine stopped responding after
  commissioning (loose cable, power-cycled EGM). Run
  `connectivity_check.py` against the same port/address to isolate
  whether this is a `sql_poll_logger.py` problem or a wiring problem —
  it usually isn't the former.
- **Database file grows large during a long stress run**: expected —
  this script never deletes rows. For a long-running lab test, either
  plan disk space accordingly or add your own periodic cleanup; see the
  forward-looking note in §6.6.

---

## 5. `saspy` — the library itself

### 5.1 What it's for, in plain terms

The example scripts above cover common cases, but at some point you'll
want to write your own program — a real polling service, an
integration into some other system, whatever your project actually
needs. `saspy` is what you build that on. It knows how to construct
and parse SAS frames correctly, handle timeouts and checksums, and
expose the machine's answers as ordinary Python objects. You do not
need to understand the wire protocol to use it — that's the point of
the library — but this section explains enough of it that the API
makes sense, and goes further for anyone who does need the detail.

### 5.2 Minimal example

```python
from saspy.client import SASClient
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

serial_port = open_serial_port("/dev/ttyUSB0", baudrate=19200)
transport = SASTransport(serial_port)
client = SASClient(transport, address=1)

exception_code = client.general_poll()
meters = client.send_meters_10_through_15()
print(meters.total_coin_in, meters.games_played)
```

Or, if you already have a config file (§3.4):

```python
from saspy.config import connect_from_config

client = connect_from_config("gateway.ini")
meters = client.send_meters_10_through_15()
```

### 5.3 What each piece does

- **`SASClient`** — the object you actually call methods on. One
  method per implemented long poll (`send_meters_10_through_15()`,
  `send_gaming_machine_id()`, `send_sas_version_and_serial()`,
  `send_enabled_features()`, plus the AFT and ticket-validation long
  polls — see the class itself for the full current list, since it
  grows over time). Each returns a frozen dataclass from
  `saspy/models.py` with named, typed fields — never a raw dict or
  tuple of bytes.
- **`SASTransport`** — owns the actual bytes-on-the-wire exchange:
  sending a poll with the wakeup bit correctly set, reading back either
  a fixed-length or length-prefixed response depending on poll type,
  and enforcing a single-flight lock so two threads can never have
  their writes interleaved mid-exchange (an in-flight poll is never
  interrupted — see the ACK-atomicity note in the module docstring).
- **`open_serial_port`** — a thin wrapper around `pyserial` returning
  something `SASTransport` can use. Kept as a separate module so the
  rest of the library never has a hard dependency on `pyserial` —
  useful if you're testing protocol logic against a fake port.
- **`GatewayConfig` / `load_config` / `save_config` /
  `connect_from_config`** (`saspy/config.py`) — the config file layer
  described in §3.4.
- **Exceptions** (`saspy/exceptions.py`) — `SASError` is the base class
  everything else inherits from, so `except SASError` catches anything
  the library can raise from a poll. More specific subclasses:
  `SASTimeoutError` (no/incomplete response in time),
  `SASChecksumError` (CRC didn't match), `SASAddressMismatchError`
  (response came from a different address than expected),
  `SASEncodingError` (malformed BCD/ASCII field in a response),
  `SASPortCapabilityError` (the serial port/driver couldn't do
  something the protocol needs — see §5.5).

### 5.4 The wakeup bit, briefly

Every poll to a machine needs to mark the first byte as "this is a new
poll starting" versus "this is a continuation." SAS hardware does this
using mark/space parity on that one byte — a physical-layer signal, not
a data byte. `SASTransport.write_with_wakeup()` handles this for you;
you never need to touch parity settings directly through the normal
`SASClient` API. The one thing worth knowing: this requires a serial
port/driver that actually supports mark/space parity. Most real
USB-to-RS232 and RS485 adapters do; some virtual or exotic ports don't.

### 5.5 `SASPortCapabilityError`

If the underlying port rejects the parity change the wakeup bit needs,
`saspy` raises `SASPortCapabilityError` (a subclass of `SASError`)
rather than letting a raw `OSError`/`termios.error` escape. If you hit
this, it means the specific port/driver you're using can't do
mark/space parity — this is a hardware/driver limitation, not a bug to
patch around in your polling code. Try a different adapter, or confirm
with your adapter's documentation that RS485/RS232 parity control is
actually supported.

### 5.6 What's implemented vs. not, and multi-version SAS

This library targets SAS 6.02, and was built and verified against the
6.02 spec. SAS is designed to be substantially backward compatible —
6.0 and 6.01 EGMs generally speak the same general-poll and long-poll
framing this library implements. In practice, whether a given older
machine answers a specific long poll correctly depends on that
machine's own firmware, not on this library. `send_sas_version_and_serial()`
(long poll `0x54`, used by `commission_gateway.py`) is the
recommended way to find out what a given machine actually claims to
speak before assuming everything here applies. Treat any specific
poll's behavior against a 6.0 or 6.01 machine as something to verify
against that machine, not something this library guarantees.

The full current set of implemented long polls is best read directly
from `saspy/client.py` and `tests/test_client.py`, since it's the part
of this project most likely to grow; this manual won't try to keep a
duplicate list in sync.

### 5.7 `legacy/` is not a tool

`legacy/sas.py` and `legacy/test_sas.py` are the original
pre-clean-room implementation, kept in the repo as reference only —
they have known bugs (documented in their own header comments) and are
not exercised by anything else here. This manual intentionally doesn't
give them a usage section, because they aren't meant to be used.

---

## 6. Structuring a SQL layer around a poller: step by step

`sql_poll_logger.py` (§4) is a complete, runnable example of everything
in this section — read this alongside that file, not instead of it.

### 6.1 Why persist anything at all

A poller that only prints to the screen is fine for a five-minute
check. The moment you want any of the following, you need storage:
a history longer than your terminal's scrollback, the ability to
answer "what did coin-in look like over the last six hours," survival
across a restart, or eventually feeding data to something else (a
dashboard, a central server, an alerting rule). SQLite is the right
starting point for all of this on a single gateway: it's a single file,
needs no server process, ships in Python's standard library, and is
more than fast enough for one machine polling every few seconds.

### 6.2 The simple version: one table, one script

At the simplest level, the pattern is three steps, run in a loop:

1. Poll the machine.
2. If it succeeded, insert the result as a new row.
3. If it failed, don't crash — log the failure and try again next
   cycle.

That's the entire shape of `sql_poll_logger.py`'s `poll_and_log()`
function (`examples/sql_poll_logger.py:63`). If you're adapting this
for your own project, this is the function to copy and change: swap
`send_meters_10_through_15()` for whichever poll(s) you actually care
about, and change the `INSERT` to match.

### 6.3 Schema design principles (and the concrete schema)

A few decisions worth making deliberately, illustrated by
`sql_poll_logger.py`'s schema:

```sql
CREATE TABLE IF NOT EXISTS meter_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    polled_at TEXT NOT NULL,
    total_cancelled_credits INTEGER,
    total_coin_in INTEGER,
    total_coin_out INTEGER,
    total_drop INTEGER,
    total_jackpot INTEGER,
    games_played INTEGER,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS poll_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    poll_name TEXT NOT NULL,
    error_type TEXT NOT NULL,
    message TEXT
);
```

- **Append-only, never update-in-place.** Every successful poll becomes
  a new row, even if the values are identical to the last one. Meters
  in SAS are cumulative counters, so a plain time series of raw reads
  is both the simplest schema and the most useful one — you can always
  compute deltas or "last known value" from it later, but you can't
  recover history from a table that only ever overwrites itself.
- **Separate the error log from the data table.** `poll_errors` exists
  so a failed poll never has to be encoded as, say, a row of nulls in
  `meter_snapshots` — a null in a meters row would be genuinely
  ambiguous (did the machine report zero, or did the poll fail?).
  Keeping failures in their own table with the exception type and
  message keeps `meter_snapshots` clean and makes "how often is this
  failing, and how" its own easy query.
- **Store a timestamp you generate, not one the trust the machine to
  give you.** `polled_at`/`occurred_at` are set by the poller
  (`utc_now()`, UTC, ISO 8601) at the moment of the exchange, not
  parsed out of the SAS response — this is when you need to know it
  happened, and it's consistent even against machines that don't
  report their own clock.
- **Leave a documented "not yet handled" column for future
  consumers.** `synced_at` starts `NULL` on every row and this script
  never sets it — see §6.6.

### 6.4 Running and verifying it

```
python3 examples/sql_poll_logger.py gateway.ini --cycles 3
sqlite3 gateway.sqlite3 "SELECT * FROM meter_snapshots;"
sqlite3 gateway.sqlite3 "SELECT * FROM poll_errors;"
```

`--cycles 3` gives you a short, bounded run to confirm rows are landing
correctly before committing to a long unattended run. If you don't
have the `sqlite3` CLI installed, `python3 -c "import sqlite3;
print(sqlite3.connect('gateway.sqlite3').execute('SELECT * FROM
meter_snapshots').fetchall())"` works just as well.

### 6.5 Adapting this for stress testing across multiple gateways/environments

The pattern scales sideways easily: each gateway gets its own
`gateway.ini` (via `commission_gateway.py` or hand-written) and its own
`--db` path, e.g. `--db lab1.sqlite3`, `--db lab2.sqlite3`. Nothing in
the schema or the script assumes there's only one gateway in the world
— the isolation is just "one process, one config, one database file"
per machine under test, which also means one gateway misbehaving
(crashing, filling its disk, hitting the machine too hard) can't affect
another test running in parallel. If you want a single combined view
across several lab machines' results, that's a job for a separate
script that reads several `.sqlite3` files (or use SQLite's
`ATTACH DATABASE`) rather than trying to make the pollers write to a
shared database directly — keeping each poller's writes local avoids
needing any network/locking coordination between them just to log
data.

### 6.6 A note on designing for eventual central sync

At some point, data in these per-gateway SQLite files needs to get to
a central place — a server, a shared database, wherever the wider
project's architecture consumes it. This manual deliberately doesn't
specify that architecture: it's covered in the project's own internal
planning documents, which are being shared with the team separately.

What's worth knowing here, generically, is why `synced_at` is in the
schema at all: it gives any future sync process a cheap, obvious
query — `WHERE synced_at IS NULL` — to find rows nobody has
acknowledged yet, without the local poller needing to know anything
about how or where syncing happens, what transport it uses, or how
often it runs. That separation of concerns (the poller's only job is
"poll reliably and log everything, including failures"; a sync
process's only job is "move rows somewhere and mark them synced") is
the one piece of forward-looking design in this example worth carrying
into whatever you build next.

---

## 7. Where to go next

- `README.md` — repository layout and the licensing note on
  `docs/spec/` and `docs/sastest/` (private repo only — not present in
  the public code-only repo).
- `saspy/client.py` and `tests/test_client.py` — the authoritative,
  current list of implemented long polls.
- `saspy/crc.py` and `saspy/framing.py` — the module docstrings there
  document the CRC-16 wire-byte-order finding and general frame
  layout, for anyone extending the protocol coverage.
