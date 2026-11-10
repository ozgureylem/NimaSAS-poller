"""The gateway-to-server frame (spec §10.1, §10.3).

The processing order IS the security property, so most of what follows
asserts the ORDER a frame is rejected in, not just that it is rejected.
§10.1: "With client_id inside the body, the server cannot select a
verification key without parsing the body, so an adversarially-nested
payload reaches the parser before authentication can reject it."
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.frame import (  # noqa: E402
    CLIENT_ID_BYTES, GATEWAY_TO_SERVER, HEADER_BYTES, KNOWN_TYPES,
    LENGTH_BYTES, MAX_FRAME_SIZE, MAX_JSON_DEPTH, MIN_LENGTH,
    SERVER_TO_GATEWAY, TAG_BYTES, AuthenticationFailed, ClientIdInvalid,
    ClientIdUnknown, Frame, FrameLengthInvalid, FrameTooShort,
    FrameVersionUnsupported, MessageType, MessageTypeUnknown,
    PayloadInvalid, SequenceRejected, decode, decode_client_id, encode,
    encode_client_id, json_depth,
)

KEY = bytes(range(32))
OTHER_KEY = bytes(range(32, 64))
CID = "TROP-042"


def keys(client_id):
    return KEY if client_id == CID else None


def one(**over):
    args = dict(client_id=CID, message_type=MessageType.HEARTBEAT, seq=1,
                payload={"alive": True}, key=KEY)
    args.update(over)
    return encode(**args)


# --- the shape ----------------------------------------------------------

def test_the_header_is_the_width_the_spec_states():
    """34 header bytes, 50 minimum length, and the length covers everything
    after itself. Asserted because every offset below depends on it."""
    assert HEADER_BYTES == 34
    assert MIN_LENGTH == 50
    frame = one(payload={})
    (length,) = struct.unpack(">I", frame[:4])
    assert length == len(frame) - LENGTH_BYTES
    assert length == HEADER_BYTES + len(b"{}") + TAG_BYTES


def test_a_frame_round_trips():
    frame = one(payload={"queue_depth": 3, "pool": {"available": 41}})
    got = decode(frame, key_for=keys)
    assert got.client_id == CID
    assert got.message_type == MessageType.HEARTBEAT
    assert got.seq == 1
    assert got.payload == {"queue_depth": 3, "pool": {"available": 41}}
    assert got.size == len(frame)


def test_the_tag_covers_the_length_prefix_too():
    """§10.1: "A tag over the payload alone would leave client_id and seq
    rewritable outside tag coverage." The length is in the coverage as
    well, so a frame cannot be re-described as a different size."""
    frame = bytearray(one())
    (length,) = struct.unpack(">I", bytes(frame[:4]))
    frame[:4] = struct.pack(">I", length)          # unchanged: still valid
    assert decode(bytes(frame), key_for=keys).seq == 1
    frame += b"\x00"                               # trailing junk ignored
    assert decode(bytes(frame), key_for=keys).size == len(frame) - 1


@pytest.mark.parametrize("field,offset", [
    ("version", 4), ("client_id", 5), ("message_type", 29), ("seq", 30),
])
def test_every_header_field_is_under_the_tag(field, offset):
    frame = bytearray(one())
    frame[offset] ^= 0x01
    with pytest.raises((AuthenticationFailed, ClientIdUnknown,
                        ClientIdInvalid, FrameVersionUnsupported)):
        decode(bytes(frame), key_for=keys)


def test_a_flipped_payload_bit_fails_authentication():
    frame = bytearray(one(payload={"amount": 100}))
    frame[-TAG_BYTES - 1] ^= 0x01
    with pytest.raises(AuthenticationFailed):
        decode(bytes(frame), key_for=keys)


def test_another_gateways_key_does_not_verify():
    """A compromised gateway can only forge as itself (§10.3)."""
    frame = one(key=OTHER_KEY)
    with pytest.raises(AuthenticationFailed):
        decode(frame, key_for=keys)


# --- step 1: length, before any allocation ------------------------------

def test_a_short_read_is_NOT_a_rejection():
    """A TCP reader with half a frame must read more, not drop the
    connection. Distinguishing the two is why FrameTooShort exists."""
    frame = one()
    with pytest.raises(FrameTooShort) as e:
        decode(frame[:10], key_for=keys)
    assert e.value.needed == len(frame)
    with pytest.raises(FrameTooShort):
        decode(b"\x00\x00", key_for=keys)


def test_a_length_below_the_minimum_is_refused_before_anything_else():
    """Step 1 happens before the body buffer exists, so this must be
    rejected even though no key could ever be found for it."""
    bad = struct.pack(">I", MIN_LENGTH - 1) + b"\x00" * 64
    with pytest.raises(FrameLengthInvalid):
        decode(bad, key_for=keys)


def test_an_oversized_length_is_refused_WITHOUT_reading_that_many_bytes():
    """The point of checking first: a 4-byte header must not be able to
    make the server allocate 4 GB. Only four bytes are supplied here."""
    claim = struct.pack(">I", MAX_FRAME_SIZE + 1)
    with pytest.raises(FrameLengthInvalid):
        decode(claim, key_for=keys)


def test_MAX_FRAME_SIZE_leaves_headroom_over_the_batch_bound():
    """§10.4 caps a batch at 64 KB and says MAX_FRAME_SIZE "must sit above
    this with headroom". A legal batch must never be refused here."""
    assert MAX_FRAME_SIZE >= 2 * 64 * 1024


# --- step 3: client_id, encoding then lookup ----------------------------

def test_client_id_round_trips_and_is_null_padded():
    raw = encode_client_id(CID)
    assert len(raw) == CLIENT_ID_BYTES
    assert raw.endswith(b"\x00")
    assert decode_client_id(raw) == CID


@pytest.mark.parametrize("bad", [
    b"\x00" * CLIENT_ID_BYTES,                       # all nulls
    b"A\x00B" + b"\x00" * (CLIENT_ID_BYTES - 3),     # embedded null
    b"A\x01B" + b"\x00" * (CLIENT_ID_BYTES - 3),     # control character
    b"short",                                        # wrong width
])
def test_a_malformed_client_id_is_refused_BEFORE_key_lookup(bad):
    """An embedded null is the interesting one: "A\\x00B" and "A" compare
    equal after a naive strip, which is a key-lookup confusion."""
    looked_up = []

    def spy(cid):
        looked_up.append(cid)
        return KEY

    with pytest.raises(ClientIdInvalid):
        decode_client_id(bad)
    if len(bad) == CLIENT_ID_BYTES:
        frame = bytearray(one())
        frame[5:5 + CLIENT_ID_BYTES] = bad
        with pytest.raises(ClientIdInvalid):
            decode(bytes(frame), key_for=spy)
        assert looked_up == [], (
            "the key store was consulted for a malformed client_id")


def test_an_unknown_client_id_is_its_own_error():
    """§10.6 counts these per SOURCE, never per client_id -- counting per
    client_id lets an attacker fill the table with ids of their choosing.
    The server can only do that if the codec tells it which case this is."""
    with pytest.raises(ClientIdUnknown):
        decode(one(client_id="NOBODY"), key_for=lambda c: None)


# --- step 5: replay -----------------------------------------------------

def test_seq_must_be_STRICTLY_greater():
    assert decode(one(seq=8), key_for=keys, seq_floor=lambda c: 7).seq == 8
    for reused in (7, 6, 0):
        with pytest.raises(SequenceRejected):
            decode(one(seq=reused), key_for=keys, seq_floor=lambda c: 7)


def test_the_sequence_check_runs_AFTER_authentication():
    """Order matters: an unauthenticated frame must not be able to move,
    probe, or even consult the server's sequence state."""
    asked = []
    frame = bytearray(one(seq=99, key=OTHER_KEY))
    with pytest.raises(AuthenticationFailed):
        decode(bytes(frame), key_for=keys,
               seq_floor=lambda c: asked.append(c) or 0)
    assert asked == [], "sequence state was consulted for a forged frame"


# --- step 6: dispatch, after verification -------------------------------

def test_an_unknown_message_type_is_rejected_AFTER_the_tag_is_checked():
    """"so the type space cannot be probed without a valid key". A bad type
    with a bad tag must read as an auth failure, not as a type error --
    otherwise the error itself answers the probe."""
    forged = bytearray(one(key=OTHER_KEY))
    forged[29] = 0x7F
    with pytest.raises(AuthenticationFailed):
        decode(bytes(forged), key_for=keys)

    signed = bytearray(one())
    signed[29] = 0x7F
    with pytest.raises(AuthenticationFailed):
        decode(bytes(signed), key_for=keys)   # the type is under the tag


def test_encode_refuses_an_unknown_type_outright():
    with pytest.raises(MessageTypeUnknown):
        encode(client_id=CID, message_type=0x7F, seq=1, payload={}, key=KEY)


def test_the_direction_sets_partition_every_known_type():
    """A HEARTBEAT arriving AT a gateway is a replayed frame pointed the
    wrong way. §10.1 names message-confusion as a thing the header exists
    to prevent, so the two sets must cover everything and overlap in
    nothing."""
    assert GATEWAY_TO_SERVER | SERVER_TO_GATEWAY == KNOWN_TYPES
    assert not (GATEWAY_TO_SERVER & SERVER_TO_GATEWAY)


def test_a_frame_pointed_the_wrong_way_is_refused():
    frame = one(message_type=MessageType.HEARTBEAT)
    assert decode(frame, key_for=keys,
                  expect_direction=GATEWAY_TO_SERVER).seq == 1
    with pytest.raises(MessageTypeUnknown):
        decode(frame, key_for=keys, expect_direction=SERVER_TO_GATEWAY)


# --- step 7: the parser, last ------------------------------------------

def test_json_depth_ignores_braces_inside_strings():
    """The guard this project gets wrong: a brace in a string literal is
    not nesting. Six text-matching guards have made that mistake."""
    assert json_depth(b'{"a":"{{{{{{{{"}') == 1
    assert json_depth(br'{"a":"\""}') == 1
    assert json_depth(b'{"a":{"b":{"c":1}}}') == 3
    assert json_depth(b'{"a":[[[1]]]}') == 4


def test_a_deeply_nested_payload_is_refused_without_parsing():
    deep = {"a": None}
    node = deep
    for _ in range(MAX_JSON_DEPTH + 5):
        node["a"] = {"a": None}
        node = node["a"]
    frame = one(payload=deep)
    with pytest.raises(PayloadInvalid, match="nests deeper"):
        decode(frame, key_for=keys)


def test_a_nested_payload_only_gets_that_far_WITH_a_valid_key():
    """Step 7 is last so an attacker without a key never reaches the
    parser at all -- the whole reason client_id lives in the header."""
    deep = {"a": None}
    node = deep
    for _ in range(MAX_JSON_DEPTH + 5):
        node["a"] = {"a": None}
        node = node["a"]
    with pytest.raises(AuthenticationFailed):
        decode(one(payload=deep, key=OTHER_KEY), key_for=keys)


def test_a_payload_that_is_not_an_object_is_refused():
    with pytest.raises(PayloadInvalid):
        encode(client_id=CID, message_type=MessageType.ALERT, seq=1,
               payload=[1, 2, 3], key=KEY)


def test_a_payload_that_is_not_utf8_json_is_refused():
    frame = bytearray(one(payload={"a": 1}))
    body_at = LENGTH_BYTES + HEADER_BYTES
    frame[body_at:len(frame) - TAG_BYTES] = b"\xff\xfe not json"
    # re-sign so it gets past step 4 and the parser is really what refuses
    import hmac as _h
    from hashlib import sha256 as _s
    frame[:4] = struct.pack(">I", HEADER_BYTES + len(b"\xff\xfe not json")
                            + TAG_BYTES)
    covered = bytes(frame[:len(frame) - TAG_BYTES])
    frame[len(frame) - TAG_BYTES:] = _h.new(KEY, covered, _s).digest()[:TAG_BYTES]
    with pytest.raises(PayloadInvalid):
        decode(bytes(frame), key_for=keys)


# --- the lab gateway stays as it is -------------------------------------

def test_NOTHING_IN_THE_POLLER_IMPORTS_THIS():
    """Operator, 2026-11-10: "the gateway I am currently using for the lab
    should work the way it is. No HMAC, it is dedicated for lab testing
    only. No server connection. Please keep it this way."

    Asserted rather than trusted. The lab SD card runs the poller and the
    deploy scripts; if either grows an import of this package, the lab
    gateway stops being the thing that was measured.
    """
    import ast

    offenders = []
    for path in sorted(list((ROOT / "examples").glob("*.py"))
                       + list((ROOT / "deploy").glob("*.py"))
                       + list((ROOT / "saspy").glob("*.py"))):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n == "gateway" or n.startswith("gateway.") for n in names):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, (
        f"these import the server-link package: {sorted(set(offenders))}. "
        f"The lab gateway runs with no HMAC and no server connection, and "
        f"that is deliberate")
