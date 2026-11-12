# NimaSAS poller

A clean-room SAS 6.02 host implementation and the gateway that runs it: one
poll loop, one SQLite database, on a Linux micro-PC wired to one gaming
machine's serial port.

This is the **code-only mirror**. The private repository it is generated
from also carries floor captures, machine serials, per-property survey data
and the lab apparatus used to measure all of it. None of that is here, and
the absence is deliberate — see *What is deliberately missing*, below,
before concluding a tool is lost.

## Start here

| | |
|---|---|
| **`MANUAL.md`** | what every tool does, and how to put a SQL layer around a poller |
| **`examples/sql_poll_logger.py`** | the gateway itself. If you read one file, read this one |
| **`saspy/`** | the SAS 6.02 client: framing, CRC, long polls, meters, RTE |
| **`gateway/`** | the gateway↔server link (spec §10). Frame codec and HMAC |
| **`tests/`** | ~1,000 tests, no hardware required |

```bash
python3 -m pytest tests/ -q
```

## Bringing a gateway up

In this order. Each step refuses rather than guessing, and the refusals are
the useful part.

1. **`examples/connectivity_check.py`** — is anything on the wire?
2. **`examples/address_scan.py`** — which SAS address answers?
3. **`examples/commission_gateway.py`** — writes `gateway.ini`
4. **`deploy/commission_machine.py`** — the floor ceremony (below)
5. **`deploy/nimasas-poller.service`** — run it under systemd

### Commissioning, and key provisioning

`deploy/commission_machine.py` is the one place a gateway is bound to a
cabinet and to the server. It does these in this order and stops at the
first thing that is wrong:

1. read the EGM's identity off the serial line
2. **check it against the roster** — before any key is installed
3. set the hostname (`RamSys` + the asset zero-padded to six digits)
4. install the per-gateway **HMAC key** and the server's **Ed25519 public
   key**
5. write `gateway.json`, including the protocol state the link starts from
6. append a record of the ceremony

Two keys, and they are not halves of each other:

- `--hmac-key` is **symmetric**, per gateway, HMAC-SHA256 (§10.3). The
  server holds the same secret. Minted by the server at the moment of
  commissioning and entered on the device; never transmitted.
- `--pool-key` is the **public** half of the server's Ed25519 signing pair
  (§7.3). The server keeps the private half, which is what stops a
  compromised gateway minting its own validation numbers.

**Neither is accepted on the command line** — arguments are visible in the
process table and land in shell history. Both are read from a file or stdin.

```bash
sudo python3 deploy/commission_machine.py /dev/ttyS0 --scan \
    --roster floor.csv --machine 7 --client-id TROP-042 \
    --hmac-key-file /run/key.hex --pool-key-file /run/pool.pub
```

Add `--dry-run` to see every step without changing anything. A gateway that
is already commissioned is **refused** unless you pass `--replace-keys`:
re-keying invalidates the key the server holds, and §10.5's reassignment is
unassign-then-assign.

## The gateway↔server link

`gateway/frame.py` implements the §10.1 wire frame and §10.3
authentication:

```
[length:4][version:1][client_id:24][message_type:1][seq:8][JSON:n][HMAC:16]
```

The processing order in `decode()` is the security property, not an
implementation detail — length before allocation, client_id before key
lookup, HMAC before the sequence check, message type after verification,
and the JSON parser last. The tests assert the order, including that the
key store is *not* consulted for a malformed client_id.

**What is not in this repository:** the TCP client, the 15-second
heartbeat, the server-invited drain, pool epochs and signature
verification, and key rotation. Eight tables carry a `synced_at` column
and nothing here sets one. The poller's `--server-ticket-authority` flag
(spec §9) writes its question to a table and needs a gateway client to
carry it; without one it refuses every ticket.

## The tools

### THE PRODUCT

the gateway itself. Without these there is nothing to run.

- **`deploy/commission_machine.py`** — the floor ceremony: verify identity against the roster, set the hostname, install keys, write client_id, log it
- **`deploy/nimasas-poller.service`** — runs it under systemd, so it survives a reboot (C3)
- **`examples/commission_gateway.py`** — finds the port and SAS address and writes gateway.ini, which the poller loads
- **`examples/sql_poll_logger.py`** — THE gateway. One poll loop, one SQLite database. Everything else in this tree exists around it
- **`examples/verify_identity.py`** — after a RAM clear: did the technician type the serial in correctly? A gateway keyed to the wrong cabinet reports someone else's money

### THE CMS INTERFACE

how RamSys talks to a gateway. The pool goes in, ticket detail comes out, and WfA pulls run through here.

- **`examples/host_cashout.py`** — answer a machine's request to cash out to the host
- **`examples/pool_replace.py`** — the server issues validation numbers into the gateway's pool. The pool exists so the CMS network is never inside §15.7's ten seconds -- dispense is deliberately local-only
- **`examples/ticket_history.py`** — the WfA pull run STANDALONE, when no poller holds the port. Walks LP 0x4D by INDEX, which leaves the read/unread pointer alone, so a cashier can trigger it without disturbing the drain the server is consuming
- **`examples/ticket_redeem.py`** — act as the host for one redemption by hand -- the path the poller automates, exercised directly
- **`examples/wfa_request.py`** — the SERVER side of a WfA pull, and the first thing in this project that asks the gateway to do something. Writes a row into host_requests and waits; the poller services it at a poll-slot boundary. It never opens the serial port -- one port, one master -- which is the whole reason an inbound table exists rather than a second tool talking to the cabinet

### BRINGING A GATEWAY UP

run once per cabinet, in this order, before trusting a poll loop on it.

- **`examples/address_scan.py`** — who is on this cable, at which address
- **`examples/connectivity_check.py`** — is anything there, and is RTE off? The first thing to run and the thing to run after any bad stop
- **`examples/polling_capability.py`** — what polling rate the machine declares, beside what the spec requires
- **`examples/wire_diagnostics.py`** — settle how THIS machine wants to be polled before trusting any poll loop on it -- cabinets differ

### OPERATING AND DIAGNOSING

the floor's day-to-day: money paths, lockups, and reading what a machine says about itself.

- **`examples/accounting_summary.py`** — the accounting figures as SAS reports them, grouped the way an operator reads them
- **`examples/aft_cashout.py`** — take credits off electronically
- **`examples/aft_push_credits.py`** — put credits on by AFT, checks first
- **`examples/aft_status.py`** — what AFT would do, before asking it to
- **`examples/bill_in_watch.py`** — every note taken, with the currency the machine thinks it is -- and the context that tells a rejected BILL from a returned TICKET
- **`examples/exception_monitor.py`** — watch exceptions in plain language while somebody plays. Also carries --disable-rte, the recovery path after a bad stop
- **`examples/handpay_clear.py`** — what the pending handpay is, and clearing it without a reset key
- **`examples/handpay_queue_drain.py`** — read the handpay queue the §7.8.1 way, one entry at a time -- several lockups can be waiting
- **`examples/meter_snapshot.py`** — one machine's complete readable state, to a file
- **`examples/ticket_in_watch.py`** — watch a ticket go in and read what the machine says

### PROOF

how a developer checks our claims instead of believing them. These are the acceptance criteria made runnable.

- **`deploy/sql_samples.py`** — every table and view in the gateway database, as CSV: the last N rows of each, and a header for EVERY table including the empty ones, because "exists and is empty" and "does not exist" are different facts. Validation numbers are replaced with a pseudonym shared across tables, so a pool number and the ticket printed with it still line up and neither leaves the gateway
- **`examples/outage_loss.py`** — what an outage COST: credits played while nobody was polling, against the events the cabinet could still hand back. The recovery is bounded by §2.2.1's queue rather than by the length of the gap, so a long outage loses nearly all of it however fast the host polls afterwards -- which is the number §19 is about (A1, C2, C4)
- **`examples/queue_safety.py`** — could the queue have overflowed, and was the gateway why (C5). Overflow is acceptable only when the link was lost
- **`examples/rte_reconcile.py`** — five detectors: did RTE lose anything, and do the meters and the events agree, award side included (C2, C4)
- **`examples/unwanted_handpays.py`** — the migration's own scorecard. 0x66 -> 0x57 -> 0x51 with no ticket printed is a handpay the HOST caused by not answering §15.7's ten seconds. Reads the cause off the wire, so no ticket-out limit has to be configured and a jackpot -- which raises no validation request -- cannot appear

### THE BENCH

for finding long polls we have not needed yet. Ships so other developers can experiment on their own projects.

- **`examples/poll_test_tool.py`** — fire any long poll at an EGM and watch what comes back. How other developers find the polls their own projects need

## What is deliberately missing

The private repository sorts every tool into tiers, and one of them is
**lab-only**: soak drivers, floor-survey apparatus, one-question probes, and
a ticket authority that redeems real tickets. 28 tools. They answer
questions about one cabinet on one bench and would be noise — or a hazard —
in a repository meant for building a product.

They are excluded by that classification rather than by hand, so a tool
classified lab-only tomorrow stops being published without anyone editing a
list. Tests whose *subject* is a lab tool go with it; tests that merely
reference one stay and skip.

Also absent: floor captures, survey data, machine serials, the licensed
specification, and `legacy/sas.py` — the Python 2 predecessor this was
cleaned from, which stays private so the clean-room separation keeps
meaning something. `legacy/meter_snapshot-2026-11-08-pre-probe-table.py` is
here, because surveys taken before that date were taken with it and a
snapshot can only be read against the tool that wrote it.

## Verified against real hardware

Every meter claim in `tests/test_hardware_vectors.py` was read over SAS from
an EGT Premier and then found on that machine's own accounting display, to
the cent. Where this project's documents and the specification disagree, the
disagreement is written down with the measurement that settled it.
