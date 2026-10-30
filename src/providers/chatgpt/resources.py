"""Sentinel SDK resources and package-local script paths."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .errors import SentinelCompatibilityError

_BOOTSTRAP_URL = "https://chatgpt.com/backend-api/sentinel/sdk.js"
_TRUSTED_HOSTS = {"chatgpt.com", "sentinel.openai.com"}
_MAX_SDK_BYTES = 8 * 1024 * 1024
_DEFAULT_CACHE_TTL_MS = 6 * 60 * 60 * 1000
_SDK_FILE = re.compile(r"sdk-[a-f0-9]{16}\.js\Z")
_SDK_REFERENCE = re.compile(r"script\.src\s*=\s*(['\"])(.*?)\1")


class Script(str, Enum):
    SIGN = "sentinel/sign.cjs"


def script_path(script: Script) -> Path:
    return Path(__file__).resolve().parent / "scripts" / script.value


@dataclass(frozen=True)
class SentinelSDK:
    implementation: str
    source: str
    sha256: str
    cache_status: str


def _validate_sdk_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (AttributeError, ValueError) as error:
        raise SentinelCompatibilityError("Sentinel SDK URL is invalid") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _TRUSTED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or value != value.strip()
        or any(ord(character) < 0x20 for character in value)
    ):
        raise SentinelCompatibilityError("Sentinel SDK URL is not trusted")
    return value


def _read_cached_sdk(directory: Path) -> tuple[SentinelSDK, int]:
    manifest = json.loads((directory / "current.json").read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("version")) is not int
        or manifest["version"] != 1
    ):
        raise ValueError("invalid Sentinel SDK cache manifest")
    filename = manifest.get("file")
    digest = manifest.get("sha256")
    checked_at = manifest.get("checked_at")
    implementation = manifest.get("implementation")
    if (
        not isinstance(filename, str)
        or not _SDK_FILE.fullmatch(filename)
        or not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
        or type(checked_at) is not int
    ):
        raise ValueError("invalid Sentinel SDK cache manifest fields")
    implementation = _validate_sdk_url(implementation)
    source_bytes = (directory / filename).read_bytes()
    if len(source_bytes) > _MAX_SDK_BYTES:
        raise ValueError("cached Sentinel SDK exceeds its size limit")
    if hashlib.sha256(source_bytes).hexdigest() != digest:
        raise ValueError("cached Sentinel SDK failed its SHA-256 check")
    source = source_bytes.decode("utf-8")
    return SentinelSDK(implementation, source, digest, "hit"), checked_at


def _fetch_text(client: httpx.Client, url: str) -> str:
    data = bytearray()
    with client.stream("GET", _validate_sdk_url(url), follow_redirects=False) as response:
        if response.status_code != 200:
            raise SentinelCompatibilityError("Sentinel SDK request was not successful")
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > _MAX_SDK_BYTES:
                raise SentinelCompatibilityError("Sentinel SDK exceeds its size limit")
    return bytes(data).decode("utf-8")


def _write_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    if os.name != "nt":
        path.chmod(0o600)


def load_sentinel_sdk(cache_root: Path, *, client: httpx.Client | None = None) -> SentinelSDK:
    """Load the current published SDK with a bounded, integrity-checked cache."""
    directory = Path(cache_root) / "sentinel"
    cached = None
    checked_at = 0
    try:
        cached, checked_at = _read_cached_sdk(directory)
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        SentinelCompatibilityError,
    ):
        pass

    try:
        cache_ttl_ms = int(os.environ.get("CHATGPT_WEB_SENTINEL_CACHE_TTL_MS", ""))
    except ValueError:
        cache_ttl_ms = 0
    if not os.environ.get("CHATGPT_WEB_SENTINEL_CACHE_TTL_MS"):
        cache_ttl_ms = _DEFAULT_CACHE_TTL_MS
    now_ms = time.time_ns() // 1_000_000
    if cached is not None and now_ms - checked_at < cache_ttl_ms:
        return cached

    owned_client = client is None
    if client is None:
        client = httpx.Client(timeout=15, follow_redirects=False)
    try:
        bootstrap = _fetch_text(client, _BOOTSTRAP_URL)
        reference = _SDK_REFERENCE.search(bootstrap)
        if reference is None:
            raise SentinelCompatibilityError("Sentinel SDK implementation URL was not found")
        implementation = _validate_sdk_url(reference.group(2))
        source = _fetch_text(client, implementation)
        source_bytes = source.encode("utf-8")
        digest = hashlib.sha256(source_bytes).hexdigest()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            directory.chmod(0o700)

        filename = f"sdk-{digest[:16]}.js"
        source_path = directory / filename
        try:
            existing = source_path.read_bytes()
        except OSError:
            existing = b""
        if hashlib.sha256(existing).hexdigest() != digest:
            _write_atomic(source_path, source_bytes)

        checked_at = time.time_ns() // 1_000_000
        manifest = {
            "version": 1,
            "implementation": implementation,
            "file": filename,
            "sha256": digest,
            "checked_at": checked_at,
        }
        _write_atomic(
            directory / "current.json",
            (json.dumps(manifest, separators=(",", ":")) + "\n").encode("utf-8"),
        )
        return SentinelSDK(
            implementation,
            source,
            digest,
            "updated" if cached is not None else "miss",
        )
    except Exception as error:
        if cached is not None:
            return replace(cached, cache_status="stale-offline")
        if isinstance(error, SentinelCompatibilityError):
            raise
        raise SentinelCompatibilityError("Sentinel SDK could not be loaded") from error
    finally:
        if owned_client:
            client.close()
