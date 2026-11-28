"""AccountsTransfers account operations backed by webaiprovider v2."""

from __future__ import annotations

import mimetypes
import os
import uuid
from concurrent.futures import CancelledError
from pathlib import Path
from urllib.parse import urlsplit

from webaiprovider import Capability
from webaiprovider.resources.message.attachment import fit_attachments
from webaiprovider.transport.downloader import DownloadSource

from .shared import origin


class AccountsTransfers:
    def _prepare_attachments(
        self, key: str, attachments, *, conversation_id=None, cancel_event=None
    ):
        """Fit staged files to the destination account and re-upload foreign IDs."""
        if not attachments:
            return [], []

        policy = self.attachment_policy(key)
        if policy is None:
            raise ValueError("Bu sağlayıcı dosya eklerini desteklemiyor")
        policy = policy.as_dict() if hasattr(policy, "as_dict") else dict(policy)

        if policy.get("max_files_scope") == "conversation" and conversation_id:
            prov = self.open_provider(key)
            try:
                used_files = prov.conversation_attachment_count(conversation_id)
            except Exception as error:
                raise ValueError(
                    "Claude sohbetindeki mevcut dosya sayısı doğrulanamadı; ekler gönderilmedi"
                ) from error
            if isinstance(used_files, bool) or not isinstance(used_files, int) or used_files < 0:
                raise ValueError(
                    "Claude sohbetindeki mevcut dosya sayısı doğrulanamadı; ekler gönderilmedi"
                )
            policy["max_files"] = max(0, int(policy.get("max_files") or 0) - used_files)
            if policy["max_files"] == 0:
                raise ValueError("Bu sohbetin dosya ekleme kotası dolu; yeni ekler gönderilmedi")

        normalized = []
        for attachment in attachments:
            resource = dict(attachment)
            remote = resource.get("id") or resource.get("file_id")
            path = resource.get("path") or self._attachment_paths.get((key, remote))
            if path:
                try:
                    local = Path(path)
                    size = local.stat().st_size
                except (OSError, TypeError, ValueError):
                    pass
                else:
                    resource.setdefault("path", str(local))
                    resource.setdefault("name", local.name)
                    resource.setdefault("mime_type", mimetypes.guess_type(local.name)[0] or "")
                    if resource.get("size_bytes") is None:
                        resource["size_bytes"] = size
            normalized.append(resource)
        selected, omitted = fit_attachments(normalized, policy)
        if not selected:
            raise ValueError("Ekler bu sağlayıcının tür veya boyut sınırlarına uymuyor")

        prepared = []
        for attachment in selected:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError()
            resource = dict(attachment)
            source_key = resource.get("source_account_key") or key
            if source_key != key:
                path = resource.get("path")
                if not path or not Path(path).is_file():
                    raise ValueError(
                        f"{resource.get('name') or 'Dosya'} hedef hesap için yeniden yüklenemiyor"
                    )
                uploaded = dict(
                    self.attach_file(key, path, cancel_event=cancel_event)
                    if cancel_event is not None
                    else self.attach_file(key, path)
                )
                for name in (
                    "queue_id",
                    "source_tab_id",
                    "path",
                    "name",
                    "size_bytes",
                    "mime_type",
                ):
                    if name in resource:
                        uploaded[name] = resource[name]
                uploaded["source_account_key"] = key
                resource = uploaded
            prepared.append(resource)
        return prepared, omitted

    def resolve_download(
        self, key: str, url: str, conversation_id: str | None = None, message_id: str | None = None
    ):
        context, remote = origin(conversation_id, key)
        if context != key:
            raise ValueError("Dosya seçili hesaba ait olmalı")
        if key:
            prov = self.open_provider(key)
            source = prov._resolve_download_data(url, conversation_id=remote, message_id=message_id)
            if source is not None:
                return source
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("Bu dosya sağlayıcı tarafından çözümlenemedi")
        return DownloadSource(url)

    def download(
        self,
        key: str,
        url: str,
        conversation_id=None,
        message_id=None,
        progress=None,
        *,
        title: str = "",
        force_download: bool = False,
        filename: str | None = None,
    ):
        return self.api.get("downloads").fetch(
            key,
            url,
            conversation_id,
            message_id,
            progress,
            title=title,
            force_download=force_download,
            filename=filename,
        )

    def save_artifact(
        self,
        key: str,
        conversation_id: str | None,
        title: str,
        filename: str,
        data: bytes,
    ):
        """Save inline artifact bytes through the download manager."""
        return self.api.get("downloads").save_export(
            key,
            conversation_id,
            title,
            filename,
            (data,),
        )

    def upload_image(self, key: str, data: bytes, name: str, mime: str, width: int, height: int):
        if key.startswith("claude/") and Capability.ATTACHMENTS not in self.capabilities(key):
            raise ValueError("Claude dosya ekleme henüz kullanılamıyor")
        limit = 30 if key.startswith("claude/") else 20
        if not data or len(data) > limit * 1024 * 1024:
            raise ValueError("Görsel boyutu geçersiz")
        path = self.directory(key) / "attachments" / (uuid.uuid4().hex + ".png")
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if key.startswith("chatgpt/"):
            path.write_bytes(data)
            try:
                prov = self.open_provider(key)
                with prov._activity(), prov._client_scope() as client:
                    result = client.upload_image(name, mime, data, width, height)
                result.update(
                    path=str(path),
                    size_bytes=len(data),
                    temporary_local=True,
                )
                remote = result.get("id") or result.get("file_id")
                if remote:
                    self._attachment_paths[key, remote] = str(path)
                return result
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(data)
        if key.startswith("claude/"):
            try:
                result = self.attach_file(key, path)
                result["name"] = name
                result["mime_type"] = mime
                result["size_bytes"] = len(data)
                result["temporary_local"] = True
                return result
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        return {
            "id": path.stem,
            "name": name,
            "path": str(path),
            "mime_type": mime,
            "size_bytes": len(data),
            "temporary_local": True,
        }

    def attach_file(self, key: str, path, *, cancel_event=None):
        path = Path(path).expanduser().resolve(strict=True)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("Dosya boş olmamalı")
        prov = self.open_provider(key)
        if Capability.ATTACHMENTS not in prov.capabilities:
            raise ValueError("Bu sağlayıcıda dosya ekleme kullanılamıyor")
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()
        if key.startswith("chatgpt/"):
            with prov._activity():
                result = prov._upload_file(path, cancel_event=cancel_event)
        else:
            result = prov.upload_file(path)
        if key.startswith("gemini/"):
            return result
        remote = result.get("id") or result.get("file_id")
        if remote:
            self._attachment_paths[key, remote] = str(path)
            if len(self._attachment_paths) > 64:
                self._attachment_paths.pop(next(iter(self._attachment_paths)))
        return result

    def release_attachment(self, attachment):
        """Remove an app-owned temporary clipboard image after its queue ends."""
        if not isinstance(attachment, dict) or not attachment.get("temporary_local"):
            return
        key = attachment.get("source_account_key")
        path_value = attachment.get("path")
        if not key or not path_value:
            return
        try:
            path = Path(path_value).resolve()
            directory = (self.directory(key) / "attachments").resolve()
        except (OSError, RuntimeError, KeyError, ValueError):
            return
        if directory not in path.parents:
            return
        path.unlink(missing_ok=True)
        remote = attachment.get("id") or attachment.get("file_id")
        if remote:
            self._attachment_paths.pop((key, remote), None)

    def attachment_policy(self, key: str):
        prov = self.provider(key)
        if Capability.ATTACHMENTS not in prov.capabilities:
            return None
        return prov.attachment_policy()

    def synchronize(self, scope: str = "all", key: str | None = None, conversation_id=None):
        """One explicit revalidation entry point for every frontend."""
        if scope not in ("all", "library", "conversation"):
            raise ValueError("Unknown synchronization scope")
        if scope == "conversation":
            if not key or not conversation_id:
                raise ValueError("Account and conversation are required")
            return self.load(conversation_id, key, force=True, window=True, window_limit=25)
        targets = (
            [self.item(key)]
            if key
            else [item for item in self.visible_accounts() if item["enabled"]]
        )
        results = {"chats": None, "projects": None, "models": {}}
        for account in targets:
            with self._lock:
                self._catalogs.pop(account["key"], None)
            try:
                self.models(account["key"], force=True)
            except Exception as error:
                results["models"][account["key"]] = str(error)
        keys = [account["key"] for account in targets]
        results["chats"] = self.list_chats(force=True, account_keys=keys)
        results["projects"] = self.all_projects(force=True, account_keys=keys)
        return results
