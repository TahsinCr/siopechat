"""Resolved file requests; credentials are transient and never archive metadata."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class DownloadSource:
    url: str
    filename: str | None = None
    force_download: bool = False
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    client_factory: Callable | None = field(default=None, repr=False, compare=False)


def cookie_header(values, names):
    """Build a transient header from explicitly allowed account cookies only."""
    selected = []
    for key in names:
        value = values.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or any(char in value for char in "\r\n;"):
            raise ValueError("Oturum kaydı geçersiz")
        selected.append(f"{key}={value}")
    return "; ".join(selected)


class NativeDownloadClient:
    """Stream bytes with the same HTTP transport used by Gemini and Claude."""

    def __init__(self, *, timeout, follow_redirects=False, trust_env=False):
        from curl_cffi.requests import Session

        self.client = Session(
            impersonate="chrome",
            timeout=(getattr(timeout, "connect", 15), getattr(timeout, "read", 120)),
            allow_redirects=False,
            trust_env=trust_env,
        )

    @property
    def cookies(self):
        return self.client.cookies

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    @contextmanager
    def stream(self, method, url, *, headers, resolved_address=None):
        from curl_cffi.requests.exceptions import RequestException

        if resolved_address:
            from urllib.parse import urlsplit

            from curl_cffi import CurlOpt

            target = urlsplit(url)
            port = target.port or (443 if target.scheme == "https" else 80)
            address = f"[{resolved_address}]" if ":" in resolved_address else resolved_address
            self.client.curl_options[CurlOpt.RESOLVE] = [f"{target.hostname}:{port}:{address}"]
        try:
            with self.client.stream(
                method, url, headers=headers, allow_redirects=False
            ) as response:
                yield _NativeDownloadResponse(response)
        except RequestException:
            # Transport errors may contain signed URLs or credentials.
            raise ValueError(
                "Dosya aktarımı kesildi; bağlantıyı kontrol edip yeniden deneyin"
            ) from None


class _NativeDownloadResponse:
    def __init__(self, response):
        self.status_code = response.status_code
        self.headers = response.headers
        self.response = response

    def iter_bytes(self, chunk_size):
        return self.response.iter_content(chunk_size=chunk_size)
