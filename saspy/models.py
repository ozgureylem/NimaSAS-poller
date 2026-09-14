"""Typed results for the implemented long polls.

Per-instance, never module-level: the legacy implementation stored all
parsed data in module-level dict globals (``meters``, ``aft_statement``,
...), so two SASClient instances talking to two different machines would
silently clobber each other's state. Every dataclass here is returned
fresh from its long-poll call and belongs to whoever called it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SASVersionInfo:
    """Response to long poll 0x54 (Table 7.15)."""

    sas_version: str
    serial_number: str


@dataclass(frozen=True)
class BasicMeters:
    """Response to long poll 0x0F (Table 7.2a)."""

    total_cancelled_credits: int
    total_coin_in: int
    total_coin_out: int
    total_drop: int
    total_jackpot: int
    games_played: int


@dataclass(frozen=True)
class ExtendedMeters:
    """Response to long poll 0x1C (Table 7.2c)."""

    total_coin_in: int
    total_coin_out: int
    total_drop: int
    total_jackpot: int
    games_played: int
    games_won: int
    slot_door_opened: int
    power_reset: int


@dataclass(frozen=True)
class GamingMachineInfo:
    """Response to long poll 0x1F (Table 7.10)."""

    game_id: str
    additional_id: str
    denomination: int
    max_bet: int
    progressive_group: int
    game_options: int
    paytable_id: str
    base_percentage: str


@dataclass(frozen=True)
class EnabledFeatures:
    """Response to long poll 0xA0 (Tables 7.14a-e)."""

    game_number: int
    jackpot_multiplier: bool
    aft_bonus_awards: bool
    legacy_bonus_awards: bool
    tournament: bool
    validation_extensions: bool
    validation_style: int  # 0=standard/none 1=system 2=secure enhanced 3=reserved
    ticket_redemption: bool


@dataclass(frozen=True)
class AftRegistrationStatus:
    """Response to long poll 0x73 (Table 8.1b)."""

    registration_status: int
    asset_number: int
    registration_key: bytes
    pos_id: int


@dataclass(frozen=True)
class AftLockStatus:
    """Response to long poll 0x74 (Table 8.2b)."""

    asset_number: int
    game_lock_status: int
    available_transfers: int
    host_cashout_status: int
    aft_status: int
    max_buffer_index: int
    current_cashable_amount: int
    current_restricted_amount: int
    current_non_restricted_amount: int
    gaming_machine_transfer_limit: int
    restricted_expiration: int
    restricted_pool_id: int


@dataclass(frozen=True)
class AftTransferResult:
    """Response to long poll 0x72 (Table 8.3c) — only the fixed-position
    fields; the cumulative-meter tail (variable length) is returned raw.
    """

    transaction_buffer_position: int
    transfer_status: int
    receipt_status: int
    transfer_type: int
    cashable_amount: int
    restricted_amount: int
    nonrestricted_amount: int
    transfer_flags: int
    asset_number: int
    transaction_id: str
    transaction_date: str
    transaction_time: str
    expiration: str
    pool_id: int
    cumulative_cashable_meter: int
    cumulative_restricted_meter: int
    cumulative_nonrestricted_meter: int


@dataclass(frozen=True)
class TicketValidationData:
    """Response to long poll 0x70 (Table 15.11a)."""

    ticket_in_escrow: bool
    amount_cents: int
    parsing_code: int
    validation_data: bytes


@dataclass(frozen=True)
class RedeemTicketResult:
    """Response to long poll 0x71 (Table 15.12b)."""

    machine_status: int
    amount_cents: int
    parsing_code: int
    validation_data: bytes


@dataclass(frozen=True)
class PendingCashoutInfo:
    """Response to long poll 0x57 (Table 15.7a), read after exception 0x57
    (system validation request). ``cashout_type`` per Table 15.7b: 0x00
    cashable ticket, 0x01 restricted promotional ticket, 0x80 not
    actually waiting for system validation (the exception fired but the
    cashout is already gone — a race, not an error; callers should treat
    this like "nothing to answer" rather than retry).
    """

    cashout_type: int
    amount_cents: int


@dataclass(frozen=True)
class SelectedMeters:
    """Response to long poll 0x2F or 0x6F (Table 7.3b / 7.21b).

    ``meters`` maps meter code (see constants.MeterCode / Table C-7) to its
    current value. Both long polls return the same shape here even though
    they differ on the wire (2F: 1-byte codes, fixed per-meter size from
    Table C-7; 6F: 2-byte codes, a self-describing size byte per meter).
    """

    game_number: int
    meters: dict[int, int]


@dataclass(frozen=True)
class EnhancedValidationInfo:
    """Response to long poll 0x4D (Table 15.10b) — one ticket-out record
    from the gaming machine's own buffer. All fields are zero when the
    requested buffer position holds no record (§15.10).
    """

    validation_type: int
    index_number: int
    date: str  # MMDDYYYY
    time: str  # HHMMSS
    validation_number: int
    amount_cents: int
    ticket_number: int
    validation_system_id: int
    expiration: str
    pool_id: int


@dataclass(frozen=True)
class ExtendedValidationStatus:
    """Response to long poll 0x7B (Table 15.2b)."""

    asset_number: int
    status_bits: int  # see Table 15.2c for individual bit meanings
    cashable_ticket_expiration_days: int
    restricted_ticket_expiration_days: int


@dataclass(frozen=True)
class EnhancedValidationId:
    """Response to long poll 0x4C (Table 15.6b)."""

    machine_id: int
    sequence_number: int


@dataclass(frozen=True)
class GamesSincePowerUpAndDoorClosure:
    """Response to long poll 0x18 (Table 7.7)."""

    games_since_power_up: int
    games_since_door_closure: int


@dataclass(frozen=True)
class Meters11Through15:
    """Response to long poll 0x19 (Table 7.2b) — the real "meters 11
    through 15" (see LongPoll.SEND_METERS_EXTENDED_GROUP's docstring for
    why 0x1C is a different, larger response despite a similar old name).
    """

    total_coin_in: int
    total_coin_out: int
    total_drop: int
    total_jackpot: int
    games_played: int


@dataclass(frozen=True)
class HandpayInformation:
    """Response to long poll 0x1B (Table 7.8). ``amount_cents`` is in cents
    if any portion is from a progressive win, otherwise in SAS accounting
    denom units — the machine does not say which, so the caller has to
    know from context (see the spec note on the Amount field).
    """

    progressive_group: int
    level: int
    amount: int
    partial_pay: int
    reset_id: int


@dataclass(frozen=True)
class BillMeters:
    """Response to long poll 0x1E (Table B-1 row 1E) — six "bills in"
    count meters for the most common denominations in one poll.
    """

    bills_1: int
    bills_5: int
    bills_10: int
    bills_20: int
    bills_50: int
    bills_100: int


@dataclass(frozen=True)
class CashOutTicketInfo:
    """Response to long poll 0x3D (Table 15.5)."""

    ticket_number: int
    amount_cents: int


@dataclass(frozen=True)
class HopperStatus:
    """Response to long poll 0x4F (Table 7.19a/7.19b). ``level`` is None
    when the machine can't detect it (length byte 02 rather than 06).
    """

    status: int
    percent_full: int
    level: int | None


@dataclass(frozen=True)
class GameNMeters:
    """Response to long poll 0x52 (Table 7.6.4b)."""

    game_number: int
    total_coin_in: int
    total_coin_out: int
    total_jackpot: int
    games_played: int


@dataclass(frozen=True)
class GameNConfiguration:
    """Response to long poll 0x53 (Table 7.6.5b)."""

    game_number: int
    game_id: str
    additional_id: str
    denomination: int
    max_bet: int
    progressive_group: int
    game_options: int
    paytable_id: str
    base_percentage: str


@dataclass(frozen=True)
class CurrentDateTime:
    """Response to long poll 0x7E (Table B-1 row 7E)."""

    date: str  # MMDDYYYY
    time: str  # HHMMSS


@dataclass(frozen=True)
class WagerCategoryInfo:
    """Response to long poll 0xB4 (Table 7.24b)."""

    payback_percentage: str  # ASCII "??.??", decimal implied
    coin_in_meter: int


@dataclass(frozen=True)
class ExtendedGameNInfo:
    """Response to long poll 0xB5 (Table 7.23b)."""

    game_number: int
    max_bet: int
    progressive_group: int
    progressive_levels: int  # bitfield, lsb=level 1, msb=level 32
    game_name: str
    paytable_name: str
    wager_categories: int
