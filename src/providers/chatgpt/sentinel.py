"""Python implementation of ChatGPT Sentinel's server-issued proof of work."""

from __future__ import annotations

import base64
import json
import math
import re
import time
from decimal import Decimal
from typing import Any

from .errors import SentinelCompatibilityError

INITIAL_TOKEN_PREFIX = "gAAAAAC"
ENFORCEMENT_TOKEN_PREFIX = "gAAAAAB"
_MAX_INITIAL_TOKEN_LENGTH = 65_536
_MAX_CONTEXT_ITEMS = 64
_MAX_ATTEMPTS = 500_000
_MAX_SEED_LENGTH = 4_096
_HEX_DIFFICULTY = re.compile(r"[0-9a-fA-F]{1,8}\Z")


def _js_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value) or value == 0:
        return "null" if not math.isfinite(value) else "0"

    absolute = abs(value)
    shortest = repr(value).lower()
    if 1e-6 <= absolute < 1e21:
        text = format(Decimal(shortest), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text

    if "e" not in shortest:
        shortest = format(Decimal(shortest).normalize(), "e")
    mantissa, exponent = shortest.split("e", 1)
    mantissa = mantissa.removesuffix(".0")
    exponent_value = int(exponent)
    sign = "+" if exponent_value >= 0 else ""
    return f"{mantissa}e{sign}{exponent_value}"


def _json_string(value: str) -> str:
    escaped = ['"']
    index = 0
    escapes = {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
    while index < len(value):
        character = value[index]
        codepoint = ord(character)
        if character in {'"', "\\"}:
            escaped.append("\\" + character)
        elif character in escapes:
            escaped.append(escapes[character])
        elif codepoint < 0x20:
            escaped.append(f"\\u{codepoint:04x}")
        elif 0xD800 <= codepoint <= 0xDBFF:
            if index + 1 < len(value) and 0xDC00 <= ord(value[index + 1]) <= 0xDFFF:
                low_surrogate = ord(value[index + 1])
                escaped.append(chr(0x10000 + ((codepoint - 0xD800) << 10) + low_surrogate - 0xDC00))
                index += 1
            else:
                escaped.append(f"\\u{codepoint:04x}")
        elif 0xDC00 <= codepoint <= 0xDFFF:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(character)
        index += 1
    escaped.append('"')
    return "".join(escaped)


def _json_value(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _json_string(value)
    if isinstance(value, (int, float)):
        return _js_number(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_json_value(item) for item in value) + "]"
    raise SentinelCompatibilityError("Sentinel context contains an unsupported value")


def _decode_initial_context(initial: str) -> list[Any]:
    if (
        not isinstance(initial, str)
        or len(initial) > _MAX_INITIAL_TOKEN_LENGTH
        or not initial.startswith(INITIAL_TOKEN_PREFIX)
    ):
        raise SentinelCompatibilityError("Sentinel returned an invalid initial proof")

    try:
        encoded = initial[len(INITIAL_TOKEN_PREFIX) :]
        if not encoded.endswith("~S"):
            raise ValueError("initial proof marker is missing")
        encoded = encoded[:-2]
        raw = base64.b64decode(encoded, validate=True)
        # JSON.parse converts every JSON number to an IEEE-754 Number. Parse
        # integers as floats too so re-stringification follows JS semantics.
        value = json.loads(raw.decode("utf-8"), parse_int=float, parse_float=float)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise SentinelCompatibilityError("Sentinel returned an invalid initial proof") from error

    if not isinstance(value, list) or not 10 <= len(value) <= _MAX_CONTEXT_ITEMS:
        raise SentinelCompatibilityError("Sentinel returned an unsupported proof context")
    return value


def _fnv1a_avalanche(value: str) -> str:
    state = 2_166_136_261
    encoded = value.encode("utf-16-le", errors="surrogatepass")
    for offset in range(0, len(encoded), 2):
        code_unit = encoded[offset] | (encoded[offset + 1] << 8)
        state = ((state ^ code_unit) * 16_777_619) & 0xFFFFFFFF

    state ^= state >> 16
    state = (state * 2_246_822_507) & 0xFFFFFFFF
    state ^= state >> 13
    state = (state * 3_266_489_909) & 0xFFFFFFFF
    state ^= state >> 16
    return f"{state & 0xFFFFFFFF:08x}"


def generate_enforcement_proof(initial: str, requirements: dict[str, Any]) -> str:
    """Solve the SDK's bounded proof-of-work challenge from server requirements.

    The initial token carries the SDK-generated per-session context. Its
    fingerprint and SID are reused; only the challenge nonce and elapsed time
    are updated, as the published SDK does while searching.
    """
    if not isinstance(requirements, dict):
        raise SentinelCompatibilityError("Sentinel returned invalid requirements")
    challenge = requirements.get("proofofwork")
    if not isinstance(challenge, dict) or challenge.get("required") is not True:
        raise SentinelCompatibilityError("Sentinel proof-of-work challenge is unsupported")

    seed = challenge.get("seed")
    difficulty = challenge.get("difficulty")
    if (
        not isinstance(seed, str)
        or not seed
        or len(seed) > _MAX_SEED_LENGTH
        or not isinstance(difficulty, str)
        or not _HEX_DIFFICULTY.fullmatch(difficulty)
    ):
        raise SentinelCompatibilityError("Sentinel proof-of-work requirements are invalid")

    context = _decode_initial_context(initial)
    started_ns = time.perf_counter_ns()
    candidate_context = context.copy()

    for nonce in range(_MAX_ATTEMPTS):
        elapsed_ms = (time.perf_counter_ns() - started_ns + 500_000) // 1_000_000
        candidate_context[3] = nonce
        candidate_context[9] = elapsed_ms
        encoded = base64.b64encode(_json_value(candidate_context).encode("utf-8")).decode("ascii")
        digest = _fnv1a_avalanche(seed + encoded)
        if digest[: len(difficulty)] <= difficulty:
            return ENFORCEMENT_TOKEN_PREFIX + encoded + "~S"

    raise SentinelCompatibilityError("Sentinel proof-of-work search limit was reached")
