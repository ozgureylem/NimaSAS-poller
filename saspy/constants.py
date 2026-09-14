"""SAS 6.02 command codes and wire-format constants actually implemented.

Only long polls with a real, spec-checked implementation in client.py are
named here. See README.md / MIGRATION.md for the full coverage map (what's
implemented vs. stubbed vs. not started) inherited from the legacy/sas.py
review.

Coverage scope, as of the pass that closed out Appendix B (Table B-1 —
the spec's own complete long-poll list) against this module: every
data-returning long poll relevant to a TITO/AFT/meters gateway is here.
Deliberately not implemented: pure command/control polls with no data to
pull (shutdown, sound on/off, bill acceptor enable/disable, maintenance
mode, and similar — see the public repo's README for the full list) as a
distinct, lower-priority piece of work; progressive/tournament/legacy-
bonus/card-reel-stop data as out of this project's stated scope; long
poll 0x8B specifically because the spec itself recommends against
implementing it (multiplied jackpots). Component authentication (0x6E)
and the multi-denom preamble (0xB0) are real gaps, not scoped out.
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
    # NAME CORRECTION: this was previously (incorrectly) named
    # SEND_METERS_11_THROUGH_15. Appendix B shows the real "Send meters 11
    # through 15" is 0x19 (Table 7.2b, 5 fields) — a different, smaller
    # command. 0x1C is Table 7.2c's "Send meters" (8 fields: adds games won,
    # slot door opened, power reset). The wire code and response parsing
    # were always correct; only the name borrowed the wrong table's title.
    SEND_METERS_EXTENDED_GROUP = 0x1C  # Table 7.2c ("Send meters")

    SEND_GAMING_MACHINE_ID = 0x1F  # Table 7.10
    SEND_TOTAL_DOLLAR_VALUE_OF_BILLS = 0x20
    ROM_SIGNATURE_VERIFICATION = 0x21  # Table 6.2a/6.2b

    # -- Single-meter reads (Appendix B, Table 7.1) — all Type R, no length
    # byte, a bare N-byte BCD value. See SIMPLE_METER_WIDTH_BCD below; read
    # with SASClient.send_meter(). Individually redundant with LP 0x0F for
    # the six it overlaps, but each is a real, addressable code in its own
    # right — kept so nothing in Appendix B's meter range is unreachable.
    SEND_TOTAL_CANCELLED_CREDITS_METER = 0x10
    SEND_TOTAL_COIN_IN_METER = 0x11
    SEND_TOTAL_COIN_OUT_METER = 0x12
    SEND_TOTAL_DROP_METER = 0x13
    SEND_TOTAL_JACKPOT_METER = 0x14
    SEND_GAMES_PLAYED_METER = 0x15
    SEND_GAMES_WON_METER = 0x16
    SEND_GAMES_LOST_METER = 0x17

    SEND_GAMES_SINCE_POWER_UP_AND_DOOR_CLOSURE = 0x18  # Table 7.7 (two 2-byte BCD fields, not the simple-meter shape)
    SEND_METERS_11_THROUGH_15 = 0x19  # Table 7.2b — the real "meters 11-15" (see SEND_METERS_GROUP_1C below)

    HANDPAY_INFORMATION = 0x1B  # Table 7.8
    TOTAL_BILL_METERS = 0x1E  # Table 7.1d-style aggregate, 6 x 4-byte BCD ($1/$5/$10/$20/$50/$100)

    SEND_TRUE_COIN_IN = 0x2A
    SEND_TRUE_COIN_OUT = 0x2B
    SEND_CURRENT_HOPPER_LEVEL = 0x2C
    SEND_TOTAL_HAND_PAID_CANCELLED_CREDITS = 0x2D  # Table 7.6.2a/7.6.2b (type M, per game_number)

    # Bill-in-meters (Appendix B rows 31-45) — named by denomination, since
    # "bills in" alone can't disambiguate 12+ near-identical codes.
    SEND_BILLS_IN_METER_1 = 0x31
    SEND_BILLS_IN_METER_2 = 0x32
    SEND_BILLS_IN_METER_5 = 0x33
    SEND_BILLS_IN_METER_10 = 0x34
    SEND_BILLS_IN_METER_20 = 0x35
    SEND_BILLS_IN_METER_50 = 0x36
    SEND_BILLS_IN_METER_100 = 0x37
    SEND_BILLS_IN_METER_500 = 0x38
    SEND_BILLS_IN_METER_1000 = 0x39
    SEND_BILLS_IN_METER_200 = 0x3A
    SEND_BILLS_IN_METER_25 = 0x3B
    SEND_BILLS_IN_METER_2000 = 0x3C

    SEND_CASH_OUT_TICKET_INFORMATION = 0x3D  # Table 15.5

    SEND_BILLS_IN_METER_2500 = 0x3E
    SEND_BILLS_IN_METER_5000 = 0x3F
    SEND_BILLS_IN_METER_10000 = 0x40
    SEND_BILLS_IN_METER_20000 = 0x41
    SEND_BILLS_IN_METER_25000 = 0x42
    SEND_BILLS_IN_METER_50000 = 0x43
    SEND_BILLS_IN_METER_100000 = 0x44
    SEND_BILLS_IN_METER_250 = 0x45

    SEND_CREDIT_AMOUNT_OF_ALL_BILLS_ACCEPTED = 0x46
    SEND_COIN_AMOUNT_FROM_EXTERNAL_ACCEPTOR = 0x47
    SEND_BILLS_IN_STACKER_COUNT = 0x49
    SEND_BILLS_IN_STACKER_CREDIT_AMOUNT = 0x4A

    SEND_CURRENT_HOPPER_STATUS = 0x4F  # Table 7.19a/7.19b

    SEND_TOTAL_GAMES_IMPLEMENTED = 0x51  # Table 7.6.3 (2-byte BCD)
    SEND_GAME_N_METERS = 0x52  # Table 7.6.4a/7.6.4b (type M)
    SEND_GAME_N_CONFIGURATION = 0x53  # Table 7.6.5a/7.6.5b (type M)

    SEND_SELECTED_METERS = 0x2F  # Table 7.3a/7.3b ("Send Selected Meters for Game N")
    SEND_EXTENDED_METERS = 0x6F  # Table 7.21a/7.21b ("Send Extended Meters for Game N")
    SEND_EXTENDED_METERS_ALTERNATE = 0xAF  # same wire shape as 0x6F, alternate code for consecutive-poll ACK purposes (§7.21)

    SEND_SELECTED_GAME_NUMBER = 0x55  # Table 7.6.6 (2-byte BCD)
    SEND_ENABLED_GAME_NUMBERS = 0x56  # Table 7.6.7 (variable-length list)

    SEND_CURRENT_DATE_AND_TIME = 0x7E  # Table B-1 row 7E
    SEND_PHYSICAL_REEL_STOP_INFORMATION = 0x8F  # Table 7.13

    SEND_TOKEN_DENOMINATION = 0xB3  # Table 7.22
    SEND_WAGER_CATEGORY_INFORMATION = 0xB4  # Table 7.24a/7.24b (type M)
    SEND_EXTENDED_GAME_N_INFORMATION = 0xB5  # Table 7.23a/7.23b (type M)

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


class ExceptionCode(enum.IntEnum):
    """General-poll exception codes (Appendix A) — only the ones this
    client's examples actually act on. 0x00 (no event) plus the five
    priority exceptions that drive ticket/cashout capture in
    examples/sql_poll_logger.py.
    """

    NONE = 0x00
    CASH_OUT_TICKET_PRINTED = 0x3D  # §15.10 — read via LP 4D (Send Enhanced Validation Information)
    HANDPAY_VALIDATED = 0x3E  # §15.10 — same read as above (spec: "functionally equivalent" to 3D)
    SYSTEM_VALIDATION_REQUEST = 0x57  # §15.7 — read via LP 57 (Send Pending Cashout Information), answered via LP 58
    TICKET_INSERTED = 0x67  # §15.11 — read via LP 70 (Send Ticket Validation Data)
    TICKET_TRANSFER_COMPLETE = 0x68  # §15.12 — read via LP 71/FF (redeem_ticket_status())


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


# Simple single-meter reads (Table 7.1-style: Type R, bare address+command
# request, response is address+command+N-byte-BCD-value+CRC, no length
# byte). Read any of these with SASClient.send_meter(). Width is in BCD
# bytes (2 digits/byte), per each code's own row in Appendix B.
SIMPLE_METER_WIDTH_BCD: dict[LongPoll, int] = {
    LongPoll.SEND_TOTAL_CANCELLED_CREDITS_METER: 4,
    LongPoll.SEND_TOTAL_COIN_IN_METER: 4,
    LongPoll.SEND_TOTAL_COIN_OUT_METER: 4,
    LongPoll.SEND_TOTAL_DROP_METER: 4,
    LongPoll.SEND_TOTAL_JACKPOT_METER: 4,
    LongPoll.SEND_GAMES_PLAYED_METER: 4,
    LongPoll.SEND_GAMES_WON_METER: 4,
    LongPoll.SEND_GAMES_LOST_METER: 4,
    LongPoll.SEND_CURRENT_CREDITS: 4,
    LongPoll.SEND_TOTAL_DOLLAR_VALUE_OF_BILLS: 4,
    LongPoll.SEND_TRUE_COIN_IN: 4,
    LongPoll.SEND_TRUE_COIN_OUT: 4,
    LongPoll.SEND_CURRENT_HOPPER_LEVEL: 4,
    LongPoll.SEND_BILLS_IN_METER_1: 4,
    LongPoll.SEND_BILLS_IN_METER_2: 4,
    LongPoll.SEND_BILLS_IN_METER_5: 4,
    LongPoll.SEND_BILLS_IN_METER_10: 4,
    LongPoll.SEND_BILLS_IN_METER_20: 4,
    LongPoll.SEND_BILLS_IN_METER_50: 4,
    LongPoll.SEND_BILLS_IN_METER_100: 4,
    LongPoll.SEND_BILLS_IN_METER_500: 4,
    LongPoll.SEND_BILLS_IN_METER_1000: 4,
    LongPoll.SEND_BILLS_IN_METER_200: 4,
    LongPoll.SEND_BILLS_IN_METER_25: 4,
    LongPoll.SEND_BILLS_IN_METER_2000: 4,
    LongPoll.SEND_BILLS_IN_METER_2500: 4,
    LongPoll.SEND_BILLS_IN_METER_5000: 4,
    LongPoll.SEND_BILLS_IN_METER_10000: 4,
    LongPoll.SEND_BILLS_IN_METER_20000: 4,
    LongPoll.SEND_BILLS_IN_METER_25000: 4,
    LongPoll.SEND_BILLS_IN_METER_50000: 4,
    LongPoll.SEND_BILLS_IN_METER_100000: 4,
    LongPoll.SEND_BILLS_IN_METER_250: 4,
    LongPoll.SEND_CREDIT_AMOUNT_OF_ALL_BILLS_ACCEPTED: 4,
    LongPoll.SEND_COIN_AMOUNT_FROM_EXTERNAL_ACCEPTOR: 4,
    LongPoll.SEND_BILLS_IN_STACKER_COUNT: 4,
    LongPoll.SEND_BILLS_IN_STACKER_CREDIT_AMOUNT: 4,
    LongPoll.SEND_TOTAL_GAMES_IMPLEMENTED: 2,
    LongPoll.SEND_SELECTED_GAME_NUMBER: 2,
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
    LongPoll.SEND_METERS_EXTENDED_GROUP: PollType.R,
    LongPoll.SEND_METERS_11_THROUGH_15: PollType.R,
    LongPoll.SEND_GAMING_MACHINE_ID: PollType.R,
    LongPoll.SEND_TOTAL_DOLLAR_VALUE_OF_BILLS: PollType.R,
    LongPoll.ROM_SIGNATURE_VERIFICATION: PollType.S,
    **{poll: PollType.R for poll in SIMPLE_METER_WIDTH_BCD},
    LongPoll.SEND_GAMES_SINCE_POWER_UP_AND_DOOR_CLOSURE: PollType.R,  # "message format identical to Table 7.1a" (§7.7)
    LongPoll.HANDPAY_INFORMATION: PollType.R,
    LongPoll.TOTAL_BILL_METERS: PollType.R,
    LongPoll.SEND_TOTAL_HAND_PAID_CANCELLED_CREDITS: PollType.M,
    LongPoll.SEND_CASH_OUT_TICKET_INFORMATION: PollType.R,
    LongPoll.SEND_CURRENT_HOPPER_STATUS: PollType.R,
    LongPoll.SEND_GAME_N_METERS: PollType.M,
    LongPoll.SEND_GAME_N_CONFIGURATION: PollType.M,
    LongPoll.SEND_SELECTED_METERS: PollType.M,  # "type M command" per §7.3
    LongPoll.SEND_EXTENDED_METERS: PollType.M,  # "type M long poll 6F" per §7.21
    LongPoll.SEND_EXTENDED_METERS_ALTERNATE: PollType.M,
    LongPoll.SEND_ENABLED_GAME_NUMBERS: PollType.R,
    LongPoll.SEND_CURRENT_DATE_AND_TIME: PollType.R,
    LongPoll.SEND_PHYSICAL_REEL_STOP_INFORMATION: PollType.R,
    LongPoll.SEND_TOKEN_DENOMINATION: PollType.R,
    LongPoll.SEND_WAGER_CATEGORY_INFORMATION: PollType.M,
    LongPoll.SEND_EXTENDED_GAME_N_INFORMATION: PollType.M,
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
