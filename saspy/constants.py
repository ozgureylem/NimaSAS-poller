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
    SEND_LAST_ACCEPTED_BILL_INFORMATION = 0x48  # Table 7.11 -- most-recently-accepted bill only; some older machines don't support it at all (see its own docstring)
    SEND_BILLS_IN_STACKER_COUNT = 0x49
    SEND_BILLS_IN_STACKER_CREDIT_AMOUNT = 0x4A

    SEND_CURRENT_HOPPER_STATUS = 0x4F  # Table 7.19a/7.19b
    SEND_VALIDATION_METERS = 0x50  # Table 15.13a/15.13b -- redundant with Table C-7 codes 0x80+ per the spec's own note

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
    """Selected-meter codes for LP 2F/6F (Table C-7, Appendix C).

    Essentially the complete table: every assigned code from 0x0000
    through 0x00BD (the core/extended/bill-denomination range, the
    SAS-validation-specific range, and the AFT-specific range), skipping
    only the reserved gaps (0x003A-0x003D, 0x0079-0x007E, 0x0094-0x009F,
    0x00B2-0x00B7) and 0x00BE-0xFFFF ("reserved for future use" per the
    table's own closing note). Transcribed directly from the spec's
    Table C-7, not from any secondary source — a wrong meter code is a
    silent wrong-answer bug (the machine responds normally; it's just
    the wrong number), so every entry here was checked against the
    table, never guessed from a plausible-looking name.

    A handful of codes are intentionally not aliased to each other even
    where the table cross-references them as equivalent (e.g. 0x000F
    "Total SAS restricted ticket in (cents) [same as meter 0082]") —
    each SAS code is kept as its own named member (see
    VALIDATION_RESTRICTED_TICKET_IN_CENTS for 0x0082's own name,
    disambiguated from RESTRICTED_TICKET_IN_CENTS at 0x000F, which
    would otherwise collide) rather than collapsed to one Python name,
    because polling both independently is exactly the kind of
    redundant cross-check this project's tooling deliberately keeps —
    see sql_poll_logger.py's module docstring.

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

    # --- Core meters (Table C-7, 0x00-0x0C) ---
    TOTAL_COIN_IN_CREDITS = 0x00  # Total coin in credits
    TOTAL_COIN_OUT_CREDITS = 0x01  # Total coin out credits
    TOTAL_JACKPOT_CREDITS = 0x02  # Total jackpot credits
    TOTAL_HAND_PAID_CANCELLED_CREDITS = 0x03  # Total hand paid cancelled credits
    TOTAL_CANCELLED_CREDITS = 0x04  # Total cancelled credits
    GAMES_PLAYED = 0x05  # Games played
    GAMES_WON = 0x06  # Games won
    GAMES_LOST = 0x07  # Games lost
    TOTAL_CREDITS_FROM_COIN_ACCEPTOR = 0x08  # Total credits from coin acceptor
    TOTAL_CREDITS_PAID_FROM_HOPPER = 0x09  # Total credits paid from hopper
    TOTAL_CREDITS_FROM_COINS_TO_DROP = 0x0A  # Total credits from coins to drop
    TOTAL_CREDITS_FROM_BILLS_ACCEPTED = 0x0B  # Total credits from bills accepted
    CURRENT_CREDITS = 0x0C  # Current credits

    # --- Extended/electronic-transfer meters (Table C-7, 0x15-0x39) ---
    TOTAL_TICKET_IN_CREDITS = 0x15  # Total ticket in credits
    TOTAL_TICKET_OUT_CREDITS = 0x16  # Total ticket out credits
    TOTAL_ELECTRONIC_TRANSFERS_TO_GAMING_MACHINE_CREDITS = 0x17  # Total electronic transfers to gaming machine credits
    TOTAL_ELECTRONIC_TRANSFERS_TO_HOST_CREDITS = 0x18  # Total electronic transfers to host credits
    TOTAL_RESTRICTED_AMOUNT_PLAYED_CREDITS = 0x19  # Total restricted amount played credits
    TOTAL_NONRESTRICTED_AMOUNT_PLAYED_CREDITS = 0x1A  # Total nonrestricted amount played credits
    CURRENT_RESTRICTED_CREDITS = 0x1B  # Current restricted credits
    TOTAL_MACHINE_PAID_PAYTABLE_WIN_CREDITS = 0x1C  # Total machine paid paytable win credits
    TOTAL_MACHINE_PAID_PROGRESSIVE_WIN_CREDITS = 0x1D  # Total machine paid progressive win credits
    TOTAL_MACHINE_PAID_EXTERNAL_BONUS_WIN_CREDITS = 0x1E  # Total machine paid external bonus win credits
    TOTAL_ATTENDANT_PAID_PAYTABLE_WIN_CREDITS = 0x1F  # Total attendant paid paytable win credits
    TOTAL_ATTENDANT_PAID_PROGRESSIVE_WIN_CREDITS = 0x20  # Total attendant paid progressive win credits
    TOTAL_ATTENDANT_PAID_EXTERNAL_BONUS_WIN_CREDITS = 0x21  # Total attendant paid external bonus win credits
    TOTAL_WON_CREDITS = 0x22  # Total won credits
    TOTAL_HAND_PAID_CREDITS = 0x23  # Total hand paid credits
    TOTAL_DROP_CREDITS = 0x24  # Total drop credits
    GAMES_SINCE_LAST_POWER_RESET = 0x25  # Games since last power reset
    GAMES_SINCE_SLOT_DOOR_CLOSURE = 0x26  # Games since slot door closure
    TOTAL_CREDITS_FROM_EXTERNAL_COIN_ACCEPTOR = 0x27  # Total credits from external coin acceptor
    TOTAL_CASHABLE_TICKET_IN_CREDITS = 0x28  # Total cashable ticket in credits
    TOTAL_REGULAR_CASHABLE_TICKET_IN_CREDITS = 0x29  # Total regular cashable ticket in credits
    TOTAL_RESTRICTED_PROMOTIONAL_TICKET_IN_CREDITS = 0x2A  # Total restricted promotional ticket in credits
    TOTAL_NONRESTRICTED_PROMOTIONAL_TICKET_IN_CREDITS = 0x2B  # Total nonrestricted promotional ticket in credits
    TOTAL_CASHABLE_TICKET_OUT_CREDITS = 0x2C  # Total cashable ticket out credits
    TOTAL_RESTRICTED_PROMOTIONAL_TICKET_OUT_CREDITS = 0x2D  # Total restricted promotional ticket out credits
    ELECTRONIC_REGULAR_CASHABLE_TRANSFERS_TO_GAMING_MACHINE_CREDITS = 0x2E  # Electronic regular cashable transfers to gaming machine credits
    ELECTRONIC_RESTRICTED_PROMOTIONAL_TRANSFERS_TO_GAMING_MACHINE_CREDITS = 0x2F  # Electronic restricted promotional transfers to gaming machine credits
    ELECTRONIC_NONRESTRICTED_PROMOTIONAL_TRANSFERS_TO_GAMING_MACHINE_CREDITS = 0x30  # Electronic nonrestricted promotional transfers to gaming machine credits
    ELECTRONIC_DEBIT_TRANSFERS_TO_GAMING_MACHINE_CREDITS = 0x31  # Electronic debit transfers to gaming machine credits
    ELECTRONIC_REGULAR_CASHABLE_TRANSFERS_TO_HOST_CREDITS = 0x32  # Electronic regular cashable transfers to host credits
    ELECTRONIC_RESTRICTED_PROMOTIONAL_TRANSFERS_TO_HOST_CREDITS = 0x33  # Electronic restricted promotional transfers to host credits
    ELECTRONIC_NONRESTRICTED_PROMOTIONAL_TRANSFERS_TO_HOST_CREDITS = 0x34  # Electronic nonrestricted promotional transfers to host credits
    TOTAL_REGULAR_CASHABLE_TICKET_IN_QUANTITY = 0x35  # Total regular cashable ticket in quantity
    TOTAL_RESTRICTED_PROMOTIONAL_TICKET_IN_QUANTITY = 0x36  # Total restricted promotional ticket in quantity
    TOTAL_NONRESTRICTED_PROMOTIONAL_TICKET_IN_QUANTITY = 0x37  # Total nonrestricted promotional ticket in quantity
    TOTAL_CASHABLE_TICKET_OUT_QUANTITY = 0x38  # Total cashable ticket out quantity
    TOTAL_RESTRICTED_PROMOTIONAL_TICKET_OUT_QUANTITY = 0x39  # Total restricted promotional ticket out quantity
    NUMBER_OF_BILLS_CURRENTLY_IN_STACKER = 0x3E  # Number of bills currently in stacker
    TOTAL_VALUE_OF_BILLS_CURRENTLY_IN_STACKER_CREDITS = 0x3F  # Total value of bills currently in stacker credits

    # --- Bill-acceptor meters by denomination (Table C-7, 0x3E-0x7F) ---
    TOTAL_NUMBER_OF_1_00_BILLS_ACCEPTED = 0x40  # Total number of $1.00 bills accepted
    TOTAL_NUMBER_OF_2_00_BILLS_ACCEPTED = 0x41  # Total number of $2.00 bills accepted
    TOTAL_NUMBER_OF_5_00_BILLS_ACCEPTED = 0x42  # Total number of $5.00 bills accepted
    TOTAL_NUMBER_OF_10_00_BILLS_ACCEPTED = 0x43  # Total number of $10.00 bills accepted
    TOTAL_NUMBER_OF_20_00_BILLS_ACCEPTED = 0x44  # Total number of $20.00 bills accepted
    TOTAL_NUMBER_OF_25_00_BILLS_ACCEPTED = 0x45  # Total number of $25.00 bills accepted
    TOTAL_NUMBER_OF_50_00_BILLS_ACCEPTED = 0x46  # Total number of $50.00 bills accepted
    TOTAL_NUMBER_OF_100_00_BILLS_ACCEPTED = 0x47  # Total number of $100.00 bills accepted
    TOTAL_NUMBER_OF_200_00_BILLS_ACCEPTED = 0x48  # Total number of $200.00 bills accepted
    TOTAL_NUMBER_OF_250_00_BILLS_ACCEPTED = 0x49  # Total number of $250.00 bills accepted
    TOTAL_NUMBER_OF_500_00_BILLS_ACCEPTED = 0x4A  # Total number of $500.00 bills accepted
    TOTAL_NUMBER_OF_1_000_00_BILLS_ACCEPTED = 0x4B  # Total number of $1,000.00 bills accepted
    TOTAL_NUMBER_OF_2_000_00_BILLS_ACCEPTED = 0x4C  # Total number of $2,000.00 bills accepted
    TOTAL_NUMBER_OF_2_500_00_BILLS_ACCEPTED = 0x4D  # Total number of $2,500.00 bills accepted
    TOTAL_NUMBER_OF_5_000_00_BILLS_ACCEPTED = 0x4E  # Total number of $5,000.00 bills accepted
    TOTAL_NUMBER_OF_10_000_00_BILLS_ACCEPTED = 0x4F  # Total number of $10,000.00 bills accepted
    TOTAL_NUMBER_OF_20_000_00_BILLS_ACCEPTED = 0x50  # Total number of $20,000.00 bills accepted
    TOTAL_NUMBER_OF_25_000_00_BILLS_ACCEPTED = 0x51  # Total number of $25,000.00 bills accepted
    TOTAL_NUMBER_OF_50_000_00_BILLS_ACCEPTED = 0x52  # Total number of $50,000.00 bills accepted
    TOTAL_NUMBER_OF_100_000_00_BILLS_ACCEPTED = 0x53  # Total number of $100,000.00 bills accepted
    TOTAL_NUMBER_OF_200_000_00_BILLS_ACCEPTED = 0x54  # Total number of $200,000.00 bills accepted
    TOTAL_NUMBER_OF_250_000_00_BILLS_ACCEPTED = 0x55  # Total number of $250,000.00 bills accepted
    TOTAL_NUMBER_OF_500_000_00_BILLS_ACCEPTED = 0x56  # Total number of $500,000.00 bills accepted
    TOTAL_NUMBER_OF_1_000_000_00_BILLS_ACCEPTED = 0x57  # Total number of $1,000,000.00 bills accepted
    TOTAL_CREDITS_FROM_BILLS_TO_DROP = 0x58  # Total credits from bills to drop
    TOTAL_NUMBER_OF_1_00_BILLS_TO_DROP = 0x59  # Total number of $1.00 bills to drop
    TOTAL_NUMBER_OF_2_00_BILLS_TO_DROP = 0x5A  # Total number of $2.00 bills to drop
    TOTAL_NUMBER_OF_5_00_BILLS_TO_DROP = 0x5B  # Total number of $5.00 bills to drop
    TOTAL_NUMBER_OF_10_00_BILLS_TO_DROP = 0x5C  # Total number of $10.00 bills to drop
    TOTAL_NUMBER_OF_20_00_BILLS_TO_DROP = 0x5D  # Total number of $20.00 bills to drop
    TOTAL_NUMBER_OF_50_00_BILLS_TO_DROP = 0x5E  # Total number of $50.00 bills to drop
    TOTAL_NUMBER_OF_100_00_BILLS_TO_DROP = 0x5F  # Total number of $100.00 bills to drop
    TOTAL_NUMBER_OF_200_00_BILLS_TO_DROP = 0x60  # Total number of $200.00 bills to drop
    TOTAL_NUMBER_OF_500_00_BILLS_TO_DROP = 0x61  # Total number of $500.00 bills to drop
    TOTAL_NUMBER_OF_1000_00_BILLS_TO_DROP = 0x62  # Total number of $1000.00 bills to drop
    TOTAL_CREDITS_FROM_BILLS_DIVERTED_TO_HOPPER = 0x63  # Total credits from bills diverted to hopper
    TOTAL_NUMBER_OF_1_00_BILLS_DIVERTED_TO_HOPPER = 0x64  # Total number of $1.00 bills diverted to hopper
    TOTAL_NUMBER_OF_2_00_BILLS_DIVERTED_TO_HOPPER = 0x65  # Total number of $2.00 bills diverted to hopper
    TOTAL_NUMBER_OF_5_00_BILLS_DIVERTED_TO_HOPPER = 0x66  # Total number of $5.00 bills diverted to hopper
    TOTAL_NUMBER_OF_10_00_BILLS_DIVERTED_TO_HOPPER = 0x67  # Total number of $10.00 bills diverted to hopper
    TOTAL_NUMBER_OF_20_00_BILLS_DIVERTED_TO_HOPPER = 0x68  # Total number of $20.00 bills diverted to hopper
    TOTAL_NUMBER_OF_50_00_BILLS_DIVERTED_TO_HOPPER = 0x69  # Total number of $50.00 bills diverted to hopper
    TOTAL_NUMBER_OF_100_00_BILLS_DIVERTED_TO_HOPPER = 0x6A  # Total number of $100.00 bills diverted to hopper
    TOTAL_NUMBER_OF_200_00_BILLS_DIVERTED_TO_HOPPER = 0x6B  # Total number of $200.00 bills diverted to hopper
    TOTAL_NUMBER_OF_500_00_BILLS_DIVERTED_TO_HOPPER = 0x6C  # Total number of $500.00 bills diverted to hopper
    TOTAL_NUMBER_OF_1000_00_BILLS_DIVERTED_TO_HOPPER = 0x6D  # Total number of $1000.00 bills diverted to hopper
    TOTAL_CREDITS_FROM_BILLS_DISPENSED_FROM_HOPPER = 0x6E  # Total credits from bills dispensed from hopper
    TOTAL_NUMBER_OF_1_00_BILLS_DISPENSED_FROM_HOPPER = 0x6F  # Total number of $1.00 bills dispensed from hopper
    TOTAL_NUMBER_OF_2_00_BILLS_DISPENSED_FROM_HOPPER = 0x70  # Total number of $2.00 bills dispensed from hopper
    TOTAL_NUMBER_OF_5_00_BILLS_DISPENSED_FROM_HOPPER = 0x71  # Total number of $5.00 bills dispensed from hopper
    TOTAL_NUMBER_OF_10_00_BILLS_DISPENSED_FROM_HOPPER = 0x72  # Total number of $10.00 bills dispensed from hopper
    TOTAL_NUMBER_OF_20_00_BILLS_DISPENSED_FROM_HOPPER = 0x73  # Total number of $20.00 bills dispensed from hopper
    TOTAL_NUMBER_OF_50_00_BILLS_DISPENSED_FROM_HOPPER = 0x74  # Total number of $50.00 bills dispensed from hopper
    TOTAL_NUMBER_OF_100_00_BILLS_DISPENSED_FROM_HOPPER = 0x75  # Total number of $100.00 bills dispensed from hopper
    TOTAL_NUMBER_OF_200_00_BILLS_DISPENSED_FROM_HOPPER = 0x76  # Total number of $200.00 bills dispensed from hopper
    TOTAL_NUMBER_OF_500_00_BILLS_DISPENSED_FROM_HOPPER = 0x77  # Total number of $500.00 bills dispensed from hopper
    TOTAL_NUMBER_OF_1000_00_BILLS_DISPENSED_FROM_HOPPER = 0x78  # Total number of $1000.00 bills dispensed from hopper
    WEIGHTED_AVERAGE_THEORETICAL_PAYBACK_PERCENTAGE = 0x7F  # Weighted average theoretical payback percentage

    # --- Validation-specific meters (Table C-7, 0x80-0x93) ---
    REGULAR_CASHABLE_TICKET_IN_CENTS = 0x80  # Regular cashable ticket in cents
    REGULAR_CASHABLE_TICKET_IN_QUANTITY = 0x81  # Regular cashable ticket in quantity
    VALIDATION_RESTRICTED_TICKET_IN_CENTS = 0x82  # Restricted ticket in cents
    VALIDATION_RESTRICTED_TICKET_IN_QUANTITY = 0x83  # Restricted ticket in quantity
    NONRESTRICTED_TICKET_IN_CENTS = 0x84  # Nonrestricted ticket in cents
    NONRESTRICTED_TICKET_IN_QUANTITY = 0x85  # Nonrestricted ticket in quantity
    REGULAR_CASHABLE_TICKET_OUT_CENTS = 0x86  # Regular cashable ticket out cents
    REGULAR_CASHABLE_TICKET_OUT_QUANTITY = 0x87  # Regular cashable ticket out quantity
    VALIDATION_RESTRICTED_TICKET_OUT_CENTS = 0x88  # Restricted ticket out cents
    VALIDATION_RESTRICTED_TICKET_OUT_QUANTITY = 0x89  # Restricted ticket out quantity
    DEBIT_TICKET_OUT_CENTS = 0x8A  # Debit ticket out cents
    DEBIT_TICKET_OUT_QUANTITY = 0x8B  # Debit ticket out quantity
    VALIDATED_CANCELLED_CREDIT_HANDPAY_RECEIPT_PRINTED_CENTS = 0x8C  # Validated cancelled credit handpay receipt printed cents
    VALIDATED_CANCELLED_CREDIT_HANDPAY_RECEIPT_PRINTED_QUANTITY = 0x8D  # Validated cancelled credit handpay receipt printed quantity
    VALIDATED_JACKPOT_HANDPAY_RECEIPT_PRINTED_CENTS = 0x8E  # Validated jackpot handpay receipt printed cents
    VALIDATED_JACKPOT_HANDPAY_RECEIPT_PRINTED_QUANTITY = 0x8F  # Validated jackpot handpay receipt printed quantity
    VALIDATED_CANCELLED_CREDIT_HANDPAY_NO_RECEIPT_CENTS = 0x90  # Validated cancelled credit handpay no receipt cents
    VALIDATED_CANCELLED_CREDIT_HANDPAY_NO_RECEIPT_QUANTITY = 0x91  # Validated cancelled credit handpay no receipt quantity
    VALIDATED_JACKPOT_HANDPAY_NO_RECEIPT_CENTS = 0x92  # Validated jackpot handpay no receipt cents
    VALIDATED_JACKPOT_HANDPAY_NO_RECEIPT_QUANTITY = 0x93  # Validated jackpot handpay no receipt quantity

    # --- AFT-specific meters (Table C-7, 0xA0-0xBD) ---
    IN_HOUSE_CASHABLE_TRANSFERS_TO_GAMING_MACHINE_CENTS = 0xA0  # In-house cashable transfers to gaming machine cents
    IN_HOUSE_TRANSFERS_TO_GAMING_MACHINE_THAT_INCLUDED_CASHABLE_AMOUNTS_QUANTITY = 0xA1  # In-house transfers to gaming machine that included cashable amounts quantity
    IN_HOUSE_RESTRICTED_TRANSFERS_TO_GAMING_MACHINE_CENTS = 0xA2  # In-house restricted transfers to gaming machine cents
    IN_HOUSE_TRANSFERS_TO_GAMING_MACHINE_THAT_INCLUDED_RESTRICTED_AMOUNTS_QUANTITY = 0xA3  # In-house transfers to gaming machine that included restricted amounts quantity
    IN_HOUSE_NONRESTRICTED_TRANSFERS_TO_GAMING_MACHINE_CENTS = 0xA4  # In-house nonrestricted transfers to gaming machine cents
    IN_HOUSE_TRANSFERS_TO_GAMING_MACHINE_THAT_INCLUDED_NONRESTRICTED_AMOUNTS_QUANTITY = 0xA5  # In-house transfers to gaming machine that included nonrestricted amounts quantity
    DEBIT_TRANSFERS_TO_GAMING_MACHINE_CENTS = 0xA6  # Debit transfers to gaming machine cents
    DEBIT_TRANSFERS_TO_GAMING_MACHINE_QUANTITY = 0xA7  # Debit transfers to gaming machine quantity
    IN_HOUSE_CASHABLE_TRANSFERS_TO_TICKET_CENTS = 0xA8  # In-house cashable transfers to ticket cents
    IN_HOUSE_CASHABLE_TRANSFERS_TO_TICKET_QUANTITY = 0xA9  # In-house cashable transfers to ticket quantity
    IN_HOUSE_RESTRICTED_TRANSFERS_TO_TICKET_CENTS = 0xAA  # In-house restricted transfers to ticket cents
    IN_HOUSE_TRANSFERS_TO_TICKET_THAT_INCLUDED_RESTRICTED_AMOUNTS_QUANTITY = 0xAB  # In-house transfers to ticket that included restricted amounts quantity
    DEBIT_TRANSFERS_TO_TICKET_CENTS = 0xAC  # Debit transfers to ticket cents
    DEBIT_TRANSFERS_TO_TICKET_QUANTITY = 0xAD  # Debit transfers to ticket quantity
    BONUS_CASHABLE_TRANSFERS_TO_GAMING_MACHINE_CENTS = 0xAE  # Bonus cashable transfers to gaming machine cents
    BONUS_TRANSFERS_TO_GAMING_MACHINE_THAT_INCLUDED_CASHABLE_AMOUNTS_QUANTITY = 0xAF  # Bonus transfers to gaming machine that included cashable amounts quantity
    BONUS_NONRESTRICTED_TRANSFERS_TO_GAMING_MACHINE_CENTS = 0xB0  # Bonus nonrestricted transfers to gaming machine cents
    BONUS_TRANSFERS_TO_GAMING_MACHINE_THAT_INCLUDED_NONRESTRICTED_AMOUNTS_QUANTITY = 0xB1  # Bonus transfers to gaming machine that included nonrestricted amounts quantity
    IN_HOUSE_CASHABLE_TRANSFERS_TO_HOST_CENTS = 0xB8  # In-house cashable transfers to host cents
    IN_HOUSE_TRANSFERS_TO_HOST_THAT_INCLUDED_CASHABLE_AMOUNTS_QUANTITY = 0xB9  # In-house transfers to host that included cashable amounts quantity
    IN_HOUSE_RESTRICTED_TRANSFERS_TO_HOST_CENTS = 0xBA  # In-house restricted transfers to host cents
    IN_HOUSE_TRANSFERS_TO_HOST_THAT_INCLUDED_RESTRICTED_AMOUNTS_QUANTITY = 0xBB  # In-house transfers to host that included restricted amounts quantity
    IN_HOUSE_NONRESTRICTED_TRANSFERS_TO_HOST_CENTS = 0xBC  # In-house nonrestricted transfers to host cents
    IN_HOUSE_TRANSFERS_TO_HOST_THAT_INCLUDED_NONRESTRICTED_AMOUNTS_QUANTITY = 0xBD  # In-house transfers to host that included nonrestricted amounts quantity


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
    MeterCode.TOTAL_COIN_IN_CREDITS: 4,
    MeterCode.TOTAL_COIN_OUT_CREDITS: 4,
    MeterCode.TOTAL_JACKPOT_CREDITS: 4,
    MeterCode.TOTAL_HAND_PAID_CANCELLED_CREDITS: 4,
    MeterCode.TOTAL_CANCELLED_CREDITS: 4,
    MeterCode.GAMES_PLAYED: 4,
    MeterCode.GAMES_WON: 4,
    MeterCode.GAMES_LOST: 4,
    MeterCode.TOTAL_CREDITS_FROM_COIN_ACCEPTOR: 4,
    MeterCode.TOTAL_CREDITS_PAID_FROM_HOPPER: 4,
    MeterCode.TOTAL_CREDITS_FROM_COINS_TO_DROP: 4,
    MeterCode.TOTAL_CREDITS_FROM_BILLS_ACCEPTED: 4,
    MeterCode.CURRENT_CREDITS: 4,
    MeterCode.TOTAL_TICKET_IN_CREDITS: 4,
    MeterCode.TOTAL_TICKET_OUT_CREDITS: 4,
    MeterCode.TOTAL_ELECTRONIC_TRANSFERS_TO_GAMING_MACHINE_CREDITS: 4,
    MeterCode.TOTAL_ELECTRONIC_TRANSFERS_TO_HOST_CREDITS: 4,
    MeterCode.TOTAL_RESTRICTED_AMOUNT_PLAYED_CREDITS: 4,
    MeterCode.TOTAL_NONRESTRICTED_AMOUNT_PLAYED_CREDITS: 4,
    MeterCode.CURRENT_RESTRICTED_CREDITS: 4,
    MeterCode.TOTAL_MACHINE_PAID_PAYTABLE_WIN_CREDITS: 4,
    MeterCode.TOTAL_MACHINE_PAID_PROGRESSIVE_WIN_CREDITS: 4,
    MeterCode.TOTAL_MACHINE_PAID_EXTERNAL_BONUS_WIN_CREDITS: 4,
    MeterCode.TOTAL_ATTENDANT_PAID_PAYTABLE_WIN_CREDITS: 4,
    MeterCode.TOTAL_ATTENDANT_PAID_PROGRESSIVE_WIN_CREDITS: 4,
    MeterCode.TOTAL_ATTENDANT_PAID_EXTERNAL_BONUS_WIN_CREDITS: 4,
    MeterCode.TOTAL_WON_CREDITS: 4,
    MeterCode.TOTAL_HAND_PAID_CREDITS: 4,
    MeterCode.TOTAL_DROP_CREDITS: 4,
    MeterCode.GAMES_SINCE_LAST_POWER_RESET: 4,
    MeterCode.GAMES_SINCE_SLOT_DOOR_CLOSURE: 4,
    MeterCode.TOTAL_CREDITS_FROM_EXTERNAL_COIN_ACCEPTOR: 4,
    MeterCode.TOTAL_CASHABLE_TICKET_IN_CREDITS: 4,
    MeterCode.TOTAL_REGULAR_CASHABLE_TICKET_IN_CREDITS: 4,
    MeterCode.TOTAL_RESTRICTED_PROMOTIONAL_TICKET_IN_CREDITS: 4,
    MeterCode.TOTAL_NONRESTRICTED_PROMOTIONAL_TICKET_IN_CREDITS: 4,
    MeterCode.TOTAL_CASHABLE_TICKET_OUT_CREDITS: 4,
    MeterCode.TOTAL_RESTRICTED_PROMOTIONAL_TICKET_OUT_CREDITS: 4,
    MeterCode.ELECTRONIC_REGULAR_CASHABLE_TRANSFERS_TO_GAMING_MACHINE_CREDITS: 4,
    MeterCode.ELECTRONIC_RESTRICTED_PROMOTIONAL_TRANSFERS_TO_GAMING_MACHINE_CREDITS: 4,
    MeterCode.ELECTRONIC_NONRESTRICTED_PROMOTIONAL_TRANSFERS_TO_GAMING_MACHINE_CREDITS: 4,
    MeterCode.ELECTRONIC_DEBIT_TRANSFERS_TO_GAMING_MACHINE_CREDITS: 4,
    MeterCode.ELECTRONIC_REGULAR_CASHABLE_TRANSFERS_TO_HOST_CREDITS: 4,
    MeterCode.ELECTRONIC_RESTRICTED_PROMOTIONAL_TRANSFERS_TO_HOST_CREDITS: 4,
    MeterCode.ELECTRONIC_NONRESTRICTED_PROMOTIONAL_TRANSFERS_TO_HOST_CREDITS: 4,
    MeterCode.TOTAL_REGULAR_CASHABLE_TICKET_IN_QUANTITY: 4,
    MeterCode.TOTAL_RESTRICTED_PROMOTIONAL_TICKET_IN_QUANTITY: 4,
    MeterCode.TOTAL_NONRESTRICTED_PROMOTIONAL_TICKET_IN_QUANTITY: 4,
    MeterCode.TOTAL_CASHABLE_TICKET_OUT_QUANTITY: 4,
    MeterCode.TOTAL_RESTRICTED_PROMOTIONAL_TICKET_OUT_QUANTITY: 4,
    MeterCode.NUMBER_OF_BILLS_CURRENTLY_IN_STACKER: 4,
    MeterCode.TOTAL_VALUE_OF_BILLS_CURRENTLY_IN_STACKER_CREDITS: 4,
    MeterCode.TOTAL_NUMBER_OF_1_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_2_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_5_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_10_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_20_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_25_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_50_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_100_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_200_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_250_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_500_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_1_000_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_2_000_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_2_500_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_5_000_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_10_000_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_20_000_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_25_000_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_50_000_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_100_000_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_200_000_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_250_000_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_500_000_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_NUMBER_OF_1_000_000_00_BILLS_ACCEPTED: 4,
    MeterCode.TOTAL_CREDITS_FROM_BILLS_TO_DROP: 4,
    MeterCode.TOTAL_NUMBER_OF_1_00_BILLS_TO_DROP: 4,
    MeterCode.TOTAL_NUMBER_OF_2_00_BILLS_TO_DROP: 4,
    MeterCode.TOTAL_NUMBER_OF_5_00_BILLS_TO_DROP: 4,
    MeterCode.TOTAL_NUMBER_OF_10_00_BILLS_TO_DROP: 4,
    MeterCode.TOTAL_NUMBER_OF_20_00_BILLS_TO_DROP: 4,
    MeterCode.TOTAL_NUMBER_OF_50_00_BILLS_TO_DROP: 4,
    MeterCode.TOTAL_NUMBER_OF_100_00_BILLS_TO_DROP: 4,
    MeterCode.TOTAL_NUMBER_OF_200_00_BILLS_TO_DROP: 4,
    MeterCode.TOTAL_NUMBER_OF_500_00_BILLS_TO_DROP: 4,
    MeterCode.TOTAL_NUMBER_OF_1000_00_BILLS_TO_DROP: 4,
    MeterCode.TOTAL_CREDITS_FROM_BILLS_DIVERTED_TO_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_1_00_BILLS_DIVERTED_TO_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_2_00_BILLS_DIVERTED_TO_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_5_00_BILLS_DIVERTED_TO_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_10_00_BILLS_DIVERTED_TO_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_20_00_BILLS_DIVERTED_TO_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_50_00_BILLS_DIVERTED_TO_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_100_00_BILLS_DIVERTED_TO_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_200_00_BILLS_DIVERTED_TO_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_500_00_BILLS_DIVERTED_TO_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_1000_00_BILLS_DIVERTED_TO_HOPPER: 4,
    MeterCode.TOTAL_CREDITS_FROM_BILLS_DISPENSED_FROM_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_1_00_BILLS_DISPENSED_FROM_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_2_00_BILLS_DISPENSED_FROM_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_5_00_BILLS_DISPENSED_FROM_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_10_00_BILLS_DISPENSED_FROM_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_20_00_BILLS_DISPENSED_FROM_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_50_00_BILLS_DISPENSED_FROM_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_100_00_BILLS_DISPENSED_FROM_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_200_00_BILLS_DISPENSED_FROM_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_500_00_BILLS_DISPENSED_FROM_HOPPER: 4,
    MeterCode.TOTAL_NUMBER_OF_1000_00_BILLS_DISPENSED_FROM_HOPPER: 4,
    MeterCode.WEIGHTED_AVERAGE_THEORETICAL_PAYBACK_PERCENTAGE: 4,
    MeterCode.REGULAR_CASHABLE_TICKET_IN_CENTS: 5,
    MeterCode.REGULAR_CASHABLE_TICKET_IN_QUANTITY: 4,
    MeterCode.VALIDATION_RESTRICTED_TICKET_IN_CENTS: 5,
    MeterCode.VALIDATION_RESTRICTED_TICKET_IN_QUANTITY: 4,
    MeterCode.NONRESTRICTED_TICKET_IN_CENTS: 5,
    MeterCode.NONRESTRICTED_TICKET_IN_QUANTITY: 4,
    MeterCode.REGULAR_CASHABLE_TICKET_OUT_CENTS: 5,
    MeterCode.REGULAR_CASHABLE_TICKET_OUT_QUANTITY: 4,
    MeterCode.VALIDATION_RESTRICTED_TICKET_OUT_CENTS: 5,
    MeterCode.VALIDATION_RESTRICTED_TICKET_OUT_QUANTITY: 4,
    MeterCode.DEBIT_TICKET_OUT_CENTS: 5,
    MeterCode.DEBIT_TICKET_OUT_QUANTITY: 4,
    MeterCode.VALIDATED_CANCELLED_CREDIT_HANDPAY_RECEIPT_PRINTED_CENTS: 5,
    MeterCode.VALIDATED_CANCELLED_CREDIT_HANDPAY_RECEIPT_PRINTED_QUANTITY: 4,
    MeterCode.VALIDATED_JACKPOT_HANDPAY_RECEIPT_PRINTED_CENTS: 5,
    MeterCode.VALIDATED_JACKPOT_HANDPAY_RECEIPT_PRINTED_QUANTITY: 4,
    MeterCode.VALIDATED_CANCELLED_CREDIT_HANDPAY_NO_RECEIPT_CENTS: 5,
    MeterCode.VALIDATED_CANCELLED_CREDIT_HANDPAY_NO_RECEIPT_QUANTITY: 4,
    MeterCode.VALIDATED_JACKPOT_HANDPAY_NO_RECEIPT_CENTS: 5,
    MeterCode.VALIDATED_JACKPOT_HANDPAY_NO_RECEIPT_QUANTITY: 4,
    MeterCode.IN_HOUSE_CASHABLE_TRANSFERS_TO_GAMING_MACHINE_CENTS: 5,
    MeterCode.IN_HOUSE_TRANSFERS_TO_GAMING_MACHINE_THAT_INCLUDED_CASHABLE_AMOUNTS_QUANTITY: 4,
    MeterCode.IN_HOUSE_RESTRICTED_TRANSFERS_TO_GAMING_MACHINE_CENTS: 5,
    MeterCode.IN_HOUSE_TRANSFERS_TO_GAMING_MACHINE_THAT_INCLUDED_RESTRICTED_AMOUNTS_QUANTITY: 4,
    MeterCode.IN_HOUSE_NONRESTRICTED_TRANSFERS_TO_GAMING_MACHINE_CENTS: 5,
    MeterCode.IN_HOUSE_TRANSFERS_TO_GAMING_MACHINE_THAT_INCLUDED_NONRESTRICTED_AMOUNTS_QUANTITY: 4,
    MeterCode.DEBIT_TRANSFERS_TO_GAMING_MACHINE_CENTS: 5,
    MeterCode.DEBIT_TRANSFERS_TO_GAMING_MACHINE_QUANTITY: 4,
    MeterCode.IN_HOUSE_CASHABLE_TRANSFERS_TO_TICKET_CENTS: 5,
    MeterCode.IN_HOUSE_CASHABLE_TRANSFERS_TO_TICKET_QUANTITY: 4,
    MeterCode.IN_HOUSE_RESTRICTED_TRANSFERS_TO_TICKET_CENTS: 5,
    MeterCode.IN_HOUSE_TRANSFERS_TO_TICKET_THAT_INCLUDED_RESTRICTED_AMOUNTS_QUANTITY: 4,
    MeterCode.DEBIT_TRANSFERS_TO_TICKET_CENTS: 5,
    MeterCode.DEBIT_TRANSFERS_TO_TICKET_QUANTITY: 4,
    MeterCode.BONUS_CASHABLE_TRANSFERS_TO_GAMING_MACHINE_CENTS: 5,
    MeterCode.BONUS_TRANSFERS_TO_GAMING_MACHINE_THAT_INCLUDED_CASHABLE_AMOUNTS_QUANTITY: 4,
    MeterCode.BONUS_NONRESTRICTED_TRANSFERS_TO_GAMING_MACHINE_CENTS: 5,
    MeterCode.BONUS_TRANSFERS_TO_GAMING_MACHINE_THAT_INCLUDED_NONRESTRICTED_AMOUNTS_QUANTITY: 4,
    MeterCode.IN_HOUSE_CASHABLE_TRANSFERS_TO_HOST_CENTS: 5,
    MeterCode.IN_HOUSE_TRANSFERS_TO_HOST_THAT_INCLUDED_CASHABLE_AMOUNTS_QUANTITY: 4,
    MeterCode.IN_HOUSE_RESTRICTED_TRANSFERS_TO_HOST_CENTS: 5,
    MeterCode.IN_HOUSE_TRANSFERS_TO_HOST_THAT_INCLUDED_RESTRICTED_AMOUNTS_QUANTITY: 4,
    MeterCode.IN_HOUSE_NONRESTRICTED_TRANSFERS_TO_HOST_CENTS: 5,
    MeterCode.IN_HOUSE_TRANSFERS_TO_HOST_THAT_INCLUDED_NONRESTRICTED_AMOUNTS_QUANTITY: 4,
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
    LongPoll.SEND_LAST_ACCEPTED_BILL_INFORMATION: PollType.R,  # "type R long poll with a 48 command code" per §7.11
    LongPoll.SEND_VALIDATION_METERS: PollType.S,  # "type S long poll with command code 50" per §15.13
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


class ValidationType(enum.IntEnum):
    """Validation-type codes for LP 0x50 (Send Validation Meters), Table
    15.13c. Not a bitmask -- these are the 12 discrete values the field can
    take, each read with its own LP 0x50 call.
    """

    CASHABLE_TICKET_OR_HANDPAY_WIN_NO_LOCKUP = 0x00
    RESTRICTED_PROMOTIONAL_TICKET_FROM_CASHOUT = 0x01
    CASHABLE_TICKET_FROM_AFT_TRANSFER = 0x02
    RESTRICTED_TICKET_FROM_AFT_TRANSFER = 0x03
    DEBIT_TICKET_FROM_AFT_TRANSFER = 0x04
    CANCELLED_CREDIT_HANDPAY_RECEIPT_PRINTED = 0x10
    JACKPOT_HANDPAY_RECEIPT_PRINTED = 0x20
    CANCELLED_CREDIT_HANDPAY_NO_RECEIPT = 0x40
    JACKPOT_HANDPAY_NO_RECEIPT = 0x60
    CASHABLE_TICKET_REDEEMED = 0x80
    RESTRICTED_PROMOTIONAL_TICKET_REDEEMED = 0x81
    NONRESTRICTED_PROMOTIONAL_TICKET_REDEEMED = 0x82
