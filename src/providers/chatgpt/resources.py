"""Sentinel SDK resources and package-local script paths."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .errors import SentinelCompatibilityError

CHATGPT_ROOT = Path(__file__).resolve().parent
SCRIPT_ROOT = CHATGPT_ROOT / "scripts"
SIGN_SCRIPT = SCRIPT_ROOT / "sign.js"

_BOOTSTRAP_URL = "https://chatgpt.com/backend-api/sentinel/sdk.js"
_TRUSTED_HOSTS = {"chatgpt.com", "sentinel.openai.com"}
_MAX_SDK_BYTES = 8 * 1024 * 1024
_MAX_SDK_FILES = 32
_DEFAULT_CACHE_TTL_MS = 6 * 60 * 60 * 1000
_TEMPORARY_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_BOOTSTRAP_FILE = re.compile(r"sentinel-bootstrap-([a-f0-9]{64})\.js\Z")
_SDK_FILE = re.compile(r"sentinel-sdk-[A-Za-z0-9._-]+-[a-f0-9]{16}-([a-f0-9]{64})\.js\Z")
_CACHE_ARTIFACT = re.compile(
    r"sentinel-(?:bootstrap-[a-f0-9]{64}\.js|sdk-[A-Za-z0-9._-]+-[a-f0-9]{16}-[a-f0-9]{64}\.js|sdk-[a-f0-9]{16}\.validators)\Z"
)
_SDK_REFERENCE = re.compile(r"script\.src\s*=\s*(['\"])(.*?)\1")
_VERSIONED_SDK_URL = re.compile(
    r"^https://(?:chatgpt\.com|sentinel\.openai\.com)/sentinel/(?!test/)[A-Za-z0-9._-]+/sdk\.js\Z"
)

_CACHE_LOCK = threading.Lock()
_MEMORY_CACHE: dict[Path, tuple[tuple[tuple[str, int, int], ...], int, SentinelSDK]] = {}


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


def _sdk_cache_prefix(url: str) -> str:
    version_match = _VERSIONED_SDK_URL.fullmatch(url)
    version = urlsplit(url).path.split("/")[-2] if version_match else "dynamic"
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"sentinel-sdk-{version}-{url_hash}"


def _sdk_references(source: str) -> list[str]:
    references = [_validate_sdk_url(match.group(2)) for match in _SDK_REFERENCE.finditer(source)]
    if not references:
        raise SentinelCompatibilityError("Sentinel SDK implementation URL was not found")
    if len(references) > _MAX_SDK_FILES:
        raise SentinelCompatibilityError("Sentinel SDK references too many script files")
    return references


def _read_bounded_file(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if size > limit:
            raise ValueError("cached Sentinel resource exceeds its size limit")
        data = stream.read(size)
        if stream.read(1):
            raise ValueError("cached Sentinel resource changed while reading")
    return data


def _cache_stat(directory: Path) -> tuple[tuple[str, int, int], ...] | None:
    try:
        entries = []
        for entry in directory.iterdir():
            if not _CACHE_ARTIFACT.fullmatch(entry.name):
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                entries.append((entry.name, info.st_mtime_ns, info.st_size))
    except OSError:
        return None
    return tuple(sorted(entries)) or None


def _read_cached_sdk(
    directory: Path,
) -> tuple[SentinelSDK, int, dict[str, tuple[str, str, str]], str | None]:
    bootstrap_files = sorted(
        (path for path in directory.iterdir() if _BOOTSTRAP_FILE.fullmatch(path.name)),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    last_error: Exception | None = None
    for bootstrap_path in bootstrap_files:
        match = _BOOTSTRAP_FILE.fullmatch(bootstrap_path.name)
        assert match is not None
        bootstrap_digest = match.group(1)
        try:
            bootstrap_bytes = _read_bounded_file(bootstrap_path, _MAX_SDK_BYTES)
            if hashlib.sha256(bootstrap_bytes).hexdigest() != bootstrap_digest:
                raise ValueError("cached Sentinel bootstrap failed its SHA-256 check")
            bootstrap = bootstrap_bytes.decode("utf-8")
            references = _sdk_references(bootstrap)

            by_url: dict[str, tuple[str, str, str]] = {}
            sources: list[str] = []
            scripts = [_BOOTSTRAP_URL]
            source_size = 0
            for implementation in references:
                filename_prefix = _sdk_cache_prefix(implementation)
                candidates = sorted(
                    directory.glob(f"{filename_prefix}-*.js"),
                    key=lambda path: path.stat().st_mtime_ns,
                    reverse=True,
                )
                if not candidates:
                    raise FileNotFoundError("cached Sentinel implementation is missing")
                source_path = candidates[0]
                source_match = _SDK_FILE.fullmatch(source_path.name)
                assert source_match is not None
                digest = source_match.group(1)
                source_bytes = _read_bounded_file(source_path, _MAX_SDK_BYTES)
                if hashlib.sha256(source_bytes).hexdigest() != digest:
                    raise ValueError("cached Sentinel SDK failed its SHA-256 check")
                if source_size + len(source_bytes) + len(sources) > _MAX_SDK_BYTES:
                    raise ValueError("cached Sentinel SDK bundle exceeds its size limit")
                source_size += len(source_bytes)
                source = source_bytes.decode("utf-8")
                by_url[implementation] = (source_path.name, digest, source)
                sources.append(source)
                scripts.append(implementation)

            implementation = references[-1]
            validator_name = f"sentinel-sdk-{hashlib.sha256(implementation.encode()).hexdigest()[:16]}.validators"
            try:
                validators = (
                    _read_bounded_file(directory / validator_name, 2048)
                    .decode("ascii")
                    .splitlines()
                )
            except (OSError, UnicodeError, ValueError):
                validators = []
            etag = _http_validator(validators[0]) if validators else None
            last_modified = _http_validator(validators[1]) if len(validators) > 1 else None
            checked_at = bootstrap_path.stat().st_mtime_ns // 1_000_000
            combined_source = "\n".join(sources)
            sdk = SentinelSDK(
                implementation,
                combined_source,
                hashlib.sha256(combined_source.encode("utf-8")).hexdigest(),
                "hit",
                etag,
                last_modified,
                tuple(scripts),
            )
            return sdk, checked_at, by_url, bootstrap_digest
        except (OSError, UnicodeError, ValueError, SentinelCompatibilityError) as error:
            last_error = error
    if last_error is not None:
        raise last_error
    raise FileNotFoundError("Sentinel SDK cache is empty")


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


def _ensure_sdk_file(directory: Path, filename: str, digest: str, source_bytes: bytes) -> str:
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
        if _CACHE_ARTIFACT.fullmatch(entry.name) and entry.name not in keep_files:
            entry.unlink(missing_ok=True)


def _validator_filename(url: str) -> str:
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"sentinel-sdk-{url_hash}.validators"


def _write_validators(
    directory: Path, url: str, etag: str | None, last_modified: str | None
) -> None:
    path = directory / _validator_filename(url)
    if etag is None and last_modified is None:
        path.unlink(missing_ok=True)
        return
    _write_atomic(path, f"{etag or ''}\n{last_modified or ''}\n".encode("ascii"))


def _read_validators(directory: Path, url: str) -> tuple[str | None, str | None]:
    try:
        values = (
            _read_bounded_file(directory / _validator_filename(url), 2048)
            .decode("ascii")
            .splitlines()
        )
    except (OSError, UnicodeError, ValueError):
        return None, None
    return (
        _http_validator(values[0]) if values else None,
        _http_validator(values[1]) if len(values) > 1 else None,
    )


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
    """Download verified Sentinel SDK files into ``scripts/`` under the ChatGPT root."""
    target_dir = Path(directory).resolve() / "scripts"
    ttl_ms = _cache_ttl_ms()
    now_ms = time.time_ns() // 1_000_000

    with _CACHE_LOCK:
        mem_entry = _MEMORY_CACHE.get(target_dir)
        stat_key = _cache_stat(target_dir)
        if (
            mem_entry is not None
            and stat_key is not None
            and mem_entry[0] == stat_key
            and 0 <= now_ms - mem_entry[1] < ttl_ms
        ):
            return mem_entry[2]

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
        stat_key = _cache_stat(target_dir)
        if stat_key is not None:
            with _CACHE_LOCK:
                _MEMORY_CACHE[target_dir] = (stat_key, checked_at, cached)
        return cached

    owned_client = client is None
    if client is None:
        client = httpx.Client(timeout=15, follow_redirects=False)
    try:
        bootstrap, _, _ = _fetch_text(client, _BOOTSTRAP_URL)
        if bootstrap is None:
            raise SentinelCompatibilityError("Sentinel SDK bootstrap was not returned")
        references = _sdk_references(bootstrap)

        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        bootstrap_bytes = bootstrap.encode("utf-8")
        bootstrap_digest = hashlib.sha256(bootstrap_bytes).hexdigest()
        bootstrap_filename = f"sentinel-bootstrap-{bootstrap_digest}.js"
        _ensure_sdk_file(target_dir, bootstrap_filename, bootstrap_digest, bootstrap_bytes)
        bootstrap_unchanged = (
            cached_bootstrap_sha256 is not None and bootstrap_digest == cached_bootstrap_sha256
        )

        keep_files = {bootstrap_filename}
        sources: list[str] = []
        scripts: list[str] = [_BOOTSTRAP_URL]
        all_revalidated = cached is not None and bootstrap_unchanged
        primary_etag: str | None = None
        primary_last_modified: str | None = None
        primary_digest: str | None = None
        source_size = 0

        for index, url in enumerate(references):
            previous_entry = cached_by_url.get(url)
            is_primary = index == len(references) - 1
            source_bytes_to_cache: bytes | None = None
            previous_etag, previous_last_modified = _read_validators(target_dir, url)
            previous_sdk = None
            if previous_entry is not None:
                previous_sdk = SentinelSDK(
                    url,
                    previous_entry[2],
                    previous_entry[1],
                    "hit",
                    previous_etag,
                    previous_last_modified,
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
                    entry_file = f"{_sdk_cache_prefix(url)}-{entry_digest}.js"
                    source_bytes_to_cache = entry_bytes
                    if previous_entry is not None and previous_entry[1] == entry_digest:
                        entry_source = previous_entry[2]
                    else:
                        all_revalidated = False
                        entry_source = fetched_source

            entry_source_size = len(entry_source.encode("utf-8"))
            if source_size + entry_source_size + len(sources) > _MAX_SDK_BYTES:
                raise SentinelCompatibilityError("Sentinel SDK bundle exceeds its size limit")
            source_size += entry_source_size
            if source_bytes_to_cache is not None:
                _ensure_sdk_file(target_dir, entry_file, entry_digest, source_bytes_to_cache)

            _write_validators(target_dir, url, etag, last_modified)

            keep_files.add(entry_file)
            if _read_validators(target_dir, url) != (None, None):
                keep_files.add(_validator_filename(url))
            if previous_entry is None:
                all_revalidated = False
            sources.append(entry_source)
            scripts.append(url)
            if is_primary:
                primary_etag = etag
                primary_last_modified = last_modified
                primary_digest = entry_digest

        implementation = references[-1]
        if primary_digest is None:
            raise SentinelCompatibilityError("Sentinel SDK primary file was not loaded")
        combined_source = "\n".join(sources)
        digest = hashlib.sha256(combined_source.encode("utf-8")).hexdigest()

        checked_at = time.time_ns() // 1_000_000
        bootstrap_path = target_dir / bootstrap_filename
        os.utime(bootstrap_path, ns=(checked_at * 1_000_000, checked_at * 1_000_000))
        _cleanup_stale_sdk_files(target_dir, keep_files)

        status = "revalidated" if all_revalidated else "updated" if cached is not None else "miss"
        result = SentinelSDK(
            implementation=implementation,
            source=combined_source,
            sha256=digest,
            cache_status=status,
            etag=primary_etag,
            last_modified=primary_last_modified,
            scripts=tuple(scripts),
        )
        stat_key = _cache_stat(target_dir)
        if stat_key is not None:
            cached_hit = replace(result, cache_status="hit")
            with _CACHE_LOCK:
                _MEMORY_CACHE[target_dir] = (stat_key, checked_at, cached_hit)
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


def sentinel_cache_status(directory: Path = CHATGPT_ROOT) -> dict[str, str | int] | None:
    """Return verified SDK identity and its last check time without network access."""
    target_dir = Path(directory).resolve() / "scripts"
    try:
        sdk, checked_at, _, _ = _read_cached_sdk(target_dir)
    except (OSError, UnicodeError, ValueError, SentinelCompatibilityError):
        return None
    return {
        "implementation": sdk.implementation,
        "sha256": sdk.sha256,
        "checked_at": checked_at,
    }
