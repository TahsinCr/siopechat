"""AccountsSending native synchronous streaming and MessageOperation bridge."""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import CancelledError
from typing import Any

from webaiprovider import (
    AccountPaused,
    ArtifactDiscovered,
    Attachment,
    GenerationEnd,
    OperationCancelledError,
    OutputBegin,
    OutputDelta,
    OutputEnd,
    OutputReplace,
    ProviderError,
    RemoteNotFound,
    ResponsePartsResolved,
    SourceDiscovered,
    TextPart,
    ThinkingBegin,
    ThinkingDelta,
    ThinkingEnd,
    ThinkingPart,
    ThinkingReplace,
    VerificationBegin,
)
from webaiprovider.accounts import session_exists

from .history import _artifact_dict, _model_slug_from, _source_dict
from .shared import identity, origin


def _resolve_model_target(provider: Any, model: str | None, effort: str | None):
    if not model:
        return None
    try:
        ref = provider.models.get(model)
    except Exception:
        return model
    if effort:
        try:
            return ref.select(effort)
        except Exception:
            return ref
    return ref


def _build_provider_attachments(attachments: list[dict[str, Any]]) -> tuple[Attachment, ...]:
    built: list[Attachment] = []
    for item in attachments:
        extra = {
            name: value
            for name, value in item.items()
            if name
            not in {
                "id",
                "name",
                "media_type",
                "mime_type",
                "remote_url",
                "path",
                "bytes",
                "size_bytes",
                "size",
                "queue_id",
                "source_account_key",
                "source_tab_id",
                "temporary_local",
            }
        }
        uploaded_id = item.get("id") or item.get("file_id")
        built.append(
            Attachment(
                id=str(uploaded_id or uuid.uuid4()),
                name=str(item.get("name") or "attachment"),
                media_type=item.get("mime_type") or item.get("media_type"),
                remote_url=item.get("remote_url"),
                path=None if uploaded_id else item.get("path"),
                content=item.get("bytes"),
                mime_type=item.get("mime_type"),
                size=item.get("size_bytes")
                if item.get("size_bytes") is not None
                else item.get("size"),
                metadata=extra,
            )
        )
    return tuple(built)


class AccountsSending:
    def _assert_send_account_available(self, key: str) -> None:
        if not self.enabled(key.split("/")[0]):
            raise ValueError("Sağlayıcı devre dışı")
        if key not in {item["key"] for item in self.active_accounts()}:
            raise ValueError(
                "Bu sağlayıcıda başka hesap etkin; Hesaplar bölümünden bu hesabı etkinleştirin"
            )

    def prepare_chat(
        self,
        key: str,
        conversation_id: str | None = None,
        *,
        project_id: str | None = None,
    ) -> None:
        """Warm provider/chat continuation context ahead of user send without duplicating work."""
        if not key or not self.account_is_visible(key):
            return
        context, remote = origin(conversation_id, key)
        if context != key:
            return
        project_remote: str | None = None
        if project_id:
            project_context, candidate_project = origin(project_id, key)
            if project_context == key:
                project_remote = candidate_project
        target = (key, remote, project_remote)
        with self._lock:
            if (
                self.closing
                or self.closed
                or target in self._preparing_chats
                or self._sending.get(key, 0)
            ):
                return
            self._preparing_chats.add(target)
        try:
            prov = self.open_provider(key)
            if remote:
                chat = self._chat_handle(key, remote, project_id=project_remote)
            else:
                project = prov.projects.get(project_remote) if project_remote else None
                chat = prov.chats.create(project=project)
            chat.prepare()
        finally:
            with self._lock:
                self._preparing_chats.discard(target)

    def send(
        self,
        key: str,
        selected_id: str | None,
        prompt: str,
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

        action = options.get("action")
        message_id = options.get("message_id")
        if action:
            if action not in ("edit", "retry") or not remote or not message_id:
                raise ValueError("Invalid message generation action")
            if attachments or options.get("temporary"):
                raise ValueError("Message actions preserve the original conversation attachments")

        with self._lock:
            if key in self._session_changes:
                raise RuntimeError("Profil doğrulaması tamamlanana kadar bekleyin")
            self._sending[key] = self._sending.get(key, 0) + 1

        cancel_event = threading.Event()
        operation_ref: list[Any] = []
        omitted_attachments: list[Any] = []
        prepared_attachments: list[dict[str, Any]] = []
        temporary = bool(options.get("temporary", False))

        class ConnectionHandle:
            @property
            def id(self):
                return operation_ref[0].message.id if operation_ref else None

            def shutdown(self, *_):
                self.close()

            def close(self):
                cancel_event.set()
                if operation_ref:
                    try:
                        operation_ref[0].cancel()
                    except Exception:
                        pass

        connection = ConnectionHandle()
        try:
            on_connection(connection)
            if cancel_event.is_set():
                raise CancelledError()
            prepared_attachments, omitted_attachments = self._prepare_attachments(
                key, attachments, conversation_id=remote, cancel_event=cancel_event
            )
            if cancel_event.is_set():
                raise CancelledError()
        except BaseException:
            connection.close()
            with self._lock:
                self._sending[key] -= 1
            raise

        archive = self.archive()
        if remote and archive and archive.deleted(key, "conversation", remote):
            with self._lock:
                self._sending[key] -= 1
            raise RemoteNotFound("Silinmiş bir sohbete mesaj gönderilemez")

        baseline = archive.conversation_window(key, remote, limit=1) if archive and remote else None
        base_end = baseline.get("history_window", {}).get("total") if baseline else None
        pending_branch = (baseline or {}).get("pending_branch")
        stream_id = uuid.uuid4().hex

        try:
            prov = self.open_provider(key)
            if (
                pending_branch
                and remote
                and hasattr(prov, "_pending_branches")
                and remote not in prov._pending_branches
            ):
                prov._pending_branches[remote] = pending_branch

            chat = self._chat_handle(
                key,
                remote,
                project_id=project,
                temporary=temporary,
                create_new=(remote is None),
            )
            if action and remote:
                known_msgs = getattr(chat.responses, "_known_messages", {})
                known_resps = getattr(chat.responses, "_known_responses", {})
                if message_id not in known_msgs and message_id not in known_resps:
                    chat, _ = prov.chats.get_with_responses(
                        remote, limit=None, include_projects=True
                    )
                    self._cache_chat_handle(key, chat)

            selected_model = _resolve_model_target(prov, model, effort)
            attachment_objs = _build_provider_attachments(prepared_attachments)

            if action == "edit":
                op = chat.responses.edit(message_id, prompt, model=selected_model)
            elif action == "retry":
                op = chat.responses.retry(message_id, model=selected_model)
            else:
                op = chat.responses.create(
                    prompt, model=selected_model, attachments=attachment_objs
                )

            operation_ref.append(op)
            with self._lock:
                self._active_operations.add(op)
            if cancel_event.is_set():
                op.cancel()
                raise CancelledError()

            last_bound_id = remote
            discovered_sources: list[dict[str, Any]] = []
            discovered_files: list[dict[str, Any]] = []
            observer = on_event

            def emit_event(payload: dict[str, Any]) -> None:
                nonlocal observer
                if observer is None:
                    return
                try:
                    observer(payload)
                except Exception:
                    observer = None
                    logging.getLogger("accounts").warning(
                        "Detached failing stream observer", exc_info=True
                    )

            def maybe_emit_bound() -> None:
                nonlocal last_bound_id
                bound = chat.remote_id
                if bound and bound != last_bound_id:
                    last_bound_id = bound
                    emit_event({"type": "bound", "conversation_id": bound})

            try:
                for event in op.stream():
                    if archive and (
                        (remote and archive.deleted(key, "conversation", remote))
                        or (project and archive.deleted(key, "container", project))
                    ):
                        op.cancel()
                        raise RemoteNotFound("Sohbet veya proje bu sırada silindi")

                    maybe_emit_bound()
                    part_id = getattr(event, "part_id", None)
                    if isinstance(event, ThinkingBegin):
                        emit_event(
                            {
                                "type": "begin",
                                "channel": "analysis",
                                "part_id": part_id,
                                "message_id": f"{stream_id}:{part_id or 'analysis'}",
                            }
                        )
                    elif isinstance(event, (ThinkingDelta, ThinkingReplace)):
                        text = event.delta if isinstance(event, ThinkingDelta) else event.text
                        emit_event(
                            {
                                "type": "delta" if isinstance(event, ThinkingDelta) else "replace",
                                "channel": "analysis",
                                "text": text,
                                "part_id": part_id,
                                "message_id": f"{stream_id}:{part_id or 'analysis'}",
                            }
                        )
                    elif isinstance(event, ThinkingEnd):
                        emit_event(
                            {
                                "type": "end",
                                "channel": "analysis",
                                "text": event.text,
                                "part_id": part_id,
                                "message_id": f"{stream_id}:{part_id or 'analysis'}",
                            }
                        )
                    elif isinstance(event, OutputBegin):
                        emit_event(
                            {
                                "type": "begin",
                                "channel": "final",
                                "part_id": part_id,
                                "message_id": f"{stream_id}:{part_id or 'final'}",
                            }
                        )
                    elif isinstance(event, (OutputDelta, OutputReplace)):
                        text = event.delta if isinstance(event, OutputDelta) else event.text
                        emit_event(
                            {
                                "type": "delta" if isinstance(event, OutputDelta) else "replace",
                                "channel": "final",
                                "text": text,
                                "part_id": part_id,
                                "message_id": f"{stream_id}:{part_id or 'final'}",
                            }
                        )
                    elif isinstance(event, OutputEnd):
                        emit_event(
                            {
                                "type": "end",
                                "channel": "final",
                                "text": event.text,
                                "part_id": part_id,
                                "message_id": f"{stream_id}:{part_id or 'final'}",
                            }
                        )
                    elif isinstance(event, GenerationEnd):
                        emit_event({"type": "response_complete"})
                    elif isinstance(event, VerificationBegin):
                        emit_event({"type": "verifying"})
                    elif isinstance(event, SourceDiscovered):
                        discovered_sources.append(_source_dict(event.source))
                    elif isinstance(event, ArtifactDiscovered):
                        discovered_files.append(_artifact_dict(event.artifact))
                    elif isinstance(event, ResponsePartsResolved):
                        emit_event(
                            {
                                "type": "parts_resolved",
                                "parts": [
                                    {
                                        "id": part.id,
                                        "role": "reasoning"
                                        if isinstance(part, ThinkingPart)
                                        else "assistant",
                                        "text": part.text,
                                    }
                                    for part in event.parts
                                ],
                            }
                        )

                response = op.wait()
                maybe_emit_bound()
            except OperationCancelledError as error:
                raise CancelledError() from error
            finally:
                with self._lock:
                    self._active_operations.discard(op)

            remote_id = chat.remote_id or remote
            if not remote_id:
                raise ProviderError("Sağlayıcı sohbet kimliği döndürmedi")
            if not temporary:
                self._cache_chat_handle(key, chat)

            resp_sources = [_source_dict(s) for s in response.sources] or discovered_sources
            resp_files = [_artifact_dict(a) for a in response.artifacts] or discovered_files
            resp_model = _model_slug_from(response.model) or response.metadata.get("model") or model
            resp_effort = effort
            if hasattr(response.model, "effort") and response.model.effort is not None:
                resp_effort = response.model.effort.id
            user_remote_id = op.message.remote_id or op.message.id
            assistant_remote_id = response.remote_id or response.id
            emit_event(
                {
                    "type": "metadata",
                    "message_id": assistant_remote_id,
                    "user_message_id": user_remote_id,
                    "model": resp_model,
                    "effort": resp_effort,
                    "actions": list(response.metadata.get("actions", ())),
                    "sources": resp_sources,
                    "files": resp_files,
                }
            )

            return self._persist_completed_send(
                key=key,
                chat=chat,
                response=response,
                op=op,
                remote=remote,
                remote_id=remote_id,
                prompt=prompt,
                attachments=prepared_attachments,
                omitted_attachments=omitted_attachments,
                model=resp_model,
                effort=resp_effort,
                project_option=options.get("project_id"),
                temporary=temporary,
                action=action,
                message_id=message_id,
                is_branch=bool(pending_branch),
                base_end=base_end,
                sources=resp_sources,
                files=resp_files,
                stream_id=stream_id,
            )
        except AccountPaused as error:
            if getattr(error, "requires_verification", True) and session_exists(
                self.credentials(key)
            ):
                self._forget_verification(key)
            raise
        finally:
            with self._lock:
                self._sending[key] -= 1

    def _persist_completed_send(
        self,
        *,
        key: str,
        chat: Any,
        response: Any,
        op: Any,
        remote: str | None,
        remote_id: str,
        prompt: str,
        attachments: list[dict[str, Any]],
        omitted_attachments: list[Any],
        model: str | None,
        effort: str | None,
        project_option: str | None,
        temporary: bool,
        action: str | None,
        message_id: str | None,
        is_branch: bool,
        base_end: int | None,
        sources: list[dict[str, Any]],
        files: list[dict[str, Any]],
        stream_id: str,
    ) -> dict[str, Any]:
        archive = self.archive()
        provider_id, account_id = key.split("/", 1)
        sent_attachment_ids = [item["queue_id"] for item in attachments if item.get("queue_id")]
        timing_data = (
            {
                "first_token_ms": response.timing.first_token_ms,
                "duration_ms": response.timing.duration_ms,
                **dict(response.timing.metrics),
            }
            if response.timing is not None
            else {}
        )

        if action or is_branch:
            try:
                prov = self.open_provider(key)
                fresh_chat, responses = prov.chats.get_with_responses(
                    remote_id, limit=None, include_projects=True
                )
                self._cache_chat_handle(key, fresh_chat)
                authoritative = self.chat_record(key, fresh_chat, responses)
                user_msg_id = op.message.remote_id or op.message.id
                asst_msg_id = response.remote_id or response.id
                first = next(iter(authoritative.get("messages", ())), {})
                replacement = (
                    message_id
                    if (action == "edit" and user_msg_id and first.get("id") == user_msg_id)
                    else None
                )
                if archive:
                    archive.replace_history(
                        key,
                        remote_id,
                        authoritative,
                        replaced_message_id=replacement,
                        retry_message_id=message_id if action == "retry" else None,
                        new_message_id=asst_msg_id if action == "retry" else None,
                    )
                    if is_branch and remote and remote_id != remote:
                        archive.delete(key, "conversation", remote)
                        archive.mark_pending_branch(key, remote_id)
                        archive.mark_branch_answered(key, remote_id)
                    self.set_active(key, remote_id)
                return {
                    "conversation_id": identity(key, remote_id),
                    "replaced_conversation_id": identity(key, remote)
                    if is_branch and remote and remote != remote_id
                    else None,
                    "sent_attachment_ids": sent_attachment_ids,
                    "attachment_omitted_count": len(omitted_attachments),
                    "timing": timing_data,
                    "conversation": {
                        "title": authoritative["title"],
                        "default_model_slug": model,
                        "messages": [],
                    },
                    "history": self.cached_window(key, remote_id) if archive else authoritative,
                }
            except Exception:
                return {
                    "conversation_id": identity(key, remote_id),
                    "cache_warning": "Yanıt alındı ancak yerel arşive kaydedilemedi",
                    "sent_attachment_ids": sent_attachment_ids,
                    "attachment_omitted_count": len(omitted_attachments),
                    "timing": timing_data,
                    "conversation": {
                        "title": prompt[:60],
                        "default_model_slug": model,
                        "messages": [],
                    },
                }

        cache_warning = None
        prior = {}
        if not temporary and archive:
            try:
                prior = self.cached_window(key, remote_id, limit=1) or {}
            except Exception:
                cache_warning = "Yanıt alındı ancak yerel geçmiş okunamadı"

        resources = []
        if archive and not temporary:
            for attachment in attachments:
                resource = {
                    name: val
                    for name, val in attachment.items()
                    if name
                    not in {
                        "queue_id",
                        "source_tab_id",
                        "source_account_key",
                        "temporary_local",
                        "path",
                        "sha256",
                    }
                }
                resources.append(resource)

        turn_messages = [
            {
                "id": op.message.remote_id or op.message.id or f"{stream_id}:user",
                "local_pending": True,
                "role": "user",
                "text": prompt,
                "resources": resources,
                "actions": ["edit"] if op.message.remote_id and not temporary else [],
            }
        ]
        user_actions = list(response.metadata.get("actions", ()))
        parts = list(response.parts)
        text_indices = [idx for idx, p in enumerate(parts) if isinstance(p, TextPart)]
        last_text_idx = text_indices[-1] if text_indices else -1
        resp_msg_id = response.remote_id or response.id or f"{stream_id}:final"
        base_meta = {
            "model": model,
            "effort": effort,
            "sources": sources,
            "files": files,
        }
        if not parts:
            turn_messages.append(
                {
                    "id": resp_msg_id,
                    "local_pending": True,
                    "role": "assistant",
                    "text": response.text,
                    "actions": user_actions,
                    "metadata": base_meta,
                }
            )
        else:
            for idx, part in enumerate(parts):
                if isinstance(part, ThinkingPart):
                    turn_messages.append(
                        {
                            "id": f"{stream_id}:{part.id}",
                            "part_id": part.id,
                            "local_pending": True,
                            "role": "reasoning",
                            "text": part.text,
                        }
                    )
                elif isinstance(part, TextPart):
                    is_last = idx == last_text_idx
                    turn_messages.append(
                        {
                            "id": resp_msg_id if is_last else f"{stream_id}:{part.id}",
                            "part_id": part.id,
                            "local_pending": True,
                            "role": "assistant",
                            "text": part.text,
                            "actions": user_actions if is_last else [],
                            "metadata": dict(base_meta) if is_last else {},
                        }
                    )

        title = (
            (chat.title if getattr(chat, "title", None) and chat.title != "New chat" else None)
            or prior.get("title")
            or (prompt[:25].strip() + ("…" if len(prompt) > 25 else ""))
        )
        record = {
            "id": identity(key, remote_id),
            "remote_id": remote_id,
            "title": title,
            "messages": turn_messages,
            "project_id": project_option if project_option is not None else prior.get("project_id"),
            "context": key,
            "provider_id": provider_id,
            "account_id": account_id,
            "model": model,
            "temporary": temporary,
        }
        if not temporary and archive:
            try:
                archive.save_turn(key, remote_id, dict(record, _archive_base_end=base_end))
                archive.mark_branch_answered(key, remote_id)
                self.set_active(key, remote_id)
            except Exception:
                cache_warning = "Yanıt alındı ancak yerel arşive kaydedilemedi"

        return {
            "conversation_id": identity(key, remote_id),
            "cache_warning": cache_warning,
            "sent_attachment_ids": sent_attachment_ids,
            "attachment_omitted_count": len(omitted_attachments),
            "timing": timing_data,
            "conversation": {
                "title": title,
                "default_model_slug": model,
                "messages": [],
                "parts": [
                    {
                        "id": part.id,
                        "role": "reasoning" if isinstance(part, ThinkingPart) else "assistant",
                        "text": part.text,
                    }
                    for part in parts
                ],
            },
        }
