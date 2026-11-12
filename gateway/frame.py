"""The wire frame, and the order it must be checked in (spec §10.1, §10.3).

    [length: 4][version: 1][client_id: 24][message_type: 1][seq: 8]
    [JSON payload: n][HMAC tag: 16]

Every field outside the JSON exists so the server can route, authenticate
and reject a frame WITHOUT parsing the body. The spec is explicit about
why, and it is the whole security argument:

    "With client_id inside the body, the server cannot select a
     verification key without parsing the body, so an adversarially-nested
     payload reaches the parser before authentication can reject it."

So the processing order below is not an implementation detail that could be
rearranged for tidiness. It is the property. `decode` performs the steps in
the specified order and the tests assert the order, not just the outcome.

    1. read the 4-byte length; reject below 50 or above MAX_FRAME_SIZE,
       BEFORE allocating any body buffer
    2. read header fields by fixed byte offset; no parser invoked
    3. validate client_id encoding; look up its key. An unknown client_id
       is rejected and counted per SOURCE, never per client_id
    4. verify the HMAC over the length prefix, header and payload
    5. only on success: check seq against the last accepted value
    6. only on success: dispatch on message_type. An unrecognised value is
       rejected HERE, after verification, so the type space cannot be
       probed without a valid key
    7. only then: hand the JSON payload to the parser, with a depth cap

WHAT THIS MODULE IS NOT. There is no socket in it, no retry, no connection
state. A frame is bytes in and bytes out, which is what makes the order
above testable without a server at the other end.
"""

from __future__ import annotations

import hmac
import json
import struct
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable

VERSION = 0x01

LENGTH_BYTES = 4
CLIENT_ID_BYTES = 24
SEQ_BYTES = 8
TAG_BYTES = 16

# version + client_id + message_type + seq. The length prefix is NOT part of
# this count -- the spec's "header fields total 34 bytes" counts what the
# length covers, and length covers everything after itself.
HEADER_BYTES = 1 + CLIENT_ID_BYTES + 1 + SEQ_BYTES
assert HEADER_BYTES == 34

# length counts header + payload + tag, so an empty payload is 50.
MIN_LENGTH = HEADER_BYTES + TAG_BYTES
assert MIN_LENGTH == 50

# §10.4 caps an EVENT_BATCH at 25 records or 64 KB, "whichever is reached
# first", and says MAX_FRAME_SIZE "must sit above this with headroom".
# 128 KiB is double the batch bound: enough that a legal batch can never be
# refused by this limit, small enough that the pre-allocation check it
# guards still means something on a 32-bit gateway with 512 MB.
MAX_FRAME_SIZE = 128 * 1024

# A payload is JSON and JSON nests. Python's parser recurses, so a deeply
# nested body is a stack exhaustion dressed as a message -- and step 7 is
# the LAST step precisely so a frame has to be authentic before it can try.
# The cap is belt and braces for the authenticated case: a compromised
# gateway should not be able to take the server down with its own key.
MAX_JSON_DEPTH = 32


class FrameError(Exception):
    """Base. Every rejection below is a distinct type, because the server's
    response differs: some are counted per source, some per client_id, and
    one of them (sequence reuse) skips the graduated ladder entirely."""


class FrameTooShort(FrameError):
    """Fewer bytes than a frame needs. Not a rejection -- read more."""
    def __init__(self, needed: int):
        super().__init__(f"need {needed} bytes")
        self.needed = needed


class FrameLengthInvalid(FrameError):
    """Step 1. Below MIN_LENGTH or above MAX_FRAME_SIZE, decided before a
    body buffer is allocated."""


class FrameVersionUnsupported(FrameError):
    """0x01 is the single valid value. Reserved and unbranched."""


class ClientIdInvalid(FrameError):
    """Step 3, encoding half. Printable ASCII, null-padded right, no
    embedded nulls. Rejected before key lookup, so a malformed id cannot
    reach the key store."""


class ClientIdUnknown(FrameError):
    """Step 3, lookup half. §10.6: counted per SOURCE, never per client_id
    -- counting per client_id would let an attacker fill the counter table
    with ids of their choosing."""


class AuthenticationFailed(FrameError):
    """Step 4. A bad tag rejects the frame, does not process it, and alerts
    a human -- never a silent drop (§10.3)."""


class SequenceRejected(FrameError):
    """Step 5. Strictly-greater, per client_id per direction. Reuse is the
    signature of a cloned key and §10.6 sends it straight to the immediate
    path, bypassing the graduated ladder."""
    def __init__(self, got: int, floor: int):
        super().__init__(f"seq {got} is not above the last accepted {floor}")
        self.got, self.floor = got, floor


class MessageTypeUnknown(FrameError):
    """Step 6, and it happens AFTER verification on purpose: an attacker
    without a key must not be able to probe which type values exist."""
    def __init__(self, value: int):
        super().__init__(f"unknown message type 0x{value:02X}")
        self.value = value


class PayloadInvalid(FrameError):
    """Step 7. Not JSON, not an object, or nested past MAX_JSON_DEPTH."""


class MessageType:
    """§10.2. Values are wire constants; do not renumber."""
    HEARTBEAT = 0x01          # G->S  alive signal and state (§10.4)
    PULL_INVITE = 0x02        # S->G  invite_id, max_events, optional top-up
    EVENT_BATCH = 0x03        # G->S  up to max_events events, oldest first
    ACK = 0x04                # S->G  per row identity, after a durable commit
    ON_DEMAND_PULL = 0x05     # S->G  request a fresh LP read (§10.7)
    ON_DEMAND_RESULT = 0x06   # G->S  the LP reply, or "unreachable"
    ALERT = 0x07              # G->S  HMAC failure, identity mismatch, ...
    ROTATE_KEY = 0x10         # S->G  key rotation (§10.5)
    REVOKE = 0x11             # S->G  key revocation
    DISABLE = 0x12            # S->G  with reason; §8.1 governs when
    ENABLE = 0x13             # S->G
    POOL_REPLACE = 0x14       # S->G  new epoch, revoke-prior-available (§7.4)
    VALIDATION_QUERY = 0x20   # G->S  ticket-in: number read from escrow (§9)
    VALIDATION_RESPONSE = 0x21  # S->G  ALLOW with amount, or REJECT + reason
    REDEMPTION_RESULT = 0x22  # G->S  completion status read from the machine


KNOWN_TYPES = frozenset(
    v for k, v in vars(MessageType).items() if not k.startswith("_"))

# Which direction each type may travel. A HEARTBEAT arriving AT a gateway is
# not a heartbeat, it is a replayed frame pointed the wrong way, and §10.1
# names message-confusion as a thing the header exists to prevent.
GATEWAY_TO_SERVER = frozenset({
    MessageType.HEARTBEAT, MessageType.EVENT_BATCH,
    MessageType.ON_DEMAND_RESULT, MessageType.ALERT,
    MessageType.VALIDATION_QUERY, MessageType.REDEMPTION_RESULT})
SERVER_TO_GATEWAY = frozenset({
    MessageType.PULL_INVITE, MessageType.ACK, MessageType.ON_DEMAND_PULL,
    MessageType.ROTATE_KEY, MessageType.REVOKE, MessageType.DISABLE,
    MessageType.ENABLE, MessageType.POOL_REPLACE,
    MessageType.VALIDATION_RESPONSE})
assert GATEWAY_TO_SERVER | SERVER_TO_GATEWAY == KNOWN_TYPES
assert not (GATEWAY_TO_SERVER & SERVER_TO_GATEWAY)


def encode_client_id(client_id: str) -> bytes:
    """24 bytes, printable ASCII, null-padded on the right."""
    raw = client_id.encode("ascii", errors="strict")
    if not raw:
        raise ClientIdInvalid("client_id is empty")
    if len(raw) > CLIENT_ID_BYTES:
        raise ClientIdInvalid(
            f"client_id is {len(raw)} bytes; the header field is "
            f"{CLIENT_ID_BYTES}")
    if any(b < 0x20 or b > 0x7E for b in raw):
        raise ClientIdInvalid("client_id must be printable ASCII")
    return raw.ljust(CLIENT_ID_BYTES, b"\x00")


def decode_client_id(raw: bytes) -> str:
    """The inverse, and strict about it.

    NULL-PADDED ON THE RIGHT, with no embedded nulls -- so "A\\x00B" padded
    out is not a valid field even though it is 24 bytes. Accepting it would
    make two different ids compare equal after a naive strip, which is a
    key-lookup confusion.
    """
    if len(raw) != CLIENT_ID_BYTES:
        raise ClientIdInvalid(f"client_id field is {len(raw)} bytes")
    stripped = raw.rstrip(b"\x00")
    if not stripped:
        raise ClientIdInvalid("client_id is all nulls")
    if b"\x00" in stripped:
        raise ClientIdInvalid("client_id has an embedded null")
    if any(b < 0x20 or b > 0x7E for b in stripped):
        raise ClientIdInvalid("client_id is not printable ASCII")
    return stripped.decode("ascii")


def _tag(key: bytes, covered: bytes) -> bytes:
    """HMAC-SHA256 truncated to 16 bytes, over EVERY byte of the frame
    except the tag -- the length prefix included.

    §10.1: "A tag over the payload alone would leave client_id and seq
    rewritable outside tag coverage." The length prefix is in there too, so
    a frame cannot be re-described as a shorter or longer one.
    """
    return hmac.new(key, covered, sha256).digest()[:TAG_BYTES]


def encode(*, client_id: str, message_type: int, seq: int, payload: dict,
           key: bytes) -> bytes:
    """Build one authenticated frame."""
    if message_type not in KNOWN_TYPES:
        raise MessageTypeUnknown(message_type)
    if not isinstance(payload, dict):
        raise PayloadInvalid("payload must be a JSON object")
    if not 0 <= seq < 2 ** 64:
        raise ValueError(f"seq {seq} does not fit in {SEQ_BYTES} bytes")
    body = json.dumps(payload, separators=(",", ":"),
                      sort_keys=True).encode("utf-8")
    length = HEADER_BYTES + len(body) + TAG_BYTES
    if length > MAX_FRAME_SIZE:
        raise FrameLengthInvalid(
            f"frame would be {length} bytes, over MAX_FRAME_SIZE "
            f"{MAX_FRAME_SIZE}")
    covered = (struct.pack(">I", length) + bytes([VERSION])
               + encode_client_id(client_id) + bytes([message_type])
               + struct.pack(">Q", seq) + body)
    return covered + _tag(key, covered)


@dataclass(frozen=True)
class Frame:
    client_id: str
    message_type: int
    seq: int
    payload: dict
    size: int          # total bytes consumed off the wire


def json_depth(body: bytes) -> int:
    """Maximum nesting depth, counted WITHOUT parsing.

    A scanner rather than json.loads, because the point is to decide
    whether parsing is safe. It tracks string state and escapes so a brace
    inside a string literal does not count -- the same mistake a naive
    guard makes, and this project has made six times elsewhere.
    """
    depth = max_depth = 0
    in_string = escaped = False
    for byte in body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:        # backslash
                escaped = True
            elif byte == 0x22:        # quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):    # { [
            depth += 1
            max_depth = max(max_depth, depth)
        elif byte in (0x7D, 0x5D):    # } ]
            depth -= 1
    return max_depth


def decode(data: bytes, *, key_for: Callable[[str], bytes | None],
           seq_floor: Callable[[str], int] | None = None,
           expect_direction: frozenset[int] | None = None) -> Frame:
    """Verify and parse one frame, in the order §10.1 specifies.

    `key_for` returns the pre-shared key for a client_id, or None if it is
    unknown. `seq_floor` returns the last accepted seq for that client_id;
    omit it to skip step 5 (useful for a codec test, never in production).
    `expect_direction` is the set of message types legal on this side.

    Raises FrameTooShort when `data` holds less than a whole frame. That is
    not a rejection -- a TCP reader should read more and call again.
    """
    # --- 1. length, before allocating anything -----------------------------
    if len(data) < LENGTH_BYTES:
        raise FrameTooShort(LENGTH_BYTES)
    (length,) = struct.unpack(">I", data[:LENGTH_BYTES])
    if length < MIN_LENGTH:
        raise FrameLengthInvalid(f"length {length} is below the minimum "
                                 f"{MIN_LENGTH}")
    if length > MAX_FRAME_SIZE:
        raise FrameLengthInvalid(f"length {length} is over MAX_FRAME_SIZE "
                                 f"{MAX_FRAME_SIZE}")
    total = LENGTH_BYTES + length
    if len(data) < total:
        raise FrameTooShort(total)

    # --- 2. header by fixed offset, no parser ------------------------------
    at = LENGTH_BYTES
    version = data[at]; at += 1
    if version != VERSION:
        raise FrameVersionUnsupported(f"version 0x{version:02X}")
    raw_client = data[at:at + CLIENT_ID_BYTES]; at += CLIENT_ID_BYTES
    message_type = data[at]; at += 1
    (seq,) = struct.unpack(">Q", data[at:at + SEQ_BYTES]); at += SEQ_BYTES
    body = data[at:total - TAG_BYTES]
    tag = data[total - TAG_BYTES:total]

    # --- 3. client_id: encoding, then lookup -------------------------------
    client_id = decode_client_id(raw_client)
    key = key_for(client_id)
    if key is None:
        raise ClientIdUnknown(client_id)

    # --- 4. authenticate ---------------------------------------------------
    if not hmac.compare_digest(_tag(key, data[:total - TAG_BYTES]), tag):
        raise AuthenticationFailed(f"bad tag from {client_id}")

    # --- 5. replay ---------------------------------------------------------
    if seq_floor is not None:
        floor = seq_floor(client_id)
        if seq <= floor:
            raise SequenceRejected(seq, floor)

    # --- 6. dispatch -------------------------------------------------------
    if message_type not in KNOWN_TYPES:
        raise MessageTypeUnknown(message_type)
    if expect_direction is not None and message_type not in expect_direction:
        raise MessageTypeUnknown(message_type)

    # --- 7. and only now, the parser ---------------------------------------
    if json_depth(body) > MAX_JSON_DEPTH:
        raise PayloadInvalid(f"payload nests deeper than {MAX_JSON_DEPTH}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise PayloadInvalid(str(e)) from e
    if not isinstance(payload, dict):
        raise PayloadInvalid("payload is not a JSON object")

    return Frame(client_id=client_id, message_type=message_type, seq=seq,
                 payload=payload, size=total)
