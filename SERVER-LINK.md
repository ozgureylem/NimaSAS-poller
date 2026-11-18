# The server link, and a server to test it against

The Linux migration specification (V2) divides the gateway into two
processes (§4.3): the **poller**, which owns the serial port, and the
**gateway client**, which owns the connection to the server. Until now
this repository held only the first. This document covers the second, the
change in the poller that lets a server answer ticket-in, and a
**stand-in server** that runs on a laptop so the whole path can be tested
before RamSys has built the real one.

| | what | where | runs on |
|---|---|---|---|
| 1 | gateway client | `python3 -m gateway.client` | the gateway (Linux) |
| 2 | ticket-in asked of the server | `sql_poll_logger.py --server-ticket-authority` | the gateway |
| 3 | server stand-in | `python -m gateway.service` | a laptop or server (Windows or Linux) |

**Read this first: what the stand-in is and is not.**

- It speaks the §10 wire contract exactly: the frame, the HMAC, the
  sequence rule, the message types, signed pool batches. A real gateway
  service must interoperate with the same gateway client.
- It applies §12.2's ticket rules: the answer table, the lease, *Redeemed
  only on positive completion*.
- It is **not the CMS**. Its database is one SQLite file (nothing to
  install — SQLite is part of Python). It **mints its own validation
  numbers at random**; the real service uses RamSys's existing scheme
  (§11.2). Numbers it issues are lab numbers.
- It does not implement `ON_DEMAND_PULL`, `ROTATE_KEY`, `REVOKE`,
  `DISABLE` or `ENABLE`. The gateway client answers each of those with an
  `ALERT` saying so.

Nothing here changes a gateway that is not asked to use it. Without
`--server-ticket-authority` the poller behaves exactly as before, and the
poller still never imports the `gateway` package.

---

## 1. The laptop (Windows 10 or Windows Server)

### 1.1 Python

The stand-in needs **Python 3.10 or newer** and nothing else — no
packages, no database server, no compiler.

**Python 3.4 will not run it**, and that is not fixable from this side:
3.4 predates f-strings, `async`/`await` and most of what this code is
written in, reached end of life in 2019, and its `pip` can no longer talk
to the package index. A new Python installs **alongside** an old one and
does not disturb it:

1. Download the current Windows installer (64-bit) from python.org.
2. Choose *Customize installation* and set the location to, for example,
   `D:\Python313`. Leave **"Add python.exe to PATH" unticked** so that
   whatever uses the 2023 install keeps finding it.
3. Check it, using the full path so there is no doubt which one ran:

```
D:\Python313\python.exe --version
```

Every command below is written as `python`. On this laptop that means
`D:\Python313\python.exe`.

### 1.2 The code

Either `git clone` this repository, or on GitHub use *Code → Download
ZIP* and unpack it. The stand-in needs only the `gateway/` folder, so
copying that one folder to the laptop is enough. Open a terminal **in the folder that
contains `gateway`** — the commands use `-m`, which looks for the `gateway` package
in the current directory.

### 1.3 Create the stand-in and commission the gateway

```
python -m gateway.service init
```

Creates `cms_standin.sqlite3` and, beside it,
`cms_standin.pool-signing.key` — the Ed25519 private key that signs pool
batches (§7.3). It prints the matching public key.

Find the laptop's address on the network the gateway is on (`ipconfig`),
then:

```
python -m gateway.service commission LAB-001 --server-host 192.168.1.50
```

`LAB-001` is the gateway's `client_id` (§1: the stable identity of a
floor position). The command mints that gateway's HMAC key and prints
**one line to run on the gateway**. That line contains the key. It is a
secret: paste it into your SSH session to the gateway and nowhere else.

Running `commission` again for the same `client_id` mints a new key,
kills the old one and resets both sequence counters (§10.3, §10.5).

**Size the pool before the first `serve`.** A full issue — the first pool,
and every rotation — is 50 numbers unless you say otherwise, and it is set
per gateway. To change it without recommissioning:

```
python -m gateway.service pool-size LAB-001 200
```

Run it with no number to see the current value. If the gateway already
holds a pool, the new size applies at the next rotation, or immediately
with `replace-pool`. The limit is 500, which is the most one batch can
carry; it is not a cap on the pool, which top-ups can grow past it.

### 1.4 Let the gateway in, and start

Windows Firewall blocks inbound connections by default. In an
**administrator** PowerShell, allowing only the gateway's address:

```
netsh advfirewall firewall add rule name="NimaSAS gateway service" dir=in action=allow protocol=TCP localport=5020 remoteip=192.168.1.60
```

Then:

```
python -m gateway.service serve
```

It listens on TCP 5020 and logs every connection, pool batch, ticket
decision and alert. Leave it running. In a second terminal, at any time:

```
python -m gateway.service status
```

**The link is plaintext by design** (§10.3): authenticated, not
encrypted. That is the specification's choice for a switch-isolated floor
segment. On an office network, anyone on that network can read meter
values and validation numbers off the wire. Use an isolated segment, or a
cable.

---

## 2. The gateway

Update the checkout, then run the line the laptop printed, **with
`POLLER_DB.sqlite3` replaced by the database file the poller writes**:

```
python3 -m gateway.client commission --db POLLER_DB.sqlite3 --client-id LAB-001 --server-host 192.168.1.50 --server-port 5020 --hmac-key ... --server-public-key ...
```

The server cannot know that path, so it prints a placeholder rather than
a guess. **The client and the poller must be on the same file.** On two
different files the client still connects, fills a pool and drains — and
never sees a ticket-in question, so every ticket is refused after the
timeout with nothing visibly wrong.

It writes `<database name>.linkkey.json` beside the database, mode `0600`.

Start the client. It never opens the serial port, so it runs beside a
poller that is already polling:

```
python3 -m gateway.client run --db gateway.sqlite3
```

`deploy/nimasas-gateway-client.service` runs the same thing under systemd.
It is deliberately a different unit from the poller's: the poller's has
the serial port and `PrivateNetwork=yes`; this one has the network and no
devices.

**What happens the moment it connects — read before you start it.** The
stand-in has never issued this gateway a number, so every `available`
number in its pool (from `--seed-validation-pool`, or inserted by hand)
is one no ticket table knows. The stand-in therefore sends a
`POOL_REPLACE`: those rows become `revoked` (kept, never deleted) and 50
signed, server-issued numbers take their place. `assigned` rows — printed
tickets — are not touched.

A database that has never been drained holds its whole history as
undrained rows. The ticket tables go first, but `machine_events` from
weeks of soaks will follow, 25 rows at a time. To send only what the
ticket test needs:

```
python3 -m gateway.client run --db gateway.sqlite3 --drain-tables ticket_out_history,ticket_in_events,ticket_in_completions,handpay_queue
```

### 2.1 Ticket-in from the server

Restart the poller with one more flag:

```
python3 examples/sql_poll_logger.py gateway.ini --db gateway.sqlite3 --rte --server-ticket-authority
```

Now, on exception `0x67`, the poller reads the ticket (LP `0x70`), writes
its question to the `validation_queries` table and waits up to
`--ticket-query-timeout` (2 s). The client sends `VALIDATION_QUERY`, the
server answers, and the poller sends LP `0x71` with **the server's code
and the server's amount**. If no answer arrives the ticket is refused with
`0x80` and handed back, and for `--ticket-query-backoff` (30 s) further
tickets are refused without asking (§9.2).

This is the only ticket-in path there is. The gateway never decides what
a ticket is worth, so a server outage halts ticket-in by design; cashout
is unaffected, because it is answered from the pool.

---

### 2.2 What the server does to the pool on its own

Three things, from the validation-pool spec change of 2026-11-12
(`docs/project/VALIDATION-POOL-RULES-SPEC-CHANGE.md`, private repository):

| when | what | size |
|---|---|---|
| first contact | `POOL_REPLACE`: whatever the gateway held is revoked | that gateway's `pool-size` |
| the heartbeat carries `low_water` | a top-up, on that same beat | `max(15, used_24h)`, at most 500 in one batch |
| the oldest live numbers reach **35 h** | `POOL_REPLACE` (a rotation) | that gateway's `pool-size` |

`used_24h` is the gateway's own count of numbers assigned in the last 24
hours — printed or not — and travels in the heartbeat. The server cannot
count it: the pool table is never drained, and the ticket history holds
only tickets that printed.

The rotation is checked every five minutes. A gateway that is offline
when its pool passes **36 h** raises a `rotation_missed` alert, once an
hour, until it comes back. The one-hour gap between 35 and 36 is
deliberate and depends on that five-minute check.

**A rotation here retires the whole pool**, which is more than the spec
change's rule 4 asks for. Rule 4 revokes only the stale numbers and
replaces half of them; that needs a number-level revoke covered by the
signature, which is not built. Until it is, a quiet machine's pool goes
back to its full size every 35 hours instead of settling at 10–25.

### 2.3 One gateway, several machines

The specification commissions a gateway to one floor position. The lab
carries one gateway from cabinet to cabinet, so every row it drains
arrives under the same `client_id`. The server keeps them apart by
**machine epoch**: the poller's own record of each cabinet it found
itself on, with the SAS serial number and asset number it read there.

Nothing needs doing on the gateway when you move it. The poller notices
the new identity when it re-interrogates the cabinet and opens a new
machine epoch; the client sends that with its next drain and names the
current machine in every heartbeat. On the laptop:

```
python -m gateway.service machines
```

lists every cabinet each gateway has been on, when, and how many rows
came from each. `status` shows the machine a gateway is on now and groups
ingested rows by machine. A ticket records the machine it **printed** at
and, once redeemed, the machine it was redeemed at.

Rows that reached the server before the gateway reported machines have
none. Once the updated client has drained once:

```
python -m gateway.service attribute
```

places them from their own timestamps. It says how many it could not
place and why; those stay unknown rather than being guessed.

Three things to know:

- **The name is shown, never used to decide.** Serial and asset are both
  typed in by a technician, the asset is not unique on the floor, and a
  RAM clear blanks both. So the server separates one cabinet from another
  only by the boundary the poller drew (`machine_changed` versus
  `ram_clear` and the rest), and reads the name off the epoch each time.
  A cabinet cleared and named an hour later carries that name for the
  rows recorded while it had none; a cabinet nobody named is listed as
  "unnamed cabinet" and is still kept apart from every other.
- **Rows without their own epoch are placed by time**, and there is a soft
  edge of a few seconds at a move: an event the new cabinet had queued,
  recorded just before the poller finished identifying it, lands on the
  old machine.
- **The validation pool moves with the gateway.** Numbers issued to
  `LAB-001` print at whichever cabinet it is plugged into. Each ticket
  records where, and a `machine_changed` alert marks each move. On a floor
  a move is an unassign-then-assign with a fresh key (§10.5); the lab
  shortcut is deliberate and is not that.

### 2.4 A server restored from a backup

The gateway keeps drained rows after the server acknowledges them — 72
hours for `machine_events`, 30 days for the other drained tables — so
that it is the server's backup of last resort. Three things make that
real, and none needs an operator:

1. **The server notices.** Each heartbeat carries `acked`. A server that
   holds fewer rows than it acknowledged raises `rows_lost_on_server` and
   sends a `replay` on its next invitation; the rows drain again and land
   on the same keys.
2. **The server can be heard.** A restored database brings back an old
   send-sequence counter, below what the gateway has already accepted, and
   every frame would be rejected as a replay. So the service moves each
   gateway's send sequence up to the clock (milliseconds since 1970) when
   it starts. A real gateway service needs the same or an equivalent.
3. **Unknown pool numbers are retired.** If the backup predates a pool
   batch, the server has no record of issuing those numbers. It raises
   `pool_issuance_lost` and replaces the gateway's pool, so nothing more
   prints from numbers it cannot honour.

**What a restore does not recover:** tickets *already printed* from a
batch the backup predates. They arrive by replay, raise
`ticket_number_not_issued`, and are refused at ticket-in until an operator
adds them with `add-ticket`. Closing that needs the gateway to keep each
signed batch it accepts, so it can present it again; that is not built.
A real service should back up its issued-number table at least as often
as it issues.

`meters_history` drains like any other table but is **not pruned** on the
gateway: that guard is deliberately still in place.

## 3. A test, with the prediction written down first

| # | do this | predict this |
|---|---|---|
| 1 | Start `serve`, then the client | Laptop log: `LAB-001 connected`, `minted replace ... epoch 1, 50 number(s)`, then `acknowledged epoch 1`. Gateway: `pool POOL_REPLACE applied: ... 50 issued, N revoked` |
| 2 | `sqlite3 gateway.sqlite3 "SELECT status, COUNT(*) FROM validation_pool GROUP BY status"` | 50 `available`; the old ones `revoked`; `assigned` unchanged |
| 3 | Cash out a small amount at the cabinet | A ticket prints as before — the laptop is **not** involved. Unplug the network cable and repeat: it still prints (§3.1 rule 1) |
| 4 | `status` on the laptop, a second or two later | The ticket under TICKETS as `Open`, with the printed amount, `source egm` |
| 5 | Insert that ticket | Laptop log: `ticket-in LAB-001 ...NNNNNN: ALLOW 0x00 Open, <amount> cents`. Credits appear. Then `REDEEMED at LAB-001 (machine status 0x00)` |
| 6 | Print another, insert it, and insert the *first* again | Second: allowed. First: `REJECT 0x80 Redeemed` — note what the **cabinet shows**; §9.4 requires the wording be checked per brand |
| 7 | Insert an old ticket the stand-in has never heard of | `REJECT 0x80 not in tickets table`, and an alert `ticket_unrecognised` |
| 8 | `python -m gateway.service add-ticket 00-0000-0000-0000-0001 --amount 25.00`, with that ticket's real number and the amount you want it to be worth; insert it | Allowed for 25.00, whatever is printed on it. This is §12.7, a cashdesk-issued ticket |
| 9 | Stop `serve`. Insert a ticket | Refused after 2 s, returned. Insert again within 30 s: refused **immediately**. Cashout still works throughout |
| 10 | Restart `serve`; `python -m gateway.service replace-pool LAB-001` | A new epoch; the 50 unused numbers `revoked`; a ticket printed **before** the replacement still redeems |

Step 6's second half and step 9 are the two worth watching closely. A
ticket paid twice, or a poller that waits thirty seconds instead of two,
would each be a defect in what is described here.

**What this run does not prove**, so nobody reports it as proven: more
than one gateway (the lease between two machines is tested in software
only); a restricted ticket on real hardware; behaviour at 1,500
connections; any brand but the one cabinet on the bench.

---

## 4. The wire, for whoever builds the real service

The frame is §10.1 and is implemented, with its processing order, in
`gateway/frame.py`. The JSON payloads below are what these two programs
actually exchange. Fields marked † are this implementation's choice where
the specification names the message but not its fields — they are
proposals, and RamSys should treat them as open until agreed.

**`0x01` HEARTBEAT** (G→S, every 15 s) — Appendix B.3:

```json
{"timestamp": "...", "queue_depth": 14,
 "pool_status": {"available_ids": 8, "low_water": true,
                 "active_batch_uuid": "...", "pool_epoch": 17,
                 "used_24h": 42, "pool_size": 50,
                 "low_water_mark": 10, "low_water_reason": "..."},
 "egm_status": {"online": true, "sas_version": "602",
                "meters_age_s": 3.1, "last_poll_ms": null, "headroom_pct": null},
 "disk_critical": false, "credit_meter": 12550,
 "machine": {"id": 3, "started_at": "...", "ended_at": null,
             "reason": "machine_changed", "serial_number": "...",
             "asset_number": 246, "sas_version": "602"}}
```

`machine` † is the open machine epoch: the cabinet the gateway is on now.
It is `null` until the poller has identified one.

Two further top-level fields †, not shown above:

- `db_instance_id` — the gateway database the `machine` epoch id counts
  in. A re-imaged gateway starts again from epoch 1, so an epoch id means
  nothing without it.
- `acked` — `{table: highest row id the server has acknowledged}`. This is
  how a server learns, with nobody telling it, that it has **lost** rows it
  once acknowledged (§2.4 below).
- `sas_link` † — what the poller last said about the **serial link**, with
  its own age. `null` when this gateway cannot tell you:

  ```json
  "sas_link": {"state": "reconnecting", "since": "2026-11-18T03:14:02+00:00",
               "for_s": 94.2, "said_s_ago": 1.1,
               "attempt": 4, "max_attempts": 10,
               "detail": "serial port lost; retrying in 30s"}
  ```

  `state` is `up`, `reconnecting` or `down` (`down` = the reconnect loop
  gave up). **Read it together with `said_s_ago`, never alone.**
  `egm_status.online` cannot answer this question: it is derived from how
  stale `meters_current` is, and a stale meter table has two causes needing
  opposite responses —

  | `state` | `said_s_ago` | reading |
  |---|---|---|
  | `up` | small | healthy |
  | `reconnecting` / `down` | small | **the cable** — send someone to the cabinet |
  | `up` | large | **the gateway** — the poller is gone; the state is its last word, not current |

  `since` survives a repeated state, so an outage can be aged from it.
  A gateway that has never written the gauge sends `null` rather than a
  default of `up`, which would be a guess about the one thing being asked.

The last four `pool_status` fields † are not in Appendix B.3 (amendment
A25, accepted 2026-11-12). `used_24h` is what a refill is sized from;
`pool_size` is what the gateway was last filled to and is `null`, not 0,
when no fill is recorded; `low_water_mark` is the number `low_water` was
compared against. A server that ignores them still works; a gateway that
omits them gets minimum-size refills.

`last_poll_ms` and `headroom_pct` are sent as `null`: the poller does not
yet record them anywhere the client can read. `meters_age_s` † is how the
client derives `online`.

**`0x02` PULL_INVITE** (S→G) —
`{"invite_id", "max_events", "pool_topup"?, "replay"?}`.
`pool_topup` is a signed batch, below. `replay` † is

```json
{"db_instance_id": "<uuid>", "after": {"machine_events": 4000, "ticket_out_history": 12}}
```

and asks the gateway to drain again every acknowledged row above the
given id in each named table. It is **by row id, never by time**: event
ids are arrival order within one collection path, so a cut by time
re-sends some rows twice and misses others. The gateway refuses a request
naming another database, ignores a table it does not drain, allows at
most three replays an hour that move rows, and records each one in
`link_replays`.

**`0x03` EVENT_BATCH** (G→S) †:

```json
{"invite_id": "inv_...", "db_instance_id": "<uuid>",
 "events": [{"table": "ticket_out_history", "id": 7, "row": {...}}]}
```

At most `max_events` rows or 64 KB. `row` is the table row as a JSON
object, minus `synced_at`. Each event also carries `epoch_id` †, and the
batch carries `epochs` †: the full machine-epoch objects those events
name, in the same shape as the heartbeat's `machine`. A server can
therefore place every row on a cabinet from the batch alone.

The batch also carries `sas_link` †, in the **same shape and from the same
source** as the heartbeat's field above (so a server that parses one parses
the other). A `PULL_INVITE` is the moment the server is about to act on
these rows, so the link state travels with the data it describes rather
than having to be correlated against a heartbeat from some other second.
**This matters most when `events` is empty**: a cable that is out means
there is nothing to drain, and an empty batch is otherwise
indistinguishable from a quiet floor.

It does **not** replace the heartbeat's copy. A pull only ever tells the
server what it thought to ask; a gateway that has stopped answering is the
case no pull reaches. Treat the heartbeat as the liveness signal and the
batch field as provenance for that batch.

**`0x04` ACK** (S→G) † — `{"acks": [{"db_instance_id", "table", "id"}]}`,
sent only after the rows are committed.

**`0x14` POOL_REPLACE** (S→G), and `pool_topup` — the same object:

```json
{"batch_uuid": "<uuid>", "pool_epoch": 18,
 "numbers": ["<18 digits>", "..."], "signature": "<128 hex chars>"}
```

The signature is Ed25519 over this byte layout (all integers big-endian),
**not** over the JSON:

```
client_id   2-byte length, then the ASCII bytes
pool_epoch  8 bytes
batch_uuid  16 bytes (the UUID's raw bytes)
count       4 bytes
numbers     each: 1-byte length, then ASCII digits, in the order sent
```

§7.3 names the first three and the numbers; the count † is added so a
truncated list is a different message. A number is 18 decimal digits: the
2-digit validation system ID, then 16. System ID `00` is refused in a pool
(Table 15.8a makes it a denial).

**`0x20` VALIDATION_QUERY** (G→S) † —
`{"query_id", "validation_number", "parsing_code", "reported_amount_cents", "asked_at"}`

**`0x21` VALIDATION_RESPONSE** (S→G) †:

```json
{"query_id": 12, "decision": "allow", "transfer_code": 0,
 "amount_cents": 2500, "restricted_expiration": 0, "pool_id": 0,
 "reason": "Open"}
```

`transfer_code` is the byte the poller puts in LP `0x71` (SAS Table
15.12a): `0x00`–`0x02` with `allow`, `0x80`–`0x88` with `reject`. The
poller refuses any other pairing with `0x80`. The **server** chooses the
code (§9.4); the stand-in's choices are:

| situation | code | why |
|---|---|---|
| Expired | `0x85` | the customer can act on it |
| detail not yet drained, or WfA | `0x86` | "try again" |
| amount disagrees with the ledger | `0x87` | |
| Pending, Redeemed, Paid, VOID, cage-only, unknown number, locked | `0x80` | §9.4: refuse neutrally. `0x84` "already redeemed" and `0x82` "not in system" are never sent |

`reason` carries the specific cause for the log.

**`0x22` REDEMPTION_RESULT** (G→S) † —
`{"query_id", "validation_number", "machine_status", "completed_at", "gateway_outcome"}`.
`machine_status` is LP `0x71`/`FF`'s answer after exception `0x68`; its
top three bits are the category (Table 15.12d): `000` redeemed, `1xx`
rejected. Sent only for a completed cycle.

**`0x07` ALERT** (G→S) † — `{"timestamp", "kind", "detail"}`.

**`0x15` AFT_TRANSFER** (S→G) † — not in §10.2; added for the lab.

```json
{"transfer_id": "...", "cashable_cents": 1919, "restricted_cents": 0,
 "nonrestricted_cents": 0, "pool_id": 0, "expiration": 0}
```

The client does not touch the cabinet: it writes a `host_requests` row
and the poller performs or refuses the transfer between exchanges. The
poller refuses unless it was started with `--allow-server-aft`.

---

## 5. Where this stands against the specification

Built and tested here: §7.2 batch acceptance, §7.3 signature
verification, §7.4 replacement, §9.1 the ticket-in sequence, §9.2 backoff
and cap, §10.1–§10.4 frame, authentication, replay protection, heartbeat
and drain, the `counters` table and `db_instance_id` (§6.2, B.2), §6.4
pruning of acknowledged rows (`examples/db_maintain.py`), and — beyond the
specification — pool sizing per gateway, rotation at 35 hours, the
row replay of §2.4, and machine identity per drained row (§2.3).

Not built: §8.1 TITO disable and re-enable; §8.2–§8.3 orphan matching and
the fast-path gate; §10.5 key rotation and revocation; §10.6 rate limits
and the alert ladder beyond a per-connection failure count; §10.7
on-demand reads over the link (the `host_requests` table still serves
them locally); number-level pool revocation (a rotation here retires the
whole pool); recovery of pool issuance after a restore (§2.4); everything
in §11.3–§12 beyond the ticket-in answer table and the lease.

Other commands of the stand-in, for the lab: `edit-ticket` and
`ticket-history` (re-value, re-open or inspect a ticket), and `push-aft`
(the `AFT_TRANSFER` above). `python -m gateway.service --help` lists them.
