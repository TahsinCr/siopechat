"""Frontend-independent streaming downloads with bounded concurrency and atomic files."""

import ipaddress
import mimetypes
import os
import re
import socket
import tempfile
import threading
import time
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx


class Downloads:
    def __init__(
        self, settings, root, accounts, client_factory=httpx.Client, *, resolver=socket.getaddrinfo
    ):
        self.settings, self.root, self.accounts = settings, Path(root), accounts
        self.client_factory = client_factory
        self.resolver = resolver
        self._gate = threading.BoundedSemaphore(2)
        self._lock = threading.Lock()
        self._active = set()

    def directory(self):
        value = self.settings.snapshot()["application"].get("download_directory", "downloads")
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.root / path).resolve()

    def fetch(self, key, url, conversation_id=None, message_id=None, progress=None):
        identity = (key, conversation_id, url)
        with self._lock:
            if identity in self._active:
                raise ValueError("Bu dosya zaten indiriliyor")
            if not self._gate.acquire(blocking=False):
                raise ValueError("İki indirme sürüyor; tamamlanınca yeniden deneyin")
            self._active.add(identity)
        try:
            source = self.accounts.resolve_download(key, url, conversation_id, message_id)
            try:
                return self._fetch(source, progress)
            except httpx.HTTPError:
                raise ValueError(
                    "Dosya aktarımı kesildi; bağlantıyı kontrol edip yeniden deneyin"
                ) from None
        finally:
            with self._lock:
                self._active.discard(identity)
            self._gate.release()

    @staticmethod
    def _web_url(url):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or any(ord(c) < 32 or ord(c) == 127 for c in url)
        ):
            raise ValueError("Dosya bağlantısı geçersiz veya desteklenmiyor")
        return parsed

    def _address(self, origin):
        allow_private = self.settings.snapshot()["application"].get("download_allow_private", False)
        port = origin.port or (443 if origin.scheme == "https" else 80)
        try:
            addresses = {
                row[4][0] for row in self.resolver(origin.hostname, port, type=socket.SOCK_STREAM)
            }
            if not addresses or (
                not allow_private
                and any(not ipaddress.ip_address(value).is_global for value in addresses)
            ):
                raise ValueError("Yerel veya özel ağ dosya hedeflerine izin verilmiyor")
        except (OSError, ValueError) as error:
            raise ValueError(
                "Dosya hedefi çözümlenemedi veya ağ politikası tarafından engellendi"
            ) from error
        return sorted(addresses)[0]

    def _fetch(self, source, progress):
        url, headers = source.url, dict(source.headers)
        factory = source.client_factory or self.client_factory
        with factory(
            timeout=httpx.Timeout(120, connect=15), follow_redirects=False, trust_env=False
        ) as client:
            for _ in range(6):
                origin = self._web_url(url)
                address = self._address(origin)
                cookies = getattr(client, "cookies", None)
                if cookies is not None:
                    cookies.clear()  # Only the explicit, origin-scoped source headers carry auth.
                if source.client_factory:
                    request_url, stream_options = url, {"resolved_address": address}
                else:
                    request_url = str(httpx.URL(url).copy_with(host=address))
                    headers = dict(headers, Host=origin.netloc)
                    stream_options = {"extensions": {"sni_hostname": origin.hostname}}
                with client.stream(
                    "GET", request_url, headers=headers, **stream_options
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        target = urljoin(url, response.headers.get("location", ""))
                        if not response.headers.get("location"):
                            raise ValueError("Dosya yönlendirmesi geçersiz")
                        next_origin = self._web_url(target)
                        if origin.scheme == "https" and next_origin.scheme != "https":
                            raise ValueError("HTTPS dosyası HTTP bağlantısına yönlendirilemez")
                        if (next_origin.scheme, next_origin.netloc) != (
                            origin.scheme,
                            origin.netloc,
                        ):
                            headers = {}  # Never forward account credentials to another origin.
                        url = target
                        continue
                    if (
                        response.status_code in (401, 403)
                        and not source.force_download
                        and not source.filename
                        and response.headers.get("content-type", "").startswith("text/html")
                    ):
                        return {"kind": "page", "url": url}
                    if response.status_code == 206:
                        # No Range was requested: some providers use 206 while generating media.
                        raise ValueError("Dosya henüz hazır değil; biraz sonra yeniden deneyin")
                    if response.status_code >= 400:
                        raise ValueError("Dosya alınamadı (HTTP {0})".format(response.status_code))
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    disposition = Message()
                    disposition["content-disposition"] = response.headers.get(
                        "content-disposition", ""
                    )
                    filename = disposition.get_filename() or source.filename
                    detected_type = media_type or mimetypes.guess_type(origin.path)[0]
                    is_file = (
                        source.force_download
                        or filename
                        or disposition.get_content_disposition() == "attachment"
                        or (
                            detected_type
                            and detected_type
                            not in ("text/html", "application/xhtml+xml", "text/event-stream")
                        )
                    )
                    if not is_file:
                        return {"kind": "page", "url": url}
                    if (
                        media_type in ("text/html", "application/xhtml+xml")
                        and source.force_download
                    ):
                        raise ValueError("Dosya yerine giriş sayfası döndü; hesabı doğrulayın")
                    filename = filename or Path(unquote(origin.path)).name or "download"
                    if not Path(filename).suffix:
                        filename += mimetypes.guess_extension(media_type) or ""
                    return self._save(response, filename, progress)
        raise ValueError("Dosya çok fazla yönlendirildi")

    def _save(self, response, filename, progress):
        directory = self.directory()
        directory.mkdir(parents=True, exist_ok=True)
        filename = (
            re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", str(filename)).strip(" .")[:180] or "download"
        )
        if filename.split(".")[0].upper() in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *("COM" + str(i) for i in range(1, 10)),
            *("LPT" + str(i) for i in range(1, 10)),
        }:
            filename = "_" + filename
        target = directory / filename
        for index in range(10000):
            candidate = (
                target
                if index == 0
                else target.with_name(f"{target.stem} ({index}){target.suffix}")
            )
            try:
                descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            os.close(descriptor)
            target = candidate
            break
        else:
            raise ValueError("İndirme dosya adı ayrılamadı")
        temporary, done = None, False
        try:
            size, last = 0, 0.0
            total = response.headers.get("content-length", "")
            total = int(total) if total.isdigit() else None
            maximum = self.settings.snapshot()["application"].get(
                "download_max_bytes", 512 * 1024 * 1024
            )
            if total is not None and total > maximum:
                raise ValueError("Dosya indirme boyutu sınırını aşıyor")
            with tempfile.NamedTemporaryFile(
                dir=directory, prefix=".download-", suffix=".part", delete=False
            ) as output:
                temporary = Path(output.name)
                for chunk in response.iter_bytes(256 * 1024):
                    if size + len(chunk) > maximum:
                        raise ValueError("Dosya indirme boyutu sınırını aşıyor")
                    output.write(chunk)
                    size += len(chunk)
                    now = time.monotonic()
                    if progress and now - last >= 0.2:
                        progress(target.name, size, total)
                        last = now
                if (
                    total is not None
                    and not response.headers.get("content-encoding")
                    and size != total
                ):
                    raise ValueError("Dosya aktarımı tamamlanmadı")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            done = True
            return {"kind": "file", "path": str(target), "size": size}
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
            if not done:
                target.unlink(missing_ok=True)
