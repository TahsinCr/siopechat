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

    def __init__(self, *, timeout, follow_redirects=False):
        from curl_cffi.requests import Session

        self.client = Session(
            impersonate="chrome",
            timeout=(getattr(timeout, "connect", 15), getattr(timeout, "read", 120)),
            allow_redirects=False,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    @contextmanager
    def stream(self, method, url, *, headers):
        from curl_cffi.requests.exceptions import RequestException

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
