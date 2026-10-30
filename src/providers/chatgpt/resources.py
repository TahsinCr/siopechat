"""Sentinel SDK resources and package-local script paths."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .errors import SentinelCompatibilityError

CHATGPT_ROOT = Path(__file__).resolve().parent
SIGN_SCRIPT = CHATGPT_ROOT / "sign.js"

_BOOTSTRAP_URL = "https://chatgpt.com/backend-api/sentinel/sdk.js"
_TRUSTED_HOSTS = {"chatgpt.com", "sentinel.openai.com"}
_MAX_SDK_BYTES = 8 * 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_DEFAULT_CACHE_TTL_MS = 6 * 60 * 60 * 1000
_TEMPORARY_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_SDK_FILE = re.compile(r"sdk-[a-f0-9]{16}\.js\Z")
_SDK_REFERENCE = re.compile(r"script\.src\s*=\s*(['\"])(.*?)\1")
_VERSIONED_SDK_URL = re.compile(
    r"^https://(?:chatgpt\.com|sentinel\.openai\.com)/sentinel/(?!test/)[A-Za-z0-9._-]+/sdk\.js\Z"
)

_CACHE_LOCK = threading.Lock()
_MEMORY_CACHE: dict[Path, tuple[int, int, int, SentinelSDK]] = {}


@dataclass(frozen=True)
class SentinelSDK:
    implementation: str
    source: str
    sha256: str
    cache_status: str
    etag: str | None = None
    last_modified: str | None = None
    scripts: tuple[str, ...] = ()


def _http_validator(value: object) -> str | None:
    if (
        isinstance(value, str)
        and 0 < len(value) <= 1024
        and all(0x20 <= ord(character) <= 0x7E for character in value)
    ):
        return value
    return None


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


def _read_bounded_file(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if size > limit:
            raise ValueError("cached Sentinel resource exceeds its size limit")
        data = stream.read(size)
        if stream.read(1):
            raise ValueError("cached Sentinel resource changed while reading")
    return data


def _manifest_stat(directory: Path) -> tuple[int, int] | None:
    try:
        info = (directory / "current.json").stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _read_cached_sdk(
    directory: Path,
) -> tuple[SentinelSDK, int, dict[str, tuple[str, str, str]], str | None]:
    manifest = json.loads(
        _read_bounded_file(directory / "current.json", _MAX_MANIFEST_BYTES).decode("utf-8")
    )
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

    bootstrap_file = manifest.get("bootstrap_file")
    bootstrap_sha256 = manifest.get("bootstrap_sha256")
    if bootstrap_file is not None or bootstrap_sha256 is not None:
        if (
            not isinstance(bootstrap_file, str)
            or not _SDK_FILE.fullmatch(bootstrap_file)
            or not isinstance(bootstrap_sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", bootstrap_sha256)
        ):
            raise ValueError("invalid Sentinel bootstrap cache fields")
        bootstrap_bytes = _read_bounded_file(directory / bootstrap_file, _MAX_SDK_BYTES)
        if hashlib.sha256(bootstrap_bytes).hexdigest() != bootstrap_sha256:
            raise ValueError("cached Sentinel bootstrap failed its SHA-256 check")

    by_url: dict[str, tuple[str, str, str]] = {}
    raw_files = manifest.get("files")
    if isinstance(raw_files, list) and raw_files:
        sources: list[str] = []
        scripts: list[str] = [_BOOTSTRAP_URL]
        for entry in raw_files:
            if not isinstance(entry, dict):
                raise ValueError("invalid Sentinel SDK file entry")
            entry_url = _validate_sdk_url(entry.get("url"))
            entry_file = entry.get("file")
            entry_sha = entry.get("sha256")
            if (
                not isinstance(entry_file, str)
                or not _SDK_FILE.fullmatch(entry_file)
                or not isinstance(entry_sha, str)
                or not re.fullmatch(r"[a-f0-9]{64}", entry_sha)
            ):
                raise ValueError("invalid Sentinel SDK file entry fields")
            entry_bytes = _read_bounded_file(directory / entry_file, _MAX_SDK_BYTES)
            if hashlib.sha256(entry_bytes).hexdigest() != entry_sha:
                raise ValueError("cached Sentinel SDK failed its SHA-256 check")
            text = entry_bytes.decode("utf-8")
            by_url[entry_url] = (entry_file, entry_sha, text)
            sources.append(text)
            scripts.append(entry_url)
        if implementation not in by_url or by_url[implementation][1] != digest:
            raise ValueError("cached Sentinel SDK primary entry mismatch")
        source = "\n".join(sources)
        script_tuple = tuple(scripts)
    else:
        source_bytes = _read_bounded_file(directory / filename, _MAX_SDK_BYTES)
        if hashlib.sha256(source_bytes).hexdigest() != digest:
            raise ValueError("cached Sentinel SDK failed its SHA-256 check")
        source = source_bytes.decode("utf-8")
        by_url[implementation] = (filename, digest, source)
        script_tuple = (_BOOTSTRAP_URL, implementation)

    sdk = SentinelSDK(
        implementation,
        source,
        digest,
        "hit",
        _http_validator(manifest.get("etag")),
        _http_validator(manifest.get("last_modified")),
        script_tuple,
    )
    return sdk, checked_at, by_url, bootstrap_sha256 if isinstance(bootstrap_sha256, str) else None


def _fetch_text(
    client: httpx.Client, url: str, *, cached: SentinelSDK | None = None
) -> tuple[str | None, str | None, str | None]:
    data = bytearray()
    headers = {}
    if cached and cached.etag:
        headers["If-None-Match"] = cached.etag
    elif cached and cached.last_modified:
        headers["If-Modified-Since"] = cached.last_modified
    with client.stream(
        "GET", _validate_sdk_url(url), headers=headers, follow_redirects=False
    ) as response:
        etag = _http_validator(response.headers.get("etag"))
        last_modified = _http_validator(response.headers.get("last-modified"))
        if response.status_code == 304 and headers:
            return None, etag, last_modified
        if response.status_code != 200:
            response.raise_for_status()
            raise SentinelCompatibilityError("Sentinel SDK returned an unexpected HTTP status")
        for chunk in response.iter_bytes():
            if len(chunk) > _MAX_SDK_BYTES - len(data):
                raise SentinelCompatibilityError("Sentinel SDK exceeds its size limit")
            data.extend(chunk)
    return data.decode("utf-8"), etag, last_modified


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


def _ensure_sdk_file(directory: Path, digest: str, source_bytes: bytes) -> str:
    filename = f"sdk-{digest[:16]}.js"
    source_path = directory / filename
    try:
        existing = _read_bounded_file(source_path, _MAX_SDK_BYTES)
    except (OSError, ValueError):
        existing = b""
    if hashlib.sha256(existing).hexdigest() != digest:
        _write_atomic(source_path, source_bytes)
    return filename


def _cleanup_stale_sdk_files(directory: Path, keep_files: set[str]) -> None:
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if _SDK_FILE.fullmatch(entry.name) and entry.name not in keep_files:
            entry.unlink(missing_ok=True)


def _cache_ttl_ms() -> int:
    raw = os.environ.get("CHATGPT_WEB_SENTINEL_CACHE_TTL_MS")
    if not raw:
        return _DEFAULT_CACHE_TTL_MS
    try:
        return int(raw)
    except ValueError:
        return 0


def load_sentinel_sdk(
    directory: Path = CHATGPT_ROOT, *, client: httpx.Client | None = None
) -> SentinelSDK:
    """Download and verify Sentinel SDK files in the ChatGPT root, reusing unchanged files."""
    target_dir = Path(directory).resolve()
    ttl_ms = _cache_ttl_ms()
    now_ms = time.time_ns() // 1_000_000

    with _CACHE_LOCK:
        mem_entry = _MEMORY_CACHE.get(target_dir)
        stat_key = _manifest_stat(target_dir)
        if (
            mem_entry is not None
            and stat_key is not None
            and (mem_entry[0], mem_entry[1]) == stat_key
            and 0 <= now_ms - mem_entry[2] < ttl_ms
        ):
            return mem_entry[3]

    cached: SentinelSDK | None = None
    checked_at = 0
    cached_by_url: dict[str, tuple[str, str, str]] = {}
    cached_bootstrap_sha256: str | None = None
    try:
        cached, checked_at, cached_by_url, cached_bootstrap_sha256 = _read_cached_sdk(target_dir)
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        SentinelCompatibilityError,
    ):
        pass

    now_ms = time.time_ns() // 1_000_000
    age_ms = now_ms - checked_at
    if cached is not None and 0 <= age_ms < ttl_ms:
        stat_key = _manifest_stat(target_dir)
        if stat_key is not None:
            with _CACHE_LOCK:
                _MEMORY_CACHE[target_dir] = (stat_key[0], stat_key[1], checked_at, cached)
        return cached

    owned_client = client is None
    if client is None:
        client = httpx.Client(timeout=15, follow_redirects=False)
    try:
        bootstrap, _, _ = _fetch_text(client, _BOOTSTRAP_URL)
        if bootstrap is None:
            raise SentinelCompatibilityError("Sentinel SDK bootstrap was not returned")
        references = [
            _validate_sdk_url(match.group(2)) for match in _SDK_REFERENCE.finditer(bootstrap)
        ]
        if not references:
            raise SentinelCompatibilityError("Sentinel SDK implementation URL was not found")

        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        bootstrap_bytes = bootstrap.encode("utf-8")
        bootstrap_digest = hashlib.sha256(bootstrap_bytes).hexdigest()
        bootstrap_filename = _ensure_sdk_file(target_dir, bootstrap_digest, bootstrap_bytes)
        bootstrap_unchanged = (
            cached_bootstrap_sha256 is not None and bootstrap_digest == cached_bootstrap_sha256
        )

        keep_files = {bootstrap_filename}
        file_entries: list[dict[str, str]] = []
        sources: list[str] = []
        scripts: list[str] = [_BOOTSTRAP_URL]
        all_revalidated = cached is not None
        primary_etag: str | None = None
        primary_last_modified: str | None = None

        for index, url in enumerate(references):
            previous_entry = cached_by_url.get(url)
            is_primary = index == len(references) - 1
            previous_sdk = (
                cached if (is_primary and cached and cached.implementation == url) else None
            )

            if (
                bootstrap_unchanged
                and previous_entry is not None
                and _VERSIONED_SDK_URL.fullmatch(url)
            ):
                entry_file, entry_digest, entry_source = previous_entry
                etag = previous_sdk.etag if previous_sdk else None
                last_modified = previous_sdk.last_modified if previous_sdk else None
            else:
                fetched_source, etag, last_modified = _fetch_text(client, url, cached=previous_sdk)
                if fetched_source is None:
                    if previous_entry is None or previous_sdk is None:
                        raise SentinelCompatibilityError(
                            "Sentinel SDK cache could not be revalidated"
                        )
                    entry_file, entry_digest, entry_source = previous_entry
                    etag = etag or previous_sdk.etag
                    last_modified = last_modified or previous_sdk.last_modified
                else:
                    entry_bytes = fetched_source.encode("utf-8")
                    entry_digest = hashlib.sha256(entry_bytes).hexdigest()
                    if previous_entry is not None and previous_entry[1] == entry_digest:
                        entry_file, _, entry_source = previous_entry
                    else:
                        all_revalidated = False
                        entry_file = _ensure_sdk_file(target_dir, entry_digest, entry_bytes)
                        entry_source = fetched_source

            keep_files.add(entry_file)
            file_entries.append({"url": url, "file": entry_file, "sha256": entry_digest})
            sources.append(entry_source)
            scripts.append(url)
            if is_primary:
                primary_etag = etag
                primary_last_modified = last_modified

        primary = file_entries[-1]
        implementation = primary["url"]
        filename = primary["file"]
        digest = primary["sha256"]
        combined_source = "\n".join(sources)

        checked_at = time.time_ns() // 1_000_000
        manifest: dict[str, object] = {
            "version": 1,
            "implementation": implementation,
            "file": filename,
            "sha256": digest,
            "bootstrap_file": bootstrap_filename,
            "bootstrap_sha256": bootstrap_digest,
            "files": file_entries,
            "checked_at": checked_at,
        }
        if primary_etag:
            manifest["etag"] = primary_etag
        if primary_last_modified:
            manifest["last_modified"] = primary_last_modified
        _write_atomic(
            target_dir / "current.json",
            (json.dumps(manifest, separators=(",", ":")) + "\n").encode("utf-8"),
        )
        _cleanup_stale_sdk_files(target_dir, keep_files)

        status = "revalidated" if all_revalidated else "updated" if cached is not None else "miss"
        result = SentinelSDK(
            implementation,
            combined_source,
            digest,
            status,
            primary_etag,
            primary_last_modified,
            tuple(scripts),
        )
        stat_key = _manifest_stat(target_dir)
        if stat_key is not None:
            cached_hit = replace(result, cache_status="hit")
            with _CACHE_LOCK:
                _MEMORY_CACHE[target_dir] = (stat_key[0], stat_key[1], checked_at, cached_hit)
        return result
    except httpx.TransportError as error:
        if cached is not None:
            return replace(cached, cache_status="stale-offline")
        raise SentinelCompatibilityError(
            "Sentinel SDK request failed due to a network error"
        ) from error
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        if cached is not None and status_code in _TEMPORARY_HTTP_STATUSES:
            return replace(cached, cache_status="stale-offline")
        raise SentinelCompatibilityError(
            f"Sentinel SDK request returned HTTP {status_code}"
        ) from error
    except SentinelCompatibilityError:
        raise
    except Exception as error:
        raise SentinelCompatibilityError("Sentinel SDK could not be loaded") from error
    finally:
        if owned_client:
            client.close()
