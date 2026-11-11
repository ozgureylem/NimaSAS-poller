# COMMANDS.md

The command for each task, grouped by what you are trying to do. Every
tool named here is in this repository; every command was run against a
real EGM or a real gateway database before it was written down.

Not installed yet: **`INSTALL.md`**. One `apt` line and a `git clone`.

## Where the rest of the documentation is

- **`INSTALL.md`** — headless install on Linux, Windows and macOS, and how
  to prove the install before going near a machine
- **`MANUAL.md`** — one section per tool: every flag, what it does on the
  wire, and its own troubleshooting
- **`README.md`** — what this repository is, and what is deliberately not
  in it

Two conventions throughout:

- `/dev/ttyUSB0` stands for your serial device. `COM3` on Windows,
  `/dev/cu.usbserial-*` on macOS — and on macOS it must be the `cu.`
  name, not `tty.` (`INSTALL.md` §6).
- `--address 1` is written out every time on purpose. The default is 1,
  so omitting it works on a cabinet at address 1 and silently will not
  anywhere else. A floor survey defaulted to address 1 for weeks and
  reported every cabinet that was not on it as a dead cable. Use
  `--scan` where a tool offers it: it finds the address by polling
  instead of assuming one.

---

## 0. The one rule that has cost the most time

**`sql_poll_logger.py` takes a config file. Everything else that talks to
a machine takes a device path.**

```bash
python3 examples/sql_poll_logger.py gateway.ini          # config file
python3 examples/meter_snapshot.py /dev/ttyUSB0 --address 1   # device path
```

Both directions of that confusion have cost bench time, including once
from a command written into a chat message. `tests/test_commands_doc.py`
now enforces it across every document in this repository, so a wrong one
fails the suite rather than wasting an afternoon.

The database tools are a third shape: they take a **`.sqlite3` path** and
never open a serial port at all (§5).

---

## 1. Read-only — safe at any time

Nothing in this section changes a meter, moves money, or alters machine
state. These are safe on a live floor cabinet with a player on it.

```bash
# Is anything there, and is real-time event reporting off?
python3 examples/connectivity_check.py /dev/ttyUSB0 --address 1

# Who is on this cable, at which address?
python3 examples/address_scan.py /dev/ttyUSB0 --range 1-8

# How does THIS cabinet want to be polled? Cabinets differ.
python3 examples/wire_diagnostics.py /dev/ttyUSB0 --address 1
python3 examples/polling_capability.py /dev/ttyUSB0 --address 1

# Everything this machine will tell you about itself, to a file
python3 examples/meter_snapshot.py /dev/ttyUSB0 --address 1
python3 examples/meter_snapshot.py /dev/ttyUSB0 --address 1 --note "after RAM clear"

# The accounting figures, grouped the way an operator reads them
python3 examples/accounting_summary.py /dev/ttyUSB0 --address 1

# Did the technician type the serial in correctly after a RAM clear?
python3 examples/verify_identity.py /dev/ttyUSB0 --address 1 \
    --expect-serial <serial-on-the-cabinet> --expect-asset <asset-number>
python3 examples/verify_identity.py /dev/ttyUSB0 --scan --roster floor.csv --machine 7

# What AFT would do, before asking it to do anything
python3 examples/aft_status.py /dev/ttyUSB0 --address 1

# What handpay is pending, without clearing it
python3 examples/handpay_clear.py /dev/ttyUSB0 --address 1
python3 examples/handpay_queue_drain.py /dev/ttyUSB0 --address 1 --dry-run
```

Watchers. These sit and report while somebody plays:

```bash
python3 examples/exception_monitor.py /dev/ttyUSB0 --address 1
python3 examples/bill_in_watch.py /dev/ttyUSB0 --address 1
python3 examples/bill_in_watch.py /dev/ttyUSB0 --address 1 --json bills.json
python3 examples/ticket_in_watch.py /dev/ttyUSB0 --address 1
```

The ticket-out buffer, read by index, which leaves the machine's
read/unread pointer alone — so a cashier can run it without disturbing a
drain the server is consuming:

```bash
python3 examples/ticket_history.py /dev/ttyUSB0 --address 1
python3 examples/ticket_history.py /dev/ttyUSB0 --address 1 --json records.json
```

### The recovery command

A poller killed with `SIGKILL` or cut off at the wall leaves real-time
event reporting **enabled in the cabinet**. The next run then reads the
RTE frame's address byte as an exception code: on address 1 that is a
phantom `exception 0x01` on every poll, plus misframed long polls. This
is the fix, and it is the first thing to run after any bad stop:

```bash
python3 examples/exception_monitor.py /dev/ttyUSB0 --address 1 --disable-rte
```

---

## 2. Bringing a gateway up

Once per cabinet, in this order.

```bash
# 1. Is it wired right? (§1)
python3 examples/connectivity_check.py /dev/ttyUSB0 --address 1

# 2. Write the config the poller loads: finds port and SAS address
python3 examples/commission_gateway.py
python3 examples/commission_gateway.py --output /etc/nimasas/gateway.ini

# 3. The floor ceremony: verify identity against the roster, set the
#    hostname, install keys, write client_id, log what was done
sudo python3 deploy/commission_machine.py /dev/ttyUSB0 --scan \
    --roster floor.csv --machine 7 \
    --client-id TROP-042 \
    --hmac-key-file /run/key.hex --pool-key-file /run/pool.pub
```

`commission_machine.py` is the only tool here that needs `sudo`, because
setting the hostname does. Rehearse it first:

```bash
sudo python3 deploy/commission_machine.py /dev/ttyUSB0 --scan \
    --roster floor.csv --machine 7 --client-id TROP-042 \
    --hmac-key-file /run/key.hex --pool-key-file /run/pool.pub --dry-run
```

**Neither key is ever passed on the command line** — both are read from
files, so neither reaches the process list or your shell history. Put
them under `/run`, which is a tmpfs and does not survive a reboot.

`gateway.json` is written **last**, as the commit marker: if
commissioning is interrupted, the absence of that file is what tells the
next run the machine is not commissioned. `--replace-keys` is the only
way to overwrite an existing commissioning, and it prints the protocol
counters it is about to reset before doing it.

---

## 3. Running the gateway

```bash
python3 examples/sql_poll_logger.py gateway.ini
python3 examples/sql_poll_logger.py gateway.ini --db gateway.sqlite3 --interval 5
```

A floor configuration, which is what `deploy/nimasas-poller.service`
runs:

```bash
python3 examples/sql_poll_logger.py gateway.ini \
    --db gateway.sqlite3 \
    --rte \
    --mode ring \
    --min-poll-interval 0.2 \
    --interval 2.0 \
    --heartbeat-every 1 \
    --table-c7-interval 300
```

Two of those flags are decisions, not tuning:

- **`--mode ring`, not `append`.** Append grows `meters_history` without
  bound; it is the audit and soak setting. Ring caps it, which is what
  the flash on a gateway micro-PC needs to survive years of poll cycles.
- **Drain priority is ON by default and is not in the list above.**
  Above 50% of the event budget §2.3.3 allows a host to collect, the
  optional meter sweeps are deferred and the poll slots go to general
  polls instead. What gets deferred is 39 single meters, 13 Table C-7
  chunks and 12 validation polls — all cumulative, so reading them a
  minute later costs nothing. What it protects is real time events, and
  a dropped event is gone for good: no meter carries a player's
  turnover. Coin in, coin out and games played are read every cycle
  regardless. `--no-drain-priority` turns it off, for measuring what the
  deferral is worth; it does not belong on a floor gateway.
- **`--min-poll-interval 0.2`** is the specification's own floor and the
  default. Do not lower it. The 40 ms relaxation available under RTE buys
  nothing here, because at most one event is delivered per poll and
  200 ms already allows five a second against a measured play rate near
  0.85.

As a service, so it survives a reboot:

```bash
sudo cp deploy/nimasas-poller.service /etc/systemd/system/
sudoedit /etc/systemd/system/nimasas-poller.service   # paths, port, address
sudo systemctl daemon-reload
sudo systemctl enable --now nimasas-poller
journalctl -fu nimasas-poller
```

Read the unit before you enable it. It ships with the lab gateway's
paths, user and serial port, and its comments name the lines a floor
install changes.

Stop it with `systemctl stop`, never `kill -9`: `SIGTERM` is what the
teardown listens for, and it disables RTE, flushes the last interleaved
event and closes the database. After a `SIGKILL`, run the `--disable-rte`
recovery in §1.

### The lab ticket authority, and why it refuses here

`sql_poll_logger.py` carries a `--lab-ticket-authority` flag that makes a
gateway an authority on what tickets are worth. The module that
implements it is lab-only and is **not** in this repository, so the flag
exits 2 with a message saying so — which is almost certainly what you
want, since such a file has no place near a floor.

The file format is documented in `lab_tickets.example.json`. Copy it to
`lab_tickets.json`, which is gitignored, and put real numbers there if
you implement your own authority. Validation numbers identify tickets a
real property issued, so they are operational data and do not belong in
a repository — which is why no number appears in this document.

---

## 4. Money-moving — lab machines only

**Everything below changes machine state or moves money.** Nothing in
this section belongs on a live floor cabinet. All of them prompt before
acting; `--yes` skips the prompt and is for scripts that already know
what they are doing.

```bash
# Take credits off electronically
python3 examples/aft_cashout.py /dev/ttyUSB0 --address 1                  # all of it
python3 examples/aft_cashout.py /dev/ttyUSB0 --address 1 --amount 11.11   # an exact amount
python3 examples/aft_cashout.py /dev/ttyUSB0 --address 1 --cashable-only  # leave the promo

# Put credits on
python3 examples/aft_push_credits.py /dev/ttyUSB0 --address 1 --amount 20.00
python3 examples/aft_push_credits.py /dev/ttyUSB0 --address 1 --amount 5.00 --nonrestricted
python3 examples/aft_push_credits.py /dev/ttyUSB0 --address 1 --amount 5.00 --restricted --pool-id 1

# Clear a pending handpay without a reset key
python3 examples/handpay_clear.py /dev/ttyUSB0 --address 1 --reset

# Answer a machine's request to cash out to the host
python3 examples/host_cashout.py /dev/ttyUSB0 --address 1
python3 examples/host_cashout.py /dev/ttyUSB0 --address 1 --deny

# Act as the host for one ticket redemption, by hand
python3 examples/ticket_redeem.py /dev/ttyUSB0 --address 1 --as cashable --amount 25.00
python3 examples/ticket_redeem.py /dev/ttyUSB0 --address 1 --as nonrestricted --amount 10.00
```

**`--restricted` and `--nonrestricted` are the easiest flags in this
repository to get backwards**, and they are not opposites of each other
in the way the words suggest. Read `MANUAL.md` §7.4 before using either
on a machine whose meters anybody is reading. `aft_push_credits.py`
defaults to cashable; neither flag is implied.

A pending handpay blocks almost everything else, and may clear without a
reset key being turned. If a tool reports nothing when you expect a
reply, check for a lockup with `handpay_clear.py` (read-only without
`--reset`) before suspecting the wire.

---

## 5. Reading the database — proof and diagnosis

These take a `.sqlite3` path. None of them opens a serial port, so they
are safe to run against a live gateway's database while it is polling,
and safe to run on a copy somewhere else.

```bash
# The migration's own scorecard: handpays the HOST caused by being slow
python3 examples/unwanted_handpays.py gateway.sqlite3
python3 examples/unwanted_handpays.py gateway.sqlite3 --window 90

# Could the exception queue have overflowed, and were we why?
python3 examples/queue_safety.py gateway.sqlite3
python3 examples/queue_safety.py gateway.sqlite3 --queue-depth 20

# What an outage actually cost
python3 examples/outage_loss.py gateway.sqlite3
python3 examples/outage_loss.py gateway.sqlite3 --min-gap 300

# Did RTE lose anything, and do meters and events agree?
python3 examples/rte_reconcile.py gateway.sqlite3
python3 examples/rte_reconcile.py gateway.sqlite3 --since 2026-10-28T20:00:00

# Every table and view as CSV, with a header for every table
# including the empty ones
python3 deploy/sql_samples.py gateway.sqlite3
```

`sql_samples.py` replaces validation numbers with a pseudonym that is
shared across tables, so a pool number and the ticket printed with it
still line up and neither leaves the gateway. "Exists and is empty" and
"does not exist" are different facts, which is why empty tables still
get a header.

The CMS side — the server issuing into the pool, and pulling ticket
detail back:

```bash
# The server issues validation numbers into the gateway's pool
python3 examples/pool_replace.py gateway.sqlite3 --count 50
python3 examples/pool_replace.py gateway.sqlite3 --count 50 --max-age-hours 36

# The server side of a WfA pull: writes a row and waits for the poller
python3 examples/wfa_request.py gateway.sqlite3 <validation-number>
python3 examples/wfa_request.py gateway.sqlite3 <validation-number> --json
```

`wfa_request.py` never opens the serial port. One port, one master — it
writes a row into `host_requests` and the poller services it at a poll
boundary. That is the whole reason an inbound table exists rather than a
second tool talking to the cabinet.

The pool exists so that the CMS network is never inside the ten-second
validation window: dispensing a number is deliberately local-only.

---

## 6. Invocations that did NOT work

Kept because each one looks right.

```bash
# WRONG: sql_poll_logger takes a config file, not a port.
python3 examples/sql_poll_logger.py /dev/ttyUSB0 --address 1

# WRONG: the other direction. meter_snapshot takes a port, not a config.
python3 examples/meter_snapshot.py gateway.ini
```

The first is the mistake that wasted a session. Both are now caught by
`tests/test_commands_doc.py` if they appear in any document here.

```bash
# WRONG on macOS: hangs with no error, looks exactly like a dead cable.
python3 examples/connectivity_check.py /dev/tty.usbserial-10 --address 1
```

Opening the `tty.` name waits for carrier detect, which an EGM does not
assert. Use `/dev/cu.usbserial-10`.

```bash
# WRONG: pip into a distribution-managed Python on a fresh Pi or Ubuntu.
pip install pyserial
```

Fails with `error: externally-managed-environment`. Use
`sudo apt install python3-serial` on a gateway, or a virtual environment
on a workstation. Do not reach for `--break-system-packages`.

---

## 7. Housekeeping

```bash
# Prove the tree before trusting it (no serial port needed)
python3 -m pytest -q
for f in examples/*.py deploy/*.py; do python3 "$f" --help >/dev/null || echo "BROKEN: $f"; done

# Which device is which
ls -l /dev/serial/by-id/

# Is the poller up, and what is it saying
systemctl status nimasas-poller
journalctl -fu nimasas-poller

# How big is the database getting
ls -lh gateway.sqlite3*
```

Tests that skip are expected: the tests exercising lab-only apparatus
skip when the apparatus is absent rather than passing quietly. Zero
failures is the bar.

### The bench tool

```bash
python3 examples/poll_test_tool.py /dev/ttyUSB0 --address 1
```

Fires any long poll at an EGM and shows what comes back. It ships so
that other developers can find the polls their own projects need; it is
not part of running a gateway, and it can write to a machine depending
on which poll you aim at it. Lab cabinets only.
