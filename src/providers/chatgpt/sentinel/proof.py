"""JavaScript-compatible serialization, FNV-1a hashing, and proof-of-work solver."""

from __future__ import annotations

import base64
import json
import math
import re
import time
from datetime import datetime
from typing import Any

from ..errors import SentinelCompatibilityError
from .spec import DEFAULT_SENTINEL_SPEC, SentinelSpec

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_CONTROL_ESCAPE = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}
_UNDEFINED = object()


def _utf16_code_units(text: str):
    index = 0
    length = len(text)
    while index < length:
        code = ord(text[index])
        if 0xD800 <= code <= 0xDBFF and index + 1 < length:
            low = ord(text[index + 1])
            if 0xDC00 <= low <= 0xDFFF:
                yield code
                yield low
                index += 2
                continue
        if code <= 0xFFFF:
            yield code
        else:
            code -= 0x10000
            yield 0xD800 + (code >> 10)
            yield 0xDC00 + (code & 0x3FF)
        index += 1


def _fnv1a_step_utf16(hash_value: int, text: str) -> int:
    for unit in _utf16_code_units(text):
        hash_value ^= unit
        hash_value = (hash_value * 16777619) & 0xFFFFFFFF
    return hash_value


def _fnv1a_finalize(hash_value: int) -> str:
    hash_value ^= hash_value >> 16
    hash_value = (hash_value * 2246822507) & 0xFFFFFFFF
    hash_value ^= hash_value >> 13
    hash_value = (hash_value * 3266489909) & 0xFFFFFFFF
    hash_value ^= hash_value >> 16
    return f"{hash_value:08x}"


def _fnv1a_avalanche(text: str) -> str:
    """Match the Sentinel SDK's UTF-16 FNV-1a and avalanche mix."""
    return _fnv1a_finalize(_fnv1a_step_utf16(2166136261, text))


def _json_string(value: str) -> str:
    parts = ['"']
    index = 0
    length = len(value)
    while index < length:
        code = ord(value[index])
        escaped = _CONTROL_ESCAPE.get(code)
        if escaped is not None:
            parts.append(escaped)
        elif code < 0x20:
            parts.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDBFF and index + 1 < length:
            low = ord(value[index + 1])
            if 0xDC00 <= low <= 0xDFFF:
                parts.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                index += 2
                continue
            parts.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDFFF:
            parts.append(f"\\u{code:04x}")
        else:
            parts.append(value[index])
        index += 1
    parts.append('"')
    return "".join(parts)


def _js_number(value: int | float) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if not math.isfinite(value):
        return "null"
    if value == 0.0:
        return "0"
    sign = "-" if value < 0 else ""
    short = repr(abs(float(value))).lower()
    if "e" in short:
        mantissa_part, _, exp_part = short.partition("e")
        exponent = int(exp_part)
        mantissa = mantissa_part.replace(".", "").rstrip("0") or "0"
    elif "." in short:
        int_part, _, frac_part = short.partition(".")
        if int_part == "0":
            stripped = frac_part.lstrip("0")
            exponent = -(len(frac_part) - len(stripped) + 1)
            mantissa = stripped.rstrip("0") or "0"
        else:
            exponent = len(int_part) - 1
            mantissa = (int_part + frac_part).rstrip("0") or "0"
    else:
        exponent = len(short) - 1
        mantissa = short.rstrip("0") or "0"
    if -6 <= exponent < 21:
        point = exponent + 1
        if point >= len(mantissa):
            return sign + mantissa + ("0" * (point - len(mantissa)))
        if point > 0:
            return sign + mantissa[:point] + "." + mantissa[point:]
        return sign + "0." + ("0" * -point) + mantissa
    rendered = mantissa if len(mantissa) == 1 else f"{mantissa[0]}.{mantissa[1:]}"
    exp_sign = "+" if exponent >= 0 else "-"
    return f"{sign}{rendered}e{exp_sign}{abs(exponent)}"


def _json_value(value: Any) -> str:
    if value is None or value is _UNDEFINED:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _js_number(value)
    if isinstance(value, str):
        return _json_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_json_value(item) for item in value) + "]"
    if isinstance(value, dict):
        items = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise SentinelCompatibilityError("Sentinel context contained a non-string key")
            if item is _UNDEFINED or callable(item):
                continue
            items.append(f"{_json_string(key)}:{_json_value(item)}")
        return "{" + ",".join(items) + "}"
    raise SentinelCompatibilityError("Sentinel context contained an unsupported JSON value")


def _btoa(text: str) -> str:
    try:
        raw = text.encode("latin-1")
    except UnicodeEncodeError as error:
        raise SentinelCompatibilityError("btoa input out of Latin-1 range") from error
    return base64.b64encode(raw).decode("ascii")


def _atob(encoded: str) -> str:
    if not isinstance(encoded, str):
        raise SentinelCompatibilityError("atob input must be a base64 string")
    cleaned = re.sub(r"\s+", "", encoded)
    if not cleaned:
        raise SentinelCompatibilityError("atob input was empty")
    padding = (-len(cleaned)) % 4
    if padding:
        cleaned += "=" * padding
    try:
        return base64.b64decode(cleaned, validate=True).decode("latin-1")
    except Exception as error:
        raise SentinelCompatibilityError("atob received invalid base64 input") from error


def _xor_strings(data: str, key: str) -> str:
    if not key:
        raise SentinelCompatibilityError("Sentinel VM key must be non-empty")
    key_codes = [ord(ch) for ch in key]
    key_len = len(key_codes)
    return "".join(chr(ord(ch) ^ key_codes[index % key_len]) for index, ch in enumerate(data))


def _format_js_date(moment: datetime | None = None) -> str:
    local_dt = moment if moment is not None else datetime.now().astimezone()
    offset = local_dt.utcoffset()
    total_minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    sign = "+" if total_minutes >= 0 else "-"
    abs_minutes = abs(total_minutes)
    hh, mm = divmod(abs_minutes, 60)
    gmt = f"GMT{sign}{hh:02d}{mm:02d}"
    tz_label = f"GMT{sign}{hh:02d}:{mm:02d}"
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    day_name = weekdays[local_dt.weekday()]
    month_name = months[local_dt.month - 1]
    return (
        f"{day_name} {month_name} {local_dt.day:02d} {local_dt.year:04d} "
        f"{local_dt:%H:%M:%S} {gmt} ({tz_label})"
    )


def _platform_for_user_agent(user_agent: str) -> str:
    if "Windows" in user_agent:
        return "Win32"
    if "Macintosh" in user_agent or "Mac OS" in user_agent:
        return "MacIntel"
    return "Linux x86_64"


def _solve_pow(
    context: list[Any],
    seed: str,
    difficulty: str,
    *,
    spec: SentinelSpec = DEFAULT_SENTINEL_SPEC,
    started_ns: int | None = None,
) -> str:
    """Solve Sentinel proof-of-work or raise ``SentinelCompatibilityError``."""
    if not isinstance(seed, str) or not seed:
        raise SentinelCompatibilityError("Sentinel proof-of-work seed was missing")
    if (
        not isinstance(difficulty, str)
        or not 1 <= len(difficulty) <= 8
        or any(ch not in _HEX_DIGITS for ch in difficulty)
    ):
        raise SentinelCompatibilityError("Sentinel proof-of-work difficulty was invalid")

    nonce_idx = spec.nonce_index
    elapsed_idx = spec.elapsed_index
    if nonce_idx < 0 or nonce_idx >= elapsed_idx or elapsed_idx >= len(context):
        raise SentinelCompatibilityError("Sentinel context does not match spec slot indices")

    prefix_json = "[" + ",".join(_json_value(item) for item in context[:nonce_idx]) + ","
    middle_json = (
        "," + ",".join(_json_value(item) for item in context[nonce_idx + 1 : elapsed_idx]) + ","
    )
    suffix_json = "," + ",".join(_json_value(item) for item in context[elapsed_idx + 1 :]) + "]"

    seed_hash = _fnv1a_step_utf16(2166136261, seed)
    target = difficulty.lower()
    target_len = len(target)
    origin_ns = time.perf_counter_ns() if started_ns is None else started_ns

    for nonce in range(spec.max_attempts):
        elapsed_ms = max(0, round((time.perf_counter_ns() - origin_ns) / 1_000_000))
        raw_json = f"{prefix_json}{nonce}{middle_json}{elapsed_ms}{suffix_json}".encode("utf-8")
        encoded_bytes = base64.b64encode(raw_json)
        h = seed_hash
        for byte in encoded_bytes:
            h ^= byte
            h = (h * 16777619) & 0xFFFFFFFF
        if _fnv1a_finalize(h)[:target_len] <= target:
            context[nonce_idx] = nonce
            context[elapsed_idx] = elapsed_ms
            return encoded_bytes.decode("ascii")

    raise SentinelCompatibilityError("Sentinel proof-of-work exceeded the attempt limit")


def _decode_initial_context(
    initial_proof: str, *, spec: SentinelSpec = DEFAULT_SENTINEL_SPEC
) -> list[Any]:
    if not isinstance(initial_proof, str) or not initial_proof.startswith(spec.initial_prefix):
        raise SentinelCompatibilityError("Sentinel initial proof had an unexpected prefix")
    if spec.proof_suffix and not initial_proof.endswith(spec.proof_suffix):
        raise SentinelCompatibilityError("Sentinel initial proof had an unexpected suffix")
    encoded = initial_proof[len(spec.initial_prefix) :]
    if spec.proof_suffix:
        encoded = encoded[: -len(spec.proof_suffix)]
    if not encoded:
        raise SentinelCompatibilityError("Sentinel initial proof was empty")
    try:
        raw = base64.b64decode(encoded, validate=True).decode("utf-8")
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise SentinelCompatibilityError("Sentinel initial proof could not be decoded") from error
    if not isinstance(value, list) or len(value) < spec.min_context_length:
        raise SentinelCompatibilityError("Sentinel initial proof had an unexpected context shape")
    return value


def generate_enforcement_proof(
    initial_proof: str,
    requirements: dict[str, Any],
    *,
    spec: SentinelSpec = DEFAULT_SENTINEL_SPEC,
) -> str:
    """Solve the ChatGPT Sentinel proof-of-work challenge from an initial context."""
    if not isinstance(requirements, dict):
        raise SentinelCompatibilityError("Sentinel requirements were not an object")
    pow_config = requirements.get("proofofwork")
    if not isinstance(pow_config, dict) or pow_config.get("required") is not True:
        raise SentinelCompatibilityError("Sentinel proof-of-work challenge was not required")
    seed = pow_config.get("seed")
    difficulty = pow_config.get("difficulty")
    context = _decode_initial_context(initial_proof, spec=spec)
    encoded = _solve_pow(context, seed, difficulty, spec=spec)
    return f"{spec.enforcement_prefix}{encoded}{spec.proof_suffix}"
