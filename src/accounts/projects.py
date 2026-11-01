"""AccountsProjects account operations."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from providers import (
    Capability,
)
from providers.errors import ProviderError, RemoteNotFound

from .shared import (
    identity,
    origin,
)


class AccountsProjects:
    def create_project(self, key, name, visibility="private", memory="default"):
        project = self.call(
            self.provider(key).create_project(name, visibility=visibility, memory=memory)
        )
        return self.store_project(key, project)

    def project_options(self, key, value=None):
        context, remote = origin(value, key)
        if context != key:
            raise ValueError("Proje seçili hesaba ait olmalı")
        return self.call(self.provider(key).project_options(remote), timeout=90)

    def project_details(self, key, value):
        context, remote = origin(value, key)
        if context != key or not remote:
            raise ValueError("Proje seçili hesaba ait olmalı")
        if not self.status(key)["saved_snapshot"]:
            archive = self.archive()
            cached = archive.get(key, "container", remote) if archive else None
            if cached is None and archive:
                cached = next(
                    (
                        item
                        for item in archive.containers(key, limit=500)
                        if item.get("remote_id") == remote
                        or origin(item.get("id"), key)[1] == remote
                    ),
                    None,
                )
            if cached is None:
                raise RuntimeError("Kayıtlı oturum yok; Hesaplar bölümünden doğrulayın")
            return dict(cached, requires_auth=True)
        return self.store_project(key, self.call(self.provider(key).get_project(remote)))

    def store_project(self, key, project):
        record = self.project_record(key, project)
        archive = self.archive()
        if archive:
            try:
                archive.put(key, "container", project.origin.remote_id, record)
                for state in archive.states():
                    if state["account"] != key or state["kind"] != "containers":
                        continue
                    page = archive.get(key, "containers", state["remote"])
                    if page is not None:
                        items = [
                            record if x.get("id") == record["id"] else x
                            for x in page.get("items", [])
                        ]
                        if not state["remote"] and not any(
                            x.get("id") == record["id"] for x in items
                        ):
                            items.insert(0, record)
                        archive.put(key, "containers", state["remote"], dict(page, items=items))
                archive.mark_ready(key, "container", project.origin.remote_id)
            except OSError:
                record["cache_warning"] = "Proje güncellendi ancak yerel arşiv kaydı tamamlanamadı"
        return record

    def change_project(self, key, value, operation="update", **values):
        context, remote = origin(value, key)
        if context != key or not remote:
            raise ValueError("Proje seçili hesaba ait olmalı")
        provider = self.provider(key)
        if operation == "update":
            current = self.call(provider.get_project(remote))
            result = self.call(provider.update_project(replace(current, **values)), timeout=90)
        elif operation == "add_source":
            result = self.call(provider.add_project_resource(remote, **values), timeout=90)
        elif operation == "remove_source":
            result = self.call(provider.remove_project_resource(remote, **values), timeout=90)
        else:
            raise ValueError("Bilinmeyen proje işlemi")
        return self.store_project(key, result)

    def delete(self, value, key, kind="conversation"):
        context, remote = origin(value, key)
        if context != key or kind not in ("conversation", "container") or not remote:
            raise ValueError("Silinecek kayıt seçili hesaba ait olmalı")
        with self._lock:
            if self._sending.get(key, 0):
                raise RuntimeError("Hesabın gönderimleri tamamlandıktan sonra silin")
        provider = self.provider(key)
        capability = Capability.DELETE if kind == "conversation" else Capability.PROJECT_DELETE
        if capability not in provider.capabilities:
            raise ValueError("Sağlayıcı bu silme işlemini desteklemiyor")

        confirmed_children = None

        def cleanup():
            archive = self.archive()
            try:
                removed = (
                    archive.delete(key, kind, remote, children=confirmed_children)
                    if archive
                    else [(key, kind, remote)]
                )
                if kind == "container":
                    for member_key, project_id in tuple(self._chat_projects.items()):
                        if member_key[0] == key and project_id == remote:
                            self._chat_projects.pop(member_key, None)
                for _, target_kind, target in removed:
                    if target_kind == "conversation":
                        pointer = archive.get(key, "pointer", "active") if archive else None
                        if isinstance(pointer, dict) and pointer.get("remote") == target:
                            self.set_active(key, None)
                        if archive:
                            self.repository.cache_delete(key, "conversation", target)
                            self.repository.cache_delete(key, "tombstone", target)
                        else:
                            self.repository.cache_mark_deleted(key, target)
                        legacy = self.cache(key)
                        legacy.conversation_cache_path(target).unlink(missing_ok=True)
                        legacy.conversation_cache_path(target).with_suffix(".deleted").unlink(
                            missing_ok=True
                        )
                        if legacy.active_conversation_id() == target:
                            legacy.set_active_conversation(None)
                        self._chat_projects.pop((key, target), None)
            except OSError as error:
                raise RuntimeError(
                    "Sağlayıcıdan silindi; yerel kayıt temizlenemedi. Yenileyin."
                ) from error
            return {"removed": [identity(a, r) for a, _, r in removed], "kind": kind}

        async def remove():
            nonlocal confirmed_children
            archive = self.archive()
            members = (
                await asyncio.to_thread(archive.repository.container_members, key, remote)
                if kind == "container" and archive
                else set()
            )
            gate = self.api.tasks.account_gate(*key.split("/", 1))
            async with gate:
                try:
                    await (
                        provider.delete_chat(remote)
                        if kind == "conversation"
                        else provider.delete_project(remote)
                    )
                except RemoteNotFound:
                    # A missing mutation route is not proof that the record is gone.
                    try:
                        await (
                            provider.get_chat(remote)
                            if kind == "conversation"
                            else provider.get_project(remote)
                        )
                    except RemoteNotFound:
                        pass
                    else:
                        raise ProviderError(
                            "Sağlayıcı silmeyi doğrulamadı; uzak ve yerel kayıt korundu"
                        )
                if kind == "container":
                    confirmed_children = set()
                    for child in members:
                        try:
                            chat = await provider.get_chat(child)
                            if chat.metadata.get("is_visible") is False:
                                confirmed_children.add(child)
                        except RemoteNotFound:
                            confirmed_children.add(child)
                        except ProviderError:
                            pass  # Unknown remote outcome must not hide a surviving chat.
                return await asyncio.to_thread(cleanup)

        return self.call(remove(), timeout=60)

    def branch_chat(self, key, conversation_id, message_id):
        context, remote = origin(conversation_id, key)
        if context != key or not remote:
            raise ValueError("The conversation belongs to a different account")
        if not self.enabled(key.split("/")[0]):
            raise ValueError("Provider is disabled")
        if key not in {item["key"] for item in self.active_accounts()}:
            raise ValueError("Select this account before creating a branch")
        archive = self.archive()
        if archive and archive.deleted(key, "conversation", remote):
            raise RemoteNotFound("The conversation was deleted")
        source = self.cached_window(key, remote) or {}
        with self._lock:
            if key in self._session_changes:
                raise RuntimeError("Account verification is still running")
            self._sending[key] = self._sending.get(key, 0) + 1

        async def branch():
            provider = self.provider(key)
            async with self.api.tasks.account_gate(provider.id, provider.account_id):
                if not self.enabled(provider.id):
                    raise ValueError("Provider is disabled")
                if archive and archive.deleted(key, "conversation", remote):
                    raise RemoteNotFound("The conversation was deleted")
                return await provider.branch_chat(remote, message_id)

        try:
            record = self.chat_record(key, self.call(branch()))
            if record["remote_id"] == remote:
                raise ProviderError("The provider did not create a separate conversation")
            if (
                record.get("metadata", {}).get("title_unavailable")
                or not str(record.get("title") or "").strip()
            ):
                record["title"] = source.get("title") or record.get("title") or "Untitled"
            if not record.get("project_id"):
                record["project_id"] = source.get("project_id")
            archive = self.archive()
            native_preview = isinstance(record.get("pending_branch"), dict)
            if native_preview:
                record["branch_unanswered"] = True
                if not archive:
                    record["pending_local_branch"] = True
            self.cache_conversation(key, record, make_active=False)
            if archive and native_preview:
                archive.mark_pending_branch(key, record["remote_id"])
            return record
        finally:
            with self._lock:
                self._sending[key] -= 1

    def callbacks(self, key):
        if not key:

            def unavailable(*args, **kwargs):
                raise ValueError("Önce hesap ekleyin")

            return {
                "load_conversation": unavailable,
                "send_prompt": unavailable,
                "load_catalog": None,
                "list_projects_callback": None,
                "get_project_callback": None,
                "list_project_chats_callback": None,
                "upload_image": None,
                "move_conversation": None,
                "rename_conversation": None,
                "session_action": lambda action: (
                    self.status("") if action == "status" else unavailable()
                ),
                "forget_temporary": None,
                "release_conversation": lambda chat: None,
                "attach_file_callback": None,
                "attachment_policy_callback": None,
            }
        provider = self.provider(key)

        def get_project(project):
            context, remote = origin(project, key)
            return self.read_record(
                context,
                "container",
                remote,
                lambda: self.project_record(
                    context, self.call(self.provider(context).get_project(remote))
                ),
            )

        def project_chats(project, cursor=None):
            context, remote = origin(project, key)

            def fetch():
                page = self.call(self.provider(context).list_project_chats(remote, cursor))
                return {
                    "items": [self.chat_record(context, item) for item in page.items],
                    "cursor": page.next_cursor,
                }

            page = self.read_record(
                context,
                "container_chats",
                remote + ":" + (cursor or ""),
                fetch,
                fresh=True,
            )
            return self._include_pending_project_branches(context, remote, page, cursor)

        def move(chat, project):
            chat_context, remote = origin(chat, key)
            project_context, project_remote = origin(project, key)
            if chat_context != key or project_context != key:
                raise ValueError("Sohbet ve proje aynı sağlayıcı hesabında olmalı")
            return self.call(provider.move_chat(remote, project_remote))

        def rename(chat, title):
            context, remote = origin(chat, key)
            target = self.provider(context)
            self.call(target.rename_chat(remote, title))
            archive = self.archive()
            if archive:
                archive.rename_conversation(context, remote, title)
            cached = self.repository.cache_get(context, "conversation", remote)
            if isinstance(cached, dict):
                cached["title"] = title
                self.repository.cache_put(context, "conversation", remote, cached)

        def forget(chat):
            _, remote = origin(chat, key)
            if hasattr(provider, "forget_temporary"):
                result = provider.forget_temporary(remote)
                if asyncio.iscoroutine(result):
                    self.call(result)
            elif hasattr(provider, "_temporary_conversations"):
                provider._temporary_conversations.pop(remote, None)

        def release_conversation(chat):
            _, remote = origin(chat, key)
            if hasattr(provider, "forget_conversation"):
                result = provider.forget_conversation(remote)
                if asyncio.iscoroutine(result):
                    self.call(result)

        return {
            "upload_image": lambda *args: self.upload_image(key, *args),
            "load_conversation": lambda value: self.load(value, key, window=True, window_limit=25),
            "send_prompt": lambda *args, **kwargs: self.send(key, *args, **kwargs),
            "load_catalog": lambda: self.models(key),
            "list_projects_callback": lambda cursor=None: self.list_projects(key, cursor),
            "get_project_callback": get_project,
            "list_project_chats_callback": project_chats,
            "move_conversation": move,
            "rename_conversation": rename,
            "session_action": lambda action: self.session_action(key, action),
            "forget_temporary": forget,
            "release_conversation": release_conversation,
            "attach_file_callback": lambda path: self.attach_file(key, path),
            "attachment_policy_callback": lambda: self.attachment_policy(key),
        }
