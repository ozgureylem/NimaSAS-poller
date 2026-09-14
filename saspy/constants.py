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

    SEND_SELECTED_METERS = 0x2F  # Table 7.3a/7.3b ("Send Selected Meters for Game N")
    SEND_EXTENDED_METERS = 0x6F  # Table 7.21a/7.21b ("Send Extended Meters for Game N")

    SET_SECURE_ENHANCED_VALIDATION_ID = 0x4C  # Table 15.6a/15.6b
    SEND_ENHANCED_VALIDATION_INFORMATION = 0x4D  # Table 15.10a/15.10b (ticket-out history)

    SEND_SAS_VERSION_AND_SERIAL = 0x54  # Table 7.15

    SEND_PENDING_CASHOUT_INFO = 0x57  # Table 15.7a ("Send Pending Cashout Information")
    SEND_VALIDATION_NUMBER = 0x58  # Table 15.9b/15.9c ("Receive validation number")

    SEND_TICKET_VALIDATION_DATA = 0x70  # Table 15.11a
    REDEEM_TICKET = 0x71  # Table 15.12a/15.12b

    EXTENDED_VALIDATION_STATUS = 0x7B  # Table 15.2a/15.2b

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


class MeterCode(enum.IntEnum):
    """Selected-meter codes for LP 2F/6F (Table C-7, Appendix C). Only the
    codes this client actually verified against the spec table are named
    here — Table C-7 runs to roughly 190 codes, most irrelevant to this
    project, and a wrong meter code is a silent wrong-answer bug (the
    machine responds normally; it's just the wrong number), so codes are
    added here only once checked against the table, not guessed from a
    plausible-looking name.

    CORRECTION: an earlier project planning document (Technical v3 draft,
    §8.2) named these as 0x1A/0x1B (cashable) and 0x35-0x3C (restricted/
    non-restricted promo). Table C-7 does not support that: 0x1A is "Total
    nonrestricted amount played" and 0x1B is "Current restricted credits" —
    unrelated meters. The values below are transcribed directly from Table
    C-7 and are what this module actually uses.
    """

    # "Total SAS cashable ticket in, including nonrestricted tickets"
    # — the pair D-01's testable prediction (Cashable Tickets In vs.
    # recorded redemptions) depends on.
    CASHABLE_TICKET_IN_CENTS = 0x0D  # 5 BCD; [same as 0080 + 0084]
    CASHABLE_TICKET_IN_QUANTITY = 0x11  # 4 BCD; [same as 0081 + 0085]

    RESTRICTED_TICKET_IN_CENTS = 0x0F  # 5 BCD; [same as 0082]
    RESTRICTED_TICKET_IN_QUANTITY = 0x13  # 4 BCD; [same as 0083]

    # "Total SAS cashable ticket out, including debit tickets"
    CASHABLE_TICKET_OUT_CENTS = 0x0E  # 5 BCD; [same as 0086 + 008A]
    CASHABLE_TICKET_OUT_QUANTITY = 0x12  # 4 BCD; [same as 0087 + 008B]

    RESTRICTED_TICKET_OUT_CENTS = 0x10  # 5 BCD; [same as 0088]
    RESTRICTED_TICKET_OUT_QUANTITY = 0x14  # 4 BCD; [same as 0089]


# LP 2F ("Send Selected Meters") reports each meter using Table C-7's own
# "Min Size" column, which is NOT the same for every meter — unlike LP 6F,
# LP 2F's response carries no per-meter size byte, so the reader has to
# already know each meter's size to parse the response at all (§7.3). This
# table is therefore only as complete as MeterCode above; send_selected_meters()
# raises rather than guess a size for a code not listed here.
METER_CODE_SIZES_BCD: dict[int, int] = {
    MeterCode.CASHABLE_TICKET_IN_CENTS: 5,
    MeterCode.CASHABLE_TICKET_IN_QUANTITY: 4,
    MeterCode.RESTRICTED_TICKET_IN_CENTS: 5,
    MeterCode.RESTRICTED_TICKET_IN_QUANTITY: 4,
    MeterCode.CASHABLE_TICKET_OUT_CENTS: 5,
    MeterCode.CASHABLE_TICKET_OUT_QUANTITY: 4,
    MeterCode.RESTRICTED_TICKET_OUT_CENTS: 5,
    MeterCode.RESTRICTED_TICKET_OUT_QUANTITY: 4,
}


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
    LongPoll.SEND_SELECTED_METERS: PollType.M,  # "type M command" per §7.3
    LongPoll.SEND_EXTENDED_METERS: PollType.M,  # "type M long poll 6F" per §7.21
    LongPoll.SET_SECURE_ENHANCED_VALIDATION_ID: PollType.S,  # "type S long poll" per §15.6
    LongPoll.SEND_ENHANCED_VALIDATION_INFORMATION: PollType.S,  # "type S long poll" per §15.10
    LongPoll.SEND_SAS_VERSION_AND_SERIAL: PollType.R,
    LongPoll.SEND_PENDING_CASHOUT_INFO: PollType.R,
    LongPoll.SEND_VALIDATION_NUMBER: PollType.S,
    LongPoll.SEND_TICKET_VALIDATION_DATA: PollType.R,
    LongPoll.REDEEM_TICKET: PollType.S,
    LongPoll.EXTENDED_VALIDATION_STATUS: PollType.S,  # "type S long poll 7B" per §15.2 (addressed form only; type G broadcast not implemented)
    LongPoll.AFT_TRANSFER_FUNDS: PollType.S,
    LongPoll.AFT_REGISTER_GAMING_MACHINE: PollType.S,
    LongPoll.AFT_GAME_LOCK_AND_STATUS: PollType.S,
    LongPoll.SEND_ENABLED_FEATURES: PollType.M,
}
