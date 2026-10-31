"""Declarative protocol specification and SDK source adaptation for ChatGPT Sentinel."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from ..errors import SentinelCompatibilityError

_DEFAULT_VM_OPCODES: Mapping[str, int] = {
    "recurse": 0,
    "xor": 1,
    "load_imm": 2,
    "resolve": 3,
    "reject": 4,
    "add_or_push": 5,
    "getprop": 6,
    "call": 7,
    "copy": 8,
    "match_script": 11,
    "load_reg_map": 12,
    "try_call_raw": 13,
    "json_parse": 14,
    "json_stringify": 15,
    "try_call": 17,
    "atob": 18,
    "btoa": 19,
    "branch_eq": 20,
    "branch_diff_gt": 21,
    "try_subqueue": 22,
    "branch_defined": 23,
    "bind_method": 24,
    "noop_25": 25,
    "noop_26": 26,
    "sub_or_remove": 27,
    "noop_28": 28,
    "lt": 29,
    "def_subroutine": 30,
    "mul": 33,
    "move": 34,
    "div": 35,
}

_KNOWN_REQUIREMENT_FIELDS = frozenset(
    {
        "prepare_token",
        "token",
        "proofofwork",
        "turnstile",
        "arkose",
        "so",
        "persona",
        "chat_requirements_token",
        "expire_after",
    }
)


@dataclass(frozen=True)
class SentinelSpec:
    """Declarative protocol specification for ChatGPT Sentinel SDK builds.

    Centralizes all version-sensitive constants, fingerprint slots, and VM opcodes
    so quarterly Sentinel SDK rotations can be applied in one place or adapted
    automatically via ``SentinelSpec.from_sdk_source(...)``.
    """

    sdk_version: str = "20260810913b"
    bootstrap_url: str = "https://chatgpt.com/backend-api/sentinel/sdk.js"
    sdk_url: str = "https://chatgpt.com/sentinel/20260810913b/sdk.js"
    initial_prefix: str = "gAAAAAC"
    enforcement_prefix: str = "gAAAAAB"
    error_prefix: str = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"
    proof_suffix: str = "~S"
    initial_difficulty: str = "0"
    max_attempts: int = 500_000
    nonce_index: int = 3
    elapsed_index: int = 9
    min_context_length: int = 10
    screen_sum: int = 3000
    heap_size_limit: int = 2_172_649_472
    hardware_concurrency: int = 8
    device_memory: int = 8
    language: str = "en-US"
    languages: tuple[str, ...] = ("en-US",)
    window_probe_keys: tuple[str, ...] = (
        "ai",
        "createPRNG",
        "cache",
        "data",
        "solana",
        "dump",
        "InstallTrigger",
    )
    navigator_keys: tuple[str, ...] = (
        "userAgent",
        "language",
        "languages",
        "platform",
        "hardwareConcurrency",
        "deviceMemory",
    )
    document_keys: tuple[str, ...] = (
        "cookie",
        "currentScript",
        "scripts",
        "location",
        "documentElement",
        "body",
        "head",
        "createElement",
        "addEventListener",
        "removeEventListener",
        "querySelector",
    )
    window_keys: tuple[str, ...] = (
        "window",
        "self",
        "top",
        "parent",
        "crypto",
        "performance",
        "document",
        "navigator",
        "location",
        "screen",
        "console",
        "SentinelSDK",
        "__sentinel_token_pending",
        "__sentinel_init_pending",
        "__sentinel_script_loads",
    )
    vm_opcodes: Mapping[str, int] = field(default_factory=lambda: dict(_DEFAULT_VM_OPCODES))
    vm_queue_register: int = 9
    vm_window_register: int = 10
    vm_key_register: int = 16
    max_vm_steps: int = 10_000

    @property
    def scripts(self) -> tuple[str, ...]:
        if self.bootstrap_url and self.sdk_url and self.bootstrap_url != self.sdk_url:
            return (self.bootstrap_url, self.sdk_url)
        return (self.sdk_url,) if self.sdk_url else ()

    @property
    def frame_url(self) -> str:
        return f"https://chatgpt.com/backend-api/sentinel/frame.html?sv={self.sdk_version}"

    @classmethod
    def from_sdk_source(
        cls,
        source: str,
        *,
        sdk_url: str | None = None,
        bootstrap_url: str | None = None,
    ) -> SentinelSpec:
        """Extract protocol constants and VM opcodes from a Sentinel SDK JS bundle."""
        if not isinstance(source, str) or not source.strip():
            raise SentinelCompatibilityError("Sentinel SDK source must be a non-empty string")

        base = DEFAULT_SENTINEL_SPEC
        version_match = re.search(
            r"sentinel/([A-Za-z0-9._-]+)/sdk\.js", f"{sdk_url or ''}\n{source}"
        )
        sdk_version = version_match.group(1) if version_match else base.sdk_version
        resolved_sdk_url = sdk_url or f"https://chatgpt.com/sentinel/{sdk_version}/sdk.js"
        resolved_bootstrap_url = bootstrap_url or base.bootstrap_url

        req_prefix_match = re.search(r"requirementsTokenPrefix\s*=\s*[\"']([^\"']+)[\"']", source)
        enf_prefix_match = re.search(r"enforcementTokenPrefix\s*=\s*[\"']([^\"']+)[\"']", source)
        err_prefix_match = re.search(r"errorPrefix\s*=\s*[\"']([^\"']+)[\"']", source)
        suffix_match = re.search(
            r"getRequirementsToken\([^)]*\)\s*\{[^}]*\+\s*[\"'](~[A-Za-z0-9_]+)[\"']",
            source,
        )
        attempts_match = re.search(r"maxAttempts\s*=\s*(\d+(?:[eE]\+?\d+)?)", source)
        slot_match = re.search(
            r"_runCheck\s*\([^)]*\)\s*\{[^}]*?\[(\d+)\]\s*=\s*[A-Za-z_$][\w$]*\s*,\s*"
            r"[A-Za-z_$][\w$]*\[(\d+)\]\s*=\s*[A-Za-z_$][\w$]*",
            source,
        )
        probe_match = re.search(
            r"\[((?:[\"'][^\"']+[\"']\s*,?\s*){3,})\]\.map\([A-Za-z_$][\w$]*\s*=>\s*"
            r"Number\([A-Za-z_$][\w$]*\s+in\s+window\)\)",
            source,
        )

        window_probes = base.window_probe_keys
        if probe_match:
            extracted_probes = tuple(re.findall(r"[\"']([^\"']+)[\"']", probe_match.group(1)))
            if extracted_probes:
                window_probes = extracted_probes

        opcodes = dict(base.vm_opcodes)
        opcode_patterns = {
            "resolve": (
                r"\.set\((\d+)\s*,\s*[A-Za-z_$][\w$]*\s*=>\s*\{\s*"
                r"[A-Za-z_$][\w$]*\s*=\s*0\s*,\s*[A-Za-z_$][\w$]*\(btoa\("
            ),
            "json_parse": (
                r"\.set\((\d+)\s*,\s*\([^)]*\)\s*=>\s*"
                r"[A-Za-z_$][\w$]*\.set\([^,]+,\s*JSON\.parse\("
            ),
            "json_stringify": (
                r"\.set\((\d+)\s*,\s*\([^)]*\)\s*=>\s*"
                r"[A-Za-z_$][\w$]*\.set\([^,]+,\s*JSON\.stringify\("
            ),
            "atob": (
                r"\.set\((\d+)\s*,\s*[A-Za-z_$][\w$]*\s*=>\s*"
                r"[A-Za-z_$][\w$]*\.set\([^,]+,\s*atob\("
            ),
            "btoa": (
                r"\.set\((\d+)\s*,\s*[A-Za-z_$][\w$]*\s*=>\s*"
                r"[A-Za-z_$][\w$]*\.set\([^,]+,\s*btoa\("
            ),
            "match_script": r"\.set\((\d+)\s*,\s*\([^)]*\)\s*=>\s*\{[^}]*document\.scripts",
            "bind_method": r"\.set\((\d+)\s*,\s*\([^)]*\)\s*=>\s*\{[^}]*\.bind\(",
        }
        for name, pattern in opcode_patterns.items():
            match = re.search(pattern, source)
            if match:
                opcodes[name] = int(match.group(1))

        max_attempts = base.max_attempts
        if attempts_match:
            parsed_attempts = int(float(attempts_match.group(1)))
            if 1 <= parsed_attempts <= 5_000_000:
                max_attempts = parsed_attempts

        nonce_index = int(slot_match.group(1)) if slot_match else base.nonce_index
        elapsed_index = int(slot_match.group(2)) if slot_match else base.elapsed_index
        if nonce_index < 0 or elapsed_index <= nonce_index:
            raise SentinelCompatibilityError("Sentinel SDK source contained invalid PoW indices")

        return replace(
            base,
            sdk_version=sdk_version,
            bootstrap_url=resolved_bootstrap_url,
            sdk_url=resolved_sdk_url,
            initial_prefix=req_prefix_match.group(1) if req_prefix_match else base.initial_prefix,
            enforcement_prefix=(
                enf_prefix_match.group(1) if enf_prefix_match else base.enforcement_prefix
            ),
            error_prefix=err_prefix_match.group(1) if err_prefix_match else base.error_prefix,
            proof_suffix=suffix_match.group(1) if suffix_match else base.proof_suffix,
            max_attempts=max_attempts,
            nonce_index=nonce_index,
            elapsed_index=elapsed_index,
            window_probe_keys=window_probes,
            vm_opcodes=opcodes,
        )


DEFAULT_SENTINEL_SPEC = SentinelSpec()
