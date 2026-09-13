"""SASClient: per-instance, spec-checked implementation of the long polls
carried over from legacy/sas.py's "core" coverage (meters, machine ID,
enabled features, AFT register/lock/transfer, ticket validation/redemption).

Every method here cites the SAS 6.02 table it was built from. Where the
legacy implementation had a verified bug (see the repo's bug-review
history), the fix is called out in a comment at the point it applies —
this is meant to make each parsing decision auditable against the spec
again in the future, not just trusted because it's here.

State is per-instance (``self``), not module-level, so multiple SASClient
objects (one per gaming machine, per the project's 1:1 micro-PC-per-EGM
topology) never share or clobber each other's data.
"""

from __future__ import annotations

from . import models
from .bcd import decode_bcd, encode_bcd
from .binary import decode_binary_le, encode_binary_le
from .constants import LONG_POLL_TYPES, LongPoll, PollType
from .exceptions import SASError
from .framing import build_command, parse_response
from .transport import SASTransport

DEFAULT_TIMEOUT = 1.0


class SASClient:
    """Talks SAS 6.02 to a single gaming machine at ``address`` over ``transport``."""

    def __init__(self, transport: SASTransport, address: int, *, timeout: float = DEFAULT_TIMEOUT):
        self.transport = transport
        self.address = address
        self.timeout = timeout

    # -- General poll ------------------------------------------------------

    def general_poll(self) -> int:
        """Send a general poll and return the raw exception code (0x00 = none)."""
        response = self.transport.general_poll(self.address, timeout=self.timeout)
        return response[0]

    # -- Meters (0x0F / 0x1C, Table 7.2a / 7.2c) ----------------------------

    def send_meters_10_through_15(self) -> models.BasicMeters:
        poll = LongPoll.SEND_METERS_10_THROUGH_15
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=28, timeout=self.timeout)
        payload = self._strip(raw)
        return models.BasicMeters(
            total_cancelled_credits=decode_bcd(payload[1:5]),
            total_coin_in=decode_bcd(payload[5:9]),
            total_coin_out=decode_bcd(payload[9:13]),
            total_drop=decode_bcd(payload[13:17]),
            total_jackpot=decode_bcd(payload[17:21]),
            games_played=decode_bcd(payload[21:25]),
        )

    def send_meters_11_through_15_extended(self) -> models.ExtendedMeters:
        poll = LongPoll.SEND_METERS_11_THROUGH_15
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=36, timeout=self.timeout)
        payload = self._strip(raw)
        return models.ExtendedMeters(
            total_coin_in=decode_bcd(payload[1:5]),
            total_coin_out=decode_bcd(payload[5:9]),
            total_drop=decode_bcd(payload[9:13]),
            total_jackpot=decode_bcd(payload[13:17]),
            games_played=decode_bcd(payload[17:21]),
            games_won=decode_bcd(payload[21:25]),
            slot_door_opened=decode_bcd(payload[25:29]),
            power_reset=decode_bcd(payload[29:33]),
        )

    # -- Gaming machine ID (0x1F, Table 7.10) -------------------------------

    def send_gaming_machine_id(self) -> models.GamingMachineInfo:
        poll = LongPoll.SEND_GAMING_MACHINE_ID
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=24, timeout=self.timeout)
        payload = self._strip(raw)
        return models.GamingMachineInfo(
            game_id=payload[1:3].decode("ascii"),
            additional_id=payload[3:6].decode("ascii"),
            # BUG FIX vs legacy: denomination is a single scalar byte. Legacy did
            # bytearray(data[6]), and bytearray(int) allocates a zero-filled
            # buffer of that many bytes rather than wrapping the byte's value.
            denomination=payload[6],
            max_bet=payload[7],
            progressive_group=payload[8],
            # BUG FIX vs legacy: "game options" is a multi-byte *binary* field.
            # Section 2.2.3: binary fields are transmitted LSB-first, unlike
            # BCD/ASCII (MSB-first). Legacy hexlified this without reordering.
            game_options=decode_binary_le(payload[9:11]),
            paytable_id=payload[11:17].decode("ascii"),
            base_percentage=payload[17:21].decode("ascii"),
        )

    # -- SAS version and serial number (0x54, Table 7.15) --------------------

    def send_sas_version_and_serial(self) -> models.SASVersionInfo:
        """The closest thing SAS has to an actual "what version are you"
        query — useful for commissioning against a fleet that isn't all on
        the same SAS revision (see the commissioning tool in this repo).
        """
        poll = LongPoll.SEND_SAS_VERSION_AND_SERIAL
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_length_prefixed(frame, timeout=self.timeout)
        payload = self._strip(raw)
        length = payload[1]
        return models.SASVersionInfo(
            sas_version=payload[2:5].decode("ascii"),
            serial_number=payload[5:2 + length].decode("ascii"),
        )

    # -- Enabled features (0xA0, Table 7.14a-e) -----------------------------

    def send_enabled_features(self, game_number: int = 0) -> models.EnabledFeatures:
        poll = LongPoll.SEND_ENABLED_FEATURES
        command_and_data = bytes([poll]) + encode_bcd(game_number, 2)
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=12, timeout=self.timeout)
        payload = self._strip(raw)
        features1 = payload[3]
        # features2 (payload[4]) and features3 (payload[5]) are not yet exposed
        # on EnabledFeatures — add fields there if a caller needs them.
        return models.EnabledFeatures(
            game_number=decode_bcd(payload[1:3]),
            jackpot_multiplier=bool(features1 & 0b00000001),
            aft_bonus_awards=bool(features1 & 0b00000010),
            legacy_bonus_awards=bool(features1 & 0b00000100),
            tournament=bool(features1 & 0b00001000),
            validation_extensions=bool(features1 & 0b00010000),
            # BUG FIX vs legacy: `data[3]&0b01100000>>5` is parsed by Python as
            # `data[3] & (0b01100000 >> 5)` because `>>` binds tighter than `&`,
            # which reads the wrong two bits entirely. Needs explicit grouping.
            validation_style=(features1 & 0b01100000) >> 5,
            ticket_redemption=bool(features1 & 0b10000000),
        )

    # -- AFT register gaming machine (0x73, Table 8.1a/8.1b) ----------------

    def aft_register_gaming_machine(
        self,
        registration_code: int = 0xFF,
        asset_number: int = 0,
        registration_key: bytes = bytes(20),
        pos_id: int = 0,
    ) -> models.AftRegistrationStatus:
        poll = LongPoll.AFT_REGISTER_GAMING_MACHINE
        if registration_code in (0xFF, 0x80):
            command_and_data = bytes([poll, 0x01, registration_code])
        else:
            if len(registration_key) != 20:
                raise SASError("registration_key must be exactly 20 bytes")
            command_and_data = (
                bytes([poll, 0x1D, registration_code])
                + encode_binary_le(asset_number, 4)
                + registration_key
                + encode_binary_le(pos_id, 4)
            )
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_length_prefixed(frame, timeout=self.timeout)
        payload = self._strip(raw)
        return models.AftRegistrationStatus(
            registration_status=payload[2],
            asset_number=decode_binary_le(payload[3:7]),
            registration_key=bytes(payload[7:27]),
            pos_id=decode_binary_le(payload[27:31]),
        )

    # -- AFT game lock and status request (0x74, Table 8.2a/8.2b) -----------

    def aft_game_lock_and_status_request(
        self, lock_code: int = 0xFF, transfer_condition: int = 0x00, lock_timeout: int = 0
    ) -> models.AftLockStatus:
        poll = LongPoll.AFT_GAME_LOCK_AND_STATUS
        command_and_data = bytes([poll, lock_code, transfer_condition])
        command_and_data += encode_bcd(lock_timeout, 2)
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_length_prefixed(frame, timeout=self.timeout)
        payload = self._strip(raw)
        return models.AftLockStatus(
            asset_number=decode_binary_le(payload[2:6]),
            game_lock_status=payload[6],
            available_transfers=payload[7],
            host_cashout_status=payload[8],
            aft_status=payload[9],
            max_buffer_index=payload[10],
            current_cashable_amount=decode_bcd(payload[11:16]),
            current_restricted_amount=decode_bcd(payload[16:21]),
            current_non_restricted_amount=decode_bcd(payload[21:26]),
            # BUG FIX vs legacy: this 5-byte field was missing entirely, which
            # shifted every field after it — restricted_expiration and
            # restricted_pool_id in legacy actually read *these* bytes, and the
            # real trailing fields were silently dropped.
            gaming_machine_transfer_limit=decode_bcd(payload[26:31]),
            restricted_expiration=decode_bcd(payload[31:35]),
            restricted_pool_id=decode_binary_le(payload[35:37]),
        )

    # -- AFT transfer funds (0x72, Table 8.3a/8.3c) -------------------------

    def aft_transfer_funds(
        self,
        transfer_code: int = 0x00,
        transaction_index: int = 0x00,
        transfer_type: int = 0x00,
        cashable_amount: int = 0,
        restricted_amount: int = 0,
        nonrestricted_amount: int = 0,
        transfer_flags: int = 0x00,
        asset_number: int = 0,
        registration_key: bytes = bytes(20),
        transaction_id: str = "",
        expiration: int = 0,
        pool_id: int = 0,
        receipt_data: bytes = b"",
    ) -> models.AftTransferResult:
        if len(registration_key) != 20:
            raise SASError("registration_key must be exactly 20 bytes")
        transaction_id_bytes = transaction_id.encode("ascii")

        poll = LongPoll.AFT_TRANSFER_FUNDS
        command_and_data = bytes([poll])
        # BUG FIX vs legacy: no `lock_timeout` field exists on long poll 0x72 —
        # that belongs to 0x74. Legacy appended two extra bytes here that the
        # spec's table 8.3a has no place for, and its length byte was computed
        # to match (double-counting transaction_id length instead of counting
        # receipt_data), which would desync framing on a real machine.
        body = bytes([transfer_code, transaction_index, transfer_type])
        body += encode_bcd(cashable_amount, 5)
        body += encode_bcd(restricted_amount, 5)
        body += encode_bcd(nonrestricted_amount, 5)
        body += bytes([transfer_flags])
        body += encode_binary_le(asset_number, 4)
        body += registration_key
        body += bytes([len(transaction_id_bytes)]) + transaction_id_bytes
        body += encode_bcd(expiration, 4)
        body += encode_binary_le(pool_id, 2)
        body += bytes([len(receipt_data)]) + receipt_data

        command_and_data += bytes([len(body)]) + body
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_length_prefixed(frame, timeout=self.timeout)
        payload = self._strip(raw)

        idx = 2  # skip command + length
        transaction_buffer_position = payload[idx]; idx += 1
        transfer_status = payload[idx]; idx += 1
        receipt_status = payload[idx]; idx += 1
        transfer_type_resp = payload[idx]; idx += 1
        cashable = decode_bcd(payload[idx:idx + 5]); idx += 5
        restricted = decode_bcd(payload[idx:idx + 5]); idx += 5
        nonrestricted = decode_bcd(payload[idx:idx + 5]); idx += 5
        flags = payload[idx]; idx += 1
        asset = decode_binary_le(payload[idx:idx + 4]); idx += 4
        tid_len = payload[idx]; idx += 1
        tid = payload[idx:idx + tid_len].decode("ascii"); idx += tid_len
        tx_date = decode_bcd(payload[idx:idx + 4]); idx += 4
        tx_time = decode_bcd(payload[idx:idx + 3]); idx += 3
        expiration_val = decode_bcd(payload[idx:idx + 4]); idx += 4
        pool = decode_binary_le(payload[idx:idx + 2]); idx += 2

        cashable_meter_size = payload[idx]; idx += 1
        cashable_meter = decode_bcd(payload[idx:idx + cashable_meter_size]); idx += cashable_meter_size
        restricted_meter_size = payload[idx]; idx += 1
        restricted_meter = decode_bcd(payload[idx:idx + restricted_meter_size]); idx += restricted_meter_size
        nonrestricted_meter_size = payload[idx]; idx += 1
        nonrestricted_meter = decode_bcd(payload[idx:idx + nonrestricted_meter_size]); idx += nonrestricted_meter_size

        return models.AftTransferResult(
            transaction_buffer_position=transaction_buffer_position,
            transfer_status=transfer_status,
            receipt_status=receipt_status,
            transfer_type=transfer_type_resp,
            cashable_amount=cashable,
            restricted_amount=restricted,
            nonrestricted_amount=nonrestricted,
            transfer_flags=flags,
            asset_number=asset,
            transaction_id=tid,
            transaction_date=f"{tx_date:08d}",
            transaction_time=f"{tx_time:06d}",
            expiration=f"{expiration_val:08d}",
            pool_id=pool,
            cumulative_cashable_meter=cashable_meter,
            cumulative_restricted_meter=restricted_meter,
            cumulative_nonrestricted_meter=nonrestricted_meter,
        )

    # -- Ticket validation data (0x70, Table 15.11a) ------------------------

    def send_ticket_validation_data(self) -> models.TicketValidationData:
        poll = LongPoll.SEND_TICKET_VALIDATION_DATA
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_length_prefixed(frame, timeout=self.timeout)
        payload = self._strip(raw)
        status = payload[2]
        if status == 0xFF:
            return models.TicketValidationData(
                ticket_in_escrow=False, amount_cents=0, parsing_code=0, validation_data=b""
            )
        return models.TicketValidationData(
            ticket_in_escrow=True,
            amount_cents=decode_bcd(payload[3:8]),
            parsing_code=payload[8],
            validation_data=bytes(payload[9:]),
        )

    # -- Redeem ticket (0x71, Table 15.12a/15.12b) ---------------------------

    def redeem_ticket(
        self,
        transfer_code: int,
        transfer_amount_cents: int,
        validation_number: int,
        restricted_expiration: int = 0,
        pool_id: int = 0,
        parsing_code: int = 0x00,
    ) -> models.RedeemTicketResult:
        """``validation_number`` is the full 18-digit decimal value (2-digit
        system ID + 16-digit number), per Table 15.11b — encoded as 9 BCD
        bytes. Legacy encoded only the trailing 16 digits as 8 bytes, silently
        dropping the mandatory system-ID prefix and under-counting the
        command length by one byte to match.
        """
        poll = LongPoll.REDEEM_TICKET
        command_and_data = bytes([poll])
        body = bytes([transfer_code])
        body += encode_bcd(transfer_amount_cents, 5)
        body += bytes([parsing_code])
        body += encode_bcd(validation_number, 9)  # BUG FIX vs legacy: was 8 bytes
        body += encode_bcd(restricted_expiration, 4)
        body += encode_binary_le(pool_id, 2)

        command_and_data += bytes([len(body)]) + body
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_length_prefixed(frame, timeout=self.timeout)
        payload = self._strip(raw)

        status = payload[2]
        length = payload[1]
        if length <= 1:
            return models.RedeemTicketResult(
                machine_status=status, amount_cents=0, parsing_code=0, validation_data=b""
            )
        return models.RedeemTicketResult(
            machine_status=status,
            amount_cents=decode_bcd(payload[3:8]),
            parsing_code=payload[8],
            validation_data=bytes(payload[9:]),
        )

    # -- Validation number (0x58, Table 15.9b/15.9c) -------------------------

    def send_validation_number(self, validation_system_id: int, validation_number: int) -> int:
        """Returns the gaming machine's status byte (0x00 = acknowledged)."""
        poll = LongPoll.SEND_VALIDATION_NUMBER
        command_and_data = bytes([poll])
        command_and_data += encode_bcd(validation_system_id, 1)
        command_and_data += encode_bcd(validation_number, 8)
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=5, timeout=self.timeout)
        payload = self._strip(raw)
        return payload[1]

    # -- helpers -------------------------------------------------------------

    def _crc_required(self, poll: LongPoll) -> bool:
        """Type R commands carry no CRC; S/M/G do (spec 2.2.2)."""
        return LONG_POLL_TYPES[poll] != PollType.R

    def _strip(self, raw: bytes) -> bytes:
        """Validate address+CRC and return the command-onward payload."""
        return parse_response(raw, self.address).payload
