"""Pure-Python ChatGPT Sentinel proof-of-work and Turnstile VM runtime package."""

from .proof import (
    _atob,
    _btoa,
    _decode_initial_context,
    _fnv1a_avalanche,
    _js_number,
    _json_string,
    _json_value,
    _solve_pow,
    _xor_strings,
    generate_enforcement_proof,
)
from .runtime import SentinelRuntime
from .spec import DEFAULT_SENTINEL_SPEC, SentinelSpec
from .vm import SentinelVM

INITIAL_TOKEN_PREFIX = DEFAULT_SENTINEL_SPEC.initial_prefix
PROOF_TOKEN_PREFIX = DEFAULT_SENTINEL_SPEC.enforcement_prefix
PROOF_TOKEN_SUFFIX = DEFAULT_SENTINEL_SPEC.proof_suffix
MAX_POW_ATTEMPTS = DEFAULT_SENTINEL_SPEC.max_attempts

__all__ = [
    "DEFAULT_SENTINEL_SPEC",
    "INITIAL_TOKEN_PREFIX",
    "MAX_POW_ATTEMPTS",
    "PROOF_TOKEN_PREFIX",
    "PROOF_TOKEN_SUFFIX",
    "SentinelRuntime",
    "SentinelSpec",
    "SentinelVM",
    "_atob",
    "_btoa",
    "_decode_initial_context",
    "_fnv1a_avalanche",
    "_js_number",
    "_json_string",
    "_json_value",
    "_solve_pow",
    "_xor_strings",
    "generate_enforcement_proof",
]
