"""AccountsTransfers account operations."""

from __future__ import annotations

import os
import uuid
from concurrent.futures import CancelledError
from pathlib import Path

from webai_provider.api import (
    Capability,
    ChatGPTClient,
    DownloadSource,
    fit_attachments,
)

from .shared import (
    HISTORY_TIMEOUT,
    origin,
)


class AccountsTransfers:
    def _prepare_attachments(self, key, attachments, *, conversation_id=None, cancel_event=None):
        """Fit staged files to the destination account and re-upload foreign IDs."""
        if not attachments:
            return [], []
        import mimetypes

        policy = self.attachment_policy(key)
        if policy is None:
            raise ValueError("Bu sağlayıcı dosya eklerini desteklemiyor")
        if hasattr(policy, "as_dict"):
            policy = policy.as_dict()
        else:
            policy = dict(policy)

        if policy.get("max_files_scope") == "conversation" and conversation_id:
            provider = self.provider(key)
            count_attachments = getattr(provider, "conversation_attachment_count", None)
            if not callable(count_attachments):
                raise ValueError("Bu sohbetin dosya kotası doğrulanamıyor; ekler gönderilmedi")
            coroutine = count_attachments(conversation_id)
            try:
                used_files = (
                    self._call_cancellable(coroutine, HISTORY_TIMEOUT, cancel_event)
                    if cancel_event is not None
                    else self.call(coroutine, timeout=HISTORY_TIMEOUT)
                )
            except BaseException:
                if getattr(coroutine, "close", None):
                    coroutine.close()
                raise
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

    def resolve_download(self, key, url, conversation_id=None, message_id=None):
        from urllib.parse import urlsplit

        context, remote = origin(conversation_id, key)
        if context != key:
            raise ValueError("Dosya seçili hesaba ait olmalı")
        provider = self.provider(key) if key else None
        if provider and hasattr(provider, "resolve_download"):
            source = self.call(provider.resolve_download(url, remote, message_id))
            if source is not None:
                return source
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("Bu dosya sağlayıcı tarafından çözümlenemedi")
        return DownloadSource(url)

    def download(
        self,
        key,
        url,
        conversation_id=None,
        message_id=None,
        progress=None,
        *,
        title="",
        force_download=False,
        filename=None,
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

    def upload_image(self, key, data, name, mime, width, height):
        if key.startswith("claude/") and Capability.ATTACHMENTS not in self.capabilities(key):
            raise ValueError("Claude dosya ekleme henüz kullanılamıyor")
        limit = 30 if key.startswith("claude/") else 20
        if not data or len(data) > limit * 1024 * 1024:
            raise ValueError("Görsel boyutu geçersiz")
        if key.startswith("chatgpt/"):
            path = self.directory(key) / "attachments" / (uuid.uuid4().hex + ".png")
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            path.write_bytes(data)
            try:
                with ChatGPTClient(self.session(key)) as client:
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
        path = self.directory(key) / "attachments" / (uuid.uuid4().hex + ".png")
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
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

    def attach_file(self, key, path, *, cancel_event=None):
        path = Path(path).expanduser().resolve(strict=True)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("Dosya boş olmamalı")
        provider = self.provider(key)
        if Capability.ATTACHMENTS not in provider.capabilities:
            raise ValueError("Bu sağlayıcıda dosya ekleme kullanılamıyor")
        upload = getattr(provider, "upload_file", None)
        if upload is None:
            raise ValueError("Sağlayıcının dosya yükleme işlemi hazır değil")
        if cancel_event is None:
            result = self.call(upload(path))
        elif key.startswith("chatgpt/"):
            result = self._call_cancellable(
                upload(path, cancel_event=cancel_event), 45, cancel_event
            )
        else:
            result = self._call_cancellable(upload(path), 45, cancel_event)
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

    def attachment_policy(self, key):
        provider = self.provider(key)
        if Capability.ATTACHMENTS not in provider.capabilities:
            return None
        describe = getattr(provider, "attachment_policy", None)
        return describe() if describe else None

    def synchronize(self, scope="all", key=None, conversation_id=None):
        """One explicit revalidation entry point for every frontend."""
        if scope not in ("all", "library", "conversation"):
            raise ValueError("Unknown synchronization scope")
        targets = [self.item(key)] if key else self.active_accounts()
        errors = []
        for item in targets:
            account = item["key"]
            if not self.status(account)["saved_snapshot"]:
                continue
            try:
                if scope == "all":
                    self.models(account, force=True)
                if scope in ("all", "library"):
                    self.list_chats(selected=[item["provider"]], force=True, account_keys={account})
                    self.list_projects(account, force=True)
                    archive = self.archive()
                    if archive:
                        archive.refresh(
                            account=account,
                            kinds={"chats", "containers", "container", "container_chats"},
                            exclude={
                                (account, "chats", ""),
                                (account, "containers", ""),
                            },
                        )
                if scope in ("all", "conversation") and conversation_id:
                    context, _ = origin(conversation_id, account)
                    if context == account:
                        self.load(conversation_id, account, force=True)
            except Exception as error:
                errors.append(item["label"] + ": " + str(error))
        if errors:
            raise RuntimeError(" · ".join(errors))
        return {"scope": scope, "queued": True}
