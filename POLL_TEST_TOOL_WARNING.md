# ⚠ Read this before running `examples/poll_test_tool.py`

**This tool sends arbitrary commands to a gaming machine. It can cost you
the machine's meters, and those do not come back.**

It exists so a lab can try SAS commands of unknown provenance — codes
from a forum post, a vendor PDF, an old integration — and see what a real
EGM does with them. That is genuinely useful work. It is also, by
definition, running commands nobody has verified, at hardware that holds
accounting records.

If you only read one section, read
[Never on an active floor](#never-on-an-active-floor) and
[Before the gateway leaves the lab](#before-the-gateway-leaves-the-lab).

---

## What can actually go wrong

Not hypotheticals. These are all reachable with a single click, or a
single pasted hex string:

| Command | Consequence |
|---|---|
| `0x01` Shutdown | Machine goes out of service, stops accepting play |
| `0x74` AFT lock | Machine locks; may need attendant or host action to clear |
| `0x71` Redeem ticket | **Pays credits onto the machine** as though a real ticket were redeemed |
| `0x72` AFT transfer | **Moves funds.** A transfer that actually completed is real money movement, logged in the machine's AFT history |
| `0x58` Validation number | Commits a cashout — the machine prints a ticket with that number |
| `0x07` Disable bill acceptor | Machine stops taking notes |
| `0x2E` Delay game | Machine stalls for the delay period |
| `0x7B` / `0x7C` / `0x7D` | Rewrite validation config, ticket expirations, printed ticket text |
| `0x7F` Set date/time | Overwrites the machine's clock — affects every timestamped record it writes afterwards |
| `0x21` ROM signature | Starts a ROM calculation; some machines lock out play while it runs |
| Unknown code | Unknown. That is the entire point of the tool, and the entire risk. |

A command that does nothing on one machine may do something on another.
Manufacturers extend SAS. "It was safe on the Aristocrat" is not evidence
about the IGT next to it.

## RAM clear, reset, and losing your meters

The usual fix for a machine stuck in a state you put it in is a **RAM
clear** (or an operator-level reset, depending on what got stuck).

**A RAM clear zeroes the machine's soft meters.** Coin in, coin out,
drop, jackpot, games played, bill counts, ticket meters — the machine's
accounting record of everything it has ever done. That is not a
recoverable operation. There is no undo, and the host cannot write the
old values back.

Consequences worth understanding before you need them:

- **Accounting records are gone.** Those meters are the machine's books.
  Whatever your jurisdiction requires around meter integrity, clearing
  them is a documented event, not a casual maintenance step. Check your
  own regulatory position before you are in a hurry — not after.
- **Your gateway database will show the discontinuity.** Every cumulative
  meter drops to zero at once. `sql_poll_logger.py` treats a cumulative
  decrease as an anomaly and arms its burst window (see MANUAL.md §4.4),
  so you will get a run of full-resolution history rows around the
  event. That is the tool working correctly, not a bug — but if you are
  comparing meter data across a RAM clear, the series is broken at that
  point and no amount of querying repairs it.
- **Some machines need more than a RAM clear.** Depending on what was
  set, you may need operator-menu access, a technician key, or the
  manufacturer's reset procedure. Know which machines in your lab you can
  actually recover before you experiment on them.

**Practical rule: experiment on machines whose meters you are willing to
lose.** If a machine's meter history matters to anyone, it is the wrong
machine for this tool.

Also: use a machine with **no credits and no money in it**. A RAM clear
undoes a lockup. It does not undo an AFT transfer that actually moved
funds, or a ticket that actually printed.

## Never on an active floor

**This tool must not be installed on a gateway serving live machines.**

A floor gateway with this tool on it is a machine where anyone with
shell access — or anyone who reaches its HTTP port — can disable an EGM,
print a ticket, or move funds, with no authentication of any kind. The
tool has no login, no audit trail beyond its own session log, and no
concept of who is using it.

Specifically:

- It belongs **only** on a gateway dedicated to laboratory use.
- It must **not** be part of any build, image, or deployment that goes to
  a production floor.
- `--bind 0.0.0.0` puts it on the network with no authentication. Only on
  a trusted, isolated lab network. Never on a floor network, not even
  briefly, not even "just to check something."

If a gateway has ever been used for this and is now destined for a floor,
treat the tool as something that must be removed and verified removed —
see below.

## Before the gateway leaves the lab

When a lab gateway is being redeployed to a floor, **erase the tool and
verify it is gone.** Deleting the `.py` file is not sufficient on its
own: Python leaves compiled bytecode behind, which is still the code
sitting on that machine.

```bash
# 1. Stop it if it is running
pkill -f poll_test_tool.py || true

# 2. Remove the tool and its compiled bytecode
rm -f  examples/poll_test_tool.py
rm -rf examples/__pycache__

# 3. Verify nothing is left — all three must return nothing
find / -name 'poll_test_tool*' 2>/dev/null
find / -name '*.pyc' -path '*poll_test*' 2>/dev/null
pgrep -af poll_test_tool

# 4. Confirm nothing is configured to start it
systemctl list-units --all 2>/dev/null | grep -i poll_test
crontab -l 2>/dev/null | grep -i poll_test
```

Also worth doing on a gateway moving to a floor:

- **Check the deployment source.** If the floor gateway pulls from a git
  repo that contains this file, it will come back on the next `git pull`.
  Deploy floor gateways from a branch or artifact that does not include
  `examples/poll_test_tool.py`, rather than relying on someone
  remembering to delete it each time.
- **Clear shell history** if hex strings and addresses were pasted into
  it (`history -c` plus `~/.bash_history`).
- **Review any `gateway.ini`** left behind from lab work — it may point
  at the wrong port or address for the floor.

## If something has already gone wrong

1. **Stop sending.** Close the tool; it holds the port exclusively, so
   killing it also releases the machine.
2. **Try the gentle path first.** `0x02` Startup returns a machine
   disabled with `0x01`. An AFT lock from `0x74` may clear on its own
   timeout, or with the lock-release form of the same poll.
3. **Escalate to operator/technician procedures** only when the protocol
   path does not recover it — a RAM clear is the end of the list, not
   the start, because of what it costs.
4. **Write down what you sent.** The tool's exchange log has the exact
   bytes, timestamped. That is the single most useful thing to hand the
   next person, whether they are recovering the machine or deciding
   whether the command is worth adding to `saspy`.

---

See **[POLL_TEST_TOOL_MANUAL.md](POLL_TEST_TOOL_MANUAL.md)** for how to
actually use it.
