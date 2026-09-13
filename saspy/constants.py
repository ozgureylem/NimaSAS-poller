"""SAS 6.02 command codes and wire-format constants actually implemented.

Only long polls with a real, spec-checked implementation in client.py are
named here. See README.md / MIGRATION.md for the full coverage map (what's
implemented vs. stubbed vs. not started) inherited from the legacy/sas.py
review.
"""

from __future__ import annotations

import enum

BROADCAST_ADDRESS = 0x00
MIN_ADDRESS = 0x01
MAX_ADDRESS = 0x7F

GENERAL_POLL_BIT = 0x80  # ORed with address for a general poll (spec 2.2.1)


class LongPoll(enum.IntEnum):
    """Long poll command codes. Comment gives the SAS 6.02 table reference."""

    SHUTDOWN = 0x01  # 6.2.1... (type S, no data)
    STARTUP = 0x02
    SOUND_OFF = 0x03
    SOUND_ON = 0x04
    REEL_SPIN_SOUNDS_DISABLE = 0x05
    ENABLE_BILL_ACCEPTOR = 0x06
    DISABLE_BILL_ACCEPTOR = 0x07
    CONFIGURE_BILL_DENOM = 0x08

    SEND_METERS_10_THROUGH_15 = 0x0F  # Table 7.2

    SEND_CURRENT_CREDITS = 0x1A
    SEND_METERS_11_THROUGH_15 = 0x1C  # "Send Total Meters" style aggregate

    SEND_GAMING_MACHINE_ID = 0x1F  # Table 7.10
    SEND_TOTAL_DOLLAR_VALUE_OF_BILLS = 0x20
    ROM_SIGNATURE_VERIFICATION = 0x21  # Table 6.2a/6.2b

    SEND_PENDING_CASHOUT_INFO = 0x57  # placeholder name kept for continuity
    SEND_VALIDATION_NUMBER = 0x58  # Table 15.9b/15.9c ("Receive validation number")

    SEND_TICKET_VALIDATION_DATA = 0x70  # Table 15.11a
    REDEEM_TICKET = 0x71  # Table 15.12a/15.12b

    AFT_TRANSFER_FUNDS = 0x72  # Table 8.3a/8.3c
    AFT_REGISTER_GAMING_MACHINE = 0x73  # Table 8.1a/8.1b
    AFT_GAME_LOCK_AND_STATUS = 0x74  # Table 8.2a/8.2b

    SEND_ENABLED_FEATURES = 0xA0  # Table 7.14a/7.14b


class PollType(enum.Enum):
    """Section 2.2.2: whether the *command* itself carries a CRC.

    Type R commands are a bare address+command byte with no CRC (the
    *response* still has one). Type S/M/G commands always carry a CRC.
    """

    R = "R"  # read-only, no CRC on the outbound command
    S = "S"  # set/configure, CRC required on the outbound command
    M = "M"  # per-game variant of S, CRC required, carries a game number
    G = "G"  # broadcast, CRC required, no ACK/NACK


LONG_POLL_TYPES: dict[LongPoll, PollType] = {
    LongPoll.SHUTDOWN: PollType.S,
    LongPoll.STARTUP: PollType.S,
    LongPoll.SOUND_OFF: PollType.S,
    LongPoll.SOUND_ON: PollType.S,
    LongPoll.REEL_SPIN_SOUNDS_DISABLE: PollType.S,
    LongPoll.ENABLE_BILL_ACCEPTOR: PollType.S,
    LongPoll.DISABLE_BILL_ACCEPTOR: PollType.S,
    LongPoll.CONFIGURE_BILL_DENOM: PollType.S,
    LongPoll.SEND_METERS_10_THROUGH_15: PollType.R,
    LongPoll.SEND_CURRENT_CREDITS: PollType.R,
    LongPoll.SEND_METERS_11_THROUGH_15: PollType.R,
    LongPoll.SEND_GAMING_MACHINE_ID: PollType.R,
    LongPoll.SEND_TOTAL_DOLLAR_VALUE_OF_BILLS: PollType.R,
    LongPoll.ROM_SIGNATURE_VERIFICATION: PollType.S,
    LongPoll.SEND_PENDING_CASHOUT_INFO: PollType.R,
    LongPoll.SEND_VALIDATION_NUMBER: PollType.S,
    LongPoll.SEND_TICKET_VALIDATION_DATA: PollType.R,
    LongPoll.REDEEM_TICKET: PollType.S,
    LongPoll.AFT_TRANSFER_FUNDS: PollType.S,
    LongPoll.AFT_REGISTER_GAMING_MACHINE: PollType.S,
    LongPoll.AFT_GAME_LOCK_AND_STATUS: PollType.S,
    LongPoll.SEND_ENABLED_FEATURES: PollType.M,
}
