"""Account-scoped Claude HTTPS operations and completion streams."""

import asyncio
import json
import mimetypes
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from ..api import MessageAction
from ..contracts import confirm_deleted
from ..errors import AccountPaused, ProviderError
from ..projects import ProjectOptions, source_file
from ..session import WebSession, read_session
from .catalog import selector_catalog


def remote_id(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid Claude identity") from None


def organization_from_bootstrap(data, selected):
    account = data.get("account")
    if not isinstance(account, dict):
        raise AccountPaused("Claude oturumu doğrulanamadı")
    organizations = [x.get("organization", {}) for x in account.get("memberships", [])]
    available = [x for x in organizations if "chat" in x.get("capabilities", [])]
    org = next((x for x in available if x.get("uuid") == selected), None)
    if org is None:
        if not selected and len(available) == 1:
            org = available[0]
        else:
            raise ProviderError("Claude çalışma alanı seçimi geçersiz; hesabı tekrar doğrulayın")
    return org["uuid"]


class ClaudeTransport:
    def __init__(self, credential_file, *, session=None):
        self.session = session or WebSession("https://claude.ai", credential_file, "sessionKey")
        self.path, self.organization, self.catalog = self.session.path, None, None
        self.authenticated = False
        self._discover_task = None
        self._discovered_at = 0.0

    async def _discover(self):
        data = await self.session.request("GET", "/api/bootstrap")
        values = read_session(self.path)
        organization = organization_from_bootstrap(
            data, values.get("cookies", values).get("lastActiveOrg")
        )
        selected = await self.session.request(
            "GET", "/api/organizations/" + remote_id(organization) + "/model_selector/chat"
        )
        catalog = selector_catalog(selected)
        self.organization = organization
        self.catalog = catalog
        self.authenticated = True
        self._discovered_at = time.monotonic()
        return catalog

    async def discover(self, *, force=False):
        if (
            not force
            and self.organization
            and self.catalog is not None
            and time.monotonic() - self._discovered_at < 300
        ):
            return self.catalog
        task = self._discover_task
        if task is None or task.done():
            task = asyncio.create_task(self._discover(), name="claude-account-discovery")
            task.add_done_callback(self._observe_discovery)
            self._discover_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._discover_task is task:
                self._discover_task = None

    @staticmethod
    def _observe_discovery(task):
        if not task.cancelled():
            task.exception()

    async def prepare(self):
        """Resolve the account workspace and its selectable model catalog once."""
        await self.discover()

    async def base(self):
        if not self.organization:
            await self.discover()
        return "/api/organizations/" + remote_id(self.organization)

    async def close(self):
        task, self._discover_task = self._discover_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.organization = None
        self.catalog = None
        self.authenticated = False
        await self.session.close()

    async def upload_file(self, path):
        """Upload a local file through Claude's web document conversion endpoint."""
        from curl_cffi import CurlMime

        path = Path(path).expanduser().resolve(strict=True)
        if (
            not path.is_file()
            or path.suffix.lower()
            not in {
                ".pdf",
                ".docx",
                ".csv",
                ".txt",
                ".html",
                ".htm",
                ".odt",
                ".rtf",
                ".epub",
                ".json",
                ".xlsx",
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".webp",
            }
            or not 0 < path.stat().st_size <= 30 * 1024 * 1024
        ):
            raise ValueError("Claude dosyaları 30 MB sınırını aşmamalı")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = CurlMime()
        try:
            body.addpart(name="file", filename=path.name, local_path=str(path), content_type=mime)
            result = await self.session.request(
                "POST", await self.base() + "/convert_document", multipart=body
            )
        finally:
            body.close()
        if not isinstance(result, dict):
            raise ProviderError("Claude dosya yükleme yanıtı geçersiz")
        file_id = result.get("file_uuid") or result.get("uuid") or result.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise ProviderError("Claude dosya yüklemesi kimlik döndürmedi")
        return {
            "id": file_id,
            "name": path.name,
            "mime_type": mime,
            "size_bytes": path.stat().st_size,
            "provider": "claude",
        }

    async def request(self, mode, **v):
        if mode in ("status", "models"):
            data = await self.discover(force=mode == "status" or bool(v.get("force", False)))
            return data if mode == "models" else {"authenticated": True}
        base = await self.base()
        cid = remote_id(v["conversation_id"]) if v.get("conversation_id") else None
        pid = remote_id(v["project_id"]) if v.get("project_id") else None
        call = self.session.request
        if mode == "chats":
            offset, limit = int(v.get("cursor") or 0), int(v.get("limit") or 50)
            if offset < 0 or not 1 <= limit <= 100:
                raise ValueError("Invalid Claude page")
            return await call("GET", base + f"/chat_conversations?limit={limit}&offset={offset}")
        if mode == "history":
            if cid is None:
                raise ValueError("Conversation required")
            return await call(
                "GET",
                base
                + "/chat_conversations/"
                + cid
                + "?tree=True&rendering_mode=messages&render_all_tools=true",
            )
        if mode == "projects":
            return await call("GET", base + "/projects")
        if mode == "create_project":
            return await call(
                "POST",
                base + "/projects",
                json={
                    "name": v["name"],
                    "description": "",
                    "prompt_template": v.get("instructions") or "",
                    "is_private": True,
                },
            )
        if mode in ("project", "project_chats", "delete_project"):
            if pid is None:
                raise ValueError("Project required")
            path = base + "/projects/" + pid
            if mode == "project_chats":
                return await call("GET", path + "/conversations")
            if mode == "delete_project":
                return await call("DELETE", path)
            detail = await call("GET", path)
            docs, files = await call("GET", path + "/docs"), await call("GET", path + "/files")
            if not isinstance(docs, list) or not isinstance(files, list):
                raise ProviderError("Claude resource contract changed")
            return dict(detail, docs=docs, files=files)
        if cid is None:
            raise ValueError("Conversation required")
        path = base + "/chat_conversations/" + cid
        if mode == "delete_chat":
            await call("DELETE", path)
            return await confirm_deleted(lambda: call("GET", path))
        if mode == "rename_chat":
            await call("PUT", path, json={"name": v["title"]})
            if (await call("GET", path)).get("name") != v["title"]:
                raise ProviderError("Claude rename readback failed")
            return None
        if mode == "move_chat":
            await call(
                "POST",
                base + "/chat_conversations/move_many",
                json={"conversation_uuids": [cid], "project_uuid": pid},
            )
            if (await call("GET", path)).get("project_uuid") != pid:
                raise ProviderError("Claude project membership readback failed")
            return None
        raise ValueError("Unsupported Claude operation")

    async def events(self, mode, **v):
        if mode != "send":
            raise ValueError("Unsupported stream operation")
        base = await self.base()
        model = v.get("model") or self.catalog["default_model"]
        choice = next((x for x in self.catalog["models"] if x["id"] == model), None)
        if choice is None or (
            v.get("effort") and not any(x["id"] == v["effort"] for x in choice["efforts"])
        ):
            raise ValueError("Select a model/thinking option from this account")
        pid = remote_id(v["project_id"]) if v.get("project_id") else None
        cid = remote_id(v["conversation_id"]) if v.get("conversation_id") else str(uuid.uuid4())
        path = base + "/chat_conversations/" + cid
        if v.get("conversation_id"):
            history = (
                await self.request("history", conversation_id=cid)
                if v.get("action")
                else await self.session.request("GET", path)
            )
            if v.get("temporary") and history.get("is_temporary") is not True:
                raise ProviderError("Claude geçici sohbeti doğrulanamadı; mesaj gönderilmedi")
            parent = history.get("current_leaf_message_uuid")
            if v.get("action"):
                from ..messages import target_message
                from .records import conversation

                role = "user" if v["action"] == MessageAction.EDIT else "assistant"
                _, target = target_message(
                    conversation(history, "").messages, v["message_id"], role
                )
                if v["action"].value not in target.actions:
                    raise ProviderError("This Claude message action is unavailable")
                original = target.metadata["native_message"]
                parent = original.get("parent_message_uuid")
                v = dict(
                    v,
                    attachments=[
                        dict(item, id=item.get("uuid") or item.get("id"))
                        for item in original.get("files", [])
                    ],
                    original_attachments=original.get("attachments", []),
                )
                if v["action"] == MessageAction.RETRY:
                    parent = original["uuid"]
        else:
            create_payload = {"uuid": cid, "name": ""}
            if v.get("temporary"):
                create_payload["is_temporary"] = True
            created = await self.session.request(
                "POST", base + "/chat_conversations", json=create_payload
            )
            if v.get("temporary") and (
                not isinstance(created, dict) or created.get("is_temporary") is not True
            ):
                try:
                    created = await self.session.request("GET", path)
                except Exception:
                    created = None
                if not isinstance(created, dict) or created.get("is_temporary") is not True:
                    try:
                        await self.request("delete_chat", conversation_id=cid)
                    except Exception:
                        raise ProviderError(
                            "Claude geçici sohbeti doğrulayamadı; mesaj gönderilmedi ve "
                            "boş sohbet temizliği doğrulanamadı"
                        ) from None
                    raise ProviderError("Claude geçici sohbeti doğrulayamadı; mesaj gönderilmedi")
            parent = created.get("current_leaf_message_uuid") if isinstance(created, dict) else None
            if pid:
                await self.request("move_chat", conversation_id=cid, project_id=pid)
        yield {"type": "bound", "id": cid}
        body = {
            "prompt": v["prompt"],
            "model": model,
            "timezone": "UTC",
            "attachments": v.get("original_attachments", []),
            "files": [item["id"] for item in v.get("attachments", []) if item.get("id")],
            "rendering_mode": "messages",
        }
        if parent:
            body["parent_message_uuid"] = parent
        effort = v.get("effort")
        if effort:
            if "selections" in choice:
                body.update(choice["selections"][effort])
            else:
                body["thinking_mode"] = effort
        completed, blocks = False, {}
        response_id, parent_id, final_index, sources, files = None, None, 0, (), ()
        response_model = None
        presented_inputs = {}
        suffix = "/retry_completion" if v.get("action") == MessageAction.RETRY else "/completion"
        async with self.session.connect().stream(
            "POST",
            self.session.url(path + suffix),
            json=body,
            headers={"accept": "text/event-stream"},
            allow_redirects=False,
            timeout=600,
        ) as response:
            self.session.check(response)
            payload = []
            payload_size = 0
            async for line in response.aiter_lines():
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                if line.startswith("data:"):
                    payload.append(line[5:].lstrip())
                    payload_size += len(payload[-1])
                    if payload_size > 1_048_576:
                        raise ProviderError("Claude stream event exceeds size limit")
                    continue
                if line or not payload:
                    continue
                raw, payload = "\n".join(payload), []
                payload_size = 0
                if raw == "[DONE]":
                    continue
                event = json.loads(raw)
                kind, delta = event.get("type"), event.get("delta", {})
                if kind == "message_start":
                    native = event.get("message") or {}
                    response_id = native.get("uuid") or native.get("id")
                    parent_id = native.get("parent_uuid")
                    response_model = native.get("model")
                if kind == "error" or event.get("error"):
                    raise ProviderError("Claude akışı hata verdi; istek tekrarlanmadı")
                if kind == "content_block_start":
                    block = event.get("content_block", {})
                    from ..messages import files_from

                    files = files_from({"files": files}, block)
                    if block.get("type") == "tool_use" and block.get("name") == "present_files":
                        from .records import presented_files

                        files = files_from({"files": files + presented_files(block)})
                        presented_inputs[event.get("index", 0)] = ""
                    blocks[event.get("index", 0)] = block.get("type")
                    if block.get("type") == "text":
                        from ..messages import sources_from

                        final_index = event.get("index", 0)
                        sources = sources_from({"sources": list(sources)}, block)
                    if block.get("type") == "text" and block.get("text"):
                        yield {"type": "delta", "text": block["text"]}
                    elif block.get("type") == "thinking" and block.get("thinking"):
                        yield {"type": "delta", "channel": "analysis", "text": block["thinking"]}
                if kind == "content_block_delta":
                    index = event.get("index", 0)
                    if delta.get("type") == "input_json_delta" and index in presented_inputs:
                        value = presented_inputs[index] + (delta.get("partial_json") or "")
                        if len(value) <= 65536:
                            presented_inputs[index] = value
                        else:
                            presented_inputs.pop(index)
                    if delta.get("type") == "citations_delta":
                        from ..messages import sources_from

                        sources = sources_from(
                            {"sources": list(sources), "citations": [delta.get("citation")]}
                        )
                    channel = blocks.get(event.get("index", 0))
                    if delta.get("type") == "thinking_delta" and channel == "thinking":
                        yield {
                            "type": "delta",
                            "channel": "analysis",
                            "text": delta.get("thinking", ""),
                        }
                    elif delta.get("type") == "text_delta" and channel == "text":
                        yield {"type": "delta", "text": delta.get("text", "")}
                if kind == "content_block_stop":
                    value = presented_inputs.pop(event.get("index", 0), "")
                    if value:
                        from .records import presented_files

                        try:
                            inputs = json.loads(value)
                        except ValueError:
                            inputs = {}
                        if isinstance(inputs, dict):
                            files = files_from(
                                {
                                    "files": files
                                    + presented_files(
                                        {
                                            "type": "tool_use",
                                            "name": "present_files",
                                            "input": inputs,
                                        }
                                    )
                                }
                            )
                elif "completion" in event:
                    yield {"type": "delta", "text": event.get("completion") or ""}
                    completed = bool(event.get("stop_reason"))
                if kind == "message_stop" or (kind == "message_delta" and delta.get("stop_reason")):
                    completed = True
        if not completed:
            raise ProviderError("Claude yanıtı tamamlanmadı; istek yeniden gönderilmedi")
        if response_id:
            yield {
                "type": "metadata",
                "message_id": response_id + ":" + str(final_index),
                "user_message_id": parent_id + ":0"
                if isinstance(parent_id, str) and v.get("action") != MessageAction.RETRY
                else None,
                "sources": sources,
                "files": files,
                "model": response_model,
                "actions": ["retry"],
            }
        yield {"type": "done"}


class ClaudeProjectEditor:
    def __init__(self, transport):
        self.transport = transport

    async def close(self):
        pass

    async def options(self, project_id=None):
        return ProjectOptions(
            fields=("name", "instructions") if project_id else ("name",),
            sources=(
                {"id": "text", "title": "Metin"},
                {"id": "file", "title": "Dosya", "accept": "*/*"},
            )
            if project_id
            else (),
            remove_sources=bool(project_id),
        )

    async def write(self, operation, project_id=None, **v):
        path = await self.transport.base() + "/projects/" + remote_id(project_id)
        call = self.transport.session.request
        if operation == "update":
            fields = {"name": v["name"]} if "name" in v else {}
            if "instructions" in v:
                fields["prompt_template"] = v["instructions"]
            return await call("PUT", path, json=fields)
        if operation == "add_source":
            if v["kind"] == "text":
                return await call(
                    "POST", path + "/docs", json={"file_name": v["name"], "content": v["text"]}
                )
            if v["kind"] != "file":
                raise ValueError("Unsupported source type")
            from curl_cffi import CurlMime

            file = source_file(v["path"], error="Source must be at most 20 MiB")
            mime = CurlMime()
            try:
                mime.addpart(name="file", filename=file.name, local_path=str(file))
                return await call("POST", path + "/upload", multipart=mime)
            finally:
                mime.close()
        if operation == "remove_source":
            rid = quote(v["resource_id"], safe="")
            current = await self.transport.request("project", project_id=project_id)
            doc = next(
                (x for x in current["docs"] if (x.get("uuid") or x.get("id")) == v["resource_id"]),
                None,
            )
            if doc is not None:
                return await call("DELETE", path + "/docs/" + rid)
            file = next(
                (x for x in current["files"] if (x.get("uuid") or x.get("id")) == v["resource_id"]),
                None,
            )
            if file is None or not file.get("file_uuid"):
                raise ValueError("Kaynak bu projeye ait değil veya dosya kimliği eksik")
            return await call(
                "POST", path + "/files/delete_many", json={"file_uuids": [file["file_uuid"]]}
            )
        raise ValueError("Unknown project operation")
