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
