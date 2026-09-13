import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.sql_poll_logger import SCHEMA, poll_and_log
from saspy.exceptions import SASTimeoutError
from saspy.models import BasicMeters


class FakeClientOK:
    def send_meters_10_through_15(self) -> BasicMeters:
        return BasicMeters(
            total_cancelled_credits=1,
            total_coin_in=2,
            total_coin_out=3,
            total_drop=4,
            total_jackpot=5,
            games_played=6,
        )


class FakeClientFailing:
    def send_meters_10_through_15(self):
        raise SASTimeoutError("no response")


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def test_poll_and_log_writes_meter_snapshot_on_success():
    conn = make_db()
    poll_and_log(FakeClientOK(), conn)
    rows = conn.execute(
        "SELECT total_coin_in, total_coin_out, games_played, synced_at FROM meter_snapshots"
    ).fetchall()
    assert rows == [(2, 3, 6, None)]
    assert conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0] == 0


def test_poll_and_log_writes_poll_error_on_failure_and_does_not_raise():
    conn = make_db()
    poll_and_log(FakeClientFailing(), conn)
    rows = conn.execute(
        "SELECT poll_name, error_type, message FROM poll_errors"
    ).fetchall()
    assert rows == [("send_meters_10_through_15", "SASTimeoutError", "no response")]
    assert conn.execute("SELECT COUNT(*) FROM meter_snapshots").fetchone()[0] == 0


def test_poll_and_log_one_failure_does_not_stop_subsequent_polls():
    conn = make_db()
    poll_and_log(FakeClientFailing(), conn)
    poll_and_log(FakeClientOK(), conn)
    assert conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM meter_snapshots").fetchone()[0] == 1
