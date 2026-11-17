# The NimaSAS gateway: what each tool is, and why it exists

**For:** the RamSys development team
**From:** the NimaSAS project
**Companion document:** *Linux Migration — Technical Specification V2.1*
(17 November 2026), shared separately. The specification says **what** the
gateway layer must do. This document explains **what each file in this
repository is for, and why it exists**, so you don't have to open every
Python file to find out.

Every script here already explains itself in its own docstring, usually
at length. This guide collects those explanations in one place. When you
need the detail, the docstring is the authority, and when a comment and
the code disagree, trust the code.

We keep testing after this hand-over: floor surveys, and polling cabinet
models we haven't polled yet. New findings will be sent to you as they
come.

---

## 0. How to read this

**Part A** describes the gateway as it runs in our lab today, in depth:
the poller, every option on its command line, and the processes around it.
This is where most design decisions live.

**Part B** covers every other file in the repository: the `saspy` library,
the `gateway` package, each example script, the deploy files and the
documents. Each entry says what the file does, why it exists, and whether
it is safe on a live machine.

**Part C** is a glossary.

### Conventions

- **"SAS §n"** means the SAS 6.02 protocol specification. **"Spec §n"**
  means the Linux Migration Technical Specification V2.1.
- **LP `0xNN`** is a SAS long poll. A bare **`0xNN`** in an event context
  is a SAS exception code (Table A-1).
- **Safety classes**, used throughout Part B:
  - **read-only**: reads the machine or the database and changes nothing;
  - **changes machine state**: alters something on the cabinet without
    moving money;
  - **moves money**: credits on, credits off, or a redemption. Lab
    machines only.
- **Port or database.** A tool either opens the serial port (and so cannot
  run while the poller holds it: one port, one master) or reads the
  gateway database (and so can run alongside the poller).

### Why serials, asset numbers and validation numbers appear in public

This is deliberate, and it follows from the design. **The server is the
only authority.** A validation number, serial or asset number is a
*question* the gateway asks the server, never a credential. Holding every
number in this repository lets no one redeem anything. That is why the
gateway has no way to authorise a ticket (§A2.12), and why a server outage
halts ticket-in.

The argument covers identifiers only. Anything that would let a holder
*act as* the authority (the pool signing key, a gateway's link key file,
any private key or shared HMAC secret) is a different category. None of
those is in the repository, and none must ever be.

---

# Part A — The gateway as it runs

## A1. The picture

```
                       ┌───────────────────────── gateway ──────────────────────────┐
                       │                                                             │
 EGM ──RS-232──────────┤  SAS poller                shared SQLite        gateway    │── LAN ── server
  (SAS 6.02)           │  sql_poll_logger.py  ◄──►  database      ◄──►   client     │         (CMS stand-in
                       │  owns the serial port      (WAL mode)            owns the   │          in the lab)
                       │  NO network                                      socket     │
                       │                                                  NO serial  │
                       └─────────────────────────────────────────────────────────────┘
```

**Exactly one process talks to the cabinet, and it has no network.
Exactly one process talks to the server, and it has no serial port. They
meet in one SQLite file.** Everything that crosses between them is a row:

| direction | table | what the row means |
|---|---|---|
| poller → server | every table with `id` + `synced_at` | an event, meter row, ticket… waiting to be drained |
| poller → server → poller | `validation_queries` | "a ticket is in escrow, what is it worth?", answered by the server |
| server → poller | `validation_pool` | pre-issued validation numbers for cashouts (signed batches) |
| server → poller | `host_requests` | an on-demand job: a server AFT push, or a WfA ticket lookup |

**Why two processes and not one** (spec §4.3). A fault on the server side
(unreachable server, malformed frame, memory pressure on a 32-bit unit)
must never stop polling, because a cabinet that isn't polled loses events,
and lost events can't be recovered (§A5). In the other direction, a poller
restart shouldn't make the server think the gateway is dead. The two
systemd units enforce the split: the poller's has `PrivateNetwork=yes`,
and the client's has `PrivateDevices=yes`.

**The alternative, which the spec leaves open.** A thread inside the
poller means fewer moving parts. If you build it that way the wire
contract doesn't change. What must stay true is "exactly one owner of the
serial port, and the client reaches the EGM only through it".

## A2. The poller's command line, option by option

The poller runs in the lab like this (the home directory shown in generic
form):

```bash
/usr/bin/python3 /home/nimasas/NimaSAS/examples/sql_poll_logger.py /home/nimasas/gateway.ini \
    --db /home/nimasas/soak.sqlite3 \
    --rte \
    --mode append \
    --min-poll-interval 0.2 \
    --interval 2.0 \
    --heartbeat-every 2 \
    --drain-fast-poll \
    --decline-aft-cashout \
    --table-c7-interval 0 \
    --full-meter-interval 593 \
    --validation-meters-interval 293 \
    --serve-host-requests \
    --allow-server-aft \
    --server-ticket-authority \
    --credit-poll-every 3 \
    --db-size-warning-mb 500
```

This is the lab soak configuration. Four of these options are lab choices
and are marked as such below. The floor configuration is
`deploy/nimasas-poller.service` (§B4).

### A2.1 Summary

| option | in one line | changes the wire? | moves money? |
|---|---|---|---|
| `gateway.ini` | which port and SAS address | — | — |
| `--db` | the shared SQLite file | — | — |
| `--rte` | real time event reporting: events arrive with data | yes | no |
| `--mode append` | a meter-history row every cycle (**lab**) | no | no |
| `--min-poll-interval 0.2` | SAS §2.3.3's 200 ms floor between messages | yes | no |
| `--interval 2.0` | the gap between cycles | yes | no |
| `--heartbeat-every 2` | drain the event queue after every 2 long polls | yes | no |
| `--drain-fast-poll` | 40 ms polling, only while draining a burst | yes | no |
| `--decline-aft-cashout` | answer `0x6A` with an all-zero LP `0x72` (**per model**) | yes | no, all amounts are zero |
| `--table-c7-interval 0` | read the Table C-7 meter set every cycle | yes | no |
| `--full-meter-interval 593` | 39 single-meter polls, about every 10 min | yes | no |
| `--validation-meters-interval 293` | 12 LP `0x50` polls, about every 5 min | yes | no |
| `--serve-host-requests` | listen to the `host_requests` table | when asked | only with the next option |
| `--allow-server-aft` | the server may push AFT credits onto the EGM (**lab**) | when asked | **yes** |
| `--server-ticket-authority` | ticket-in is decided by the server, never locally | yes | **yes**, when the server allows |
| `--credit-poll-every 3` | read the credit meter, but only on an empty queue (**lab**) | yes | no |
| `--db-size-warning-mb 500` | warn when the database file reaches this size | no | no |

### A2.2 Defaults in force that aren't on the command line

| setting | default | why it matters |
|---|---|---|
| drain priority | **on** | the main burst defence (§A5.4) |
| `--drain-burst-preemptions` | 12 | pre-emptions of one long poll that count as a burst (§A5.5) |
| `--drain-fast-poll-fallback` | 0.10 s | the draining rate for a cabinet that doesn't advertise 40 ms |
| `--ticket-query-timeout` | 2.0 s | how long the poll loop waits for the server's ticket answer |
| `--ticket-query-backoff` | 30 s | after an unanswered query, refuse tickets locally for this long |
| ticket query cap | 10 per 60 s | so reinsertions can't hammer the server |
| `--comms-gap-seconds` | 30 s | a link outage this long triggers a full re-interrogation |
| `--general-poll-retries` | 3 | immediate re-polls of a failed general poll |
| `--c7-probation-minutes` | 10 | decides whether the cabinet answers Table C-7 at all |
| `--pool-depth-alert` | 10, or 20% of the last fill | the low-water mark for validation numbers |
| `--pool-age-alert-hours` | 36 | alarm if the oldest unused number is this old |
| interrogate on connect | on | a full identity and configuration read at start |

### A2.3 `gateway.ini` and `--db`

`gateway.ini` names the serial port, the SAS address, the baud rate
(19,200, fixed by SAS) and the per-exchange read timeout. Its format is
in `saspy/config.py`, and `examples/commission_gateway.py` can write it.

**The gateway client must be started against the same database file.**
If the two processes point at different files, ticket-in stops working
without an obvious error: every inserted ticket is refused after the
query timeout and handed back.

### A2.4 `--rte`: real time event reporting

**What.** LP `0x0E` puts the cabinet into real time event mode (SAS §12).
Events then arrive as Table 12.5 messages carrying data: a game start's
wager and running coin-in meter, a game end's win, a bill's denomination.
Without RTE they arrive as a bare one-byte code. The decoded fields are
stored as JSON in `machine_events.rte_fields`.

**Why.** The migration exists to fix unwanted handpays and **wrong player
data**. Player data is per game. Meters count the machine, not the
session, so **a lost game-start event loses that game's bet, denomination
and coin-in anchor, with no second copy anywhere in the protocol.** RTE is
the only mode in which the events themselves carry enough to audit the
stream (§A5.6).

**What RTE requires of the host, and how the poller meets it:**

1. **RTE state lives in the EGM, not the gateway.** A poller that is
   killed or loses power leaves the cabinet in RTE mode. The next program
   to poll it would read Table 12.5's address byte as an exception code
   and misframe every long poll. So the poller probes its own framing
   **before anything else reads the wire** and clears a stuck RTE. It
   disables RTE on every orderly stop (SIGTERM and SIGHUP become a clean
   teardown). The service unit also runs an advisory
   `exception_monitor.py --disable-rte` before start. Both mitigations
   are needed, because the acknowledgement of LP `0x0E` can itself be
   pre-empted by a queued event.
2. **The cabinet boots more slowly than the gateway.** After a power cut
   the gateway is polling before the cabinet answers, so the RTE enable is
   **retried every cycle until it takes**. One failed attempt must never
   leave the whole run in the wrong mode.
3. **Any long poll can come back as an event instead of its answer**
   (SAS §12.3). See §A5.2: this shaped most of the poll loop.

### A2.5 `--mode append` (lab)

**What.** A full meter row (about 200 columns) goes into `meters_history`
every cycle.

**Why, in the lab.** Every cycle's meters are what the event stream is
reconciled against.

**Why not on a floor.** It grows without bound and writes flash on every
cycle. The floor setting is `--mode ring`: one row per minute, a cap of
200 rows, and a burst of full-resolution rows after any anomaly (spec
§6.3). **Ring eviction never deletes a row the server hasn't
acknowledged.** During an outage the ring stops evicting and grows at the
append rate until the link returns. That is a soft cap, and the correct
direction: disk space can be recovered, a deleted unacknowledged row
can't.

### A2.6 `--min-poll-interval 0.2`

**What.** At least 200 ms between the start of one message to the cabinet
and the start of the next. **General polls and long polls count against
one budget.**

**Why it is enforced in the transport, on every write**
(`saspy/transport.py`). Every caller (sweep, heartbeat, ticket handler,
on-demand request) has to obey the floor, and pacing done in the callers
can be bypassed by accident. In the transport it can't be.

**The ceiling this sets.** One poll collects at most one event, so 200 ms
means at most 5 events per second **for any SAS host**. Ordinary play is
well inside that. A person pressing buttons rapidly can exceed it. That is
what drain priority and fast poll exist for (§A5).

**Pacing and the read timeout are independent.** Pacing gates the write;
the timeout gates the read. Shortening the timeout doesn't poll faster; it
only stops the host waiting on silence.

### A2.7 `--interval 2.0`

The gap between cycles. SAS §2.3.3 allows 0.2–5.0 s, and the option
refuses anything outside that range. **It is not a lever for event loss.**
Under load the gap isn't even idle: while drain priority is on, it is
spent on general polls, because the gap between cycles is the longest
uninterrupted stretch in the loop.

### A2.8 `--heartbeat-every 2`

**What.** During the meter sweep, after every 2 long polls, a heartbeat
runs general polls **until the cabinet reports an empty queue** (capped at
20 per heartbeat).

**Why drain-to-empty.** SAS §2.2.1 guarantees only a 20-event queue and
drops the oldest event on overflow. A heartbeat that took one event and
returned would fall behind during a burst. Draining to empty means the
length of the sweep no longer matters: the queue can only grow by what
arrives between two heartbeats.

**Why 2 here.** It works well on our lab cabinet. The floor unit uses 1,
the more conservative choice for a cabinet nobody has characterised.

**Time-critical codes don't wait for the sweep to end.** A heartbeat that
finds `0x67` (ticket inserted), `0x57` (validation request), `0x68`,
`0x51` or `0x3D` acts on it **immediately**. A ticket has 30 s (SAS
§15.12), a validation request only **10 s** (SAS §15.7), and a missed
validation request becomes a handpay. Everything else is filed after the
sweep.

### A2.9 `--drain-fast-poll`

**What.** While drain priority is engaged (§A5.4), poll at **40 ms**
instead of 200 ms, and return to 200 ms the moment it disengages.

**Why.** One poll drains one event. At 200 ms the ceiling is 5 events/s,
and a burst faster than that can never be caught up; the queue only
decides how long before events are lost. At 40 ms the ceiling is 25/s, so
the queue can empty again. **The faster rate buys retention, not just
latency.**

**Limits:**
- It is used only if the cabinet advertises 40 ms support (LP `0xA0`,
  Table 7.14e bit 0). A cabinet that hasn't said either way gets the
  0.10 s fallback.
- It never goes below 40 ms.
- It is restored before the RTE teardown, so the next tool never inherits
  a cabinet being polled at 40 ms.

**Compared with RamSys today.** The current RamSys poller already polls
at 40 ms, all the time. This poller is more conservative: 200 ms
normally, and 40 ms only while drain priority is on, and only on a
cabinet that announces 40 ms support.

### A2.10 `--decline-aft-cashout` (per model)

**What.** On `0x6A`/`0x6B` (AFT request for host cashout), answer with an
LP `0x72` whose amounts are all zero. SAS §8.7 defines that as "perform the
cashout by whatever other means are available". **It moves no money.**

**Why.** A cashout is a three-stage ladder, and the spec describes only
the middle stage:

```
stage 1   0x6A  AFT request for host cashout     the cabinet asks the host to take the credits
stage 2   0x57  system validation request        the ticket path: answered from the pool (§A4.1)
stage 3   0x51  handpay pending                  nobody answered
```

When nothing answers stage 1, every cashout waits out the cabinet's AFT
timeout before the ticket path even starts, with a full pool and nothing
wrong. The decline tells the cabinet straight away to go to stage 2.

**It adds no dependency.** If the gateway is down, stage 1 times out as it
does today. Stage 2 is the opposite: there, a missing answer *causes* a
handpay.

**How it behaves.** A decline must carry the cabinet's asset number, or
the cabinet rejects it, so the poller reads that from the open machine
epoch and logs `NoAssetNumber` instead of sending a doomed decline. It
sends one decline per cashout and waits for the completion (`0x69`). It
doesn't answer every repeated `0x6A`, which would only waste poll slots in
the middle of a cashout.

**Why it is off by default and set per model.** On a cabinet with **no
printer**, "whatever other means" is an attendant, so declining makes a
handpay arrive sooner. Enable it per model, after a test cashout.

### A2.11 The three meter cadences

| source | polls | meters | cadence here | role |
|---|---|---|---|---|
| Table C-7 (LP `0x6F`/`0xAF`, 12 codes per chunk) | 13 | ~154 | **every cycle** | **primary** |
| single-meter sweep (LP `0x10`–`0x55`) | 39 | 39 | about every 10 min | verify, and fallback |
| validation meters (LP `0x50`) | 12 | 24 | about every 5 min | cross-check |
| **8 grouped polls** (LP `0x0F`, `0x2F` ticket meters, `0x19`, `0x1C`, `0x18`, `0x1E`, `0x2D`, `0x4F`) | 8 | coin in/out, games, tickets, bills | **every cycle, never deferred** | reconciliation anchor |
| last accepted bill (LP `0x48`) | 1 | 1 | every cycle | per-note currency |

**Why C-7 is primary.** Thirteen chunked polls fetch what the 39 single
polls fetch, plus about 115 more meters. It is the cheapest and richest
source.

**Why the single sweep is kept:**
- **To verify.** Nine meters can be read both ways. When both run in the
  same cycle they are compared, and a disagreement larger than the meter
  moved during that cycle is logged as `MeterDisagreement`. The two reads
  happen at different moments in the sweep, so a played machine can
  legitimately advance between them. Beyond that, two reads of one meter
  can't both be right, and a field-order mistake otherwise passes every
  test written from the spec.
- **As a fallback.** A cabinet that doesn't implement LP `0x6F` doesn't
  refuse it: SAS §4.4 lets it stay silent, so each chunk costs a full
  timeout. After three sweeps with zero codes answered, the C-7 probation
  turns C-7 off **and forces both cadenced sweeps back to every cycle**.
  Answering only some codes is normal, and SAS §7.21b allows it.

**Why these cadences.** Both sweeps are read often enough that a
reconciliation window of a few minutes contains more than one reading of
each, so their cross-checks have something to compare. Both are optional,
so drain priority defers them during a burst. The values are odd and not
multiples of each other, so the two sweeps rarely fall due in the same
cycle. These are lab reconciliation settings; the floor unit configures
its own.

**Why the C-7 chunks alternate `0x6F` and `0xAF`.** They return identical
data. A repeated identical long poll is an implied NACK (SAS §3.2), so a
compliant cabinet could answer chunks 2–13 with chunk 1's data: wrong
values in the right columns, with no error. SAS added `0xAF` for exactly
this reason.

**Reading the data.** `meters_current` is rewritten every cycle, and a
cycle that didn't sweep C-7 writes NULL into the C-7 columns (NULL means
"not read this cycle" throughout the schema). **Read C-7 through the
`latest_c7_meters` view.**

**Per-denomination bill counts: use Table C-7.** LP `0x1E` has no field
for a 2-denomination note in any currency, so it silently misses those;
C-7 has one. Both count in the machine's configured currency.

**Ticket-in meters: don't add `0028` to `002B`.** By Table C-7's own
definitions, `0028` is total cashable ticket in *including*
nonrestricted promotional tickets, so it already equals `0029` (cashable)
plus `002B` (nonrestricted promotional). The gateway sends all three. A
CMS that adds `0028` and `002B` counts promotional tickets twice. Use
`0029` and `002B` separately, or `0028` alone.

### A2.12 `--server-ticket-authority`

**What.** On `0x67` the poller reads the escrowed ticket (LP `0x70`,
re-read up to 4 times, because the cabinet raises `0x67` before the ticket
reaches escrow). It writes a `pending` row to `validation_queries` and
waits, for at most `--ticket-query-timeout`, for the gateway client to
bring back the server's answer. It then relays that answer to the cabinet
with LP `0x71`.

**The gateway decides nothing, but it does act.** It decides neither
validity nor amount, not even for a ticket it printed itself, and the
amount sent is always the server's figure. It *acts* in two ways: it
relays the server's verdict, and it sends a local `0x80` ("unable to
validate") when it can't get an answer.

**Why the gateway holds no ticket authority, and never will.** Cashout is
the one direction where the gateway answers alone, from a pool the server
trusted it with. Ticket-in is the opposite. A ticket issued at a
**cashdesk** was never seen by this gateway, so only the server can know
whether the cage already paid it. A gateway allowed to answer offline
could pay a ticket twice. **A server outage halts ticket-in,
deliberately.**

**Rules around the wait** (spec §9.2):
- **No answer within the timeout:** refuse with `0x80`. Then **refuse
  every ticket locally, without asking,** for the backoff period, so a
  customer reinserting a ticket doesn't pile load onto a struggling
  server.
- **A late answer is never acted on.** The row is claimed with a
  conditional UPDATE, so whichever side gets there first wins.
- **Every server answer is checked before it is relayed.** "Allow" needs
  a payable transfer code (`0x00`–`0x02`) and a positive amount. "Reject"
  needs a code in `0x80`–`0x88`. Anything else becomes `0x80`, with an
  alert.
- **Every outcome is written down**, including tickets refused during
  backoff, which move no meter and reach no server.

**The cost.** The poll loop is single-threaded, so nothing else is polled
while it waits. The timeout is capped at 4 s, inside SAS's 5 s maximum
between polls. Polling while waiting is possible and would be better for
the event queue, but it means re-entering the dispatcher from inside
itself.

**For the CMS:** `0x68` ("ticket transfer complete") means the cycle
**ended**, not that it succeeded. The outcome comes from LP `0x71`/`FF`
after `0x68`. The poller stores it in `ticket_in_completions`, and the
client reports it to the server.

### A2.13 `--serve-host-requests` and `--allow-server-aft` (lab)

**What.** `host_requests` is the poller's only inbound command channel.
Another process inserts a row, and the poller services it **only from
inside a heartbeat**, which by construction runs between exchanges. So an
on-demand request can never interrupt a poll in flight (spec §5.3); that
is guaranteed by structure, not by care.

There are two kinds of request:

- **`aft_push`**: the server asks for credits to be put on the EGM (spec
  Appendix C, SAS §8.3). The gateway client turns the server's
  `AFT_TRANSFER` into a row, because only the poller may talk to the
  cabinet.
  - **Without `--allow-server-aft` it is refused**, and the reason is
    written into the row, so the server learns why nothing happened.
  - With the option, per-run caps still apply: a maximum number of pushes,
    a maximum total, and a minimum interval.
  - Before sending money the poller reads LP `0x74` (status only, no
    lock). **A failed read means no push.**
  - A failed transfer **is not retried with a fresh transaction ID**. A
    failure after the cabinet acted looks the same as one before it, and
    a fresh ID is how a machine gets paid twice.
- **`wfa_pull`**: "what was this ticket worth?" (*Waiting for Amount*,
  spec §12.5). Answered from `ticket_out_history` at no poll cost where
  possible. Otherwise the poller walks the cabinet's 31-record ticket
  buffer by index (LP `0x4D`, which leaves the unread pointer alone), a
  few records per heartbeat, until the request's deadline.
  `examples/wfa_request.py` raises one.

**Why two options, both off by default.** A gateway's standing posture is
**read-only to the EGM**. `--allow-server-aft` reverses that, so it is a
separate option someone must type for a specific cabinet, and the poller
announces it at every start. Giving it without `--serve-host-requests` is
an error, because it would claim "the server may move money" while doing
nothing.

### A2.14 `--credit-poll-every 3` (lab)

**What.** Every third time a heartbeat has **just found the queue empty**,
read the credit meter (LP `0x1A`) into `credit_samples`.

**Why.** It feeds an offline check (credits before, minus wagers, plus
wins, equals credits after) that can score individual game ends. That
needs credit readings closer together than one per cycle.

**Why its cost is bounded.** It fires only on an empty queue, so the slot
it uses wasn't carrying an event. During a burst the queue is never empty,
so the sampler never runs. If an event slips in first, SAS §12.3 delivers
the event in place of the reply: the event is kept and only the sample is
lost. A floor gateway has no such report to feed.

### A2.15 `--db-size-warning-mb 500`

A warning on every cycle once the file reaches this size. **It is a fault
indicator, not a capacity valve** (spec §6.4). Rising disk use means the
event rate is far above expectation, or the drain is failing while the
link stays up. **The poller never deletes rows to make room.** A failed
commit (`SQLITE_FULL`) is reported loudly every time, because a gateway
that silently stops buffering brings back the original defect.

---

## A3. One run, start to finish

### A3.1 Start-up, in order

1. **Validate the options before touching hardware**, so a refusal never
   holds the port.
2. **Open the port exclusively** (`TIOCEXCL`) and **check it can do
   mark/space parity**, which is how SAS's ninth "wake-up" bit is
   produced. A port that silently drops it looks exactly like a cabinet
   that isn't answering. A second process on the same port exits with
   code 2 rather than corrupt the first one's frames.
3. **Open SQLite** in WAL mode with a 30 s busy timeout and incremental
   auto-vacuum (new databases). Create the schema and run migrations.
4. **Check the framing and clear any stuck RTE** (§A2.4).
5. **Tell systemd it is ready** (`READY=1`), only now, so "started" means
   the port is open and the configuration was accepted.
6. **Interrogate the machine** (§A6), with heartbeats running, because an
   interrogation that didn't drain the queue would cause the losses it is
   meant to detect.
7. **Enable RTE**, retried every cycle if the cabinet isn't answering yet.
8. **Drain whatever the cabinet queued before this run**, and label it
   `startup-backlog`, so it isn't mistaken for this run's events.
9. **Write a `poller_lifecycle` row ('started')** with the PID and the
   full command line.

### A3.2 One cycle

1. Check pool health: the age and depth of `validation_pool` (§A4.1).
2. **General poll**, re-polled immediately on failure (an undrained
   exception can be overwritten). Failed general polls and dead meter
   polls both start the link-outage clock. A recovery after
   `--comms-gap-seconds` triggers a full re-interrogation, and **every**
   recovery forces an identity read. On a built-in serial port a pulled
   cable looks exactly like a quiet machine, and this is what notices a
   cable moved to another cabinet. A poll the cabinet simply doesn't
   implement is excluded, so a missing feature never reads as an outage.
3. Retry the RTE enable if it hasn't taken yet.
4. Decide which optional sweeps are due, and whether to drain (§A5.4).
5. **The meter sweep**, with heartbeats, the mid-sweep drain probe and the
   burst counter (§A5).
6. Cross-check single meters against C-7, if both ran.
7. Deferred heavy work: a re-interrogation after an outage, dispatch of
   the heartbeat's queued codes, and a configuration re-read after `0x3C`.
8. **Epoch bookkeeping** (§A6), *before* the meter row is written, so a
   RAM clear or a machine swap never lands rows in the epoch it ended.
9. Write `meters_current`, and `meters_history` according to the mode.
10. File the events that pre-empted long polls, each with its own arrival
    time.
11. **Headroom**: events collected per second against `1 / pacing`.
    Warn at 70% or above, and warn about loss only on evidence (§A5.7).
12. One log line per cycle.
13. The gap between cycles: a sleep, or general polls while draining.

### A3.3 Stopping

SIGTERM and SIGHUP become a clean teardown (an inherited `SIG_IGN`, as
under `nohup`, is respected). The teardown restores the normal poll rate,
disables RTE, files the last events, writes a `poller_lifecycle` row
('stopped') with the reason, and closes the database.

**Exit codes:** `0` clean stop; `2` a deliberate refusal (bad option, port
held, port can't do parity); `3` an unhandled error. Exit 3 says plainly
that it is a bug in the poller, not a cable fault, and prints the
traceback.

---

## A4. The money paths

### A4.1 Cashout: answered locally, from a pool (`0x57`)

On `0x57` the poller reads the pending amount (LP `0x57`), takes the
**oldest-issued** available number from `validation_pool`, and answers
with LP `0x58`. **The server isn't in this path.** That is the core of the
migration (spec §2.2, §7): a slow server can no longer turn into a
handpay.

Each of these rules exists for a reason:

- **Idempotent, not sequential.** The cabinet repeats `0x57` until it is
  answered. Answering each repeat with a fresh number would mark numbers
  as issued that never reach paper. A repeat within the same cashout, for
  the same amount, gets **the same number** back.
- **Oldest first, by issue order, not by number.** Numeric order matches
  issue order only if every batch was minted in ascending order. The
  order is `issue_seq`, then `issued_at`, with numbers of unknown age
  last.
- **A number becomes `assigned` only after the cabinet accepts it.** A
  rejected number stays available.
- **The gateway never mints a number.** With an empty pool the `0x57`
  goes unanswered and `PoolExhausted` is logged.

**Alerts.** Below the low-water mark the poller prints a repeated alert
with the burn rate and the estimated time left. **The same mark is the
server's refill trigger.** A separate alert fires when the oldest unused
number is too old. Age says nothing about validity; it means either
nothing is dispensing or nothing is topping the gateway up.

### A4.2 Ticket-in

See §A2.12. The server decides; the gateway relays and records.

### A4.3 Handpays (`0x51`)

On every `0x51` the poller reads LP `0x1B` (the handpay queue, SAS §7.8.1)
into `handpay_queue`. **Reading is mandatory.** A host that doesn't read
keeps receiving `0x51` every 15 s, and once more than five entries
accumulate **the oldest handpay records are lost.** A `0x51` can report an
*unreported queue entry* on a machine that isn't locked at all. An
all-zero read is a normal re-read of an entry already acknowledged.

The poller **never resets handpays** (LP `0x94`). For a cancelled-credits
handpay it logs what can be said about the money's make-up. The
restricted portion can be read; nonrestricted promotional credit
**can't** be separated over SAS, so an attendant can't tell from the
lockup whether promotional credit is being paid out as cash. The control
belongs upstream: promotions issued as *restricted* print
"playable only" and never reach this path.

### A4.4 AFT completion (`0x69`)

A transfer that answered "pending" completes with `0x69`, which the
cabinet repeats every 15 s, **as a priority exception**, until the host
interrogates and acknowledges it (SAS §8.8). The poller acknowledges it
and records the result in `aft_transfer_completions`.

---

## A5. Protecting the event stream

### A5.1 Why events are the scarce thing

> Meter reads are for money; events are for detail.

Meters are cumulative and non-volatile, so a deferred meter read costs
resolution, not data: the next sweep reads the same totals. **A lost event
can't be recovered.** No meter carries a player's per-game play. The
priority order the poller follows is: **ticket-out first**, then **every
customer bet recorded correctly**, then, only if there is headroom, the
optional meter sweeps.

### A5.2 Pre-emption: the long poll that comes back as an event

Under RTE a long poll may be answered with an event (SAS §12.3), which the
host must treat as a NACK and re-issue (§12.4). The transport
(`exchange_rte_aware` in `saspy/transport.py`):

- recognises the event (its second byte is `0xFF`, which no long-poll
  command uses) and **keeps it** rather than decoding it as data;
- **gives every re-issue its own full timeout.** One shared deadline would
  let the pacing setting silently decide how many re-issues fit;
- caps each poll at 24 re-issues or 15 s, so one cabinet can't hold the
  port indefinitely;
- lets a poll give up early when the poller signals a burst
  (`give_up_requeue`). Every re-issue carried a real event away, so
  nothing is lost; the meter is read again next sweep.

### A5.3 The heartbeat

See §A2.8. It drains to empty, acts on time-critical codes immediately,
and files every event in true arrival order. Pre-empting events are
drained *before* each heartbeat for that reason.

### A5.4 Drain priority (on by default)

**What.** When the measured event rate reaches **50%** of the
**baseline** collection budget (1 / 200 ms = 5 per second), defer the
optional sweeps (39 single meters, 13 C-7 chunks, 12 validation polls,
the bill poll). Spend those slots on general polls, and turn the gap
between cycles into polling. Leave below **30%**. The 8 grouped polls
**always** run, so every reconciliation window keeps a meter anchor.

**Why.** A long poll that isn't pre-empted drains nothing. During a burst,
the slots spent on optional meter reads are slots not spent collecting
events. Meters can wait, and events can't.

**Why 50/30 rather than one threshold.** A single threshold oscillates:
one busy cycle defers the sweep, the next quiet one resumes it, and the
sweep never completes while play sits near the line.

**Why it is measured against the baseline, not the 40 ms budget.**
Measured against 25/s, the rate would fall below 30% the moment fast poll
engaged, drop back to 200 ms, and re-enter straight away. So drain
priority stays on while someone plays hard, and C-7 is deferred for that
whole time. The cost is meter resolution during the burst, not totals.

**It reacts inside the cycle.** A decision taken once per cycle would
always arrive after a short burst had ended. A probe checked during the
sweep can abandon the rest of it, and it counts events collected through
pre-emption as well as through heartbeats. Otherwise a saturated channel,
where most events arrive by pre-emption, would read as a quiet one.

### A5.5 The burst counter (`--drain-burst-preemptions`, default 12)

The rate probe needs a few seconds of sweep before a rate means anything,
so a burst at the **start** of a sweep falls inside its blind spot. A run
of consecutive pre-emptions of one long poll is a *count*, not a rate, and
needs no settling time. Both mandatory and optional polls feed it.

**Setting it.** It must be above the number of events one ordinary spin
produces (a game start, reel stops, a game end and lamp toggles can arrive
together), or drain priority engages on normal play and Table C-7 is
starved. It must also be below 24, where it would only duplicate the
re-issue cap. **Tune it per model, by testing.** Don't derive it from
timestamps (§A5.8).

### A5.6 The stream audits itself (`stream_gaps`)

SAS §12.5.3 puts the coin-in meter **inside every game-start event, after
the wager**, so two consecutive starts must differ by exactly the later
one's wager:

```
this.total_coin_in − previous.total_coin_in == this.wager
```

If the difference is larger, a game start went missing, and the excess is
what was wagered in it. The poller checks this **live, on every game
start**, needs no meter poll to do it, and writes a `stream_gaps` row with
the unaccounted credits and an estimate in spins. Three exceptions:
- a falling meter is a RAM clear or a bad read, not a loss;
- the first start of a run has nothing to compare with;
- the chain resets across a link outage, because play continues while
  nobody polls.

**Detect by arithmetic, not by counting.** Comparing start counts with end
counts can't see a game whose start *and* end were both lost.

### A5.7 What it warns about, and what it refuses to estimate

**The rate at which events arrive can't be observed.** A host sees what it
collected and what the cabinet says it lost (`0x70`), never what the
cabinet produced. A formula like "queue depth ÷ rate = time to overflow"
uses the rate the queue was *emptied*, which is the wrong number. So the
poller warns on exactly two things:

1. **Evidence of loss:** a `0x70` from the cabinet, or a stream gap.
2. **Saturation:** `EVENT HEADROOM` at or above 70% of the collection
   budget.

**Spec criterion C5:** a `0x70` while the gateway is connected is a
**defect with a findable cause**. It is acceptable only after the link
was lost (a reboot, power, the cable).

### A5.8 What `occurred_at` actually means

`machine_events.occurred_at` is **when the gateway filed the event**, not
when the cabinet raised it, because RTE events carry no timestamp. So:

- **Within one collection path**, row order is the cabinet's queue order.
  **Across paths** (`rte`, `rte-heartbeat`, `rte-interleaved`),
  differences under about a second are collection noise. Priority
  exceptions also skip the queue: a `0x6A` can arrive before the `0x66`
  that caused it.
- **Any sub-second statistic from this column is partly a picture of the
  gateway's own poll rate.** Don't derive cabinet timing from it.
  `COMMANDS.md`, "Is a timing figure real, or is it your own polling?",
  has the check.

---

## A6. Which machine is this? Identity, epochs and re-interrogation

**One cabinet carries three identifiers, and none maps to another:** the
chassis label, the SAS serial (LP `0x54`), and sometimes a vendor terminal
ID. **The SAS serial and asset number are typed in by a technician during
a RAM clear**, so they can be blank, mistyped, or swapped. The asset
number isn't even unique on a floor: a multi-station cabinet reports its
station index in that field.

So identity is never trusted once. The poller keeps **epochs**
(`machine_epochs`): boundaries across which meter deltas aren't
comparable. A new epoch opens on:

- `first_seen`;
- `machine_changed`: the identity differs after a reconnect;
- `ram_clear`: announced by the cabinet (`0x7A`, `0x32`, `0x3A`), or, if
  it happened while the gateway was off, inferred from most cumulative
  meters falling to zero over several cycles;
- `comms_restored`: the same machine, but the events during the gap are
  gone;
- `options_changed`: after `0x3C`, a change in denomination, paytable or
  base percentage. A denomination change rescales every credit figure.

Meter rows carry `epoch_id`. **The CMS acknowledges epochs; the gateway
never does.**

**Re-interrogation** reads identity, enabled features, every game's
configuration and the ticket-out buffer. It runs with heartbeats, and
every poll in it is individually optional. It runs at start-up, after an
outage of `--comms-gap-seconds` or longer (long enough to move a cable to
another cabinet), and after `0x3C`. A `0x3C` after which nothing readable
changed is recorded as "an option SAS doesn't report changed", **never
dismissed as spurious**. A cabinet's money limits are one example: SAS
can't read them at all.

**A per-model fact worth knowing:** on our lab cabinet, coin-in increments
at game *start* and games-played at game *end*. A meter window that closes
mid-spin therefore shows one more wager than games played, and that
doesn't mean the meters disagree. `COMMANDS.md`, "Does this cabinet count
a game at START or at END?", has the check for other models.

---

## A7. The database

WAL mode, a 30 s busy timeout, and one opener function for every process
(spec §6.1). **"Drained"** means the gateway client ships rows with
`synced_at IS NULL` when invited, and sets `synced_at` only after the
server's durable-commit ACK.

| table | written by | what it holds | drained? |
|---|---|---|---|
| `machine_events` | poller | **every** exception, with RTE fields and the collection path | yes |
| `stream_gaps` | poller | live §12.5.3 continuity failures | yes |
| `meters_current` | poller | one row, rewritten each cycle | **never** (a gauge; sent in the heartbeat) |
| `meters_history` | poller | meter rows (ring or append) | yes |
| `latest_c7_meters` (view) | — | the newest row carrying real C-7 values | — |
| `credit_samples` | poller | credit meter on an empty queue | no (lab diagnostics) |
| `ticket_out_history` | poller | the cabinet's ticket-out records (LP `0x4D`), deduplicated | yes, **first** |
| `ticket_in_events` / `ticket_in_completions` | poller | each insertion and how it ended (joined by the `ticket_in_history` view) | yes |
| `validation_queries` | poller ↔ client | each ticket-in question, answer and outcome | no |
| `validation_pool`, `pool_fills`, `counters` | client fills, poller assigns | numbers available / assigned / revoked; batch epoch | no; never pruned |
| `handpay_queue` | poller | each LP `0x1B` read | yes |
| `aft_transfer_completions` | poller | `0x69` acknowledgements | yes |
| `host_requests` | client / tools → poller | AFT pushes and WfA pulls, with outcome | no |
| `lab_auto_actions` | poller | each AFT decline and server push, with status | yes |
| `machine_epochs` | poller | identity boundaries | not as rows: the open epoch is in every heartbeat, and each batch carries the epochs its rows belong to |
| `interrogations` | poller | each identity and configuration read | yes |
| `interrogation_games` | poller | per-game configuration | no |
| `poll_errors` | poller | every failed exchange, by name | no |
| `poller_lifecycle` | poller | each start and stop, with reason and command line | yes |
| `link_replays` | client | each re-send the server asked for after a restore, applied or refused, with the reason | no |

**Three rules in the schema:**

- **Raw tables record what the wire said, and are append-only.**
  Correlations are *views*: a wrong view can be redefined, a wrong value
  written into a column can't be taken back.
- **NULL means "not read".** A skipped read writes NULL, never the
  previous value, so a stale value can't pass as fresh.
- **A table drains if and only if it has `id` and `synced_at`.** The
  client discovers this from the schema, so adding the column is what
  makes a table drain. A crash-loop is visible to the server only because
  `interrogations` and `poller_lifecycle` drain.

---

## A8. The gateway client and the CMS stand-in

### A8.1 The gateway client (`gateway/client.py`)

The gateway's half of spec §10. It uses the standard library only (no
pyserial, no compiled crypto) and runs one thread with one `select()`
loop. It:

- **sends a heartbeat every 15 s**: pool status, EGM status (from the age
  of `meters_current`), `disk_critical`, the credit meter and the open
  machine epoch;
- **drains only when invited** (`PULL_INVITE`), within a row and byte
  limit, **money tables first**, then everything else, oldest first. Each
  batch carries the epochs its rows belong to. Rows are identified as
  `(db_instance_id, table, id)`, so a re-imaged database can't collide
  with rows the server already holds;
- **handles ticket-in**: picks up `pending` queries, sends
  `VALIDATION_QUERY`, writes the answer back conditionally, and reports
  the cabinet's final status (`REDEMPTION_RESULT`);
- **handles the pool**: verifies each signed top-up or `POOL_REPLACE`
  (Ed25519 over a defined byte layout, with `client_id` inside the
  signature), applies the epoch rule, and revokes the old available
  numbers and issues the new ones **in one transaction**;
- **reports what the server has acknowledged**: every heartbeat carries
  `acked`, the highest acknowledged row id per drained table, plus the
  database's `db_instance_id`. A server restored from a backup can see
  from that alone which rows it has lost;
- **re-sends on request (replay).** A `PULL_INVITE` may carry a `replay`
  naming, per table, the last row id the server still holds. The client
  puts every acknowledged row above it back in the drain, by row id
  (never by time, because row order isn't time order across collection
  paths). It refuses a request addressed to a different `db_instance_id`,
  allows at most three replays an hour, records each one in
  `link_replays`, and tells the server with an `ALERT`;
- **turns `AFT_TRANSFER`** into a `host_requests` row for the poller to
  accept or refuse;
- **refuses what isn't built**: `ON_DEMAND_PULL`, `ROTATE_KEY`, `REVOKE`,
  `DISABLE` and `ENABLE` are authenticated, answered with an `ALERT`
  naming them, and otherwise ignored.

Every frame carries an HMAC tag and a monotonic sequence number, and is
authenticated before its body is parsed (spec §10.3). Run it with
`python3 -m gateway.client run --db <the poller's database>`, or with
`deploy/nimasas-gateway-client.service`.

### A8.2 The CMS stand-in (`gateway/service.py`)

The smallest honest version of your future gateway service (spec
§11–12), so the poller and client can be tested end to end before your
side exists. It speaks the §10 wire contract, signs pool batches, keeps a
ticket table with the §12.2 state rules and lease ("Redeemed" only on
positive completion), stores drained rows, and can push AFT. Set-up,
commands and a step-by-step test are in `SERVER-LINK.md`.

**What it is not:** it isn't the CMS. Its database is one SQLite file, and
**it mints its own validation numbers at random**, where your service will
use your existing scheme. Where the spec names a message but not its
fields, the field names are our proposals, open until agreed.

## A9. Running as a service

The poller unit settings, and the reason for each:

- **`Type=notify`, `WatchdogSec=60`.** The poller pings the watchdog from
  **every exchange**, so a long sweep keeps it fed while a wedged poller
  (alive, holding the port, no longer polling) is restarted. That is the
  one failure `Restart=` alone can't see.
- **`Restart=always`, `RestartSec=30`, no start limit.** A gateway that
  gives up at 3 a.m. is worse than one that keeps retrying. 30 s rather
  than 10 s because each start runs a full interrogation, and flash
  endurance is the binding constraint.
- **`KillSignal=SIGTERM`, `TimeoutStopSec=45`**, to leave room for the RTE
  teardown.
- **`PrivateNetwork=yes`, `NoNewPrivileges`, `ProtectSystem=strict`.**
  The poller needs one serial port and one directory, nothing else.

## A10. The server link as built: where it differs from Specification V2.1

The specification describes the server link as designed. Specification
V2.1 already carries the retention windows, the disaster-recovery replay
and the validation-pool rules; this section lists what it does not, and
marks what is specified but not yet built. Where the two disagree, this
section describes the code. Field
names marked as proposals in `SERVER-LINK.md` §4 are open until agreed
with you.

**Messages (spec §10.2).**
- **Sixteen types, not fifteen.** `0x15 AFT_TRANSFER` (server → gateway)
  is added: it asks for credits to be put on the EGM. The gateway refuses
  it unless its poller was started with `--allow-server-aft` (§A2.13).
- **Not built on the gateway:** `ON_DEMAND_PULL`, `ROTATE_KEY`, `REVOKE`,
  `DISABLE` and `ENABLE`. Each is authenticated, answered with an
  `ALERT` naming it, and otherwise ignored.

**Heartbeat (spec Appendix B.3).** Beyond the specification's fields it
carries:
- `machine`: the open machine epoch, the cabinet the gateway is on now;
- `db_instance_id`: the identity of the gateway's database;
- `acked`: per drained table, the highest row id the server has
  acknowledged;
- in `pool_status`: `used_24h`, `pool_size` (the last fill), and
  `low_water_mark` and `low_water_reason` (what `low_water` was judged
  against).

**Event batches.** Each event carries `epoch_id`, and each batch carries
the full epochs its rows belong to, so the server can place every row on
a cabinet from the batch alone.

**What drains (spec §6.2 and Appendix B).**
- `meters_history` **does** drain.
- `meters_current` never does; it is a gauge, sent in the heartbeat.
- `machine_epochs` is not drained as rows; epochs travel in the heartbeat
  and the batches.
- The client decides from the schema: any table with `id` and
  `synced_at` drains (§A7).

**Retention (spec §6.4).**
- Acknowledged rows are pruned after **72 hours** for `machine_events`
  and **30 days** for every other drained table, by `db_maintain.py`
  every 12 hours.
- A row the server hasn't acknowledged is never deleted.
- These windows are the **disaster-recovery budget**: how old a server
  backup can be and still be made whole from the floor by replay. They
  aren't a disk-tidiness setting; shortening them shortens that.
- The validation pool and machine epochs are never pruned.
  `meters_history` isn't pruned either today: the specification gives it
  72 hours, but `db_maintain.py` keeps a guard on it, by the operator's
  choice.

**The validation pool (spec §7.1 and §15).**
- **Size is per gateway**, starting at 50, set on the server for each
  gateway. There is no cap; one batch carries at most 500 numbers.
- **Low-water mark:** the larger of 10 and 20% of the last fill. The same
  number is the operator's alert and the server's refill trigger.
- **Refill size:** the larger of 15 and the gateway's reported `used_24h`.
- **Rotation:** the server replaces each pool at **35 hours**. The
  gateway's own age alert fires at 36 hours, and then means one thing:
  this gateway missed its rotation.
- **Issue order:** oldest issued first, by issue sequence, never by
  number.
- **Uniqueness** is enforced on the 16-digit part of the number as well
  as the full 18 digits, by the server, which is the only place it can be
  floor-wide.
- **Not built yet: per-number revocation.** The specification (§7.4)
  requires the server to revoke unassigned numbers older than 35 hours
  one by one, with a signed revoke list, and to replace half as many.
  Today a rotation is a full `POOL_REPLACE` instead: every available
  number is retired and the pool is re-issued at its full size. The
  revoke list and its signature (§7.3) are still to be built, on both
  sides.

**Recovering from a server restore (not in the specification).**
- **Sequence numbers.** At start, the server moves every gateway's send
  sequence up to the current time in milliseconds. A server restored from
  an old backup would otherwise send sequence numbers the gateway has
  already seen, and every frame would be rejected as a replay.
- **Lost rows.** The server compares each heartbeat's `acked` with the
  rows it actually holds, and puts a `replay` in its next `PULL_INVITE`.
  The gateway re-sends from its retained rows (§A8.1). This works for
  anything inside the retention window.
- **A forgotten pool batch.** If the gateway reports a pool epoch the
  server has no record of issuing, the server issues a replacement pool
  above that epoch and raises `pool_issuance_lost` once.
- **Not recovered:** tickets already printed from a forgotten batch. They
  stay unredeemable until an operator adds them by hand (`add-ticket` on
  the stand-in). A proper repair would have the gateway keep each signed
  batch it accepts, so it can present it again and the server can verify
  its own signature. That isn't built.

---

# Part B — Every other file, and why it exists

## B1. `saspy/`: the SAS 6.02 host library

A clean-room implementation built from the specification. The poller and
every port-opening tool use it.

| file | what it is | why it exists |
|---|---|---|
| `transport.py` | the wire layer: wake-up bit, pacing, exact reads, pre-emption handling | SAS's ninth bit has to be produced by toggling parity per write, and done carelessly the first byte goes out wrong. Pacing lives here so no caller can breach §2.3.3. Pre-emption handling (§A5.2) lives here because every long poll needs it |
| `serial_port.py` | opens the real port: exclusive lock, parity capability check | so a second process can't interleave bytes into the first one's frames, and a port that can't produce the wake-up bit is refused at start instead of looking like a dead machine |
| `client.py` | `SASClient`: one method per implemented long poll, each citing its SAS table | per-instance state (one client per machine), with every parsing decision auditable against the spec |
| `framing.py` | builds and parses frames, no I/O | so framing can be tested without hardware |
| `crc.py` | SAS CRC-16, transcribed from the spec's own routine | the CRC byte order was cross-checked against IGT's own simulator: get it backwards and every frame fails |
| `bcd.py`, `binary.py` | BCD and binary field codecs | SAS sends BCD and ASCII most-significant byte first and binary fields **least**-significant byte first. Easy to miss when reading one table at a time |
| `models.py` | typed results for each long poll | results belong to the caller, never shared module-level state |
| `constants.py` | command codes, exception names, meter codes, decoding tables | one source for every name, so tools print "self test or operator menu entered" rather than `0x84` |
| `exceptions.py` | typed errors (`SASTimeoutError`, `SASPreempted`, …) | so a failure can never be mistaken for data |
| `config.py` | the per-gateway INI file | one gateway, one port, one EGM; the poller loads it at start |

**Deliberately not implemented:** remote control beyond enable/disable
(sound, bill acceptor, maintenance mode). The project doesn't want to
override machine set-up remotely. Component authentication (LP `0x6E`)
and the multi-denomination preamble (LP `0xB0`) are genuine gaps.

## B2. `gateway/`: the server link

| file | what it is | why it exists |
|---|---|---|
| `client.py` | the gateway client (§A8.1) | the second process of spec §4.3 |
| `service.py` | the CMS stand-in (§A8.2) | so the gateway side can be proved before your service exists |
| `frame.py` | the wire frame: length, version, client id, type, sequence, JSON, HMAC tag | so the server can route, authenticate and reject a frame **without parsing its body** |
| `pool.py` | signed batches: canonical byte layout, the epoch acceptance rule, atomic replacement | the three places a pool goes wrong quietly if left to the caller |
| `ed25519.py` | Ed25519 verification in the standard library only | the 32-bit gateways make a compiled crypto dependency an install problem. It is slow, which is acceptable for a top-up a couple of times an hour and nowhere else |
| `db.py` | the database opener for the client | the client must not import the poller (and pyserial), so this is a second copy of the opener, kept identical by a test |
| `machine.py` | which machine a drained row came from | a gateway moved between cabinets must not add one cabinet's coin-in to another's; rows are placed in the epoch that was open when they were recorded |

## B3. `examples/`: the tools

### Bringing a gateway up (run once per cabinet, before trusting a poll loop)

| tool | safety | why it exists |
|---|---|---|
| `address_scan.py` | read-only, port | who is on this cable, at which SAS address. A multi-station cabinet may put every station on one bus or give each its own port, and nothing in SAS says which |
| `connectivity_check.py` | read-only, port (`--test-shutdown` changes state, opt-in) | the first wiring check, and the thing to run after a bad stop: is anything there, and is RTE off? |
| `wire_diagnostics.py` | read-only, port | settles how this machine wants to be polled (general polls against long polls, whether a sync byte helps) before any poll loop is trusted on it. Cabinets differ |
| `polling_capability.py` | read-only, port | what polling rate the machine declares (LP `0xA0`), beside what the spec requires of host and machine. Settles the recurring claim that a host *must* poll at 40 ms |
| `commission_gateway.py` | read-only, port | finds the port and address, confirms the machine's identity, and writes `gateway.ini` |
| `verify_identity.py` | read-only, port | after a RAM clear: did the technician type the serial correctly? A gateway keyed to the wrong cabinet reports someone else's money |

### Watching and diagnosing a machine

| tool | safety | why it exists |
|---|---|---|
| `exception_monitor.py` | read-only, port | exceptions in plain language while someone plays. Also carries `--disable-rte`, the recovery after a bad stop, which the poller's unit runs before start |
| `accounting_summary.py` | read-only, port | the accounting figures grouped the way the machine's own accounting screen groups them, so the two can be compared by eye: the one check that doesn't come from inside our own code |
| `meter_snapshot.py` | read-only, port | one machine's complete readable state written to a JSON file, so two snapshots can be diffed |
| `bill_in_watch.py` | read-only, port | every note taken, with the currency code the machine reports for it (LP `0x48`, read straight after each bill event because it holds only the most recent note). Also tells a rejected bill from a returned ticket |
| `ticket_in_watch.py` | read-only, port | watches a ticket go in and reads what the machine says, authorising nothing |
| `ticket_history.py` | read-only, port | the machine's own record of tickets printed and taken, walked by buffer index so the unread pointer is never moved. It is the WfA lookup when no poller holds the port |
| `aft_status.py` | read-only, port | what AFT would do (registration, lock state, enabled features) before anyone asks it to move money |
| `handpay_queue_drain.py` | reads, but acknowledging removes queue entries; port | reads the handpay queue one entry at a time, as §7.8.1 says to. Several lockups can be waiting, and an unread queue loses the oldest records |

### Moving money (lab machines only)

| tool | safety | why it exists |
|---|---|---|
| `aft_push_credits.py` | **moves money** onto the machine, port | puts credits on by AFT, with the status checks done first |
| `aft_cashout.py` | **moves money** off the machine, port | takes credits off electronically, started by the host. What a CMS does when a session ends |
| `host_cashout.py` | **moves money** off the machine, port | answers the player's cash-out request to the host (`0x6A`) within its deadline; `--deny` declines it |
| `ticket_redeem.py` | **creates credits**, port | acts as the host for one ticket redemption by hand: the path the poller automates under server authority |
| `handpay_clear.py` | read-only by default; **moves money** with `--reset` (LP `0x94`), port | what a pending handpay is, and clearing it without a reset key. A live handpay blocks AFT, tickets and cashouts at once, and each refusal looks like a different fault |

### The CMS interface

| tool | safety | why it exists |
|---|---|---|
| `pool_replace.py` | database | retires the numbers a gateway holds and issues new ones on a live database, under the three rules: revoke and never delete, issued numbers stay redeemable, all or nothing. A lab stand-in for what the server does |
| `wfa_request.py` | database | the server side of a WfA pull: writes a `host_requests` row and waits for the poller's answer. Never opens the port |
| `db_maintain.py` | database | keeps the database from growing beside a running poller: incremental vacuum, WAL checkpoint, and pruning **only** rows the server has acknowledged. A full `VACUUM` would hold the database longer than a cashout's 10 s, so it is a separate, deliberate command run with the poller stopped |

### Proof: checking the gateway against itself and the meters

All read-only and database-only, so they are safe beside a running poller.

| tool | why it exists |
|---|---|
| `rte_reconcile.py` | did the event stream lose anything, and do events and meters agree? Eight independent detectors: the cabinet's own overflow report, game-start continuity, bill-event continuity, repeated events, the wager-side meter window, the payout identity (jackpots and progressives included), the credit ledger, and games won + lost = played. Several independent checks, because one detector saying PASS is only a tool's opinion |
| `unwanted_handpays.py` | the migration's scorecard: `0x66 → 0x57 → 0x51` with no ticket printed is a handpay the **host** caused. Classified by event order, not amount, because cabinet limits are invisible over SAS |
| `cashout_ladder.py` | how long a successful cashout takes and where the time goes: AFT stage, then the print path. Matches each printed ticket to its buffer slot, which is the only way to tell two cashouts from one event delivered twice |
| `queue_safety.py` | could the cabinet's queue have overflowed, and was the gateway the reason? Enforces criterion C5: an overflow is acceptable only when the link was lost |
| `outage_loss.py` | what an outage cost: credits played while nobody polled, against the events the cabinet could still hand back afterwards |
| `whois_playing.py` | what is driving this cabinet's meters: the machine playing itself, a person, or a cable moved to a different machine |

### The bench (lab only)

| tool | safety | why it exists |
|---|---|---|
| `poll_test_tool.py` | **any**: it sends arbitrary SAS commands | finds long polls nobody has needed yet, and exports a working exchange as a test vector. **Read `POLL_TEST_TOOL_WARNING.md` first.** It never belongs on a gateway serving live machines, and recovering a machine it has stuck can end in a RAM clear, which permanently zeroes the meters |

## B4. `deploy/`

| file | why it exists |
|---|---|
| `nimasas-poller.service` | the poller as a floor service: ring mode, throttled sweeps, read-only to the EGM, no lab options. Its comments argue each choice. Floor installs belong in `/opt/nimasas`, with the database in `/var/lib/nimasas` |
| `nimasas-gateway-client.service` | the client as its own unit, with network and no serial port, so a client fault can't stop polling |
| `nimasas-db-maintain.service` + `.timer` | runs `db_maintain.py` twice a day. It prunes only rows the server has acknowledged, older than their table's retention period |
| `commission_machine.py` | commissions one gateway to one EGM: reads the identity, checks it against the roster built from the labels, sets the hostname, installs the HMAC key and the server's public key, writes `client_id`, and records the ceremony. It stops at the first thing that is wrong |
| `sql_samples.py` | every table and view as CSV, so the data model can be read without a gateway. Validation numbers are replaced with a consistent pseudonym |

## B5. Documents and `legacy/`

| file | what it is |
|---|---|
| `README.md` | the repository overview |
| `INSTALL.md` | installing the poller on a gateway |
| `MANUAL.md` | the user manual: each main tool's options, and how to build a SQL layer around a poller |
| `COMMANDS.md` | commands that have worked, and how to re-measure on your own cabinet |
| `SERVER-LINK.md` | the stand-in server, commissioning a gateway against it, a test sequence, and the wire payloads |
| `POLL_TEST_TOOL_WARNING.md`, `POLL_TEST_TOOL_MANUAL.md` | the bench tool's consequences and erase-before-redeploy checklist, and its use |
| `legacy/meter_snapshot-…py` + README | the survey tool exactly as it was when the early floor surveys were taken, frozen so those snapshots can still be read against the tool that wrote them. Don't fix bugs in it |

---

# Part C — Glossary

| term | meaning |
|---|---|
| general poll | a one-byte poll asking "any exception?"; returns one event |
| long poll (LP) | a command with a reply: meters, identity, tickets, AFT |
| exception / event | the cabinet's report that something happened (Table A-1) |
| RTE | real time event reporting: events carry data and can pre-empt long polls |
| pre-emption | a long poll answered with an event instead of its reply (SAS §12.3) |
| heartbeat | general polls run between long polls to keep the queue empty |
| drain priority | deferring optional meter sweeps so poll slots go to events |
| Table C-7 | SAS's catalogue of meter codes, read 12 at a time with LP `0x6F`/`0xAF` |
| validation pool | server-issued ticket numbers, held on the gateway for cashouts |
| WfA | Waiting for Amount: the server knows a ticket number but not its value |
| epoch | a boundary across which meter deltas aren't comparable |
| headroom | events collected per second as a percentage of `1 / pacing` |
| stream gap | a missing game start, proven by §12.5.3's coin-in arithmetic |
| `0x57` / LP `0x58` | the cabinet asks for a validation number / the host gives one |
| `0x67` / LP `0x70` / LP `0x71` | ticket inserted / read it / tell the cabinet the verdict |
| `0x6A` | AFT request for host cashout (stage 1 of the cashout ladder) |
| `0x70` | exception buffer overflow: the cabinet admits it lost events |
