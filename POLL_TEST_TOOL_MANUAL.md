# Poll Test Tool — manual

`examples/poll_test_tool.py` is a bench instrument for firing SAS long
polls at a gaming machine and reading what comes back. Two jobs:

1. **Exercise the polls this client already implements**, without writing
   a script for each one — confirm a real EGM answers them the way the
   test suite says it should.
2. **Try codes of unknown provenance** — from a forum post, a vendor doc,
   an old integration — and find out what a real machine actually does.
   When one works, the exchange becomes the test vector for adding it to
   `saspy` properly.

---

## Before you start: the risk, in short

**Read [POLL_TEST_TOOL_WARNING.md](POLL_TEST_TOOL_WARNING.md) first.** The
condensed version:

- This sends whatever you tell it to. SAS long polls are not all reads —
  some **move funds**, **pay out a ticket**, or **take the machine out of
  service**. An unverified code may be any of those.
- Getting a machine stuck often ends in a **RAM clear**, which **zeroes
  the machine's meters permanently**. Use machines whose meter history
  you are willing to lose.
- Use a machine with **no credits and no money in it**. A RAM clear
  undoes a lockup; it does not undo a transfer that actually moved funds.
- **This tool must never be installed on a gateway serving live
  machines**, and must be erased and verified gone before a lab gateway
  is redeployed to a floor. Procedure is in the warning document.

---

## 1. Where to run it

It runs on Linux, Windows and macOS. Two shapes, pick whichever suits the
bench:

**A — on a laptop**, with the USB-serial adapter plugged into the laptop.
Simplest for one person at a bench.

**B — on the lab gateway** (micro PC), driven from a laptop's browser.
Better when the adapter is already wired to the gateway, or when two
people want to watch the same screen.

```bash
# A: laptop
python3 examples/poll_test_tool.py COM3 --address 1                    # Windows
python3 examples/poll_test_tool.py /dev/cu.usbserial-10 --address 1    # macOS
python3 examples/poll_test_tool.py /dev/ttyUSB0 --address 1            # Linux
# then open http://localhost:8080

# B: on the gateway, browsed from a laptop
python3 examples/poll_test_tool.py /dev/ttyUSB0 --address 1 --bind 0.0.0.0
# then open http://<gateway-ip>:8080 from the laptop
```

`--bind 0.0.0.0` has **no authentication**. Trusted, isolated lab network
only.

**Only one process may hold a serial port.** Stop `sql_poll_logger.py`
before starting this — if you forget, you get a clear
`Could not exclusively lock port` rather than silent corruption.

## 2. Finding the port

```bash
python3 examples/poll_test_tool.py --list-ports
```

Works the same on all three OSes, and the UI shows the same list.

- **Linux** — usually `/dev/ttyUSB0`. If it is missing, check `dmesg |
  tail` and that your user is in the `dialout` group.
- **Windows** — `COM3`, `COM4`... Device Manager → Ports (COM & LPT).
- **macOS** — use the **`/dev/cu.*`** name, not `/dev/tty.*`. The `tty`
  variant blocks waiting for carrier detect and will appear to hang. The
  tool flags this for you in the port list.

## 3. Trying it with no hardware

```bash
python3 examples/poll_test_tool.py --simulate
```

Opens no port and fabricates replies. Use it to learn the UI, demo the
tool, or check the layout before you are standing in front of a machine.
Nothing it does can reach an EGM.

## 4. The interface

**Left panel — implemented long polls.** Every long poll `saspy` knows
about, grouped (Meters, Machine identity & config, Ticketing &
validation, AFT, Enable/disable), each with a plain-language tag
("Total value of bills in"), its enum name, its SAS type, and whether it
needs data bytes. This list is generated from `saspy.constants` at
startup, so it cannot drift from what the client actually implements.

Each row carries a badge:

| Badge | Meaning |
|---|---|
| `READ` | Known-safe read. Sends on one click. |
| `STATE` | Known to change machine state. Confirmation required (unless lab mode). |

A poll marked **needs data bytes** will prompt for them — a game number,
a meter code, a validation type. Enter hex; leave blank to send the bare
command. Note that SAS *type S* means "carries a CRC", **not** "takes
data": Shutdown is type S and takes no data at all.

**Right panel — custom command.** Enter the command byte onward, in hex.
The address is prepended and the CRC computed for you, and the
**"Exactly these bytes go on the wire"** box shows the literal frame
before you send anything. Read that box. It is the last checkpoint.

Untick **append CRC** only if your pasted string already includes one.

**Bottom — exchange log.** Every send, newest first: TX bytes, RX bytes,
decoded address/command/payload, whether the **CRC validates**, and — for
commands `saspy` implements — the response decoded into named fields. See
§5.

## 5. Reading a response

**You always see every byte the machine sent.** The exchange log prints
the full RX frame, raw, before it interprets anything. Nothing is hidden
or summarised away.

On top of that, the tool says as much about the bytes as it can honestly
establish, in two layers.

**Layer 1 — structure.** This applies to every response, known command or
not:

- **`CRC valid`** — the machine sent a well-formed, framed response. This
  is the signal that a command is real and worth keeping.
- **`CRC INVALID`** — either the machine sent something that is not a
  standard framed response, bytes were lost, or the response has a
  different shape than address + data + CRC. Not automatically a failure:
  some responses genuinely are not framed that way (an ACK to `0x01` is a
  bare address byte with no CRC at all).
- **single byte** — when the whole response *is* one byte, that byte is
  the answer, and the tool tells you which one it is: your address (ACK),
  your address OR `0x80` (NACK), or something else. This is the shape
  Table 7.4b defines for Shutdown/Startup — it is not a summary of a
  longer reply.
- **no response (timeout)** — per §4.4 of the spec, a machine that does
  not support a long poll must **ignore it silently**, not NACK it. So
  silence usually means "this machine doesn't implement that", not "the
  wiring is broken" — as long as other polls are answering.

**Layer 2 — named fields.** For the commands `saspy` already implements
and that take no data bytes, the bench sends the poll through the
client's own method and prints the decoded fields under the raw bytes.
Asking a machine for the last ticket it printed (`0x3D`) looks like this:

```
TX      01 3D
RX      01 3D 00 00 12 34 00 00 00 25 00 61 D6
command 0x3D | CRC 61 D6 valid
payload 00 00 12 34 00 00 00 25 00
parsed  via saspy send_cash_out_ticket_information()
          ticket_number   1234
          amount_cents    2500
```

The raw frame is still right there; the parse is additional. That matters
because the payload above is BCD — without the field names you would be
decoding `00 00 12 34` by eye to get ticket 1234.

Note what this is doing: the parse comes from `saspy`'s real parser, not
a second decoder written for this tool. So a field that comes out wrong
here is a genuine finding about the client against that machine, not a
bench artefact — which is the whole reason it is wired this way.

**Where layer 2 does not apply**, you get layer 1 only — raw bytes, CRC
verdict, payload:

- custom commands (the tool has no idea what they mean — that is the
  point of trying them);
- polls that need data bytes (game number, meter code, validation type);
- anything `saspy` does not implement.

That is deliberate. The decoder refuses to invent a field layout for a
command it does not know, because a confidently-wrong decode of an
unknown code is worse than no decode at all.

## 6. Reading one meter by hand

Every meter in Table C-7 is reachable through one long poll, `6F`, which
takes up to twelve meter codes and returns each with its own size byte. So
a single table covers all of them, and nothing here needs a dedicated
command.

The frames below are for **address 1**. The CRC is the last two bytes and
depends on everything before it, so changing the address means recomputing
it — paste the frame with the address byte changed and the hex box will
compute the new CRC for you before you send.

### Meters this machine has been proved to report

Each label below was confirmed by reading a non-zero value off the
machine's own Machine Accounting screen and matching it, to the unit,
against what the gateway reported. Labels are the machine's own wording,
not the spec's.

| | **Total Bet / Total Won** | |
| `0000` | Total Bet (Coin In) | `01 6F 04 00 00 00 00 FD 08` |
| `0001` | Winnings from Paytable (Coin Out) | `01 6F 04 00 00 01 00 25 11` |
| `0022` | Total Won | `01 6F 04 00 00 22 00 7E 18` |
| `001F` | Attendant Paid Paytable Winnings | `01 6F 04 00 00 1F 00 A4 1E` |
| `0019` | Total Non-cashable Played | `01 6F 04 00 00 19 00 74 4A` |
| `001A` | Total Promotional Played | `01 6F 04 00 00 1A 00 1C 60` |
| | **Total In** | |
| `0024` | Total In | `01 6F 04 00 00 24 00 AE 4C` |
| `000B` | Bill In | `01 6F 04 00 00 0B 00 55 EC` |
| `0017` | Cashless In | `01 6F 04 00 00 17 00 64 D0` |
| `002E` | Cashable Electronic In | `01 6F 04 00 00 2E 00 DE B1` |
| `0030` | Promotional Electronic In | `01 6F 04 00 00 30 00 5F BE` |
| `002F` | Non-cashable Electronic In | `01 6F 04 00 00 2F 00 06 A8` |
| | **Total Out** | |
| `0004` | Total Cancelled Credits | `01 6F 04 00 00 04 00 9D 6F` |
| `0003` | Cancelled Credits Paid by Attendant | `01 6F 04 00 00 03 00 95 22` |
| `0018` | Cashless Out | `01 6F 04 00 00 18 00 AC 53` |
| `0032` | Cashable Electronic Out | `01 6F 04 00 00 32 00 EF 8D` |
| `0034` | Promotional Electronic Out | `01 6F 04 00 00 34 00 3F D9` |
| `0033` | Non-cashable Electronic Out | `01 6F 04 00 00 33 00 37 94` |
| | **Voucher In** | |
| `0015` | Voucher In | `01 6F 04 00 00 15 00 D4 E3` |
| `002B` | Promotional Voucher In | `01 6F 04 00 00 2B 00 66 CF` |
| `002A` | Non-cashable Voucher In | `01 6F 04 00 00 2A 00 BE D6` |
| | **Voucher Out** | |
| `0016` | Voucher Out | `01 6F 04 00 00 16 00 BC C9` |
| `002C` | Cashable Voucher Out | `01 6F 04 00 00 2C 00 6E 82` |
| | **Games played / won / lost** | |
| `0005` | Total Games Played | `01 6F 04 00 00 05 00 45 76` |
| `0006` | Total Games Won | `01 6F 04 00 00 06 00 2D 5C` |
| `0007` | Total Games Lost | `01 6F 04 00 00 07 00 F5 45` |
| | **On the machine now** | |
| `000C` | credit display -- total credits | `01 6F 04 00 00 0C 00 5D A1` |
| `001B` | credit display -- non-cashable portion | `01 6F 04 00 00 1B 00 C4 79` |

### Reading several at once

`6F` takes up to twelve codes in one exchange, which is both faster and
atomic — all twelve come from the same instant rather than from twelve
separate moments. The body is the game number (`00 00` for the machine as
a whole) followed by each code as two bytes, low byte first:

```
01 6F 06 00 00 0C 00 1B 00 8E 91      <- 000C and 001B together
   |  |  |  |  |     |     |
   |  |  |  |  |     |     CRC
   |  |  |  |  |     second code, 001B, low byte first
   |  |  |  |  first code, 000C, low byte first
   |  |  |  game number 0000 = the gaming machine
   |  |  body length, 2 + 2 per code
   |  send extended meters
   address
```

A code the machine does not implement comes back with **size 0** rather
than an error (§7.21b), so an absent meter is reported as absent and the
rest of the poll still answers. That is how the 37 codes this cabinet does
not implement were identified.

### What is NOT in the table

Labels are only claimed where there is a photograph behind them. Three
kinds of near-evidence were refused, and the gaps are deliberate:

- **A zero matching a zero.** `0020` and `0021` have read 0.00 on every
  capture against screen lines also reading 0.00. Any zero matches any
  zero.
- **Arithmetic agreement with no screen line.** `0023` satisfies the
  spec's own definition (`0003 + 0002`) exactly, every time, but no screen
  line carrying it was ever seen.
- **Agreement with another SAS response.** `00A0` matched `002E` exactly,
  which is what established that one credit is one cent here — but both
  come from SAS, so neither names the other.

`0002` is the sharpest case: it has equalled `001F` in eight consecutive
readings and moved with it, so they are certainly the same money — but no
screen line naming a jackpot was ever photographed, and knowing what a
meter *equals* is not knowing what the machine *calls* it.

`saspy/constants.py` names all 162 codes from the spec. That is what each
code *should* be. This table is the narrower claim of what this machine
*demonstrated*, and it grows as tests confirm more.

### Which meters this machine cannot currently tell apart

Before spending a session photographing screens, ask the machine which
matches are even available to be made:

```bash
python3 examples/meter_fingerprint.py /dev/ttyUSB0 --address 1
```

It reports every group of meters holding the same value (mutually
unmatchable), the meters at zero (unmatchable from any number of readings),
and a set of amounts to push through next that collides with nothing already
on the machine and with no sum of each other. MANUAL.md §7.1 explains why
that last constraint is not optional — every total meter is a sum of
components, so 111.11 and 222.22 down two paths makes the total read exactly
333.33 and collide with the third.

## 7. Lab mode

```bash
python3 examples/poll_test_tool.py /dev/ttyUSB0 --lab-mode
```

Drops the typed confirmation for state-changing and custom commands —
everything sends on one click. Intended for a bench where a locked
machine is a non-event and a RAM-clear is on hand.

It is a startup flag and never the default, so the tool stays safe if it
is ever run somewhere it should not be. When it is on you cannot miss it:
a red bar across the UI, a badge in the header, and a line in the console
banner.

Lab mode removes a speed bump, not a consequence. Everything in the
warning document still applies.

## 8. Workflow: turning a found code into a real feature

This is the point of the tool.

1. **Look it up first.** Check Appendix B of the SAS spec for the command
   code. If the spec describes it, you already know more than the forum
   post does — including whether it is a read or a setter.
2. **Classify it honestly.** If you cannot establish that it is a read,
   treat it as state-changing and put it on a machine you can afford to
   reset.
3. **Send it and record the exchange.** The log gives you TX, RX and CRC
   validity, timestamped.
4. **Repeat it.** A response that appears once and not again is not a
   feature. Try it several times, and on more than one machine model if
   you have them.
5. **Hand it over with the bytes.** The exact TX/RX pair is what lets
   someone add the poll to `saspy` with a real test behind it —
   `tests/test_client.py` builds its fixtures field by field from the
   spec tables, and a captured exchange is exactly what is needed to
   write one.

What makes a finding useful: the machine make/model, its SAS version
(long poll `0x54`), the exact bytes both ways, and whether it reproduced.

## 9. Troubleshooting

| Symptom | Cause |
|---|---|
| `Could not exclusively lock port` | Something else holds the port — usually `sql_poll_logger.py` or another copy of this tool. Stop it first. |
| `cannot set mark/space parity` at open | The adapter's driver refuses the mark/space parity SAS needs for the wakeup bit. Some USB bridges cannot do it; a virtual tty never can. Not wiring, baud or address. Try a different adapter, or `--simulate`. |
| Everything times out, but the port opened cleanly | A clean open only means the driver did not *refuse* mark/space parity. Some drivers accept it and silently ignore it, and nothing on this side of the wire can tell that apart from a working port. So the adapter is not cleared — if address and wiring check out, suspect it next, and confirm with a known-good adapter. |
| Everything times out | Wrong address is by far the most common cause. Then baud, then wiring. `connectivity_check.py` isolates this faster. |
| One poll times out, others answer | That machine probably does not implement it (§4.4). A real finding, not a fault. |
| UI unreachable from the laptop | Started without `--bind 0.0.0.0`, or a firewall on the gateway. |
| Machine stopped responding after a command | See "If something has already gone wrong" in the warning document. |

## 10. When the gateway leaves the lab

**Erase the tool and verify it is gone**, including compiled bytecode in
`__pycache__`. Full checklist in
[POLL_TEST_TOOL_WARNING.md](POLL_TEST_TOOL_WARNING.md#before-the-gateway-leaves-the-lab).

Better still, deploy floor gateways from a branch or artifact that never
contained this file, so it cannot return on the next `git pull`.
