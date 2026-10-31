"""Thread-safe pure-Python Sentinel runtime and strict challenge enforcement."""

from __future__ import annotations

import random
import re
import sys
import threading
import time
import uuid
from typing import Any

from ..errors import SentinelCompatibilityError
from .proof import (
    _format_js_date,
    _js_number,
    _solve_pow,
    generate_enforcement_proof,
)
from .spec import _KNOWN_REQUIREMENT_FIELDS, DEFAULT_SENTINEL_SPEC, SentinelSpec
from .vm import SentinelVM, _build_virtual_window, _js_str


class SentinelRuntime:
    """Thread-safe pure-Python runtime for ChatGPT Sentinel proofs and challenges."""

    def __init__(
        self,
        spec: SentinelSpec | str | None = None,
        *,
        user_agent: str = "Mozilla/5.0",
    ) -> None:
        if isinstance(spec, str):
            self.spec = SentinelSpec.from_sdk_source(spec)
        elif isinstance(spec, SentinelSpec):
            self.spec = spec
        elif spec is None:
            self.spec = DEFAULT_SENTINEL_SPEC
        else:
            raise SentinelCompatibilityError("Unsupported Sentinel specification type")

        self.user_agent = user_agent or self._default_user_agent()
        self._lock = threading.Lock()
        self._closed = False
        self._sid = str(uuid.uuid4())
        self._requirements_seed = _js_number(random.random())
        self._time_origin_ms = round(time.time() * 1000.0, 3)
        self._started_ns = time.perf_counter_ns()
        self._window = _build_virtual_window(
            user_agent=self.user_agent,
            spec=self.spec,
            time_origin_ms=self._time_origin_ms,
            started_ns=self._started_ns,
        )
        self._vm = SentinelVM(self._window, spec=self.spec)
        self._last_initial_proof: str | None = None

    @staticmethod
    def _default_user_agent() -> str:
        platform = (
            "Windows NT 10.0; Win64; x64"
            if sys.platform == "win32"
            else "Macintosh; Intel Mac OS X 10_15_7"
            if sys.platform == "darwin"
            else "X11; Linux x86_64"
        )
        return (
            f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        )

    def _require_open(self) -> None:
        if self._closed:
            raise SentinelCompatibilityError("Sentinel runtime is closed")

    def _random_navigator_fingerprint(self) -> str:
        navigator = self._window["navigator"]
        key = random.choice(self.spec.navigator_keys)
        value = navigator.get(key)
        if value is None:
            return key
        return f"{key}\u2212{_js_str(value)}"

    def _data_build(self) -> str | None:
        pattern = re.compile(r"c/[^/]*/_")
        for url in self.spec.scripts:
            match = pattern.search(url)
            if match:
                return match.group(0)
        return None

    def build_fingerprint_config(self) -> list[Any]:
        """Construct the 25-element Sentinel browser fingerprint context array."""
        now_ms = round(max(0.0, (time.perf_counter_ns() - self._started_ns) / 1_000_000.0), 3)
        scripts = self.spec.scripts
        chosen_script = random.choice(scripts) if scripts else None
        probes = [int(key in self._window) for key in self.spec.window_probe_keys]
        return [
            self.spec.screen_sum,
            _format_js_date(),
            self.spec.heap_size_limit,
            random.random(),
            self.user_agent,
            chosen_script,
            self._data_build(),
            self.spec.language,
            ",".join(self.spec.languages),
            random.random(),
            self._random_navigator_fingerprint(),
            random.choice(self.spec.document_keys),
            random.choice(self.spec.window_keys),
            now_ms,
            self._sid,
            "",
            self.spec.hardware_concurrency,
            self._time_origin_ms,
            *probes,
        ]

    def create_initial_proof(self) -> str:
        """Generate a fresh ``gAAAAAC...~S`` requirements proof token."""
        with self._lock:
            self._require_open()
            started_ns = time.perf_counter_ns()
            config = self.build_fingerprint_config()
            encoded = _solve_pow(
                config,
                self._requirements_seed,
                self.spec.initial_difficulty,
                spec=self.spec,
                started_ns=started_ns,
            )
            token = f"{self.spec.initial_prefix}{encoded}{self.spec.proof_suffix}"
            self._last_initial_proof = token
            return token

    def _validate_supported_challenges(self, requirements: dict[str, Any]) -> None:
        if requirements.get("error") or requirements.get("detail"):
            raise SentinelCompatibilityError("Sentinel prepare response reported an error")
        for key, value in requirements.items():
            if key in ("proofofwork", "turnstile", "so"):
                continue
            if isinstance(value, dict) and value.get("required") is True:
                raise SentinelCompatibilityError(f"Unsupported required Sentinel challenge: {key}")
            if key not in _KNOWN_REQUIREMENT_FIELDS and isinstance(value, (dict, list)):
                raise SentinelCompatibilityError(
                    f"Unknown structured Sentinel requirement field: {key}"
                )

    def create_enforcement_tokens(
        self, requirements: dict[str, Any]
    ) -> tuple[str, str, str | None]:
        """Produce ``(initial_proof, enforcement_proof, turnstile_token)`` in pure Python."""
        if not isinstance(requirements, dict):
            raise SentinelCompatibilityError("Sentinel requirements were not an object")

        with self._lock:
            self._require_open()
            self._validate_supported_challenges(requirements)
            initial = self._last_initial_proof
            if initial is None:
                initial = self.create_initial_proof()

            pow_config = requirements.get("proofofwork")
            if isinstance(pow_config, dict) and pow_config.get("required") is True:
                proof = generate_enforcement_proof(initial, requirements, spec=self.spec)
            elif pow_config is None or (
                isinstance(pow_config, dict) and pow_config.get("required") is False
            ):
                proof = initial
            else:
                raise SentinelCompatibilityError("Sentinel proof-of-work challenge was invalid")

            turnstile: str | None = None
            turnstile_config = requirements.get("turnstile")
            if isinstance(turnstile_config, dict) and turnstile_config.get("required") is True:
                dx = turnstile_config.get("dx")
                if not isinstance(dx, str) or not dx.strip():
                    raise SentinelCompatibilityError(
                        "Sentinel turnstile challenge is missing bytecode (dx)"
                    )
                turnstile = self._vm.execute(dx, initial)
            elif turnstile_config is not None and not (
                isinstance(turnstile_config, dict) and turnstile_config.get("required") is False
            ):
                raise SentinelCompatibilityError("Sentinel turnstile field was invalid")

            return initial, proof, turnstile

    def create_session_observer_token(self, so_requirements: dict[str, Any]) -> str | None:
        """Execute a Sentinel ``so`` (`snapshot_dx` / `collector_dx`) challenge if required."""
        if not isinstance(so_requirements, dict) or so_requirements.get("required") is not True:
            return None
        with self._lock:
            self._require_open()
            initial = self._last_initial_proof or self.create_initial_proof()
            collector_dx = so_requirements.get("collector_dx")
            snapshot_dx = so_requirements.get("snapshot_dx")
            if isinstance(collector_dx, str) and collector_dx.strip():
                collector_res = self._vm.execute(collector_dx, initial, allow_empty_resolve=True)
                if not isinstance(snapshot_dx, str) or not snapshot_dx.strip():
                    return collector_res
            if not isinstance(snapshot_dx, str) or not snapshot_dx.strip():
                raise SentinelCompatibilityError(
                    "Sentinel session observer challenge is missing bytecode"
                )
            return self._vm.execute(snapshot_dx, initial)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._last_initial_proof = None

    def __enter__(self) -> SentinelRuntime:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
