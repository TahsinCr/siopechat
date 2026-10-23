"""Origin-scoped HTTP web sessions and private, atomic credential persistence."""

import asyncio
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from .errors import AccountPaused, ProviderError, RateLimited, RemoteNotFound


@runtime_checkable
class CredentialStore(Protocol):
    def load(self) -> dict: ...
    def save(self, value: dict) -> None: ...
    def exists(self) -> bool: ...
    def delete(self) -> None: ...


def session_exists(source):
    return source.exists() if isinstance(source, CredentialStore) else Path(source).is_file()


def delete_session(source):
    if isinstance(source, CredentialStore):
        source.delete()
    else:
        Path(source).unlink(missing_ok=True)


_SESSION_WRITE_LOCKS = {}
_SESSION_WRITE_LOCKS_GUARD = threading.Lock()


def _session_write_lock(path):
    key = os.path.normcase(os.path.abspath(path))
    with _SESSION_WRITE_LOCKS_GUARD:
        return _SESSION_WRITE_LOCKS.setdefault(key, threading.Lock())


def _replace_with_retry(source, destination):
    for attempt in range(5):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 4:
                raise
            time.sleep(0.01 * (2**attempt))


def read_session(path):
    if isinstance(path, CredentialStore):
        try:
            value = path.load()
            if not isinstance(value, dict):
                raise ValueError("session shape")
            return value
        except (OSError, ValueError):
            raise AccountPaused("Kayıtlı oturum yok veya geçersiz; hesabı doğrulayın") from None
    path = Path(path)
    try:
        if path.stat().st_size > 1_048_576:
            raise ValueError("session size")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("session shape")
        return value
    except (OSError, ValueError):
        raise AccountPaused(
            "Kayıtlı oturum yok veya geçersiz; Hesaplar bölümünden doğrulayın"
        ) from None


def save_session(path, value, *, ensure_ascii=False, newline=False):
    if isinstance(path, CredentialStore):
        path.save(value)
        return
    path = Path(path)
    with _session_write_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as output:
                name = output.name
                if os.name != "nt":
                    os.chmod(name, 0o600)
                json.dump(value, output, ensure_ascii=ensure_ascii)
                if newline:
                    output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            _replace_with_retry(name, path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)


class WebSession:
    """Reuse an authenticated HTTP connection; never launch processes or follow redirects."""

    def __init__(self, origin, credential_file, required_cookie, *, client_factory=None):
        if origin not in ("https://claude.ai", "https://gemini.google.com"):
            raise ValueError("unsupported session origin")
        self.origin, self.path, self.required_cookie = (
            origin,
            credential_file,
            required_cookie,
        )
        self.factory, self.client, self.authenticated = client_factory, None, False
        self._saved_cookies = None
        self._save_lock = asyncio.Lock()

    def connect(self):
        if self.client is None:
            values = read_session(self.path)
            cookies = values.get("cookies", values)
            if not isinstance(cookies, dict) or not cookies.get(self.required_cookie):
                raise AccountPaused("Oturumunuzu Hesaplar bölümünden bir kez yeniden doğrulayın")
            if any(
                not isinstance(k, str) or not isinstance(v, str) or "\n" in k + v or "\r" in k + v
                for k, v in cookies.items()
            ):
                raise AccountPaused("Oturum kaydı geçersiz")
            self._saved_cookies = dict(cookies)
            factory = self.factory
            if factory is None:
                from curl_cffi.requests import AsyncSession

                factory = AsyncSession
            self.client = factory(
                cookies=cookies,
                impersonate="chrome",
                timeout=60,
                headers={
                    "origin": self.origin,
                    "referer": self.origin + "/",
                    "accept": "application/json",
                },
            )
        return self.client

    def url(self, path):
        parsed = urlsplit(path)
        if (
            parsed.scheme
            or parsed.netloc
            or not path.startswith("/")
            or ".." in parsed.path
            or path.startswith("//")
        ):
            raise ValueError("session request must remain on its origin")
        return self.origin + path

    @staticmethod
    def check(response):
        code = response.status_code
        if code == 401:
            raise AccountPaused("Sağlayıcı oturumu sona ermiş; Hesaplar bölümünden doğrulayın")
        if code == 403:
            raise ProviderError("Sağlayıcı bu işleme erişim vermedi (403); istek tekrarlanmadı")
        if code == 404:
            raise RemoteNotFound("Uzak kayıt bulunamadı")
        if code == 429:
            try:
                delay = float(response.headers.get("retry-after", ""))
            except ValueError:
                delay = None
            raise RateLimited(delay)
        if not 200 <= code < 300:
            raise ProviderError(f"Sağlayıcı isteği tamamlanamadı (HTTP {code})")

    async def request(self, method, path, **options):
        url = self.url(path)
        response = await self.connect().request(method, url, allow_redirects=False, **options)
        self.check(response)
        self.authenticated = True
        await self.persist()
        if response.status_code == 204 or not response.content:
            return None
        if len(response.content) > 16 * 1024 * 1024:
            raise ProviderError("Sağlayıcı yanıtı boyut sınırını aştı")
        try:
            return response.json()
        except ValueError:
            raise ProviderError("Sağlayıcı yanıt biçimi değişmiş") from None

    async def persist(self):
        if self.client is None or self._saved_cookies is None:
            return
        async with self._save_lock:
            # Only save the selected origin's existing credential names.
            jar = getattr(self.client.cookies, "jar", None)
            if jar is None:
                cookies = {
                    k: self.client.cookies.get(k) or v for k, v in self._saved_cookies.items()
                }
            else:
                host = urlsplit(self.origin).hostname
                cookies = dict(self._saved_cookies)
                for cookie in sorted(jar, key=lambda c: len(c.domain or "")):
                    if cookie.name in cookies and (
                        not cookie.domain or host == cookie.domain.lstrip(".")
                    ):
                        cookies[cookie.name] = cookie.value
            if cookies != self._saved_cookies:
                try:
                    await asyncio.to_thread(save_session, self.path, cookies)
                    self._saved_cookies = cookies
                except OSError:
                    self.persistence_warning = "Oturum yenilendi ancak yerel kayıt güncellenemedi"

    async def close(self):
        client = self.client
        if client is not None:
            try:
                await self.persist()
            finally:
                self.client = None
                await client.close()
