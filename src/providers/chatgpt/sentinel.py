"""ChatGPT Sentinel QuickJS-NG runtime and server-issued proof-of-work solver."""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import math
import platform
import re
import secrets
import time
from decimal import Decimal
from typing import Any

import quickjs

from .errors import SentinelCompatibilityError
from .resources import SIGN_SCRIPT, SentinelSDK

INITIAL_TOKEN_PREFIX = "gAAAAAC"
ENFORCEMENT_TOKEN_PREFIX = "gAAAAAB"
_MAX_INITIAL_TOKEN_LENGTH = 65_536
_MAX_CONTEXT_ITEMS = 64
_MAX_ATTEMPTS = 500_000
_MAX_SEED_LENGTH = 4_096
_HEX_DIFFICULTY = re.compile(r"[0-9a-fA-F]{1,8}\Z")
_RUNTIME_MEMORY_LIMIT_BYTES = 32 * 1024 * 1024
_RUNTIME_STACK_SIZE_BYTES = 1024 * 1024
_RUNTIME_TIMEOUT_SECONDS = 8.0
_MAX_SDK_BYTES = 8 * 1024 * 1024
_INITIAL_RANDOM_BYTES = 65_536


@functools.lru_cache(maxsize=1)
def _load_sign_source() -> str:
    return SIGN_SCRIPT.read_text(encoding="utf-8")


def _platform_label() -> str:
    system = platform.system()
    machine = platform.machine()
    return f"{system} {machine}".strip() or "Linux x86_64"


def _random_hex(byte_length: int = 2048) -> str:
    size = max(16, min(int(byte_length), 65_536))
    return secrets.token_hex(size)


def _configure_quickjs_context(context: quickjs.Context) -> None:
    context.set_memory_limit(_RUNTIME_MEMORY_LIMIT_BYTES)
    context.set_max_stack_size(_RUNTIME_STACK_SIZE_BYTES)
    context.set_time_limit(_RUNTIME_TIMEOUT_SECONDS)
    context.add_callable("__siopechatRandomHex", _random_hex)
    context.set("__siopechatRandomPool", secrets.token_hex(_INITIAL_RANDOM_BYTES))


def _wait_for_completion(
    context: quickjs.Context,
    timeout_seconds: float = _RUNTIME_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while context.execute_pending_job():
        if time.monotonic() >= deadline:
            raise SentinelCompatibilityError("Sentinel JavaScript execution timed out")

    while True:
        step = int(context.eval("__siopechatStep()"))
        while context.execute_pending_job():
            if time.monotonic() >= deadline:
                raise SentinelCompatibilityError("Sentinel JavaScript execution timed out")
        if step == 0:
            break
        if time.monotonic() >= deadline:
            raise SentinelCompatibilityError("Sentinel JavaScript execution timed out")
        if step < 0:
            if not context.execute_pending_job():
                raise SentinelCompatibilityError(
                    "Sentinel SDK stopped before completing proof generation"
                )
            continue
        if step > 1:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SentinelCompatibilityError("Sentinel JavaScript execution timed out")
            time.sleep(min((step - 2) / 1000.0, remaining, 0.02))

    error = context.get("__siopechatError")
    if isinstance(error, str) and error:
        raise SentinelCompatibilityError(f"Sentinel JavaScript error: {error}")


class SentinelRuntime:
    """Isolated QuickJS-NG context that executes the verified Sentinel SDK."""

    def __init__(self, sdk: SentinelSDK, *, user_agent: str = "Mozilla/5.0") -> None:
        if not isinstance(sdk, SentinelSDK) or not sdk.implementation or not sdk.source:
            raise SentinelCompatibilityError("Sentinel SDK resource is invalid")
        source_bytes = sdk.source.encode("utf-8")
        if len(source_bytes) > _MAX_SDK_BYTES:
            raise SentinelCompatibilityError("Sentinel SDK exceeds size limit")
        if hashlib.sha256(source_bytes).hexdigest() != sdk.sha256:
            raise SentinelCompatibilityError("Sentinel SDK integrity check failed")

        self._sdk = sdk
        self._closed = False
        self._initial: str | None = None
        self._requests_started = False
        self._sdk_factory_enabled = False
        context = quickjs.Context()
        try:
            _configure_quickjs_context(context)
            config = {
                "implementation": sdk.implementation,
                "scripts": list(sdk.scripts) or [sdk.implementation],
                "userAgent": user_agent or "Mozilla/5.0",
                "platform": _platform_label(),
            }
            context.set(
                "__siopechatConfigJson",
                json.dumps(config, ensure_ascii=False, separators=(",", ":")),
            )
            context.eval(_load_sign_source())
            # Compile this pinned SDK once; later requests get a fresh SDK closure
            # without reparsing the full source in this reused context.
            if sdk.source.lstrip().startswith("var SentinelSDK="):
                context.set("__siopechatSdkSource", sdk.source)
                try:
                    context.eval(
                        "globalThis.__siopechatSdkFactory = new Function("
                        'globalThis.__siopechatSdkSource + "\\nreturn SentinelSDK;");'
                        "delete globalThis.__siopechatSdkSource;"
                        "const sdk = globalThis.__siopechatSdkFactory.call(globalThis);"
                        'if (!sdk || typeof sdk.init !== "function" || '
                        'typeof sdk.token !== "function") throw new Error("invalid SDK factory result");'
                        "globalThis.SentinelSDK = sdk;"
                    )
                    self._sdk_factory_enabled = True
                except Exception:
                    context.eval(
                        "delete globalThis.__siopechatSdkSource;"
                        "delete globalThis.__siopechatSdkFactory;"
                    )
                    context.eval(sdk.source)
            else:
                context.eval(sdk.source)
            while context.execute_pending_job():
                pass
        except SentinelCompatibilityError:
            del context
            raise
        except Exception as error:
            del context
            raise SentinelCompatibilityError(
                f"Failed to initialize Sentinel QuickJS runtime: {error}"
            ) from error
        self._context: quickjs.Context | None = context

    def close(self) -> None:
        if self._closed or self._context is None:
            return
        self._closed = True
        try:
            self._context.eval("__siopechatDispose()")
        except Exception:
            pass
        self._context = None

    def __enter__(self) -> SentinelRuntime:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _require_context(self) -> quickjs.Context:
        if self._closed or self._context is None:
            raise SentinelCompatibilityError("Sentinel QuickJS runtime is closed")
        return self._context

    def create_initial_proof(self) -> str:
        """Execute ``SentinelSDK.init`` and return the initial ``gAAAAAC...~S`` proof."""
        context = self._require_context()
        try:
            context.set_time_limit(_RUNTIME_TIMEOUT_SECONDS)
            if self._requests_started:
                context.eval("__siopechatDispose()")
                context.set("__siopechatRandomPool", secrets.token_hex(_INITIAL_RANDOM_BYTES))
                context.eval("__siopechatConfigure(JSON.parse(globalThis.__siopechatConfigJson))")
                context.eval("__siopechatResetRequest(false)")
                if self._sdk_factory_enabled:
                    context.eval(
                        "globalThis.SentinelSDK = "
                        "globalThis.__siopechatSdkFactory.call(globalThis);"
                    )
                else:
                    context.eval(self._sdk.source)
                while context.execute_pending_job():
                    pass
            self._requests_started = True
            self._initial = None
            context.eval("__siopechatStartInitial()")
            _wait_for_completion(context)
            initial = context.get("__siopechatInitial")
        except SentinelCompatibilityError:
            raise
        except Exception as error:
            raise SentinelCompatibilityError(
                f"Sentinel initial proof generation failed: {error}"
            ) from error

        if not isinstance(initial, str) or not initial.startswith(INITIAL_TOKEN_PREFIX):
            raise SentinelCompatibilityError("Sentinel returned an invalid initial proof")
        self._initial = initial
        return initial

    def create_enforcement_tokens(
        self,
        requirements: dict[str, Any],
    ) -> tuple[str, str, str | None]:
        """Execute ``SentinelSDK.token`` against server chat-requirements."""
        if not isinstance(requirements, dict):
            raise SentinelCompatibilityError("Sentinel returned invalid requirements")
        context = self._require_context()
        try:
            context.set_time_limit(_RUNTIME_TIMEOUT_SECONDS)
            context.set(
                "__siopechatRequirementsJson",
                json.dumps(requirements, ensure_ascii=False, separators=(",", ":")),
            )
            context.eval("__siopechatStartToken()")
            _wait_for_completion(context)
            initial = context.get("__siopechatInitial") or self._initial
            raw_token = context.get("__siopechatToken")
        except SentinelCompatibilityError:
            raise
        except Exception as error:
            raise SentinelCompatibilityError(
                f"Sentinel enforcement token generation failed: {error}"
            ) from error

        if not isinstance(initial, str) or not initial.startswith(INITIAL_TOKEN_PREFIX):
            raise SentinelCompatibilityError("Sentinel returned an invalid initial proof")
        if not isinstance(raw_token, str) or not raw_token:
            raise SentinelCompatibilityError("Sentinel returned an invalid enforcement token")

        try:
            parsed = json.loads(raw_token)
        except json.JSONDecodeError as error:
            raise SentinelCompatibilityError(
                "Sentinel returned malformed enforcement token JSON"
            ) from error

        proof = parsed.get("p") if isinstance(parsed, dict) else None
        if not isinstance(proof, str) or not proof.startswith(ENFORCEMENT_TOKEN_PREFIX):
            raise SentinelCompatibilityError("Sentinel returned an invalid enforcement proof")
        turnstile = parsed.get("t") if isinstance(parsed, dict) else None
        if not isinstance(turnstile, str) or not turnstile:
            turnstile = None
        return initial, proof, turnstile


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
