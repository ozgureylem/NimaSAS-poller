"""Dump every table and view in the gateway database to CSV, for review.

    python3 deploy/sql_samples.py                       # default db, default dir
    python3 deploy/sql_samples.py ~/NimaSAS/gateway.sqlite3
    python3 deploy/sql_samples.py --rows 500 --push

WHAT IT IS FOR. "What is actually in the database" has been answered three
times in this project by pasting a screenshot of one query. This writes the
whole shape down: every table, every column, the last N rows of each, and a
row count and date range per table -- so the next person can read the data
model without a gateway in front of them.

EVERY TABLE GETS A FILE, INCLUDING THE EMPTY ONES. A table with no rows
still writes its header. "This table exists and is empty" and "this table
does not exist" are different facts, and a directory listing that omits the
empty ones cannot tell them apart -- which is the failure this project has
now made four times in other forms.

VALIDATION NUMBERS NEVER LEAVE. Five columns carry a real printed number:

    ticket_out_history.validation_number
    validation_pool.validation_number
    host_requests.validation_number
    ticket_in_events.validation_data_hex
    ticket_in_completions.validation_data_hex

They are replaced with a dense per-export pseudonym -- vn#1, vn#2 -- so the
structure survives (you can still see that the same ticket appears twice, or
that an assigned pool number matches a printed one) while the value does
not. docs/project is outside the public mirror's allowlist, but the rule
that keeps printed numbers out of files is not one to lean on a directory
for; soak_report.sh makes the same choice and says so.

READ-ONLY. Opens the database with mode=ro and runs nothing but SELECT, so
it is safe beside a live soak.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import os
import subprocess
import sys
from pathlib import Path

# BY COLUMN NAME, ACROSS EVERY OBJECT -- not by (table, column) pair.
#
# The first version listed the five TABLES that carry a number and missed
# ticket_in_history, which is a VIEW over two of them and republishes
# validation_data_hex under the same name. A test caught it on the day it
# was written. Keying on the pair means every future view, and every table
# that gains one of these columns, has to be remembered; keying on the name
# means it cannot be forgotten.
#
# This is an EXACT name match, not a substring. A substring would catch
# meter columns like c7_validated_jackpot_handpay_receipt_printed_cents,
# which are counts of receipts and not numbers -- the same trap this
# project has walked into six times with text-matching guards.
REDACT_COLUMNS = frozenset({"validation_number", "validation_data_hex"})

# Kept for the guard test: the tables known to carry one, so a schema
# change that adds a sixth is noticed rather than silently covered.
REDACT = {
    ("ticket_out_history", "validation_number"),
    ("validation_pool", "validation_number"),
    ("host_requests", "validation_number"),
    ("ticket_in_events", "validation_data_hex"),
    ("ticket_in_completions", "validation_data_hex"),
}

# Columns a table is ordered and dated by, most specific first. A table with
# none of these is dumped by rowid and reported without a date range.
TIME_COLUMNS = ("occurred_at", "captured_at", "polled_at", "detected_at",
                "started_at", "taken_at_utc", "requested_at", "at",
                "issued_at", "sampled_at", "attempted_at")


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main(argv=None) -> int:
    import sqlite3

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("database", nargs="?", default=None,
                    help="the gateway SQLite file (default: ./gateway.sqlite3 "
                         "or ~/NimaSAS/gateway.sqlite3)")
    ap.add_argument("--rows", type=int, default=200,
                    help="most recent rows per table (default: %(default)s)")
    ap.add_argument("--dir", default=None,
                    help="output directory (default: docs/project/sql-samples "
                         "inside this repository)")
    ap.add_argument("--push", action="store_true",
                    help="git add, commit and push the directory afterwards")
    args = ap.parse_args(argv)

    repo = Path(__file__).resolve().parent.parent
    db = args.database
    if db is None:
        for candidate in (Path.cwd() / "gateway.sqlite3",
                          Path.home() / "NimaSAS" / "gateway.sqlite3"):
            if candidate.exists():
                db = str(candidate)
                break
    if db is None or not os.path.exists(db):
        print(f"no database at {db}", file=sys.stderr)
        return 2

    out = Path(args.dir) if args.dir else repo / "docs" / "project" / "sql-samples"
    out.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    objs = list(conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table','view') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"))

    # One pseudonym map per export, shared across tables, so a pool number
    # and the ticket printed with it still line up.
    pseudonym: dict[object, str] = {}

    def mask(value):
        if value is None:
            return None
        if value not in pseudonym:
            pseudonym[value] = f"vn#{len(pseudonym) + 1}"
        return pseudonym[value]

    index = []
    for kind, name in objs:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
        if not cols:
            continue
        total = conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        time_col = next((c for c in TIME_COLUMNS if c in cols), None)
        order = time_col if time_col else ("rowid" if kind == "table" else cols[0])
        rows = list(conn.execute(
            f"SELECT {', '.join(cols)} FROM {name} "
            f"ORDER BY {order} DESC LIMIT ?", (args.rows,)))
        rows.reverse()                       # oldest first, as a log reads

        redact_idx = [i for i, c in enumerate(cols) if c in REDACT_COLUMNS]
        path = out / f"{name}.csv"
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols)                 # ALWAYS, even with no rows
            for row in rows:
                row = list(row)
                for i in redact_idx:
                    row[i] = mask(row[i])
                w.writerow(row)

        span = ""
        if time_col and total:
            lo, hi = conn.execute(
                f"SELECT min({time_col}), max({time_col}) FROM {name}").fetchone()
            span = f"{str(lo)[:19]} .. {str(hi)[:19]}"
        index.append((kind, name, len(cols), total, len(rows), span,
                      bool(redact_idx)))
        print(f"  {name:26} {len(cols):>3} cols  {total:>8,} rows  "
              f"-> {len(rows):>4} written{'  [redacted]' if redact_idx else ''}")

    readme = out / "README.md"
    with readme.open("w") as fh:
        fh.write("# Gateway database — shape and samples\n\n")
        fh.write(f"Generated `{_utc_stamp()}` from `{db}`, read-only.\n")
        fh.write(f"Last **{args.rows}** rows of each table, oldest first.\n\n")
        fh.write("**Every table has a file, including the empty ones.** A\n")
        fh.write("table with no rows still writes its header: \"exists and is\n")
        fh.write("empty\" and \"does not exist\" are different facts.\n\n")
        fh.write("**Validation numbers are replaced with `vn#N`**, a dense\n")
        fh.write("pseudonym shared across tables — so a pool number and the\n")
        fh.write("ticket printed with it still line up, while no printed\n")
        fh.write("number leaves the gateway.\n\n")
        fh.write("| | table | cols | rows total | sampled | span |\n")
        fh.write("|---|---|---:|---:|---:|---|\n")
        for kind, name, ncols, total, got, span, red in index:
            tag = "view" if kind == "view" else ""
            star = " 🔒" if red else ""
            fh.write(f"| {tag} | `{name}`{star} | {ncols} | {total:,} | "
                     f"{got} | {span} |\n")
        fh.write("\n🔒 = carries a redacted column.\n")
    print(f"\nwrote {len(index)} files and README.md to {out}")

    try:
        rel = out.relative_to(repo)
    except ValueError:
        rel = None          # --dir pointed outside the repository

    if args.push and rel is None:
        print("\n--push needs the output inside the repository; "
              f"{out} is not. Nothing was committed.", file=sys.stderr)
        return 2
    if args.push:
        subprocess.run(["git", "-C", str(repo), "add", str(rel)], check=False)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m",
                        f"SQL samples ({_utc_stamp()})"], check=False)
        for i in range(1, 5):
            if subprocess.run(["git", "-C", str(repo), "push", "-u", "origin",
                               "HEAD"], check=False).returncode == 0:
                break
            print(f"push failed, retry {i}")
            import time as _t
            _t.sleep(2 ** i)
    elif rel is not None:
        print(f"\nTo put it on GitHub:\n\n    cd {repo}\n"
              f"    git add {rel}\n"
              f"    git commit -m 'SQL samples'\n"
              f"    git push -u origin $(git branch --show-current)\n\n"
              f"Or re-run with --push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
