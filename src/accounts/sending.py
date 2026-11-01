"""AccountsSending account operations."""

from __future__ import annotations

import asyncio
import threading
import uuid
from concurrent.futures import CancelledError, Future
from dataclasses import asdict

from providers import (
    BranchContext,
    ConversationBound,
    MessageAction,
    MessageMetadata,
    ReplaceText,
    ResponseComplete,
    SendRequest,
    TextDelta,
)
from providers.errors import AccountPaused, ProviderError, RemoteNotFound

from .shared import (
    identity,
    origin,
)


class AccountsSending:
    def _assert_send_account_available(self, key):
        if not self.enabled(key.split("/")[0]):
            raise ValueError("Sağlayıcı devre dışı")
        if key not in {item["key"] for item in self.active_accounts()}:
            raise ValueError(
                "Bu sağlayıcıda başka hesap etkin; Hesaplar bölümünden bu hesabı etkinleştirin"
            )

    def send(
        self,
        key,
        selected_id,
        prompt,
        attachments,
        on_event,
        on_connection,
        model=None,
        effort=None,
        **options,
    ):
        self._assert_send_account_available(key)
        context, remote = origin(selected_id, key)
        project_context, project = origin(options.get("project_id"), key)
        if context != key or project_context != key:
            raise ValueError("Sohbet ve proje seçili hesaba ait olmalı")
        if options.get("action"):
            action = MessageAction(options["action"])
            if action == MessageAction.BRANCH or not remote or not options.get("message_id"):
                raise ValueError("Invalid message generation action")
            if attachments or options.get("temporary"):
                raise ValueError("Message actions preserve the original conversation attachments")
        self._start()
        with self._lock:
            if key in self._session_changes:
                raise RuntimeError("Profil doğrulaması tamamlanana kadar bekleyin")
            self._sending[key] = self._sending.get(key, 0) + 1
        cancel_event = threading.Event()
        task = None
        job_id = None
        cancel_requested = False
        self_loop = self._loop

        def cancel():
            nonlocal cancel_requested
            cancel_event.set()
            if not cancel_requested:
                cancel_requested = True
                if task is not None and not task.done():
                    task.cancel()

        class Cancellation:
            @property
            def id(self):
                return job_id

            def shutdown(self, *_):
                self.close()

            def close(self):
                cancel_event.set()
                try:
                    self_loop.call_soon_threadsafe(cancel)
                except RuntimeError:
                    pass

        connection = Cancellation()
        try:
            on_connection(connection)
            if cancel_event.is_set():
                raise CancelledError()
            attachments, omitted_attachments = self._prepare_attachments(
                key, attachments, conversation_id=remote, cancel_event=cancel_event
            )
            if cancel_event.is_set():
                raise CancelledError()
        except BaseException:
            connection.close()
            raise
        finally:
            with self._lock:
                self._sending[key] -= 1
        provider_attachments = tuple(
            {
                name: value
                for name, value in attachment.items()
                if name
                not in {
                    "path",
                    "queue_id",
                    "source_account_key",
                    "source_tab_id",
                    "temporary_local",
                }
            }
            for attachment in attachments
        )
        archive = self.archive()
        if remote and archive and archive.deleted(key, "conversation", remote):
            raise RemoteNotFound("Silinmiş bir sohbete mesaj gönderilemez")
        baseline = archive.conversation_window(key, remote, limit=1) if archive and remote else None
        if baseline is None and remote:
            baseline = self.cached(key, remote)
        base_end = baseline.get("history_window", {}).get("total") if baseline else None
        pending_branch = (baseline or {}).get("pending_branch")
        request = SendRequest(
            key.split("/")[0],
            key.split("/")[1],
            selected_id or uuid.uuid4().hex,
            prompt,
            remote,
            model,
            effort,
            project,
            options.get("temporary", False),
            provider_attachments,
            action=MessageAction(options["action"]) if options.get("action") else None,
            message_id=options.get("message_id"),
            branch=BranchContext(**pending_branch) if pending_branch else None,
        )
        self.provider(key)
        stream_id = uuid.uuid4().hex

        completion = Future()

        async def consume():
            nonlocal job_id

            def guard():
                current_archive = self.archive()
                if current_archive and (
                    (remote and current_archive.deleted(key, "conversation", remote))
                    or (project and current_archive.deleted(key, "container", project))
                ):
                    raise RemoteNotFound("Sohbet veya proje bu sırada silindi")

            def deliver(event):
                if isinstance(event, ConversationBound):
                    on_event({"type": "bound", "conversation_id": event.conversation_id})
                    return
                if isinstance(event, MessageMetadata):
                    on_event(dict(asdict(event), type="metadata"))
                    return
                if isinstance(event, ResponseComplete):
                    on_event({"type": "response_complete"})
                    return
                if isinstance(event, (TextDelta, ReplaceText)):
                    on_event(
                        {
                            "type": "delta" if isinstance(event, TextDelta) else "replace",
                            "text": event.text,
                            "channel": event.channel,
                            "message_id": stream_id + ":" + (event.channel or "final"),
                        }
                    )

            def persist(snapshot):
                remote_id = snapshot.conversation_id or remote
                if not remote_id:
                    raise RuntimeError("Sağlayıcı sohbet kimliği döndürmedi")
                cache_warning = None
                try:
                    prior = (
                        {}
                        if request.temporary
                        else (
                            self.cached_window(key, remote_id)
                            if self.archive()
                            else self.cached(key, remote_id)
                        )
                        or {}
                    )
                except OSError:
                    prior = {}
                    cache_warning = "Yanıt alındı ancak yerel geçmiş okunamadı"

                resources = []
                archive = self.archive()
                if archive and not request.temporary:
                    for attachment in attachments:
                        resource = dict(attachment)
                        for internal in (
                            "queue_id",
                            "source_tab_id",
                            "source_account_key",
                            "temporary_local",
                        ):
                            resource.pop(internal, None)
                        # Keep the provider's attachment identity and display
                        # metadata, never the user's local path or file bytes.
                        resource.pop("path", None)
                        resource.pop("sha256", None)
                        resources.append(resource)
                messages = ([] if self.archive() else prior.get("messages", [])) + [
                    {
                        "id": (snapshot.metadata.user_message_id if snapshot.metadata else None)
                        or stream_id + ":user",
                        "local_pending": True,
                        "role": "user",
                        "text": prompt,
                        "resources": resources,
                        "actions": ["edit"]
                        if snapshot.metadata
                        and snapshot.metadata.user_message_id
                        and not request.temporary
                        else [],
                    }
                ]
                reasoning_text = snapshot.reasoning
                if reasoning_text:
                    messages.append(
                        {
                            "id": stream_id + ":analysis",
                            "local_pending": True,
                            "role": "reasoning",
                            "text": reasoning_text,
                        }
                    )
                messages.append(
                    {
                        "id": (snapshot.metadata.message_id if snapshot.metadata else None)
                        or stream_id + ":final",
                        "local_pending": True,
                        "role": "assistant",
                        "text": snapshot.text,
                        "actions": list(snapshot.metadata.actions) if snapshot.metadata else [],
                        "metadata": {
                            "model": (snapshot.metadata.model if snapshot.metadata else None)
                            or model,
                            "effort": (snapshot.metadata.effort if snapshot.metadata else None)
                            or effort,
                            "sources": list(snapshot.metadata.sources) if snapshot.metadata else [],
                            "files": list(snapshot.metadata.files) if snapshot.metadata else [],
                        },
                    }
                )
                title = prior.get("title") or (
                    prompt[:25].strip() + ("…" if len(prompt) > 25 else "")
                )
                record = {
                    "id": remote_id,
                    "title": title,
                    "messages": messages,
                    "project_id": options["project_id"]
                    if "project_id" in options
                    else prior.get("project_id"),
                    "context": key,
                    "provider_id": request.provider_id,
                    "account_id": request.account_id,
                    "model": model,
                    "temporary": request.temporary,
                }
                if not archive:
                    for field in (
                        "pending_local_branch",
                        "branch_listed_general",
                        "branch_listed_project",
                    ):
                        if prior.get(field):
                            record[field] = prior[field]
                    if prior.get("pending_local_branch"):
                        record["branch_unanswered"] = False
                if not request.temporary:
                    try:
                        archive = self.archive()
                        if archive:
                            archive.save_turn(
                                key,
                                remote_id,
                                dict(
                                    record, id=identity(key, remote_id), _archive_base_end=base_end
                                ),
                            )
                            archive.mark_branch_answered(key, remote_id)
                            self.set_active(key, remote_id)
                        else:
                            self.cache_conversation(key, record)
                    except (OSError, ValueError):
                        cache_warning = "Yanıt alındı ancak yerel arşive kaydedilemedi"
                return {
                    "conversation_id": identity(key, remote_id),
                    "cache_warning": cache_warning,
                    "sent_attachment_ids": [
                        item["queue_id"] for item in attachments if item.get("queue_id")
                    ],
                    "attachment_omitted_count": len(omitted_attachments),
                    "conversation": {"title": title, "default_model_slug": model, "messages": []},
                }

            async def complete(snapshot):
                if not (snapshot.conversation_id or remote):
                    raise ProviderError("Sağlayıcı sohbet kimliği döndürmedi")
                try:
                    if request.action or request.branch:
                        remote_id = snapshot.conversation_id or remote
                        authoritative = self.chat_record(
                            key, await self.provider(key).get_chat(remote_id)
                        )

                        def save_revision():
                            archive = self.archive()
                            if archive:
                                first = next(iter(authoritative.get("messages", ())), {})
                                replacement = (
                                    request.message_id
                                    if (
                                        request.action == MessageAction.EDIT
                                        and snapshot.metadata
                                        and first.get("id") == snapshot.metadata.user_message_id
                                    )
                                    else None
                                )
                                archive.replace_history(
                                    key,
                                    remote_id,
                                    authoritative,
                                    replaced_message_id=replacement,
                                    retry_message_id=(
                                        request.message_id
                                        if request.action == MessageAction.RETRY
                                        else None
                                    ),
                                    new_message_id=(
                                        snapshot.metadata.message_id
                                        if request.action == MessageAction.RETRY
                                        and snapshot.metadata
                                        else None
                                    ),
                                )
                                if request.branch and remote_id != remote:
                                    archive.delete(key, "conversation", remote)
                                    archive.mark_pending_branch(key, remote_id)
                                    archive.mark_branch_answered(key, remote_id)
                                self.set_active(key, remote_id)
                            else:
                                if request.branch:
                                    authoritative["pending_local_branch"] = True
                                    authoritative["branch_unanswered"] = False
                                self.cache_conversation(key, authoritative)
                                if request.branch and remote_id != remote:
                                    self.repository.cache_mark_deleted(key, remote)
                            return {
                                "conversation_id": identity(key, remote_id),
                                "replaced_conversation_id": identity(key, remote)
                                if request.branch and remote != remote_id
                                else None,
                                "conversation": {"title": authoritative["title"], "messages": []},
                                "history": self.cached_window(key, remote_id)
                                if archive
                                else authoritative,
                            }

                        return await asyncio.to_thread(save_revision)
                    return await asyncio.to_thread(persist, snapshot)
                except Exception:
                    # The remote turn has already completed. Local failures must
                    # not invite a duplicate POST or report the accepted send as failed.
                    return {
                        "conversation_id": identity(key, snapshot.conversation_id or remote),
                        "cache_warning": "Yanıt alındı ancak yerel arşive kaydedilemedi",
                        "sent_attachment_ids": [
                            item["queue_id"] for item in attachments if item.get("queue_id")
                        ],
                        "attachment_omitted_count": len(omitted_attachments),
                        "conversation": {
                            "title": prompt[:60],
                            "default_model_slug": model,
                            "messages": [],
                        },
                    }

            jobs = self.api.tasks
            job_id = jobs.submit(
                request, on_event=deliver, on_complete=complete, guard=guard, queue_chat=True
            )
            try:
                return await jobs.result(job_id)
            except asyncio.CancelledError:
                await jobs.cancel(job_id)
                raise
            finally:
                jobs.forget(job_id)

        with self._lock:
            if key in self._session_changes:
                raise RuntimeError("Profil doğrulaması tamamlanana kadar bekleyin")
            self._assert_send_account_available(key)
            self._sending[key] = self._sending.get(key, 0) + 1

            def finished(inner):
                try:
                    completion.set_result(inner.result())
                except asyncio.CancelledError:
                    completion.set_exception(CancelledError())
                except BaseException as error:
                    completion.set_exception(error)

            def start():
                nonlocal task
                task = self._loop.create_task(consume(), name="siope-send:" + key)
                task.add_done_callback(finished)
                if cancel_requested or cancel_event.is_set():
                    task.cancel()

            try:
                self._loop.call_soon_threadsafe(start)
            except BaseException:
                self._sending[key] -= 1
                raise

        try:
            return completion.result()
        except AccountPaused:
            self._forget_verification(key)
            raise
        finally:
            with self._lock:
                self._sending[key] -= 1
