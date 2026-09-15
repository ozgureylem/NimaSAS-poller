#!/usr/bin/env python3
"""Interactive SAS poll bench: fire long polls at an EGM and watch what
comes back.

Two panels. The left one lists every long poll this client already
implements, tagged in plain language ("Total coin in meter"), built from
saspy.constants at import time so it can never drift from the code. The
right one is a free-form hex box: paste a command you found in a forum
post or a vendor doc, see exactly what will go on the wire (CRC computed
for you), send it, and read the response decoded as far as it can be.

The point is the second half. When an unknown code turns out to work, the
exchange can be exported as a ready-made test vector and folded into
saspy properly, with a real test behind it.

Runs on Linux, Windows and macOS -- pyserial handles the port, and the
UI is just a browser. Two deployment shapes, both supported:

  On a laptop, adapter plugged into the laptop:
    python3 examples/poll_test_tool.py COM3 --address 1              (Windows)
    python3 examples/poll_test_tool.py /dev/cu.usbserial-10 --address 1   (macOS)
    python3 examples/poll_test_tool.py /dev/ttyUSB0 --address 1      (Linux)

  On the gateway / micro PC, driven from a laptop's browser:
    python3 examples/poll_test_tool.py /dev/ttyUSB0 --bind 0.0.0.0
    then open http://<gateway-ip>:8080 from the laptop

Not sure which port? ``--list-ports`` enumerates them on any OS, and the
UI shows the same list. On macOS prefer the ``/dev/cu.*`` name over
``/dev/tty.*``; the tty variant blocks waiting for carrier detect.

SAFETY
======
This sends whatever you tell it to send. SAS long polls are not all
reads: some move funds (AFT 0x72), pay out a ticket (0x71), lock or
disable the machine (0x01/0x74), or rewrite ticket/validation
configuration (0x4C/0x7B/0x7C/0x7D). A code copied from the internet
may be any of those, or may not be what the post claimed at all.

Every command is classified, and by default the classification is
enforced in the backend rather than merely displayed:

  read          Known-safe read. Sends on one click.
  state-change  Known to alter machine state. Requires typed confirmation.
  custom        Anything hand-entered. Requires typed confirmation,
                because we cannot know what it does.

``--lab-mode`` drops the confirmation step for a bench where locking a
machine is a non-event and a RAM-clear is on hand. It is a deliberate
startup flag and never the default, so the tool stays safe if it is
ever run somewhere it shouldn't be. Even in lab mode, prefer a machine
with no credits on it: a RAM-clear undoes a lockup, but an AFT transfer
that actually moved money is a different kind of problem.

Only one process may hold the serial port (see MANUAL.md 4.5), so stop
sql_poll_logger.py before starting this.
"""

from __future__ import annotations

import argparse
import datetime
import http.server
import json
import queue
import socketserver
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saspy.client import SASClient
from saspy.constants import LONG_POLL_TYPES, SIMPLE_METER_WIDTH_BCD, LongPoll, PollType
from saspy.crc import crc16_bytes
from saspy.exceptions import SASError
from saspy.framing import build_command
from saspy.serial_port import open_serial_port
from saspy.transport import SASTransport

# --- safety classification ---------------------------------------------
#
# Explicit, not derived from PollType: PollType says whether a command
# carries a CRC, which has nothing to do with whether it changes machine
# state. LP 0x4D is Type S and a pure read; LP 0x01 is Type S and takes a
# machine out of service. Anything not named here is treated as
# state-changing, so a newly added long poll is conservative by default.
STATE_CHANGING: dict[int, str] = {
    0x01: "Takes the machine OUT OF SERVICE (lock out play)",
    0x02: "Returns the machine to service",
    0x03: "Disables all sounds",
    0x04: "Enables all sounds",
    0x05: "Disables reel spin sounds",
    0x06: "Enables the bill acceptor",
    0x07: "DISABLES the bill acceptor",
    0x08: "Rewrites which bill denominations are accepted",
    0x09: "Enables/disables a game",
    0x0A: "Enters maintenance mode",
    0x0B: "Exits maintenance mode",
    0x0E: "Enables/disables real-time event reporting",
    0x21: "Starts a ROM signature calculation",
    0x2E: "DELAYS the gaming machine",
    0x4C: "Rewrites the secure enhanced validation ID",
    0x58: "Hands the machine a validation number -- COMMITS A CASHOUT",
    0x71: "REDEEMS A TICKET -- pays credits onto the machine",
    0x72: "AFT FUNDS TRANSFER -- moves real money",
    0x73: "Writes AFT registration",
    0x74: "Can LOCK the machine",
    0x75: "Rewrites AFT receipt data",
    0x76: "Rewrites custom AFT ticket data",
    0x7B: "Rewrites validation config and ticket expirations",
    0x7C: "Rewrites extended ticket data",
    0x7D: "Rewrites ticket data (host ID, expiry, address lines)",
    0x7F: "Sets the machine's date and time",
    0x80: "Broadcasts a progressive amount",
    0x8A: "Pays a legacy bonus",
    0x8B: "Initiates multiplied jackpot mode",
    0x8C: "Enters/exits tournament mode",
    0x94: "Resets a pending handpay",
    0xA8: "Changes the jackpot reset method",
    0xAA: "Enables/disables auto rebet",
}

# Plain-language tags for the polls a bench operator actually hunts for.
# Everything else falls back to a title-cased enum name, which is already
# readable ("SEND_TOTAL_COIN_IN_METER" -> "Send total coin in meter").
FRIENDLY_TAGS: dict[int, str] = {
    0x0F: "Core meters: cancelled credits, coin in/out, drop, jackpot, games played",
    0x19: "Meters 11-15: coin in/out, drop, jackpot, games played",
    0x1C: "Meters (8 fields): adds games won, slot door, power reset",
    0x1E: "Bill meters by denomination ($1/$5/$10/$20/$50/$100)",
    0x1F: "Gaming machine ID, denomination, paytable, base %",
    0x20: "Total value of bills in (dollar value of bills meter)",
    0x2F: "Selected meters for game N (ticket meters live here)",
    0x48: "Last accepted bill (country, denomination, count)",
    0x4D: "Enhanced validation info -- ticket-out history buffer",
    0x4F: "Current hopper status (level, % full)",
    0x50: "Validation meters for one validation type",
    0x54: "SAS version and machine serial number",
    0x57: "Pending cashout info (amount awaiting a validation number)",
    0x6F: "Extended meters -- up to 12 Table C-7 codes per poll",
    0xA0: "Enabled features (what this machine actually supports)",
    0xAF: "Extended meters (alternate code, for consecutive polls)",
    0xB3: "Token denomination",
}

GROUPS = [
    ("Meters", lambda c: c in SIMPLE_METER_WIDTH_BCD or c in
     (0x0F, 0x18, 0x19, 0x1C, 0x1E, 0x2D, 0x2F, 0x48, 0x4F, 0x50, 0x52, 0x6F, 0xAF)),
    ("Machine identity & config", lambda c: c in
     (0x1F, 0x21, 0x51, 0x53, 0x54, 0x55, 0x56, 0x7E, 0x8F, 0xA0, 0xB3, 0xB4, 0xB5)),
    ("Ticketing & validation", lambda c: c in (0x3D, 0x1B, 0x4C, 0x4D, 0x57, 0x58, 0x70, 0x71, 0x7B)),
    ("AFT", lambda c: c in (0x72, 0x73, 0x74)),
    ("Enable / disable", lambda c: c in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x0A, 0x0B)),
]


# Type S means "this command carries a CRC", NOT "this command takes data"
# -- the same confusion that makes PollType useless for safety. Table 7.4a's
# enable/disable family is Type S and is address + command + CRC, nothing
# else. Getting this wrong tells an operator to type data bytes into a
# shutdown command.
TYPE_S_WITHOUT_DATA = frozenset({0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0A, 0x0B})


def takes_data(poll: LongPoll) -> bool:
    if LONG_POLL_TYPES.get(poll, PollType.R) == PollType.R:
        return False
    return poll.value not in TYPE_S_WITHOUT_DATA


def classify(code: int) -> str:
    return "state-change" if code in STATE_CHANGING else "read"


def list_serial_ports() -> list[dict]:
    """Enumerate serial ports on whatever OS this is. Works the same on
    Linux (/dev/ttyUSB0), Windows (COM3) and macOS (/dev/cu.usbserial-*).
    """
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    out = []
    for p in sorted(list_ports.comports(), key=lambda p: p.device):
        hint = ""
        if sys.platform == "darwin" and "/tty." in p.device:
            hint = "on macOS prefer the matching /dev/cu.* name -- /dev/tty.* blocks on carrier detect"
        out.append({
            "device": p.device,
            "description": (p.description or "").strip() or "n/a",
            "hwid": (p.hwid or "").strip() or "n/a",
            "hint": hint,
        })
    return out


def tag_for(poll: LongPoll) -> str:
    if poll.value in FRIENDLY_TAGS:
        return FRIENDLY_TAGS[poll.value]
    name = poll.name.replace("_", " ").lower()
    return name[:1].upper() + name[1:]


def build_catalog() -> list[dict]:
    """The left-hand panel, derived from saspy.constants so it cannot
    drift from what the client actually knows about.
    """
    entries = []
    for poll in sorted(LongPoll, key=lambda p: p.value):
        code = poll.value
        group = next((g for g, pred in GROUPS if pred(code)), "Other")
        entries.append({
            "code": f"{code:02X}",
            "name": poll.name,
            "tag": tag_for(poll),
            "group": group,
            "type": LONG_POLL_TYPES.get(poll, PollType.R).name,
            "safety": classify(code),
            "warning": STATE_CHANGING.get(code, ""),
            "takes_data": takes_data(poll),
        })
    return entries


# --- serial worker ------------------------------------------------------

class Bench:
    """Owns the port. One exchange at a time, so the UI can never
    interleave two commands on the wire.
    """

    def __init__(self, port: str, address: int, baud: int, timeout: float,
                 simulate: bool = False, lab_mode: bool = False):
        self.address = address
        self.timeout = timeout
        self.simulate = simulate
        self.lab_mode = lab_mode
        self._lock = threading.Lock()
        self.log: list[dict] = []
        if simulate:
            self.serial = None
            self.transport = None
            self.client = None
        else:
            self.serial = open_serial_port(port, baudrate=baud)
            self.transport = SASTransport(self.serial, byte_timeout=timeout)
            self.client = SASClient(self.transport, address, timeout=timeout)

    def _record(self, entry: dict) -> dict:
        entry["at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        self.log.append(entry)
        del self.log[:-500]
        return entry

    def raw_exchange(self, frame: bytes) -> bytes:
        """Write a frame and read whatever comes back, using an inter-byte
        gap to decide the response is over. Arbitrary commands have no
        known response length, so a gap is the only general answer.
        """
        if self.simulate:
            time.sleep(0.05)
            body = bytes([self.address, frame[1] if len(frame) > 1 else 0x00, 0x02, 0x12, 0x34])
            return body + crc16_bytes(body)
        self.transport.write_with_wakeup(frame)
        deadline = time.monotonic() + self.timeout
        buf = bytearray()
        last = None
        while time.monotonic() < deadline:
            chunk = self.serial.read(64)
            if chunk:
                buf.extend(chunk)
                last = time.monotonic()
            elif buf and last and (time.monotonic() - last) > 0.040:
                break
            else:
                time.sleep(0.002)
        return bytes(buf)

    def send(self, command_hex: str, *, label: str, auto_crc: bool, safety: str) -> dict:
        """command_hex is the command byte onward (address is prepended,
        CRC appended when auto_crc). Returns a log entry.
        """
        try:
            payload = bytes.fromhex(command_hex.replace(" ", "").replace("0x", ""))
        except ValueError as e:
            return self._record({"ok": False, "label": label, "error": f"not valid hex: {e}"})
        if not payload:
            return self._record({"ok": False, "label": label, "error": "no command bytes given"})

        frame = bytes([self.address]) + payload
        if auto_crc:
            frame += crc16_bytes(frame)

        with self._lock:
            started = time.monotonic()
            try:
                reply = self.raw_exchange(frame)
                err = None
            except SASError as e:
                reply, err = b"", f"{type(e).__name__}: {e}"
            except Exception as e:  # noqa: BLE001 - a bench tool should show anything
                reply, err = b"", f"{type(e).__name__}: {e}"
            elapsed = time.monotonic() - started

        return self._record({
            "ok": err is None and bool(reply),
            "label": label,
            "safety": safety,
            "tx": frame.hex(" ").upper(),
            "rx": reply.hex(" ").upper() if reply else "",
            "elapsed_ms": round(elapsed * 1000, 1),
            "error": err or ("no response (timeout)" if not reply else None),
            "decode": decode_response(reply, self.address) if reply else None,
        })


def decode_response(raw: bytes, expected_address: int) -> dict:
    """Best-effort structural decode. Deliberately does not pretend to
    know field layouts for unknown commands -- it reports what is
    structurally true and leaves interpretation to the operator.
    """
    out: dict = {"length": len(raw)}
    if len(raw) == 1:
        out["shape"] = (
            f"single byte 0x{raw[0]:02X} -- "
            + ("ACK (our address)" if raw[0] == expected_address
               else f"NACK (address | 0x80)" if raw[0] == (expected_address | 0x80)
               else "general-poll exception code, or another address")
        )
        return out
    out["address"] = f"0x{raw[0]:02X}"
    out["address_ok"] = raw[0] == expected_address
    if len(raw) >= 2:
        out["command"] = f"0x{raw[1]:02X}"
    if len(raw) >= 4:
        body, claimed = raw[:-2], raw[-2:]
        computed = crc16_bytes(body)
        out["crc_on_wire"] = claimed.hex(" ").upper()
        out["crc_computed"] = computed.hex(" ").upper()
        out["crc_ok"] = claimed == computed
        out["payload"] = body[2:].hex(" ").upper()
        if out["crc_ok"] and len(body) > 2:
            out["hint"] = (
                "CRC valid -- this looks like a real, well-formed response. "
                "Export it as a test vector if the command is one we want to keep."
            )
        elif not out["crc_ok"]:
            out["hint"] = (
                "CRC does NOT match. Either the machine sent something that "
                "isn't a standard framed response, bytes were lost, or this "
                "response has a different shape than address+data+CRC."
            )
    return out


# --- HTTP layer ---------------------------------------------------------

def make_handler(bench: Bench, catalog: list[dict], html: str):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter console
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, html.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/catalog":
                self._json({"catalog": catalog, "address": bench.address,
                            "simulate": bench.simulate, "lab_mode": bench.lab_mode,
                            "ports": list_serial_ports(), "platform": sys.platform})
            elif self.path == "/api/ports":
                self._json({"ports": list_serial_ports(), "platform": sys.platform})
            elif self.path == "/api/log":
                self._json({"log": bench.log[-100:]})
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/api/send":
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json({"ok": False, "error": "bad JSON"}, 400)
                return

            command_hex = str(req.get("command", "")).strip()
            label = str(req.get("label", "")) or f"custom {command_hex[:8]}"
            safety = str(req.get("safety", "custom"))
            confirmed = bool(req.get("confirmed"))

            # Server-side enforcement. The UI also gates these, but a tool
            # that can pay out a ticket must not rely on its own frontend.
            if safety in ("state-change", "custom") and not confirmed and not bench.lab_mode:
                self._json({"ok": False, "error":
                            "refused: this command is not a known-safe read and "
                            "was not confirmed"}, 403)
                return

            self._json(bench.send(command_hex, label=label,
                                  auto_crc=bool(req.get("auto_crc", True)),
                                  safety=safety))

    return Handler


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SAS Poll Bench</title>
<style>
  :root {
    --ground:#f4f6f7; --surface:#fff; --surface2:#eef2f3; --ink:#14191c; --ink2:#5a686e;
    --rule:#d5dee1; --accent:#0e6d78; --ok:#1a6b45; --warn:#8a5200; --risk:#a32a20;
    --ok-bg:#e4f1ea; --warn-bg:#faeedd; --risk-bg:#fae8e6;
  }
  @media (prefers-color-scheme: dark) { :root {
    --ground:#0e1417; --surface:#151e22; --surface2:#1b262b; --ink:#e6eef0; --ink2:#9bacb3;
    --rule:#27363c; --accent:#48c4d2; --ok:#59c08b; --warn:#e0a44a; --risk:#f1756a;
    --ok-bg:#112a20; --warn-bg:#2b2113; --risk-bg:#2e1714;
  } }
  *{box-sizing:border-box}
  body{margin:0;background:var(--ground);color:var(--ink);
       font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  code,.mono{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
  header{padding:14px 20px;border-bottom:1px solid var(--rule);background:var(--surface);
         display:flex;flex-wrap:wrap;gap:8px 20px;align-items:baseline}
  h1{font-size:16px;margin:0;letter-spacing:-.01em}
  .meta{font-size:12px;color:var(--ink2)}
  .sim{background:var(--warn-bg);color:var(--warn);padding:2px 8px;border-radius:3px;font-size:12px;font-weight:600}
  .lab{background:var(--risk);color:#fff;padding:2px 8px;border-radius:3px;font-size:12px;font-weight:700;letter-spacing:.04em}
  .labbar{background:var(--risk-bg);border-bottom:1px solid var(--risk);color:var(--risk);
          padding:8px 20px;font-size:13px;font-weight:600}
  .ports{padding:10px 14px;font-size:12px;color:var(--ink2);border-bottom:1px solid var(--rule)}
  .ports b{color:var(--ink);font-family:ui-monospace,Menlo,monospace}
  main{display:grid;grid-template-columns:minmax(320px,1fr) minmax(320px,1fr);gap:16px;padding:16px;align-items:start}
  @media (max-width:860px){main{grid-template-columns:1fr}}
  section{background:var(--surface);border:1px solid var(--rule);border-radius:4px;overflow:hidden}
  h2{font-size:13px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink2);
     margin:0;padding:10px 14px;border-bottom:1px solid var(--rule);background:var(--surface2)}
  .grp{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--ink2);
       padding:10px 14px 4px;font-weight:700}
  .cmd{display:flex;gap:10px;align-items:center;padding:7px 14px;border-bottom:1px solid var(--rule)}
  .cmd:hover{background:var(--surface2)}
  .code{font-family:ui-monospace,Menlo,monospace;font-weight:700;min-width:32px;color:var(--accent)}
  .tag{flex:1;min-width:0}
  .tag b{display:block;font-weight:600}
  .tag span{font-size:12px;color:var(--ink2)}
  .pill{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
        padding:2px 6px;border-radius:3px;white-space:nowrap}
  .p-read{background:var(--ok-bg);color:var(--ok)}
  .p-state{background:var(--risk-bg);color:var(--risk)}
  button{font:inherit;font-size:12px;padding:5px 11px;border:1px solid var(--rule);
         border-radius:3px;background:var(--surface);color:var(--ink);cursor:pointer}
  button:hover{border-color:var(--accent);color:var(--accent)}
  button.danger{border-color:var(--risk);color:var(--risk)}
  .pad{padding:14px;display:flex;flex-direction:column;gap:10px}
  label{font-size:12px;color:var(--ink2);font-weight:600}
  input[type=text]{font-family:ui-monospace,Menlo,monospace;font-size:14px;padding:9px 11px;width:100%;
    border:1px solid var(--rule);border-radius:3px;background:var(--ground);color:var(--ink)}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .preview{background:var(--ground);border:1px solid var(--rule);border-radius:3px;padding:9px 11px;
    font-family:ui-monospace,Menlo,monospace;font-size:13px;word-break:break-all}
  .note{font-size:12px;color:var(--ink2)}
  .danger-box{background:var(--risk-bg);border-left:3px solid var(--risk);padding:10px 12px;font-size:13px}
  #log{max-height:460px;overflow:auto;font-family:ui-monospace,Menlo,monospace;font-size:12px}
  .entry{padding:9px 14px;border-bottom:1px solid var(--rule)}
  .entry .hd{display:flex;gap:8px;justify-content:space-between;flex-wrap:wrap}
  .ok{color:var(--ok)} .bad{color:var(--risk)}
  .kv{color:var(--ink2)}
  table{border-collapse:collapse;width:100%;margin-top:6px}
  td{padding:2px 8px 2px 0;vertical-align:top}
  td:first-child{color:var(--ink2);white-space:nowrap;width:1%}
</style></head><body>
<header>
  <h1>SAS Poll Bench</h1>
  <span class="meta">address <b id="addr">-</b></span>
  <span class="meta">one process owns the port &mdash; stop the poll logger first</span>
  <span id="simbadge"></span>
</header>
<div id="labbar"></div>
<main>
  <section>
    <h2>Implemented long polls</h2>
    <div class="ports" id="ports"></div>
    <div id="catalog"></div>
  </section>
  <section>
    <h2>Custom command</h2>
    <div class="pad">
      <div>
        <label for="cmd">Command byte onward, hex &mdash; address and CRC are added for you</label>
        <input type="text" id="cmd" placeholder="e.g.  2F 03 00 00 00" autocomplete="off">
      </div>
      <div class="row">
        <label><input type="checkbox" id="autocrc" checked> append CRC</label>
        <span class="note">address <b id="addr2">-</b> is prepended automatically</span>
      </div>
      <div><label>Exactly these bytes go on the wire</label><div class="preview" id="preview">-</div></div>
      <div class="danger-box" id="customwarn">
        <b>Unknown commands can move money, pay a ticket, or disable the machine.</b>
        Use a machine with no cash and no players. You will be asked to confirm.
      </div>
      <div class="row"><button class="danger" id="sendcustom">Send custom command</button></div>
    </div>
  </section>
  <section style="grid-column:1/-1">
    <h2>Exchange log <button id="clear" style="float:right;margin-top:-3px">clear</button></h2>
    <div id="log"></div>
  </section>
</main>
<script>
let ADDR = 1, LAB = false;
const $ = s => document.querySelector(s);

function preview() {
  const raw = $('#cmd').value.replace(/[^0-9a-fA-F]/g,'');
  if (!raw) { $('#preview').textContent = '-'; return; }
  const hex = raw.match(/../g) || [];
  const a = ADDR.toString(16).padStart(2,'0').toUpperCase();
  let s = [a, ...hex.map(h=>h.toUpperCase())].join(' ');
  if ($('#autocrc').checked) s += '  + CRC';
  $('#preview').textContent = s;
}

async function load() {
  const r = await (await fetch('/api/catalog')).json();
  ADDR = r.address;
  $('#addr').textContent = r.address; $('#addr2').textContent = r.address;
  if (r.simulate) $('#simbadge').innerHTML = '<span class="sim">SIMULATE &mdash; no real port open</span>';
  LAB = !!r.lab_mode;
  if (LAB) {
    $('#simbadge').innerHTML += ' <span class="lab">LAB MODE</span>';
    $('#labbar').className = 'labbar';
    $('#labbar').textContent = 'LAB MODE — confirmations are off. Every command sends on one click, '
      + 'including AFT transfers and machine lockouts.';
    $('#customwarn').innerHTML = '<b>Unknown commands can move money, pay a ticket, or disable '
      + 'the machine.</b> Lab mode is on, so this sends immediately with no confirmation.';
  }
  const ports = r.ports || [];
  $('#ports').innerHTML = ports.length
    ? 'Ports on this machine (' + r.platform + '): ' + ports.map(p=>'<b>'+p.device+'</b>'
        + (p.description && p.description!=='n/a' ? ' <span>'+p.description+'</span>' : '')).join(' &nbsp;·&nbsp; ')
    : 'No serial ports detected on this machine (' + r.platform + ').';
  const byGroup = {};
  r.catalog.forEach(c => (byGroup[c.group] = byGroup[c.group] || []).push(c));
  const el = $('#catalog'); el.innerHTML = '';
  for (const [g, items] of Object.entries(byGroup)) {
    const h = document.createElement('div'); h.className='grp'; h.textContent=g; el.appendChild(h);
    items.forEach(c => {
      const d = document.createElement('div'); d.className='cmd';
      const state = c.safety === 'state-change';
      d.innerHTML = `<span class="code">${c.code}</span>
        <span class="tag"><b>${c.tag}</b><span>${c.name} &middot; type ${c.type}${c.takes_data?' &middot; needs data bytes':''}</span></span>
        <span class="pill ${state?'p-state':'p-read'}">${state?'state':'read'}</span>`;
      const b = document.createElement('button');
      b.textContent = 'Send'; if (state) b.className = 'danger';
      b.onclick = () => send(c.code, c.tag, c.safety, c.warning, c.takes_data);
      d.appendChild(b); el.appendChild(d);
    });
  }
  refresh();
}

async function send(codeHex, label, safety, warning, takesData) {
  let cmd = codeHex;
  if (takesData) {
    const extra = prompt(`${label}\n\nLong poll ${codeHex} is a type S/M command and needs data bytes `
      + `(game number, meter code, etc). Enter them in hex, or leave blank to send the bare command:`, '');
    if (extra === null) return;
    cmd = codeHex + extra.replace(/[^0-9a-fA-F]/g,'');
  }
  if (safety !== 'read' && !LAB) {
    const typed = prompt(`STATE-CHANGING COMMAND\n\n${codeHex}: ${warning || label}\n\n`
      + `This alters the machine. Type SEND to confirm:`);
    if (typed !== 'SEND') return;
  }
  await post({command: cmd, label, safety, auto_crc: true, confirmed: safety !== 'read'});
}

$('#sendcustom').onclick = async () => {
  const cmd = $('#cmd').value.replace(/[^0-9a-fA-F]/g,'');
  if (!cmd) return alert('Enter some hex first.');
  if (!LAB) {
    const typed = prompt(`CUSTOM COMMAND\n\nAbout to send: ${$('#preview').textContent}\n\n`
      + `We cannot know what this does. It may move money, pay a ticket, or disable the machine.\n\n`
      + `Type SEND to confirm:`);
    if (typed !== 'SEND') return;
  }
  await post({command: cmd, label: 'custom ' + cmd.toUpperCase(), safety: 'custom',
              auto_crc: $('#autocrc').checked, confirmed: true});
};

async function post(body) {
  const r = await fetch('/api/send', {method:'POST', headers:{'Content-Type':'application/json'},
                                      body: JSON.stringify(body)});
  const j = await r.json();
  if (j.error && !j.tx) alert(j.error);
  refresh();
}

async function refresh() {
  const {log} = await (await fetch('/api/log')).json();
  $('#log').innerHTML = log.slice().reverse().map(e => {
    const d = e.decode || {};
    let rows = '';
    if (e.rx) {
      rows += `<tr><td>RX</td><td>${e.rx}</td></tr>`;
      if (d.shape) rows += `<tr><td>shape</td><td>${d.shape}</td></tr>`;
      if (d.command) rows += `<tr><td>command</td><td>${d.command}${d.address_ok===false?' <span class="bad">(address mismatch)</span>':''}</td></tr>`;
      if (d.payload) rows += `<tr><td>payload</td><td>${d.payload}</td></tr>`;
      if ('crc_ok' in d) rows += `<tr><td>CRC</td><td class="${d.crc_ok?'ok':'bad'}">${d.crc_on_wire} ${d.crc_ok?'valid':'INVALID (computed '+d.crc_computed+')'}</td></tr>`;
      if (d.hint) rows += `<tr><td>note</td><td>${d.hint}</td></tr>`;
    }
    return `<div class="entry">
      <div class="hd"><b>${e.label}</b><span class="kv">${e.at} &middot; ${e.elapsed_ms} ms</span></div>
      <table><tr><td>TX</td><td>${e.tx||'-'}</td></tr>${rows}
      ${e.error?`<tr><td>result</td><td class="bad">${e.error}</td></tr>`:''}</table>
    </div>`;
  }).join('') || '<div class="entry kv">Nothing sent yet.</div>';
}

$('#cmd').addEventListener('input', preview);
$('#autocrc').addEventListener('change', preview);
$('#clear').onclick = () => { $('#log').innerHTML = '<div class="entry kv">Cleared (server log kept).</div>'; };
load();
</script></body></html>
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("port", nargs="?", help="serial device, e.g. /dev/ttyUSB0")
    p.add_argument("--address", type=int, default=1, help="SAS address (1-127)")
    p.add_argument("--baud", type=int, default=19200)
    p.add_argument("--timeout", type=float, default=1.0, help="per-exchange timeout, seconds")
    p.add_argument("--port-http", type=int, default=8080, help="HTTP port for the UI")
    p.add_argument("--bind", default="127.0.0.1",
                   help="interface to serve on. Default localhost; use 0.0.0.0 to reach it "
                        "from a laptop on the bench (there is no authentication, so only do "
                        "that on a trusted lab network)")
    p.add_argument("--list-ports", action="store_true",
                   help="list the serial ports this machine can see, and exit. Works on "
                        "Linux, Windows and macOS")
    p.add_argument("--lab-mode", action="store_true",
                   help="skip the typed confirmation for state-changing and custom commands. "
                        "For a bench where a locked machine is a non-event and a RAM-clear is "
                        "on hand. Never the default")
    p.add_argument("--open-browser", action="store_true",
                   help="open the UI in this machine's default browser once it is serving")
    p.add_argument("--simulate", action="store_true",
                   help="don't open a port; fabricate responses so the UI can be explored "
                        "with no hardware attached")
    args = p.parse_args()

    if args.list_ports:
        ports = list_serial_ports()
        if not ports:
            print("No serial ports detected.")
            print("  Linux:   is the adapter plugged in? check `dmesg | tail` and group perms (dialout)")
            print("  Windows: check Device Manager > Ports (COM & LPT)")
            print("  macOS:   look for /dev/cu.usbserial-* ; install the adapter's driver if absent")
            return 1
        print(f"Serial ports visible to this machine ({sys.platform}):\n")
        for prt in ports:
            print(f"  {prt['device']}")
            print(f"      {prt['description']}")
            if prt["hwid"] != "n/a":
                print(f"      {prt['hwid']}")
            if prt["hint"]:
                print(f"      note: {prt['hint']}")
        return 0

    if not args.port and not args.simulate:
        p.error("give a serial port, or --simulate to explore the UI without hardware")

    try:
        bench = Bench(args.port or "", args.address, args.baud, args.timeout,
                      simulate=args.simulate, lab_mode=args.lab_mode)
    except Exception as e:  # noqa: BLE001 - turn any port failure into something actionable
        print(f"Could not open {args.port}:\n  {type(e).__name__}: {e}\n", file=sys.stderr)
        msg = str(e).lower()
        if "exclusively lock" in msg:
            print("Another process already holds this port. Stop sql_poll_logger.py (or the\n"
                  "other copy of this tool) first -- only one process may use a SAS port.",
                  file=sys.stderr)
        elif "invalid argument" in msg or "termios" in type(e).__name__.lower():
            print("The driver rejected the port settings. SAS needs mark/space parity for the\n"
                  "wakeup bit, and not every USB-serial adapter supports it -- a virtual tty\n"
                  "(pty) never does. Try a different adapter, or --simulate to explore the UI\n"
                  "without hardware.", file=sys.stderr)
        else:
            print("Check the device path and that the adapter is plugged in. --simulate opens\n"
                  "no port at all if you just want to look at the UI.", file=sys.stderr)
        return 2

    catalog = build_catalog()

    reads = sum(1 for c in catalog if c["safety"] == "read")
    print(f"SAS Poll Bench — address {args.address}"
          + ("  [SIMULATE: no real port]" if args.simulate else f"  on {args.port}"))
    print(f"  {len(catalog)} long polls in the catalog ({reads} read, {len(catalog)-reads} state-changing)")
    if not args.simulate:
        print("  This port is now held exclusively — stop sql_poll_logger.py if it is running.")
    if args.lab_mode:
        print("  LAB MODE: confirmations are OFF. Every command sends on one click,\n"
              "            including AFT transfers and machine lockouts.")
    if args.bind == "127.0.0.1":
        print("  Serving on localhost only. Use --bind 0.0.0.0 to reach this from a laptop\n"
              "  (no authentication -- trusted lab networks only).")
    url = f"http://{'localhost' if args.bind == '127.0.0.1' else args.bind}:{args.port_http}"
    print(f"\n  open  {url}\n")
    if args.open_browser:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    # Threaded: TCPServer is single-threaded, and with HTTP/1.1 keep-alive a
    # single open browser tab would hold the connection and block every other
    # client -- two people opening the UI, and the second one just hangs.
    # Bench serializes the actual port with its own lock, so this is safe.
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server((args.bind, args.port_http),
                                make_handler(bench, catalog, HTML)) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
