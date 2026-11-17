"""Which MACHINE a row came from.

A gateway is commissioned to a floor position (`client_id`), and the
specification's model is one gateway, one machine (§10.5). The lab does not
work like that: one gateway is carried from cabinet to cabinet, and every
row it drains arrives under the same `client_id` and the same database. A
server that keys on `client_id` alone would add one cabinet's coin-in to
another's and credit a ticket printed at one to the wrong machine.

THE GATEWAY ALREADY KNOWS. The poller keeps `machine_epochs`: a new row
each time the cabinet's identity changes (`machine_changed`,
`asset_changed`) or its meters are cleared (`ram_clear`), each carrying the
SAS serial number and asset number it read off the wire. What was missing
is that none of it reached the server -- the table has no `synced_at`, so
it was never drained, and the event tables carry no epoch at all.

So this module does two things, the same way on both ends:

  * decides which epoch a row belongs to;
  * describes an epoch as the small object that travels.

A ROW WITH ITS OWN `epoch_id` COLUMN USES IT. The meter tables and
`interrogations` have one, written by the poller at the moment it knew.

EVERY OTHER ROW IS PLACED BY TIME: the epoch that had started when the row
was recorded. That is an inference and it has one known soft edge. When a
gateway is moved, the poller notices the new identity only when it
re-interrogates the machine, a few seconds after the cable is back. A row
written in those seconds -- an event the NEW cabinet had queued -- carries
a time just before the new epoch opens and is placed in the OLD one. The
window is seconds wide and sits exactly at a move; a move is also when a
person is standing at the gateway. It is the price of not adding a column
to every event table in the poller, and it is stated here rather than
discovered in a report.

THE NAME IS SHOWN, NEVER USED TO DECIDE. Serial and asset are both typed
in by a technician (HANDOVER §7f), the asset is not unique on the floor
(§7b), and a RAM clear blanks both. So the server shows the pair, and
separates one machine from another only by the boundary the poller drew
-- see `stays()` below. The name lives on the epoch, never copied onto a
row: correcting it is one update, not a migration of every row it covers.
"""

from __future__ import annotations

import datetime
import sqlite3

# The column that says when a row was recorded. Every drained table has
# one and no two agree on its name; first match wins.
TIME_COLUMNS = ("occurred_at", "captured_at", "started_at", "sampled_at",
                "polled_at", "at", "asked_at", "filled_at")
EPOCH_FIELDS = ("id", "started_at", "ended_at", "reason", "serial_number",
                "asset_number", "sas_version")


def parse_time(value) -> datetime.datetime | None:
    """An aware datetime, or None. PARSED, never compared as text: the
    poller writes isoformat() with a 'T' and an offset, and this repository
    has already lost a day to comparing such strings as strings."""
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=datetime.timezone.utc)


def row_time(row: dict) -> datetime.datetime | None:
    for name in TIME_COLUMNS:
        when = parse_time(row.get(name))
        if when is not None:
            return when
    for name, value in row.items():
        if name.endswith("_at") and name != "synced_at":
            when = parse_time(value)
            if when is not None:
                return when
    return None


def read_epochs(conn: sqlite3.Connection) -> list[dict]:
    """Every epoch this gateway has recorded, oldest first. [] when the
    poller has never run against this database -- which is "no machine
    known", and must not be read as "one machine"."""
    try:
        cursor = conn.execute(
            "SELECT id, started_at, ended_at, reason, serial_number, "
            "asset_number, sas_version FROM machine_epochs ORDER BY id")
    except sqlite3.OperationalError:
        return []
    return [dict(zip(EPOCH_FIELDS, row)) for row in cursor.fetchall()]


def clean_epoch(raw) -> dict | None:
    """One epoch as received from the other end, or None if it is not one.
    The frame was authenticated, so this is a shape check, not a trust
    decision -- but a malformed epoch must not become a machine."""
    if not isinstance(raw, dict):
        return None
    epoch_id = raw.get("id")
    if isinstance(epoch_id, bool) or not isinstance(epoch_id, int) or epoch_id < 1:
        return None
    if parse_time(raw.get("started_at")) is None:
        return None
    asset = raw.get("asset_number")
    if isinstance(asset, bool) or not (asset is None or isinstance(asset, int)):
        return None
    serial = raw.get("serial_number")
    if not (serial is None or isinstance(serial, str)):
        return None
    out = {name: raw.get(name) for name in EPOCH_FIELDS}
    out["serial_number"] = serial.strip() if isinstance(serial, str) else None
    return out


def epoch_for(epochs: list[dict], row: dict) -> int | None:
    """The id of the epoch `row` belongs to, or None.

    None is an answer: the row predates every epoch this gateway recorded,
    or carries no time at all. It is stored as "machine unknown", never
    rounded to the nearest epoch.
    """
    own = row.get("epoch_id")
    if isinstance(own, int) and not isinstance(own, bool):
        return own
    when = row_time(row)
    if when is None:
        return None
    chosen = None
    for epoch in epochs:
        started = parse_time(epoch.get("started_at"))
        if started is not None and started <= when:
            chosen = epoch["id"]
        # NOT `break` on the first later one: ids are in order of creation
        # and started_at should be too, but a gateway whose clock stepped
        # backwards wrote them out of order, and the last epoch that had
        # started is still the right one.
    return chosen


def current(epochs: list[dict]) -> dict | None:
    """The epoch that is open now: the machine this gateway is on."""
    return epochs[-1] if epochs else None


def describe(epoch: dict | None) -> str:
    if not epoch:
        return "machine unknown"
    asset = epoch.get("asset_number")
    serial = epoch.get("serial_number")
    return (f"asset {asset if asset is not None else '?'}"
            f" / serial {serial if serial else '?'}")


# A STAY: one cabinet, from the moment the gateway found itself on it
# until it found itself on another. One or more epochs.
#
# The gateway can ALWAYS detect the boundary and can only SOMETIMES read
# the name (AGENT-BOARD, 2026-11-13, the operator's and Opus's point). A
# RAM clear wipes the asset number and the serial, both of which are typed
# in -- so the same cabinet goes from "asset 246" to no name at all, and a
# server that compared names would announce a move that never happened,
# and file the cabinet's next accounting under a different machine.
#
# So the name is never what separates two machines. The poller's `reason`
# is: it decided, from meters and identity together, whether this is the
# same cabinet. These reasons CONTINUE a stay:
SAME_CABINET = frozenset({"ram_clear", "asset_changed", "comms_restored",
                          "options_changed"})
# 'first_seen' and 'machine_changed' begin one. So does ANY reason this
# code has not heard of: a reason added to the poller tomorrow that means
# "different cabinet" must not be read as "same". Splitting one machine in
# two is visible and reversible; merging two machines' accounting is
# neither, so the unknown case fails toward splitting.


def stays(epochs: list[dict]) -> list[dict]:
    """Group a gateway's epochs into the cabinets it was on, oldest first.

    Each stay carries the ids of its epochs and ONE name: the most recent
    serial and asset any of its epochs read. A cabinet cleared and then
    named again an hour later is the same stay throughout, and takes the
    later name -- including for the rows recorded while it had none.

    A stay no epoch ever named has serial and asset None. It is still a
    distinct machine: its rows are kept apart from every other stay's,
    which is the property that matters. What cannot be said is WHICH
    cabinet, and that is a commissioning act by a person.
    """
    out: list[dict] = []
    for epoch in sorted(epochs, key=lambda e: e["id"]):
        if not out or epoch.get("reason") not in SAME_CABINET:
            out.append({"first_epoch_id": epoch["id"], "epoch_ids": [],
                        "serial_number": None, "asset_number": None,
                        "started_at": epoch.get("started_at"),
                        "ended_at": None, "cleared": 0})
        stay = out[-1]
        stay["epoch_ids"].append(epoch["id"])
        stay["ended_at"] = epoch.get("ended_at")
        if epoch.get("reason") == "ram_clear":
            stay["cleared"] += 1
        # The LATEST reading wins, field by field: an asset typed in after
        # a clear replaces the blank, and a corrected one replaces a typo.
        if epoch.get("serial_number"):
            stay["serial_number"] = epoch["serial_number"]
        if epoch.get("asset_number") is not None:
            stay["asset_number"] = epoch["asset_number"]
    return out


def stay_of(epochs: list[dict], epoch_id) -> dict | None:
    for stay in stays(epochs):
        if epoch_id in stay["epoch_ids"]:
            return stay
    return None


def describe_stay(stay: dict | None) -> str:
    if not stay:
        return "machine unknown"
    if stay["serial_number"] is None and stay["asset_number"] is None:
        return (f"unnamed cabinet (no serial or asset read; epoch "
                f"{stay['first_epoch_id']})")
    return describe(stay)
