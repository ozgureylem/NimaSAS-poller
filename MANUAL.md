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

This tool runs one continuous poll loop against a machine and writes
everything it sees into a local SQLite database file: meters (a current
value plus a running history), ticket-out history, ticket-in capture,
and gateway-local cashout validation. One program, one poll loop, one
database — not four separate tools — because only one process can
safely own the serial port, and ticket/cashout capture depends on
seeing the same general-poll stream meters share the connection with
(see §4.4).

**Meters** cover as much of this client's meter coverage as fits in one
row: the six core counters (`0x0F`), the eight cumulative ticket meters
(`0x2F`, the only way to reach these), independent re-reads of several
of the same core counters via `0x19`/`0x1C` (`0x1C` also adds games
won/slot door opened/power reset), games since power-up/door-closure
(`0x18`), bill meters by denomination (`0x1E`), hand-paid cancelled
credits (`0x2D`), current hopper status (`0x4F`), unless
`--skip-full-meter-sweep` is given every other single-meter long poll
this client implements (`0x10`-`0x51`/`0x55`, roughly 39 more), and
unless `--skip-table-c7-sweep` is given essentially the rest of Table
C-7 — every remaining assigned meter code (~154 more: per-denomination
bill-acceptor counts, SAS-validation-specific meters, AFT transfer
meters, and more) read via `0x6F` in ~13 chunked exchanges of 12 codes
each, self-describing size. See `ALL_METER_FIELDS` in
`sql_poll_logger.py` for the exact, current column list — north of 230
columns, that source list is the one this manual won't try to keep a
duplicate of. Several of these are deliberately redundant: three long
polls disagreeing about "total coin in" this cycle is a real finding a
single poll can never surface, and this is a reference/stress-testing
tool, not one trying to economize on wire traffic — see §4.4 and the
module docstring for the full reasoning, and `--skip-full-meter-sweep`/
`--skip-table-c7-sweep` if either sweep is too much traffic for your
hardware — the Table C-7 sweep is much cheaper per meter (12 per
exchange) than the single-meter one, so it's worth leaving on even
where that one isn't affordable. All of it lands in one row, one
history table, in one of two modes picked with `--mode`:

- **`ring`** (the default) — a single always-current-value row, plus a
  history table capped at a fixed size (oldest rows evicted) and
  written on its own, slower schedule. This is the shape to deploy on
  real gateway hardware, and the reasoning for why is worth reading —
  see §6.7.
- **`append`** — the original behavior: every poll writes a new,
  never-deleted history row. Right for a lab machine or a stress-test
  run on ordinary disk, where you want every sample and don't care
  about write volume.

**Ticket-out history** (`ticket_out_history`) is read two ways: once at
startup, every buffer position the machine holds (5–31 records,
non-destructive); and continuously, drained in full every time the
machine signals a new one is ready. Rows are deduplicated, so re-running
the startup read or seeing the same ticket both ways never double-counts
it.

**Ticket-in capture** (`ticket_in_events`) logs the validation data and
amount of every ticket a player inserts, the moment it happens.
`ticket_in_completions` separately logs how that cycle ended — stacked
or rejected, via exception `0x68` and a safe, read-only status query
(`redeem_ticket_status()`, long poll `0x71`/`FF`). Both stay
**read-only** — this tool never authorizes or redeems a ticket (see
§4.4 for why, and what happens to an unredeemed ticket); the second
table only observes an outcome that already happened, it never decides
one.

**Gateway-local cashout validation** (`validation_pool`) is the other
direction: when a machine is ready to print a cashout ticket, it waits
for the host to hand it a validation number. This tool answers that
locally, from a pool you seed in advance (`--seed-validation-pool`, or
hand-inserted real numbers) — no server round-trip, which is exactly
the split your own project's Technical v3 §5.4/§5.8 describes: cashout
is gateway-authority, redemption isn't. See §4.4 for the full mechanism
and §6.3 for why this direction gets a spend-from pool while ticket-in
only ever gets a read-only log. Every cycle also checks the pool's
*age* (`--pool-age-alert-hours`, default 36 — Decisions Annex D-16): a
pool that stale is raised as an active, repeated alert, never used to
block dispensing — age isn't an integrity signal, it's a sign that
nothing is topping this gateway's pool up.

**General-poll reliability** (Decisions Annex D-09): a failed general
poll is re-polled immediately, up to `--general-poll-retries` (default
3) consecutive attempts, rather than waiting out the rest of
`--interval` — SAS delivers one pending exception per poll, and an
undrained one is silently overwritten by the next with no trail at all.

Two asymmetries worth knowing before you rely on this. First: ticket-
*out* history has a real buffer on the machine, so the startup read
gets you history from before this tool ever ran; ticket-*in* has no
such buffer anywhere in SAS — the machine only ever exposes the
*current* redemption cycle, never a log of past ones, so
`ticket_in_events` can only ever contain tickets seen while this tool
was running. Second, in the other direction: cashout validation numbers
are something this tool *spends*, not observes — `validation_pool`
starts empty and stays empty until you seed it; nothing about starting
this tool conjures real, usable validation numbers out of nowhere (see
§4.4 for exactly what `--seed-validation-pool` numbers are and aren't).

### 4.2 Basic use

```
python3 examples/sql_poll_logger.py gateway.ini
```

This reads connection details from `gateway.ini` (the file
`commission_gateway.py` writes — see §3.4), polls the meters every 5
seconds, and writes to `gateway.sqlite3` in the current directory using
the default `ring` mode (history capped at 200 rows, written at most
once a minute). Press Ctrl-C to stop.

```
python3 examples/sql_poll_logger.py gateway.ini --mode append --db /var/log/nimasas/lab1.sqlite3 --interval 2 --cycles 500
```

A lab/stress-test run: logs every single poll (uncapped), polling every
2 seconds, and stops automatically after 500 cycles.

### 4.3 All flags

| Flag | Default | Meaning |
|---|---|---|
| `config` (positional) | — | Path to a gateway `.ini` file (see §3.4). |
| `--db` | `gateway.sqlite3` | SQLite file to write to. Created automatically if it doesn't exist. |
| `--interval` | `5.0` | Seconds to sleep between poll cycles (how often the machine is asked). Restricted to `0.2`-`5.0` — SAS 6.02 §2.3.3's own hard bounds for polling a single machine, rejected outright outside that range rather than just documented (see §4.4). |
| `--cycles` | `0` | Stop after this many cycles. `0` means run until Ctrl-C. |
| `--mode` | `ring` | `ring` (capped history, decoupled cadence — the deploy default) or `append` (uncapped, every poll — lab/stress use). |
| `--history-cap` | `200` | Max rows kept in the history table in `ring` mode. Ignored in `append` mode. |
| `--history-interval` | `60.0` | Minimum seconds between history writes in `ring` mode, outside a burst (see §4.4). Ignored in `append` mode — how often the *value* is checked (`--interval`) is not how often history is *recorded*. |
| `--burst-count` | `10` | Consecutive polls logged at full resolution, ignoring `--history-interval`, right after a meter decrease or a failed poll. |
| `--skip-ticket-out-backfill` | off | Skip the one-time startup read of the full ticket-out buffer (indices 1–31). |
| `--seed-validation-pool N` | `0` | Top up `validation_pool` to at least `N` `available` rows with random 16-digit test numbers. `0` means don't seed — do this if you're hand-inserting real numbers instead. |
| `--db-size-warning-mb MB` | `0` | Print a warning every cycle the database file is at or above this size — an early signal, not a substitute for the loud failure a full partition already produces on its own (see §4.4). `0` disables it. |
| `--skip-full-meter-sweep` | off | Skip the ~39 individual single-meter long polls each cycle (their columns are left `NULL`); every other meter poll (core, ticket, `0x18`/`0x19`/`0x1C`/`0x1E`/`0x2D`/`0x4F`, and the Table C-7 sweep below) still runs. Lighter per-cycle wire traffic for hardware where the full sweep isn't affordable — see §4.4. |
| `--skip-table-c7-sweep` | off | Skip the ~154-meter Table C-7 sweep (13 chunked `0x6F` exchanges; their columns are left `NULL`); every other meter poll still runs. Much cheaper per meter than `--skip-full-meter-sweep`'s target (12 meters/exchange vs. 1), so worth leaving on even where that one isn't affordable — see §4.4. |
| `--general-poll-retries N` | `3` | Consecutive immediate re-poll attempts on a failed general poll before waiting for the next `--interval` cycle (Decisions Annex D-09). `1` disables retrying. |
| `--pool-age-alert-hours HOURS` | `36.0` | Print an active, repeated `ALERT:` line (and log a `PoolStale` row) every cycle the oldest available `validation_pool` number is at or above this age (Decisions Annex D-16). Never blocks dispensing. `0` or negative disables it. |

### 4.4 What's actually happening (technical)

On startup, it loads the config with
`saspy.config.connect_from_config()` (opens the port, builds a
`SASTransport` and `SASClient` in one step) and creates the tables if
they don't already exist — see §6.3 for the meters schema. Unless
`--skip-ticket-out-backfill` is given, it then reads the machine's
entire ticket-out buffer once: indices 1 through 31 via
`send_enhanced_validation_information()` (long poll `0x4D`), which is
non-destructive at a specific index — it doesn't disturb the "unread"
state the live capture below depends on. Every non-empty record found
is stored in `ticket_out_history`.

Each cycle then does three things, in order:

**0. A validation_pool age check.** Independent of whether a cashout is
even pending this cycle — see step 1's `0x57` handling below for the
pool itself — every cycle checks how old the oldest still-`available`
row is. At or above `--pool-age-alert-hours` (default 36, per the
project's own Decisions Annex D-16) it prints an unmissable `ALERT:`
line and writes a `PoolStale` row to `poll_errors`, repeated every
cycle the condition holds — the same treatment as `--db-size-warning-mb`
above, not a quiet log entry. This **never blocks dispensing**: age
isn't an integrity signal (an old validation number is exactly as valid
as a new one), and D-16 is explicit that an age-based block would
recreate — on a slower clock — the correlated, outage-time TITO disable
the local pool exists to prevent in the first place. A pool this stale
almost always means either nothing is being dispensed, or nothing is
topping this gateway's pool up.

**1. A general poll.** This is the same `general_poll()` that
`connectivity_check.py` uses to check whether a machine is live, except
here its return value — the exception code — is actually acted on. On a
`SASError`, it's re-polled **immediately** — not deferred to the next
`--interval` cycle — for up to `--general-poll-retries` (default 3)
consecutive attempts, each failure logged as its own `poll_errors` row
(`general_poll(attempt N/M)`). This is the project's own Decisions
Annex D-09: SAS delivers exactly one pending exception per poll, and if
the host doesn't drain it fast enough the next exception silently
overwrites it — "the mirror of ghost redemption," a real event that
leaves no trail at all. Setting `--general-poll-retries 1` disables the
extra attempts and restores the plain one-shot behavior. "Immediate" is
paced, not zero-delay: SAS 6.02 §2.3.3 forbids polling a single machine
faster than once per 200 ms, so a retry that fails fast (not a timeout
— a checksum error, say) still waits out whatever's left of that
window before trying again; a retry that already took 200 ms or more
(a timeout, typically) fires again with no extra wait. Once a poll
succeeds (first attempt or a later one), its exception code is
dispatched exactly as before:

- Exception `0x67` (ticket inserted) → read the ticket's validation
  data (long poll `0x70`) and insert it into `ticket_in_events`.
  Deliberately nothing more: this tool never calls `redeem_ticket()` to
  authorize or reject the ticket. Deciding whether to pay a ticket is a
  real business/security decision (see your project's own Decisions
  Annex on exactly this), and a reference poller has no way to make
  that decision correctly. Left unredeemed, the machine returns the
  ticket to the player on its own after a spec-guaranteed 30-second
  timeout — so running this against a real machine never risks an
  incorrect payout.
- Exception `0x68` (ticket transfer complete — the redemption cycle
  finished, stacked or rejected, not which one per the spec's own
  Appendix A note) → read the completion status via
  `redeem_ticket_status()` (long poll `0x71` with transfer code `FF`,
  a safe read-only status query — never a real `redeem_ticket()` call)
  and insert it into `ticket_in_completions`. `machine_status` `0xFF`
  ("no completed cycle since last polled") is logged as-is, not
  filtered — it means the exception fired but the cycle was already
  gone by the time this read landed, which is itself worth knowing,
  not an error. Nothing here correlates a completion back to the
  insertion that started its cycle — SAS gives no shared ID to join
  on, so the two tables are independent logs; see §6.3.
- Exception `0x3D` or `0x3E` (a ticket-out record is ready) → drain
  *every* currently-unread ticket-out record (long poll `0x4D`,
  function code `0x00`, which marks each one read as it goes) into
  `ticket_out_history`, stopping when the machine reports the buffer
  has nothing more unread. This drains in a loop, not just once, so a
  burst of several tickets printed between poll cycles is still
  captured in full rather than only the oldest of them.
- Exception `0x57` (system validation request — the machine is ready to
  print a cashout ticket and is waiting for a validation number) →
  read the pending amount (long poll `0x57`), take the lowest-numbered
  `available` row from `validation_pool`, and answer with it (long poll
  `0x58`). Three outcomes, each handled differently:
  - The machine acknowledges (status `0x00`) → the row moves to
    `assigned`, permanently — `validation_pool` doubles as its own
    audit trail of everything this gateway has ever handed out.
  - The machine rejects it (`0x80`/`0x81` — Table 15.8b) → the row is
    left `available`, since the number itself was never actually
    consumed.
  - `validation_pool` has no `available` row at all → nothing is sent;
    a `PoolExhausted` row is written to `poll_errors` and the machine's
    own 10-second cashout timer runs out on its own. This is the local
    equivalent of what your project's own design calls "pool_low" —
    this reference tool just logs it rather than raising an alert or
    disabling TITO.

  One race is handled explicitly: if `send_pending_cashout_info()`
  comes back with cashout type `0x80` ("not waiting for system
  validation," Table 15.7b), the exception fired but the cashout is
  already gone by the time this tool read it — nothing is answered,
  and no pool number is spent.
- Any other exception code, including `0x00` (nothing pending), is
  ignored by this tool.

If every retry attempt fails, the cycle moves on to meters anyway — a
general-poll failure, retried or exhausted, never blocks anything else.

**Every write goes through `_safe_commit()`**, not a bare `conn.commit()`
— if the commit fails (almost always `SQLITE_FULL`, the partition
holding the database file is out of space), it's reported loudly to
stderr rather than raised or silently dropped, and the loop continues.
This tool never deletes an existing row to make room, regardless of how
long that takes to matter — see §6.3's `synced_at` discussion for why,
and why that decision belongs to a future real sync process, not to
this tool guessing at a safe row count to keep. `--db-size-warning-mb`
gives you a signal *before* that happens: every cycle, if given, it
checks the database file's size and prints a warning once it's at or
above the threshold — a heads-up, not a fix, and not required for the
loud-failure behavior above, which happens regardless of whether you set
it.

**2. Meters.** `_poll_all_meters()` runs every meter poll this tool
knows (see §4.1's list and `sql_poll_logger.py`'s `ALL_METER_FIELDS`),
**all of which must succeed** before anything is written — the six core
meters (`0x0F`), the eight ticket meters (`0x2F`), `0x18`/`0x19`/`0x1C`/
`0x1E`/`0x2D`/`0x4F`, (unless `--skip-full-meter-sweep`) the ~39
single-meter polls, and (unless `--skip-table-c7-sweep`) ~13 chunked
`0x6F` exchanges covering essentially the rest of Table C-7. This is a
deliberate, explicit design choice, not an accident of how the code
happens to be structured: **any one poll in this group failing aborts
the entire cycle's meter write**, the same as it always has for the
original two. A row that's fresh in some meter columns and stale (or
missing) in others would misrepresent what "as of `polled_at`"
actually means, and that's just as true whether it's 2 polls or 60.

On success of every poll, `meters_current` (always exactly one row) is
overwritten with the latest values from all of them, unconditionally,
every cycle. Whether that cycle *also* writes a new row to
`meters_history` depends on the mode:

- In `append` mode, always.
- In `ring` mode, only if: this is the first poll ever, or
  `--history-interval` seconds have passed since the last history
  write, or a **burst** is active.

A burst starts when any *cumulative* meter value goes down since the
last successful poll (SAS meters are cumulative counters — a decrease
usually means something worth a closer look, like a meter rollover or a
reset), or when any poll in the group fails outright, or when a caller
embedding `poll_and_log()` directly (rather than running this as a
script) passes `anomaly=True` from its own logic. A handful of fields
are gauges, not counters — current hopper level/status, current
credits, selected game number — and are excluded from this check (see
`GAUGE_METER_FIELDS`), since a decrease there is normal operation, not
an anomaly. Once a burst starts, it writes the next `--burst-count`
successful polls to history at full resolution — one per cycle, cadence
ignored — before returning to the normal interval. This is deliberate:
that's where the diagnostic value actually is, and it's cheap precisely
because it's rare.

On a `SASError` from *any* poll in the group, the exception's type,
message, and the specific poll that failed are inserted into
`poll_errors` (this table's shape and behavior are unchanged from
before — see §6.3), and **nothing** is written to `meters_current` or
`meters_history` that cycle. The loop continues after `--interval`
seconds either way; one bad exchange — a timeout, a checksum failure,
anything — never stops the run. `--skip-full-meter-sweep` and
`--skip-table-c7-sweep` are the practical levers if this many polls per
cycle (8 grouped polls, ~39 single-meter ones, and ~13 Table C-7
chunks — 60 total by default) is more wire traffic than your hardware
or `--interval` can absorb. They're independent and asymmetric: the
Table C-7 sweep covers roughly four times as many meters (~154 vs.
~39) in about a third as many exchanges (13 vs. 39), since `0x6F`
batches 12 meters per request — so if only one has to go, drop the
single-meter sweep first.

**Is 60 exchanges a cycle actually fast enough on real hardware?**
This manual won't guess — every cycle's own log line answers it
directly: `meter_poll=X.XXXs/N polls` is measured wall-clock time
against whatever `sql_poll_logger.py` is actually talking to, not a
theoretical wire-speed calculation. Run it against your lab's real
EGM(s) and read that number back; if it's a meaningful fraction of
`--interval`, you'll also get an unmissable `WARNING: meter poll took
X.XXXs ... at or above --interval Ns` line the moment the sweep alone
doesn't leave room for the general poll and the configured sleep. At
that point `--skip-full-meter-sweep`, `--skip-table-c7-sweep`, or a
larger `--interval` are the practical levers — which combination
depends on whether you need those columns refreshed every cycle or can
live with them stale/`NULL`.

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
- **`meters_history` looks sparse compared to how often it's polling**:
  expected, in `ring` mode — history is written on its own
  `--history-interval` cadence, not every poll. `meters_current` is
  still fresh every cycle; check that first if you want the latest
  value, not `meters_history`.
- **`meters_current`/`meters_history` stop updating, but `poll_errors`
  is filling up**: `poll_name` names exactly which of this tool's many
  meter polls is failing (`send_meters_10_through_15`,
  `send_selected_meters(ticket_meters)`, `send_meter(SEND_TRUE_COIN_IN)`,
  etc.) — check the error's own type/message column first. Any one of
  them failing blocks that cycle's *entire* meter write (see §4.4), so
  this isn't a partial-data situation to work around, it's a real
  problem with that specific long poll on this machine.
- **Every cycle logs a `send_meter(...)` failure and nothing else looks
  wrong**: this machine doesn't answer one (or several) of the ~39
  single-meter polls in the sweep — some SAS implementations only
  support a subset of Appendix B. `--skip-full-meter-sweep` drops that
  whole sweep (leaving its columns `NULL`) while keeping every other
  meter poll working normally; that's the practical fix if this
  machine simply doesn't speak all of them.
- **Every cycle logs a `send_extended_meters(chunk N/13)` failure**:
  this machine doesn't answer `0x6F` at all, or rejects a specific code
  inside that chunk — check whether `send_extended_meters_group` (LP
  `0x1C`, a different long poll, also `0x6F`-shaped in Table 7.21 but
  not the same request) succeeds; if it does but the Table C-7 sweep
  doesn't, this machine likely has a narrower LP `0x6F` implementation
  than the spec's own maximum. `--skip-table-c7-sweep` is the practical
  fix — every other meter poll, including the single-meter sweep,
  keeps working.
- **`poll_errors` shows repeated `general_poll(attempt N/M)` rows**:
  expected on a flaky link — each consecutive immediate retry (up to
  `--general-poll-retries`) is logged individually, numbered, so you
  can see the retry sequence rather than one opaque failure. If `M`
  attempts fail every cycle, that's a real, persistent link problem
  (loose cable, wrong address, a machine that's actually down), not a
  transient one — `connectivity_check.py` is the next step, same as
  above.
- **A console line starts with `ALERT: validation_pool's oldest
  available number is ...`**: the pool hasn't been topped up in
  `--pool-age-alert-hours` (default 36h) — in this reference tool, that
  almost always means `--seed-validation-pool` was only run once and
  the pool has simply been consumed by test cashouts since. Re-seed it,
  or lower `--pool-age-alert-hours` if 36h doesn't suit a fast lab
  test cycle. This never blocks dispensing on its own (Decisions Annex
  D-16) — it's a health signal, not a validity check.
- **Database file grows large during a long `append`-mode stress run**:
  expected — that mode never deletes rows by design. Either plan disk
  space accordingly or switch to `ring` mode, which won't grow past
  `--history-cap` rows in its history table (`meters_current` is always
  one row in either mode). See §6.7 for why this distinction exists.
- **`ticket_out_history` is empty even though the machine has printed
  tickets before**: check whether `--skip-ticket-out-backfill` was
  passed — without it, the startup read should find whatever the
  machine's buffer currently holds. If the flag wasn't passed and it's
  still empty, the buffer itself may genuinely be empty (it holds at
  most 31 records and is overwritten oldest-first — old tickets fall
  out of it over time, this tool or no).
- **`ticket_in_events` is missing a ticket you know was inserted**:
  this table can only ever record tickets seen while the tool was
  running — see §4.1's note on the ticket-in/ticket-out asymmetry.
  There's no way to retroactively pull ticket-in history from the
  machine; if the tool wasn't running when a ticket came in, that
  event is gone.
- **A ticket sits in escrow and is never picked up in `ticket_in_events`**:
  confirm the general poll is actually returning `0x67` for it — a
  machine not configured for the validation mode this tool expects
  (or one being polled by a different, competing host) may not report
  the exception this tool is watching for.
- **A cashout never gets a validation number and the machine falls back
  to another payout method**: check `validation_pool` for any
  `available` rows first — `--seed-validation-pool` has to actually be
  passed (or real numbers hand-inserted) before this tool has anything
  to hand out, and it defaults to not seeding at all. A `PoolExhausted`
  row in `poll_errors` confirms this is what happened, versus a wiring
  or configuration problem further up.
- **`send_validation_number` keeps getting rejected (status `0x80` or
  `0x81`)**: the machine either isn't currently waiting for system
  validation (the race this tool already checks for via cashout type
  `0x80` on the read side, §4.4) or isn't configured for system
  validation at all — confirm the machine's validation mode before
  assuming the pool numbers themselves are the problem. Rejected
  numbers are left `available` for reuse, so this doesn't burn through
  the pool on its own.
- **A `FAULT: database write failed` line appears on stderr**: the
  partition holding the database file is almost certainly full — that
  row was not saved, and none of this tool's other rows were deleted to
  make room (§4.4/§6.6 explain why it never does that). Free space on
  that partition, or move the database file to one with more; running
  with `--db-size-warning-mb` set going forward gives you a warning
  before this happens instead of only after.

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

`legacy/pre_pollcode_expansion/` is a different, unrelated thing: a
frozen snapshot of `saspy/constants.py`, `client.py`, and `models.py`
taken immediately before LP 2F/6F, 4C, 4D, 7B and
`redeem_ticket_status()` were added, kept purely as a fast, file-level
rollback path during live floor testing (alongside ordinary git
history). See its own `README.md` for what it covers and how to use
it. It is not maintained going forward and is not a second
implementation to build against.

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
CREATE TABLE IF NOT EXISTS meters_current (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    polled_at TEXT NOT NULL,
    total_cancelled_credits INTEGER,
    total_coin_in INTEGER,
    total_coin_out INTEGER,
    total_drop INTEGER,
    total_jackpot INTEGER,
    games_played INTEGER,
    ticket_in_cashable_cents INTEGER,
    ticket_in_cashable_count INTEGER,
    -- ... and north of 220 more meter columns, generated (not
    -- hand-typed) from ALL_METER_FIELDS in sql_poll_logger.py -- see
    -- that name for the full, current, authoritative list; this manual
    -- won't try to keep a duplicate of a list that size in sync by
    -- hand. Covers every ticket meter (LP 0x2F), LP
    -- 0x18/0x19/0x1C/0x1E/0x2D/0x4F, (unless --skip-full-meter-sweep)
    -- every other single-meter long poll this client implements, and
    -- (unless --skip-table-c7-sweep) essentially the rest of Table C-7
    -- via chunked LP 0x6F reads -- 230+ columns total, deliberately
    -- including several that read the same underlying counter as
    -- total_coin_in above through an entirely independent long poll.
    -- meters_history has the identical column set (same generation).
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
```

`issued_at` is set by `seed_validation_pool()` to when a row entered the
pool — this reference tool's stand-in for a real server recording when
it minted a batch. It's what the pool-age check (§4.4, Decisions Annex
D-16) reads; a hand-inserted real number with no batch info recorded
leaves it `NULL`, which the age check treats as unknown rather than
brand-new or infinitely old.

- **Split "current" from "history."** Almost every consumer of this
  data only ever wants the latest value — a dashboard tile, a health
  check, an operator screen. Forcing that consumer to run
  `ORDER BY polled_at DESC LIMIT 1` over a growing log table is solving
  an easy problem the hard way. `meters_current` is a single row
  (`id` is constrained to always be `1`; each poll does an
  `INSERT OR REPLACE`), so reading "the current value" is the cheapest
  query SQLite can do. `meters_history` exists separately, purely for
  "what happened over time" questions — trend, diagnostics, audit.
- **Bound the history table, and decouple how often it's written from
  how often the machine is polled.** In `ring` mode,
  `meters_history` is capped at a fixed row count (oldest evicted —
  see the ring-buffer delete below) and written on its own timer, not
  every poll cycle. Polling every few seconds does not have to mean
  writing every few seconds — those are two different concerns
  (freshness of the current value vs. depth/resolution of the
  history), and conflating them either polls too slowly or writes too
  often. §6.7 explains why this specific split matters for this
  project's hardware.
- **Capture bursts of full resolution around anomalies, not all the
  time.** A meter decrease, a failed poll, or an explicit signal from
  calling code — these are exactly the moments a coarse, decoupled
  cadence would otherwise blur together, and exactly the moments worth
  seeing at full resolution. Reacting to them with a short burst of
  every-poll writes (see `sql_poll_logger.py`'s `burst_count`) gets the
  diagnostic value of dense logging without paying for it continuously.
- **Separate the error log from the data table.** `poll_errors` exists
  so a failed poll never has to be encoded as, say, a row of nulls in
  a meters table — a null there would be genuinely ambiguous (did the
  machine report zero, or did the poll fail?). Keeping failures in
  their own table with the exception type and message keeps the meters
  tables clean and makes "how often is this failing, and how" its own
  easy query.
- **Store a timestamp you generate, not one you trust the machine to
  give you.** `polled_at`/`occurred_at` are set by the poller
  (`utc_now()`, UTC, ISO 8601) at the moment of the exchange, not
  parsed out of the SAS response — this is when you need to know it
  happened, and it's consistent even against machines that don't
  report their own clock.
- **A cumulative meter and its event table are not the same data, even
  when they cover the same thing.** `meters_current`/`meters_history`'s
  `ticket_in_*`/`ticket_out_*` columns are the machine's own running
  totals (LP 2F — see §4); `ticket_in_events`/`ticket_out_history`
  are one row per transaction. Both are worth keeping: the cumulative
  meter is what you'd reconcile against (does the machine's own count
  agree with what we captured?), and it's polled/written on exactly the
  same cadence/rollover logic as every other meter column — any one
  meter poll failing (not just LP 0x0F/0x2F; see §4.4) aborts that
  cycle's write for all of them rather than saving a row that's fresh in
  some columns and stale or missing in others.
- **Identity, not position, decides what counts as a duplicate.**
  `ticket_out_history` is read two different ways — a non-destructive
  startup walk by buffer position, and a live, destructive drain
  triggered by an exception — and both can legitimately observe the
  same physical ticket. Buffer position isn't a stable identity (it's a
  ring; the same slot gets reused), so the table's `UNIQUE` constraint
  is on the ticket's own identity (validation number, date, time)
  instead, and every insert uses `INSERT OR IGNORE`. Reach for this
  whenever more than one path can produce the same logical row —
  deduping on a source-specific detail like position or sequence number
  looks reasonable right up until two sources disagree about what that
  detail means.
- **Only the event stream drains; meter history never does.**
  `poll_errors`, `ticket_out_history`, `ticket_in_events`, and
  `ticket_in_completions` are this example's event stream — real,
  per-occurrence records a future sync process is meant to eventually
  acknowledge and clear (see §6.6 for the general `synced_at` pattern).
  `meters_current` and `meters_history` deliberately carry no
  `synced_at` column at all — they're a local diagnostic buffer, not a
  queue, and there's nothing for a sync column to mean on a table
  that's read in place and never drained. The distinction is what a
  row *means*, not which table it happens to live in: a meter reading
  is a snapshot you can afford to lose (another poll gets you a fresh
  one); a ticket event is the only record that a specific thing
  happened at a specific time, and losing it is not recoverable by
  polling again.
- **A table with `synced_at` and nothing consuming it yet is still
  correct.** `ticket_out_history`, `ticket_in_events`, and
  `ticket_in_completions` carry the column even though this reference
  tool has no real drain process to ever set it — see §4.4. That's
  deliberate, not premature: the column is what makes the eventual,
  correct cleanup query obvious and safe (`DELETE ... WHERE synced_at
  IS NOT NULL`), and its absence is what would make "just cap it at N
  rows" look like the only available option. Add the column for what a
  table *is* — here, an audit-style event log — not only once
  something exists to populate it.
- **Two related events don't need a shared table if nothing correlates
  them.** `ticket_in_events` (exception `0x67`, an insertion) and
  `ticket_in_completions` (exception `0x68`, how that cycle ended) are
  about the same underlying ticket, but SAS gives no ID that ties one
  to the other — no sequence number, no shared key, nothing. Bolting
  them into one table would invite treating adjacent rows as related
  when they might not be (two tickets inserted in quick succession).
  Two independent append-only logs, correlated later by whoever
  consumes them (timestamp proximity, `validation_data_hex`) if they
  need to be, is the honest shape for data SAS itself doesn't link.
- **A table you spend from needs a status column; a table you only
  observe doesn't.** Every other table here is written by the poller
  and read by someone else. `validation_pool` is the opposite: this
  tool is the one *consuming* it, one row at a time, and once a row is
  spent it must never be handed out again — that's a correctness
  requirement, not a convenience. `status` (`available` → `assigned`)
  is what enforces that, and rows are never deleted even after they're
  spent, so the table stays its own audit trail of every number this
  gateway has ever issued. This is the same shape as a job queue or a
  ticket-lock table in any other system: whenever "pick the next one
  and never reuse it" matters, reach for a status column before reaching
  for `DELETE` or an external tracking structure.

### 6.4 Running and verifying it

```
python3 examples/sql_poll_logger.py gateway.ini --cycles 3 --seed-validation-pool 10
sqlite3 gateway.sqlite3 "SELECT * FROM meters_current;"
sqlite3 gateway.sqlite3 "SELECT * FROM meters_history;"
sqlite3 gateway.sqlite3 "SELECT * FROM poll_errors;"
sqlite3 gateway.sqlite3 "SELECT * FROM ticket_out_history;"
sqlite3 gateway.sqlite3 "SELECT * FROM ticket_in_events;"
sqlite3 gateway.sqlite3 "SELECT * FROM validation_pool;"
```

`--cycles 3` gives you a short, bounded run to confirm rows are landing
correctly before committing to a long unattended run. `meters_current`
should show exactly one row after this. In the default `ring` mode,
`meters_history` will also show just one row from a short run like
this — the first poll always writes a baseline, and the next write
isn't due for another `--history-interval` seconds (60 by default), so
three cycles a few seconds apart isn't enough to see a second one. Add
`--mode append` to this command (or lower `--history-interval`) if you
want to confirm every poll is landing a history row while you're
testing. If you don't have the `sqlite3` CLI installed,
`python3 -c "import sqlite3; print(sqlite3.connect('gateway.sqlite3').execute('SELECT * FROM meters_current').fetchall())"`
works just as well.

`ticket_out_history` should show rows from the very first cycle (the
startup backfill runs before the poll loop even starts) if the machine
has printed any tickets recently. `ticket_in_events` will only show
rows if you actually feed a ticket into the machine while the tool is
running — three quick cycles with nothing inserted is expected to leave
it empty, and that's not a failure. `validation_pool` should show 10
`available` rows immediately (seeding happens at startup, before the
loop) with random test numbers, not real ones — cash out from the
machine while the tool is running to see one flip to `assigned`.

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

### 6.6 A note on designing for eventual central sync — and what should never drain

At some point, data in these per-gateway SQLite files needs to get to
a central place — a server, a shared database, wherever the wider
project's architecture consumes it. This manual deliberately doesn't
specify that architecture: it's covered in the project's own internal
planning documents, which are being shared with the team separately.

What's worth knowing here, generically, is the shape of the pattern:
a table that's actually a queue of things to acknowledge — this
example's `poll_errors`, `ticket_out_history`, and `ticket_in_events`
are all one; the wider project's own event stream is another — can
carry a `synced_at` column, left `NULL` until a sync process marks a
row handled. That gives any future sync process a cheap, obvious query
(`WHERE synced_at IS NULL`) to find unacknowledged rows, without the
local poller needing to know anything about how or where syncing
happens, what transport it uses, or how often it runs. That separation
of concerns — the poller's only job is "poll reliably and log
everything, including failures"; a sync process's only job is "move
rows somewhere and mark them synced" — is the one piece of
forward-looking design here worth carrying into whatever you build
next. It also settles what a local disk-pressure problem should do:
nothing, on its own. Freeing space by deleting unsynced rows means
guessing that a row won't be needed — a guess only the sync process
(or a human, deliberately) is in a position to make correctly. The
poller's job under pressure is to fail loudly (§4.4's `_safe_commit()`)
and optionally warn early (`--db-size-warning-mb`), not to pick which
records to keep.

That pattern belongs to the event stream specifically, and *only* the
event stream. It's deliberately not on `meters_current` or
`meters_history` (§6.3): meter data is a local diagnostic buffer read
in place, not a queue of things waiting to be collected and cleared
elsewhere — it never drains, so a `synced_at` column on it wouldn't
have anything true to mean. Reaching for the same "add a sync column"
instinct on every table is the mistake to avoid here; whether a table
needs one depends on whether anything downstream is actually supposed
to acknowledge and clear its rows.

### 6.7 Why meter history is a ring buffer, not a log, in production

The append-everything version of this pattern (`--mode append`) is
exactly right for a lab machine or a bounded stress-test run: you want
every sample, the disk is ordinary SSD or a dev machine's own storage,
and the run has a known end. It stops being right the moment the
target is a production deployment on constrained hardware — which,
concretely, is what a lot of this project's gateway fleet actually is.

A meaningful share of the gateways this software will run on are
retained 32-bit units booting and running from flash storage, not SSD.
The concern there is not disk space — it's **write endurance**. Flash
storage wears out after a bounded number of write cycles per cell, and
an unbounded per-poll `INSERT` — one new row every few seconds,
forever — hits that wear limit well before it hits any capacity limit.
SQLite's write-ahead log (WAL) mode makes this worse, not better: each
logical write typically touches more physical flash than the row
itself would suggest, since WAL journals the change before it's
checkpointed into the main database file.

Two changes together remove this: a **capped ring buffer** for
`meters_history` (a fixed number of rows, oldest evicted — so the table
never grows, and total lifetime writes are bounded by the poll count
divided by however sparse the write cadence is, not by how long the
gateway has been running) and a **write cadence decoupled from the
poll rate** (so "poll every 5 seconds" doesn't imply "write every 5
seconds" — `meters_current` still updates every cycle, cheaply, since
it's always exactly one row being overwritten in place, but
`meters_history` writes only every `--history-interval` seconds by
default). The burst mechanism (§6.3, §4.4) is what keeps this from
losing the moments that actually matter: an anomaly forces a short run
of full-resolution writes exactly when the extra wear is worth paying
for, rather than paying for it on every single poll regardless of
whether anything interesting happened.

If you're setting this up for a lab or stress-testing run on normal
disk, `--mode append` is still there, unchanged, and still the more
useful choice for that job — this isn't "ring mode is correct and
append mode is a bug," it's two different jobs with two different
right answers, and the tool picks the production-shaped one as its
default because that's the deployment more of this fleet will actually
see.

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
