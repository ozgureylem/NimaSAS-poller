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

## 6. Lab mode

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

## 7. Workflow: turning a found code into a real feature

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

## 8. Troubleshooting

| Symptom | Cause |
|---|---|
| `Could not exclusively lock port` | Something else holds the port — usually `sql_poll_logger.py` or another copy of this tool. Stop it first. |
| `The driver rejected the port settings` | The adapter cannot do mark/space parity, which SAS needs for the wakeup bit. Some USB adapters cannot; a virtual tty never can. Try a different adapter, or `--simulate`. |
| Everything times out | Wrong address is by far the most common cause. Then baud, then wiring. `connectivity_check.py` isolates this faster. |
| One poll times out, others answer | That machine probably does not implement it (§4.4). A real finding, not a fault. |
| UI unreachable from the laptop | Started without `--bind 0.0.0.0`, or a firewall on the gateway. |
| Machine stopped responding after a command | See "If something has already gone wrong" in the warning document. |

## 9. When the gateway leaves the lab

**Erase the tool and verify it is gone**, including compiled bytecode in
`__pycache__`. Full checklist in
[POLL_TEST_TOOL_WARNING.md](POLL_TEST_TOOL_WARNING.md#before-the-gateway-leaves-the-lab).

Better still, deploy floor gateways from a branch or artifact that never
contained this file, so it cannot return on the next `git pull`.
