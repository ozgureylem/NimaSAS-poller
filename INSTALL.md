# Installing the poller, headless

This is the whole install, on a machine with no desktop and no browser.
It is short because the poller has one dependency.

Everything in section 1 and section 2 was run end to end on
Ubuntu 24.04 / Python 3.11 before this file was written. The Windows and
macOS sections are the same four steps with that platform's spellings;
where a step could not be exercised here it says so, rather than
implying it was.

---

## 0. What the poller actually needs

| | |
|---|---|
| Python | **3.10 or newer** (`requires-python = ">=3.10"` in `pyproject.toml`) |
| One Python package | **`pyserial` 3.5 or newer** |
| For the test suite only | `pytest` 7 or newer |
| Serial line | **19200 baud, 8 data bits, 1 stop bit, SPACE parity** |
| Adapter | one whose UART can set **mark/space parity** |

Two of those deserve more than a table row.

**There is nothing to build and nothing to `pip install` from this
repository.** Every tool puts the repository root on `sys.path` itself,
so a tool runs from any working directory as soon as `pyserial` is
importable. `pip install -e .` works and is harmless, but it is not a
step. This was checked the blunt way: all 29 tools answered `--help`
from `/`, in a virtual environment containing `pyserial` and nothing
else.

**The adapter is the part people get wrong.** SAS marks the first byte
of every message with a 9th bit, which this code produces as mark/space
parity (`saspy/serial_port.py`). Plenty of cheap USB-RS232 bridges
cannot do mark/space parity at all — the lab's udev rule binds an FTDI
device (`idVendor 0403`) for exactly this reason. Buy FTDI-class
hardware, or expect to debug a wiring problem you do not have.

An adapter that *refuses* space parity is caught at open, with a message
that names the adapter as the cause. An adapter that *accepts* the
setting and then quietly ignores it cannot be detected from this side of
the wire — `saspy/serial_port.py` says so in its own docstring. So a
port that opens cleanly means "the driver did not refuse", **not** "the
9th bit is reaching the machine". If every poll still times out, the
adapter is not yet cleared.

---

## 1. Linux (Debian, Raspberry Pi OS, Ubuntu)

### 1.1 System packages

```bash
sudo apt update
sudo apt install -y git python3 python3-serial
```

`python3-serial` is the distribution's `pyserial`. Installing it with
apt rather than pip is deliberate on a gateway: it puts the module where
the **system** `python3` can see it, which is what
`deploy/nimasas-poller.service` runs, and it survives a distribution
upgrade without anyone remembering a virtual environment exists.

> **Do not reach for `pip install pyserial` on a fresh Raspberry Pi OS
> or Ubuntu.** Both ship PEP 668 marker files, and pip will refuse with
> `error: externally-managed-environment`. That refusal is correct and
> should not be argued with using `--break-system-packages`. Use the apt
> package above for a gateway, or a virtual environment (§1.2) for a
> workstation.

### 1.2 Only on a workstation: a virtual environment

Skip this on a gateway. On a development machine, where you want the
test suite and do not want to touch system Python:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install pyserial pytest
```

On Debian/Ubuntu `python3 -m venv` needs `sudo apt install -y python3-venv`
first; Raspberry Pi OS already has it.

### 1.3 The repository

```bash
sudo mkdir -p /opt/nimasas
sudo chown "$USER" /opt/nimasas
git clone https://github.com/ozgureylem/NimaSAS-poller.git /opt/nimasas
cd /opt/nimasas
```

`/opt/nimasas` is the path `MANUAL.md` §4.6 assumes for a floor install.
Anywhere else works; if you choose elsewhere, the paths in
`deploy/nimasas-poller.service` are the three lines to change.

### 1.4 Permission to open the serial port

On Debian the serial device belongs to group `dialout`. Without
membership the open fails with `EACCES`, which reads like a missing port
rather than a missing group:

```bash
sudo usermod -aG dialout "$USER"
```

**Log out and back in** — group membership is picked up at login, not by
the running shell. `id -nG` should then list `dialout`.

### 1.5 Which device is it

```bash
ls -l /dev/serial/by-id/      # USB adapters, by their own identity
ls -l /dev/ttyUSB* /dev/ttyS* 2>/dev/null
```

A USB adapter is normally `/dev/ttyUSB0`; an on-board UART is
`/dev/ttyS0`. Prefer the stable name under `/dev/serial/by-id/`, or bind
your own with a udev rule — `MANUAL.md` §4.6 has the rule and the
reason, which is that a re-enumeration can otherwise point a gateway at
a different cabinet without saying so.

On a Raspberry Pi using the **on-board** UART rather than a USB adapter,
the serial console owns that port until you take it away
(`sudo raspi-config` → Interface Options → Serial Port → login shell
**no**, hardware **yes**). A USB adapter needs none of this, which is
one more reason the lab uses one.

---

## 2. Proving the install, with no EGM attached

Do this before going near a machine. It needs no serial port at all.

```bash
cd /opt/nimasas
for f in examples/*.py deploy/*.py; do
    python3 "$f" --help >/dev/null || echo "BROKEN: $f"
done
echo "sweep done"
```

Silence followed by `sweep done` means every tool in the tree parses,
imports and can describe itself. Any line starting `BROKEN:` names a
tool whose dependencies are not satisfied — in practice, `pyserial`
missing.

With `pytest` available, the real check:

```bash
python3 -m pytest -q
```

Look for **zero failures**. A number of tests skip, and that is
expected: this repository is a mirror of a lab tree, and the tests that
exercise lab-only apparatus skip when the apparatus is absent rather
than quietly passing. Skips are fine. Failures are not.

> This is as far as you can get without hardware, and it is further than
> it sounds — it proves Python version, `pyserial`, the protocol codec
> and the SQL layer. What it cannot prove is parity on the wire. A
> virtual tty accepts mark/space parity and ignores it, so a loopback
> test of the wakeup bit would report success it has not earned.

---

## 3. Proving the install against a real machine

In this order. Each step is cheap and tells you something the next one
assumes.

```bash
python3 examples/connectivity_check.py /dev/ttyUSB0 --address 1
```

"Address is live" means wiring, baud, parity and address are all right.
If it fails, the tool's own troubleshooting output is the next thing to
read — at this stage nearly every problem is wiring, address or
adapter, not code.

Do not know the address:

```bash
python3 examples/address_scan.py /dev/ttyUSB0 --range 1-8
```

Then, before trusting a poll loop on this cabinet:

```bash
python3 examples/wire_diagnostics.py /dev/ttyUSB0 --address 1
python3 examples/polling_capability.py /dev/ttyUSB0 --address 1
```

All four of these only read. None changes a meter, moves money, or
alters machine state.

---

## 4. From working install to running gateway

```bash
python3 examples/commission_gateway.py --output gateway.ini
python3 examples/sql_poll_logger.py gateway.ini --db gateway.sqlite3
```

`commission_gateway.py` finds the port and address and writes the config
file; `sql_poll_logger.py` is the gateway proper.

**`sql_poll_logger.py` is the one tool that takes a config file rather
than a device path.** Every other tool that talks to a machine takes
`/dev/...`. Both directions of that confusion have cost real bench time,
so the test suite now enforces it across every document in this
repository (`tests/test_commands_doc.py`).

To survive a reboot, install the unit:

```bash
sudo cp deploy/nimasas-poller.service /etc/systemd/system/
sudoedit /etc/systemd/system/nimasas-poller.service   # paths, port, address
sudo systemctl daemon-reload
sudo systemctl enable --now nimasas-poller
journalctl -fu nimasas-poller
```

**Read the unit before enabling it.** It ships with the lab gateway's
own paths, user and serial port, and its comments say which lines a
floor install changes. Editing it is a step, not an optional tidy-up.

`COMMANDS.md` is the per-task command list from here on.

---

## 5. Windows

Same four steps. The spellings differ and no step below was executed on
Windows while writing this file.

```
winget install Python.Python.3.12
winget install Git.Git
```

```
git clone https://github.com/ozgureylem/NimaSAS-poller.git C:\nimasas
cd C:\nimasas
py -3 -m venv .venv
.venv\Scripts\activate
pip install pyserial pytest
```

Windows has no PEP 668 marker, so `pip install pyserial` into a virtual
environment is the ordinary path here.

Find the port in Device Manager under **Ports (COM & LPT)**, or:

```
python -c "import serial.tools.list_ports as p; [print(x.device, x.description) for x in p.comports()]"
```

Ports are `COM1`, `COM3`, … and go where `/dev/ttyUSB0` goes:

```
python examples\connectivity_check.py COM3 --address 1
python -m pytest -q
```

FTDI adapters need FTDI's VCP driver if Windows does not supply one.
A COM number above 9 is written `COM12` in these commands and
`\\.\COM12` only if some other program insists on it; `pyserial` handles
the plain form.

---

## 6. macOS

```
brew install python@3.12 git
git clone https://github.com/ozgureylem/NimaSAS-poller.git ~/nimasas
cd ~/nimasas
python3 -m venv .venv
. .venv/bin/activate
pip install pyserial pytest
```

Homebrew Python has no PEP 668 marker; a virtual environment is still
the right habit.

Finding the port, and the one macOS trap:

```
ls /dev/cu.*
```

**Use the `/dev/cu.*` name, not `/dev/tty.*`.** They are the same
hardware. Opening the `tty.` name makes the process wait for carrier
detect, which an EGM does not assert, so the tool hangs with no error —
it looks exactly like a dead cable. The `cu.` name does not wait.

```
python3 examples/connectivity_check.py /dev/cu.usbserial-10 --address 1
python3 -m pytest -q
```

No `dialout` equivalent is needed; macOS grants the console user access
to USB serial devices.

---

## 7. When it does not work

| Symptom | Cause | Fix |
|---|---|---|
| `error: externally-managed-environment` | PEP 668: system Python is distribution-managed | `sudo apt install python3-serial`, or a virtual environment (§1.2) |
| `ModuleNotFoundError: No module named 'serial'` | `pyserial` missing, or installed for a different interpreter | Install it for the interpreter you are actually running; `python3 -c "import serial; print(serial.__version__)"` |
| `Permission denied` / `EACCES` on the port | not in `dialout` | §1.4, **then log out and back in** |
| `cannot set mark/space parity` | the adapter's UART cannot do the 9th bit | Use an FTDI-class adapter. Not the wiring, baud or address |
| Port opens, every poll times out | wiring, address, or an adapter that accepts space parity and ignores it | `address_scan.py`; then suspect the adapter again (§0) |
| Tool hangs with no output, macOS | opened `/dev/tty.*` instead of `/dev/cu.*` | §6 |
| Phantom `exception 0x01` on every poll | a previous run was killed with RTE still enabled in the cabinet | `python3 examples/exception_monitor.py /dev/ttyUSB0 --address 1 --disable-rte` |
| Device name moved between reboots | USB re-enumeration | `/dev/serial/by-id/`, or a udev `SYMLINK` — `MANUAL.md` §4.6 |
| `/dev/ttyS0` busy on a Raspberry Pi | the serial console holds it | `raspi-config`, §1.5 — or use a USB adapter |

---

## Where to go next

- **`COMMANDS.md`** — the command for each task, grouped by what it does
- **`MANUAL.md`** — one section per tool, with flags and troubleshooting
- **`README.md`** — what this repository is, and what is deliberately not in it
