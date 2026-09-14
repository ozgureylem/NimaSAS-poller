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
from .constants import LONG_POLL_TYPES, METER_CODE_SIZES_BCD, SIMPLE_METER_WIDTH_BCD, LongPoll, PollType
from .exceptions import SASEncodingError, SASError
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

    # -- Single-meter reads (Table 7.1 / Appendix B) -------------------------

    def send_meter(self, poll: LongPoll) -> int:
        """Read one meter with a Table-7.1-style single-meter long poll —
        a bare address+command request, response is just the BCD value
        (see constants.SIMPLE_METER_WIDTH_BCD for the covered codes and
        their widths). Individually redundant with send_meters_10_through_15()
        for the six meters it overlaps, but each of these is a real,
        addressable code in its own right.
        """
        width = SIMPLE_METER_WIDTH_BCD.get(poll)
        if width is None:
            raise SASError(f"{poll!r} is not a simple single-meter poll — see SIMPLE_METER_WIDTH_BCD")
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=1 + 1 + width + 2, timeout=self.timeout)
        payload = self._strip(raw)
        return decode_bcd(payload[1:1 + width])

    def send_games_since_power_up_and_door_closure(self) -> models.GamesSincePowerUpAndDoorClosure:
        poll = LongPoll.SEND_GAMES_SINCE_POWER_UP_AND_DOOR_CLOSURE
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=8, timeout=self.timeout)
        payload = self._strip(raw)
        return models.GamesSincePowerUpAndDoorClosure(
            games_since_power_up=decode_bcd(payload[1:3]),
            games_since_door_closure=decode_bcd(payload[3:5]),
        )

    def send_meters_11_through_15(self) -> models.Meters11Through15:
        """Long poll 0x19 (Table 7.2b) — the real "meters 11 through 15".
        Strictly a subset of send_meters_10_through_15()'s response (same
        five fields minus cancelled credits); kept because 0x19 is a real,
        independently addressable code, not because it adds information.
        """
        poll = LongPoll.SEND_METERS_11_THROUGH_15
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=24, timeout=self.timeout)
        payload = self._strip(raw)
        return models.Meters11Through15(
            total_coin_in=decode_bcd(payload[1:5]),
            total_coin_out=decode_bcd(payload[5:9]),
            total_drop=decode_bcd(payload[9:13]),
            total_jackpot=decode_bcd(payload[13:17]),
            games_played=decode_bcd(payload[17:21]),
        )

    def send_handpay_information(self) -> models.HandpayInformation:
        poll = LongPoll.HANDPAY_INFORMATION
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=24, timeout=self.timeout)
        payload = self._strip(raw)
        return models.HandpayInformation(
            progressive_group=payload[1],
            level=payload[2],
            amount=decode_bcd(payload[3:8]),
            partial_pay=decode_bcd(payload[8:10]),
            reset_id=payload[10],
        )

    def send_total_bill_meters(self) -> models.BillMeters:
        poll = LongPoll.TOTAL_BILL_METERS
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=28, timeout=self.timeout)
        payload = self._strip(raw)
        return models.BillMeters(
            bills_1=decode_bcd(payload[1:5]),
            bills_5=decode_bcd(payload[5:9]),
            bills_10=decode_bcd(payload[9:13]),
            bills_20=decode_bcd(payload[13:17]),
            bills_50=decode_bcd(payload[17:21]),
            bills_100=decode_bcd(payload[21:25]),
        )

    def send_total_hand_paid_cancelled_credits(self, game_number: int = 0) -> int:
        poll = LongPoll.SEND_TOTAL_HAND_PAID_CANCELLED_CREDITS
        command_and_data = bytes([poll]) + encode_bcd(game_number, 2)
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=10, timeout=self.timeout)
        payload = self._strip(raw)
        return decode_bcd(payload[3:7])

    def send_cash_out_ticket_information(self) -> models.CashOutTicketInfo:
        poll = LongPoll.SEND_CASH_OUT_TICKET_INFORMATION
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=13, timeout=self.timeout)
        payload = self._strip(raw)
        return models.CashOutTicketInfo(
            ticket_number=decode_bcd(payload[1:5]),
            amount_cents=decode_bcd(payload[5:10]),
        )

    def send_current_hopper_status(self) -> models.HopperStatus:
        poll = LongPoll.SEND_CURRENT_HOPPER_STATUS
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_length_prefixed(frame, timeout=self.timeout)
        payload = self._strip(raw)
        length = payload[1]
        level = decode_bcd(payload[4:8]) if length == 6 else None
        return models.HopperStatus(status=payload[2], percent_full=payload[3], level=level)

    def send_game_n_meters(self, game_number: int = 0) -> models.GameNMeters:
        poll = LongPoll.SEND_GAME_N_METERS
        command_and_data = bytes([poll]) + encode_bcd(game_number, 2)
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=22, timeout=self.timeout)
        payload = self._strip(raw)
        return models.GameNMeters(
            game_number=decode_bcd(payload[1:3]),
            total_coin_in=decode_bcd(payload[3:7]),
            total_coin_out=decode_bcd(payload[7:11]),
            total_jackpot=decode_bcd(payload[11:15]),
            games_played=decode_bcd(payload[15:19]),
        )

    def send_game_n_configuration(self, game_number: int = 0) -> models.GameNConfiguration:
        poll = LongPoll.SEND_GAME_N_CONFIGURATION
        command_and_data = bytes([poll]) + encode_bcd(game_number, 2)
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=26, timeout=self.timeout)
        payload = self._strip(raw)
        return models.GameNConfiguration(
            game_number=decode_bcd(payload[1:3]),
            game_id=payload[3:5].decode("ascii"),
            additional_id=payload[5:8].decode("ascii"),
            denomination=payload[8],
            max_bet=payload[9],
            progressive_group=payload[10],
            game_options=decode_binary_le(payload[11:13]),
            paytable_id=payload[13:19].decode("ascii"),
            base_percentage=payload[19:23].decode("ascii"),
        )

    def send_selected_game_number(self) -> int:
        return self.send_meter(LongPoll.SEND_SELECTED_GAME_NUMBER)

    def send_enabled_game_numbers(self) -> list[int]:
        poll = LongPoll.SEND_ENABLED_GAME_NUMBERS
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_length_prefixed(frame, timeout=self.timeout)
        payload = self._strip(raw)
        length = payload[1]
        data = payload[2:2 + length]
        num_games = data[0]
        games = []
        idx = 1
        for _ in range(num_games):
            games.append(decode_bcd(data[idx:idx + 2]))
            idx += 2
        return games

    def send_current_date_and_time(self) -> models.CurrentDateTime:
        poll = LongPoll.SEND_CURRENT_DATE_AND_TIME
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=11, timeout=self.timeout)
        payload = self._strip(raw)
        return models.CurrentDateTime(
            date=f"{decode_bcd(payload[1:5]):08d}",
            time=f"{decode_bcd(payload[5:8]):06d}",
        )

    def send_physical_reel_stop_information(self) -> bytes:
        poll = LongPoll.SEND_PHYSICAL_REEL_STOP_INFORMATION
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=13, timeout=self.timeout)
        payload = self._strip(raw)
        return bytes(payload[1:10])

    def send_token_denomination(self) -> int:
        poll = LongPoll.SEND_TOKEN_DENOMINATION
        frame = build_command(self.address, bytes([poll]), crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=5, timeout=self.timeout)
        payload = self._strip(raw)
        return payload[1]

    def send_wager_category_information(self, game_number: int = 0, wager_category: int = 0) -> models.WagerCategoryInfo:
        poll = LongPoll.SEND_WAGER_CATEGORY_INFORMATION
        command_and_data = bytes([poll]) + encode_bcd(game_number, 2) + encode_bcd(wager_category, 2)
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_length_prefixed(frame, timeout=self.timeout)
        payload = self._strip(raw)
        data = payload[2:2 + payload[1]]
        payback_percentage = data[4:8].decode("ascii")
        coin_in_size = data[8]
        coin_in_meter = decode_bcd(data[9:9 + coin_in_size]) if coin_in_size else 0
        return models.WagerCategoryInfo(payback_percentage=payback_percentage, coin_in_meter=coin_in_meter)

    def send_extended_game_n_information(self, game_number: int = 0) -> models.ExtendedGameNInfo:
        poll = LongPoll.SEND_EXTENDED_GAME_N_INFORMATION
        command_and_data = bytes([poll]) + encode_bcd(game_number, 2)
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_length_prefixed(frame, timeout=self.timeout)
        payload = self._strip(raw)
        data = payload[2:2 + payload[1]]
        resp_game_number = decode_bcd(data[0:2])
        max_bet = decode_bcd(data[2:4])
        progressive_group = data[4]
        progressive_levels = decode_binary_le(data[5:9])
        idx = 9
        name_len = data[idx]
        idx += 1
        game_name = data[idx:idx + name_len].decode("ascii")
        idx += name_len
        paytable_len = data[idx]
        idx += 1
        paytable_name = data[idx:idx + paytable_len].decode("ascii")
        idx += paytable_len
        wager_categories = decode_bcd(data[idx:idx + 2])
        return models.ExtendedGameNInfo(
            game_number=resp_game_number,
            max_bet=max_bet,
            progressive_group=progressive_group,
            progressive_levels=progressive_levels,
            game_name=game_name,
            paytable_name=paytable_name,
            wager_categories=wager_categories,
        )

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

    def send_extended_meters_group(self) -> models.ExtendedMeters:
        """Long poll 0x1C (Table 7.2c, "Send meters") — NOT the same as
        Table 7.2b's actual "Send meters 11 through 15" (0x19, a smaller,
        5-field response — see send_meters_11_through_15() below). This
        method used to be misnamed after that other table; the wire code
        and parsing were always correct, only the name was borrowed from
        the wrong one.
        """
        poll = LongPoll.SEND_METERS_EXTENDED_GROUP
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

    # -- Selected meters (0x2F, Table 7.3a/7.3b) -----------------------------

    def send_selected_meters(self, meter_codes: list[int], *, game_number: int = 0) -> models.SelectedMeters:
        """Read up to 10 meters by code in one poll. Unlike LP 6F, LP 2F's
        response has no per-meter size byte (§7.3), so every code in
        ``meter_codes`` must be one this client knows the size of — see
        ``constants.METER_CODE_SIZES_BCD`` / ``constants.MeterCode``. This is
        the only way to reach ticket meters (e.g. Cashable Tickets In,
        MeterCode.CASHABLE_TICKET_IN_CENTS/_QUANTITY) — LP 0x0F/0x1C cannot
        report them at all (Table 7.2a/7.2c only cover the six core meters).
        """
        if not 1 <= len(meter_codes) <= 10:
            raise SASError(f"send_selected_meters takes 1-10 meter codes, got {len(meter_codes)}")
        unknown = [c for c in meter_codes if c not in METER_CODE_SIZES_BCD]
        if unknown:
            raise SASEncodingError(
                f"unknown meter code(s) {[hex(c) for c in unknown]} — "
                "add their Table C-7 size to METER_CODE_SIZES_BCD before requesting them"
            )

        poll = LongPoll.SEND_SELECTED_METERS
        body = encode_bcd(game_number, 2) + bytes(meter_codes)
        command_and_data = bytes([poll]) + bytes([len(body)]) + body
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_length_prefixed(frame, timeout=self.timeout)
        payload = self._strip(raw)

        length = payload[1]
        data = payload[2:2 + length]
        resp_game_number = decode_bcd(data[0:2])
        meters: dict[int, int] = {}
        idx = 2
        while idx < len(data):
            code = data[idx]
            idx += 1
            size = METER_CODE_SIZES_BCD.get(code)
            if size is None:
                # A code we didn't ask for, or whose size we don't know —
                # can't tell where the next code/value pair starts either.
                raise SASEncodingError(f"response contains unrecognised meter code 0x{code:02X}")
            meters[code] = decode_bcd(data[idx:idx + size])
            idx += size
        return models.SelectedMeters(game_number=resp_game_number, meters=meters)

    # -- Extended meters (0x6F, Table 7.21a/7.21b) ---------------------------

    def send_extended_meters(self, meter_codes: list[int], *, game_number: int = 0) -> models.SelectedMeters:
        """Read up to 12 meters by code in one poll. Unlike LP 2F, each meter
        in the response carries its own size byte (§7.21), so — unlike
        send_selected_meters() — any meter code from Table C-7 works here,
        not just ones this client has a size table for.
        """
        return self._send_extended_meters(LongPoll.SEND_EXTENDED_METERS, meter_codes, game_number=game_number)

    def send_extended_meters_alternate(self, meter_codes: list[int], *, game_number: int = 0) -> models.SelectedMeters:
        """Identical wire shape to send_extended_meters() (0x6F) but sent as
        0xAF instead — the spec provides two codes for the same data purely
        so consecutive polls can each get their own implied ACK (§7.21:
        "Two different long poll codes can be used to access the exact same
        meter data... to allow a host to perform consecutive meter polls
        and still provide a proper implied acknowledgement").
        """
        return self._send_extended_meters(
            LongPoll.SEND_EXTENDED_METERS_ALTERNATE, meter_codes, game_number=game_number
        )

    def _send_extended_meters(
        self, poll: LongPoll, meter_codes: list[int], *, game_number: int = 0
    ) -> models.SelectedMeters:
        if not 1 <= len(meter_codes) <= 12:
            raise SASError(f"send_extended_meters takes 1-12 meter codes, got {len(meter_codes)}")

        body = encode_bcd(game_number, 2)
        for code in meter_codes:
            body += encode_binary_le(code, 2)
        command_and_data = bytes([poll]) + bytes([len(body)]) + body
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_length_prefixed(frame, timeout=self.timeout)
        payload = self._strip(raw)

        length = payload[1]
        data = payload[2:2 + length]
        resp_game_number = decode_bcd(data[0:2])
        meters: dict[int, int] = {}
        idx = 2
        while idx < len(data):
            code = decode_binary_le(data[idx:idx + 2])
            idx += 2
            size = data[idx]
            idx += 1
            if size == 0:
                continue  # unsupported meter: code + size=0, no value bytes (§7.21)
            meters[code] = decode_bcd(data[idx:idx + size])
            idx += size
        return models.SelectedMeters(game_number=resp_game_number, meters=meters)

    # -- Secure enhanced validation ID (0x4C, Table 15.6a/15.6b) -------------

    def set_secure_enhanced_validation_id(
        self, machine_id: int = 0, sequence_number: int = 0
    ) -> models.EnhancedValidationId:
        """Set (or, with machine_id=0, read back) the machine's secure
        enhanced validation ID and starting sequence number. Commissioning
        only — see §15.6.
        """
        poll = LongPoll.SET_SECURE_ENHANCED_VALIDATION_ID
        command_and_data = bytes([poll]) + encode_binary_le(machine_id, 3) + encode_binary_le(sequence_number, 3)
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=10, timeout=self.timeout)
        payload = self._strip(raw)
        return models.EnhancedValidationId(
            machine_id=decode_binary_le(payload[1:4]),
            sequence_number=decode_binary_le(payload[4:7]),
        )

    # -- Enhanced validation information / ticket-out history (0x4D, Table 15.10a/15.10b) --

    def send_enhanced_validation_information(self, function_code: int = 0xFF) -> models.EnhancedValidationInfo:
        """Read one ticket-out record from the machine's own buffer —
        function_code 0x00 returns the next unread record and marks it read,
        0x01-0x1F reads buffer index n directly, 0xFF (the default here)
        peeks at the next unread record without marking it read. This is the
        multi-record buffer walk boot reconciliation depends on (§5.6): walk
        indices 0x01-0x1F to read the whole buffer without disturbing the
        unread/read state (see Appendix A — LP 7B does NOT do this; it's a
        status/config poll, not a history read).
        """
        poll = LongPoll.SEND_ENHANCED_VALIDATION_INFORMATION
        command_and_data = bytes([poll, function_code])
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_fixed(frame, response_length=35, timeout=self.timeout)
        payload = self._strip(raw)
        return models.EnhancedValidationInfo(
            validation_type=payload[1],
            index_number=payload[2],
            date=f"{decode_bcd(payload[3:7]):08d}",
            time=f"{decode_bcd(payload[7:10]):06d}",
            validation_number=decode_bcd(payload[10:18]),
            amount_cents=decode_bcd(payload[18:23]),
            ticket_number=decode_binary_le(payload[23:25]),
            validation_system_id=decode_bcd(payload[25:26]),
            expiration=f"{decode_bcd(payload[26:30]):08d}",
            pool_id=decode_binary_le(payload[30:32]),
        )

    # -- Extended validation status (0x7B, Table 15.2a/15.2b) ----------------

    def extended_validation_status(
        self,
        *,
        control_mask: int = 0x0000,
        status_bit_control_states: int = 0x0000,
        cashable_ticket_expiration_days: int = 0,
        restricted_ticket_expiration_days: int = 0,
    ) -> models.ExtendedValidationStatus:
        """Inquire (all-defaults call) or set validation/ticket-printing
        parameters. A 0 control_mask bit leaves that function's current
        state untouched (§15.2) — the all-defaults call is a pure status
        read. Not a history read: this cannot walk the ticket-out buffer —
        see send_enhanced_validation_information() (LP 4D) for that.
        """
        poll = LongPoll.EXTENDED_VALIDATION_STATUS
        body = encode_binary_le(control_mask, 2)
        body += encode_binary_le(status_bit_control_states, 2)
        body += encode_bcd(cashable_ticket_expiration_days, 2)
        body += encode_bcd(restricted_ticket_expiration_days, 2)
        command_and_data = bytes([poll]) + bytes([len(body)]) + body
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_length_prefixed(frame, timeout=self.timeout)
        payload = self._strip(raw)
        return models.ExtendedValidationStatus(
            asset_number=decode_binary_le(payload[2:6]),
            status_bits=decode_binary_le(payload[6:8]),
            cashable_ticket_expiration_days=decode_bcd(payload[8:10]),
            restricted_ticket_expiration_days=decode_bcd(payload[10:12]),
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
        return self._parse_redeem_ticket_response(payload)

    def redeem_ticket_status(self) -> models.RedeemTicketResult:
        """Safe, read-only status query for the current ticket redemption
        cycle: transfer code FF, all other fields omitted (§15.12b — "the
        host may use long poll 71 to request the current ticket status at
        any time by setting the transfer code to FF and omitting the
        transfer amount, parsing code and validation data fields"). This is
        the exchange reading a ticket's completion status safely depends on
        (Appendix D Step 7: `2A 71 01 FF cc cc`, a 1-byte body) — sending a
        full redeem_ticket() call instead risks the machine reading it as a
        new redemption attempt rather than a status read.

        machine_status FF means there has been no previous redemption cycle
        on this machine since it was last polled.
        """
        poll = LongPoll.REDEEM_TICKET
        command_and_data = bytes([poll, 0x01, 0xFF])  # length=1, body=transfer_code FF only
        frame = build_command(self.address, command_and_data, crc_required=self._crc_required(poll))
        raw = self.transport.exchange_length_prefixed(frame, timeout=self.timeout)
        payload = self._strip(raw)
        return self._parse_redeem_ticket_response(payload)

    def _parse_redeem_ticket_response(self, payload: bytes) -> models.RedeemTicketResult:
        """Shared by redeem_ticket() and redeem_ticket_status() — both get
        the same response shape (Table 15.12b).
        """
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
