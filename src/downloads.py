"""Frontend-independent streaming downloads with bounded concurrency and atomic files."""

import ipaddress
import mimetypes
import os
import re
import socket
import tempfile
import threading
import time
import uuid
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from urllib.parse import unquote as decode_id

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
        self._history_lock = threading.RLock()
        self._store_lock = threading.RLock()
        self._records = {}
        self._running = set()
        self._history_archive = None
        self.revision = 0
        self.history_available = False

    def _history(self):
        # Called under the storage lock, never under the render snapshot lock.
        archive = self.accounts.archive() if hasattr(self.accounts, "archive") else None
        self.history_available = archive is not None
        if archive is not None and archive is not self._history_archive:
            rows = archive.downloads.list()
            if not isinstance(rows, list):
                return None
            with self._history_lock:
                for row in rows:
                    if row["state"] in ("preparing", "downloading"):
                        row.update(state="interrupted", error="")
                    self._records.setdefault(row["id"], row)
                snapshots = [dict(row) for row in self._records.values()]
                self.revision += 1
            for row in snapshots:
                archive.downloads.put(row)
            self._history_archive = archive
        return archive

    def snapshot(self):
        """Memory-only read for frontend rendering; refresh performs disk work."""
        return self.snapshot_state()[1]

    def snapshot_state(self):
        with self._history_lock:
            return self.revision, [dict(row) for row in self._records.values()]

    def active_snapshot(self):
        with self._history_lock:
            return [dict(self._records[key]) for key in self._running if key in self._records]

    def _update(self, identity, *, persist=False, **values):
        with self._history_lock:
            row = self._records.get(identity)
            if row is None:
                return
            row.update(values, updated=time.time())
            if row["state"] in ("preparing", "downloading"):
                self._running.add(identity)
            else:
                self._running.discard(identity)
            self.revision += 1
        if persist:
            try:
                with self._store_lock:
                    archive = self._history()
                    with self._history_lock:
                        row = dict(self._records[identity]) if identity in self._records else None
                    if archive and row:
                        row.pop("history_warning", None)
                        archive.downloads.put(row)
                        with self._history_lock:
                            if identity in self._records:
                                self._records[identity].pop("history_warning", None)
            except (OSError, LookupError):
                with self._history_lock:
                    if identity in self._records:
                        self._records[identity]["history_warning"] = True

    def refresh(self):
        """Reconcile files and known remote deletions without network requests."""
        with self._store_lock:
            archive = self._history()
            deleted = archive.repository.tombstones() if archive else set()
        for row in self.snapshot():
            values = {}
            if row["state"] in ("completed", "deleted") and row.get("path"):
                try:
                    exists = Path(row["path"]).is_file()
                except OSError:
                    exists = False
                values["state"] = "completed" if exists else "deleted"
            conversation = row.get("conversation_id") or ""
            remote = decode_id(conversation.removeprefix("@" + row["account"] + "/"))
            values["chat_deleted"] = (row["account"], "conversation", remote) in deleted
            if row.get("history_warning") or any(
                row.get(key) != value for key, value in values.items()
            ):
                self._update(row["id"], persist=True, **values)
        return self.snapshot()

    def remove(self, identity, *, delete_file=False):
        with self._store_lock:
            archive = self._history()
            with self._history_lock:
                row = dict(self._records[identity])
            if row["state"] in ("preparing", "downloading"):
                raise ValueError("İndirme sürerken kayıt silinemez")
            if delete_file:
                if row.get("path"):
                    Path(row["path"]).unlink(missing_ok=True)
                self._update(identity, persist=True, state="deleted")
                return
            if archive:
                archive.downloads.remove(identity)
            with self._history_lock:
                del self._records[identity]
                self.revision += 1

    def directory(self):
        value = self.settings.snapshot()["application"].get("download_directory", "downloads")
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.root / path).resolve()

    def fetch(self, key, url, conversation_id=None, message_id=None, progress=None, *, title=""):
        identity = (key, conversation_id, url)
        with self._lock:
            if identity in self._active:
                raise ValueError("Bu dosya zaten indiriliyor")
            if not self._gate.acquire(blocking=False):
                raise ValueError("İki indirme sürüyor; tamamlanınca yeniden deneyin")
            self._active.add(identity)
        record_id = uuid.uuid4().hex

        last_saved = 0.0

        def report(name, size, total):
            nonlocal last_saved
            now = time.monotonic()
            persist = now - last_saved >= 1
            if persist:
                last_saved = now
            self._update(
                record_id, persist=persist, state="downloading", name=name, size=size, total=total
            )
            if progress:
                progress(name, size, total)

        try:
            try:
                with self._store_lock:
                    self._history()
            except (OSError, LookupError):
                pass  # A disabled/unavailable archive cannot stop file delivery.
            with self._history_lock:
                self._records[record_id] = dict(
                    id=record_id,
                    account=key,
                    conversation_id=conversation_id,
                    title=title,
                    created=time.time(),
                    updated=time.time(),
                    state="preparing",
                    name="",
                    path=None,
                    size=0,
                    total=None,
                    chat_deleted=False,
                    error="",
                )
            self._update(record_id, persist=True)
            source = self.accounts.resolve_download(key, url, conversation_id, message_id)
            try:
                result = self._fetch(source, report)
                if result["kind"] == "file":
                    self._update(
                        record_id,
                        persist=True,
                        state="completed",
                        path=result["path"],
                        name=Path(result["path"]).name,
                        size=result["size"],
                        total=result["size"],
                    )
                else:
                    self.remove_page(record_id)
                return result
            except httpx.HTTPError:
                raise ValueError(
                    "Dosya aktarımı kesildi; bağlantıyı kontrol edip yeniden deneyin"
                ) from None
        except Exception:
            if record_id in self._records:
                self._update(record_id, persist=True, state="failed", error="download_failed")
            raise
        finally:
            with self._lock:
                self._active.discard(identity)
            self._gate.release()

    def remove_page(self, identity):
        with self._store_lock:
            try:
                archive = self._history()
                if archive:
                    archive.downloads.remove(identity)
            except (OSError, LookupError):
                pass
            with self._history_lock:
                self._records.pop(identity, None)
                self._running.discard(identity)
                self.revision += 1

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
            if response.headers.get("content-encoding"):
                total = None  # Encoded Content-Length cannot measure decoded stream progress.
            if progress:
                progress(target.name, 0, total)
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
