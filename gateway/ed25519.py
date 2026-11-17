"""Ed25519 (RFC 8032), in the standard library and nothing else.

WHY THIS FILE EXISTS AT ALL. §7.3 signs every validation-pool batch with
Ed25519, and Python's standard library has no Ed25519. The poller has one
dependency (pyserial) and a 32-bit Debian gateway is exactly the place a
second one -- a compiled one -- becomes an install problem. So this is the
RFC's own reference algorithm, written out: slow, small, and with nothing
to build.

WHAT "SLOW" MEANS, SO NOBODY IS SURPRISED. A verify is a few milliseconds
on a laptop and will be tens of milliseconds on a 32-bit gateway. §7.2
says of the batch-acceptance path "there is no latency pressure on this
path" -- a top-up arrives about twice an hour -- so that is affordable
there and would not be anywhere else. Do not use this for per-frame
authentication; frames use HMAC (§10.3) for exactly that reason.

WHAT IT IS NOT. It is not constant-time. On the gateway that is fine by
construction: a gateway only ever VERIFIES, with a public key, and there
is no secret for a timing channel to leak. SIGNING happens on the server
and uses the private key; the lab stand-in signs with this code, and a
production gateway service should sign with a vetted library or an HSM
(§7.3 lists private-key storage as an open item, and this is the same
question). The signatures are identical either way -- Ed25519 is
deterministic -- which tests/test_gateway_ed25519.py checks against the
RFC's vectors and, where the `cryptography` package happens to be
installed, against that implementation too.
"""

from __future__ import annotations

import hashlib

__all__ = ["public_key", "sign", "verify", "SEED_BYTES", "PUBLIC_KEY_BYTES",
           "SIGNATURE_BYTES"]

SEED_BYTES = 32
PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = 15112221349535400772501151409588531511454012693041857206046113283949847762202
# Extended homogeneous coordinates (X, Y, Z, T), x = X/Z, y = Y/Z, xy = T/Z.
_G = (_GX, _GY, 1, _GX * _GY % _P)
_IDENTITY = (0, 1, 1, 0)


def _add(a, b):
    A = (a[1] - a[0]) * (b[1] - b[0]) % _P
    B = (a[1] + a[0]) * (b[1] + b[0]) % _P
    C = 2 * a[3] * b[3] * _D % _P
    D = 2 * a[2] * b[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _mul(scalar: int, point):
    result = _IDENTITY
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _equal(a, b) -> bool:
    # x1/z1 == x2/z2 and y1/z1 == y2/z2, without an inversion.
    return ((a[0] * b[2] - b[0] * a[2]) % _P == 0
            and (a[1] * b[2] - b[1] * a[2]) % _P == 0)


def _compress(point) -> bytes:
    zinv = pow(point[2], _P - 2, _P)
    x = point[0] * zinv % _P
    y = point[1] * zinv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decompress(raw: bytes):
    """A point, or None when the 32 bytes are not one. None is an ordinary
    answer here -- a forged key or signature is the case being defended
    against, not an exceptional one."""
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        if sign:
            return None
        return (0, y, 1, 0)
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return (x, y, 1, x * y % _P)


def _expand(seed: bytes):
    if len(seed) != SEED_BYTES:
        raise ValueError(f"an Ed25519 private key seed is {SEED_BYTES} bytes, "
                         f"got {len(seed)}")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def _h_int(*parts: bytes) -> int:
    return int.from_bytes(hashlib.sha512(b"".join(parts)).digest(), "little")


def public_key(seed: bytes) -> bytes:
    """The 32-byte public key for a 32-byte private seed."""
    a, _ = _expand(seed)
    return _compress(_mul(a, _G))


def sign(seed: bytes, message: bytes) -> bytes:
    """SERVER SIDE ONLY. A gateway never holds a seed (§7.3)."""
    a, prefix = _expand(seed)
    pub = _compress(_mul(a, _G))
    r = _h_int(prefix, message) % _L
    big_r = _compress(_mul(r, _G))
    h = _h_int(big_r, pub, message) % _L
    s = (r + h * a) % _L
    return big_r + s.to_bytes(32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """True only for a valid signature. Never raises on bad input: every
    malformed key or signature is simply False, so a caller cannot turn a
    parse error into an accepted batch by catching the wrong exception."""
    if (not isinstance(public, (bytes, bytearray))
            or not isinstance(signature, (bytes, bytearray))
            or len(public) != PUBLIC_KEY_BYTES
            or len(signature) != SIGNATURE_BYTES):
        return False
    a_point = _decompress(bytes(public))
    r_point = _decompress(bytes(signature[:32]))
    if a_point is None or r_point is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        # RFC 8032 §5.1.7: a non-canonical S is rejected, or one message
        # has more than one valid signature.
        return False
    h = _h_int(bytes(signature[:32]), bytes(public), bytes(message)) % _L
    return _equal(_mul(s, _G), _add(r_point, _mul(h, a_point)))
