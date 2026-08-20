"""The work a player does before an instance is created.

kCTF's scheme, so the solver at pwn.red does these: repeated modular square
roots mod 2^1279-1. Sequential by construction -- a hundred cores get the answer
no sooner than one does -- and far cheaper to check than to do, which is the
whole trade.

    challenge   s.<b64(difficulty)>.<b64(x)>
    solution    s.<b64(y)>
"""

import base64
import secrets

VERSION = "s"
MODULUS = 2 ** 1279 - 1
CHALSIZE = 2 ** 128

# A solution is one number under the modulus; anything longer is not one, and
# should not get as far as being turned into an integer.
MAX_SOLUTION = 512


def sloth_root(x, diff, p):
    """The slow half: diff square roots, each waiting on the one before it."""
    exponent = (p + 1) // 4
    for _ in range(diff):
        x = pow(x, exponent, p) ^ 1
    return x


def sloth_square(y, diff, p):
    """The fast half: squaring back to where the roots started.

    p is Mersenne, so reduction is a shift and an add rather than a division --
    which is most of what makes checking an answer cheap.
    """
    bits = p.bit_length()
    for _ in range(diff):
        y = (y ^ 1) ** 2
        y = (y & p) + (y >> bits)
        y = (y & p) + (y >> bits)
        if y >= p:
            y -= p
    return y


def encode_number(num):
    size = (num.bit_length() // 24) * 3 + 3
    return str(base64.b64encode(num.to_bytes(size, "big")), "utf-8")


def decode_number(enc):
    return int.from_bytes(base64.b64decode(bytes(enc, "utf-8")), "big")


def encode_challenge(parts):
    return ".".join([VERSION] + [encode_number(part) for part in parts])


def decode_challenge(enc):
    parts = enc.split(".")
    if parts[0] != VERSION:
        raise ValueError("unknown challenge version")
    return [decode_number(part) for part in parts[1:]]


def new_challenge(diff):
    return encode_challenge([diff, secrets.randbelow(CHALSIZE)])


def solve(challenge):
    diff, x = decode_challenge(challenge)
    return encode_challenge([sloth_root(x, diff, MODULUS)])


def verify(challenge, solution):
    """Does this solve that? Never raises -- the solution is a player's to write."""
    if not isinstance(solution, str) or len(solution) > MAX_SOLUTION:
        return False
    try:
        diff, x = decode_challenge(challenge)
        y, = decode_challenge(solution)      # binascii.Error is a ValueError
    except ValueError:
        return False
    if not 0 <= y < MODULUS:
        return False
    squared = sloth_square(y, diff, MODULUS)
    return squared == x or MODULUS - squared == x
