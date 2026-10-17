#!/usr/bin/env python3
"""Standalone synchronous ChatGPT web client with injected browser session."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from ..errors import RateLimited
from . import discovery
from .constants import CHATGPT_ORIGIN, DEFAULT_PAGE_SIZE, FORBIDDEN_REPLAY_HEADERS
from .errors import ChatGPTWebError, SentinelCompatibilityError
from .formats import message_parts, text_from_parts
from .resources import Script, script_path
from .session import BrowserSession


class ChatGPTClient:
    def __init__(self, session: BrowserSession, *, authenticate: bool = True) -> None:
        self.session = session
        self.cdp_url = session.cdp_url
        self.list_url = ""
        self.headers: dict[str, str] = {}
        self.temporary_conversations: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._last_stream_messages: list[dict[str, Any]] = []
        self.http = httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(30.0),
        )
        if authenticate:
            try:
                self.refresh_auth()
            except BaseException:
                self.http.close()
                raise

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "ChatGPTClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def refresh_auth(self, prefer_browser: bool = False) -> None:
        if prefer_browser and self.session.is_ready():
            self.list_url, self.headers = self.session.capture_auth()
            self.session.save(self.list_url, self.headers)
            return
        try:
            self.list_url, self.headers = self.session.load()
        except ChatGPTWebError:
            if not self.session.is_ready():
                raise
            self.list_url, self.headers = self.session.capture_auth()
            self.session.save(self.list_url, self.headers)

    def refresh_session(self) -> None:
        headers = self.headers.copy()
        headers.pop("authorization", None)
        response = self.http.get(
            urljoin(CHATGPT_ORIGIN, "/api/auth/session"),
            headers=headers,
            timeout=30,
        )
        if response.status_code != 200:
            raise ChatGPTWebError("saved session expired; run siopechat login")
        try:
            value = response.json()
            token = value.get("accessToken")
        except (ValueError, AttributeError) as error:
            raise ChatGPTWebError("ChatGPT session refresh returned invalid data") from error
        if not isinstance(token, str) or not token:
            raise ChatGPTWebError("saved session expired; run siopechat login")
        self.headers["authorization"] = f"Bearer {token}"
        cookie_header = self.headers.get("cookie", "")
        cookies = SimpleCookie()
        cookies.load(cookie_header)
        for cookie in response.cookies.jar:
            cookies[cookie.name] = cookie.value
        self.headers["cookie"] = "; ".join(
            f"{name}={morsel.value}" for name, morsel in cookies.items()
        )
        self.session.save(self.list_url, self.headers)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        target = urlsplit(url)
        if target.scheme != "https" or target.netloc != "chatgpt.com":
            raise ChatGPTWebError("authenticated requests must stay on the ChatGPT origin")
        for attempt in range(2):
            request_headers = self.headers.copy()
            # httpx only decodes Brotli and Zstandard when optional packages are
            # installed. Ask for encodings available in the minimal uv setup.
            request_headers["accept-encoding"] = "gzip, deflate"
            target_path = urlsplit(url).path
            request_headers["x-openai-target-path"] = target_path
            request_headers["x-openai-target-route"] = target_path
            response = self.http.request(method, url, headers=request_headers, **kwargs)
            if response.status_code not in (401, 403) or attempt == 1:
                break
            try:
                self.refresh_session()
            except ChatGPTWebError:
                if not self.session.is_ready():
                    raise
                self.list_url, self.headers = self.session.capture_auth()
                self.session.save(self.list_url, self.headers)
        if response.status_code == 429:
            try:
                delay = float(response.headers.get("retry-after", ""))
            except ValueError:
                delay = None
            raise RateLimited(delay)
        if response.status_code >= 400:
            raise ChatGPTWebError(
                f"ChatGPT returned HTTP {response.status_code} for {method} {response.url.path}"
            )
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            raise ChatGPTWebError(
                f"ChatGPT returned unexpected content type {content_type or '<missing>'}"
            )
        return response

    def verify(self) -> dict[str, Any]:
        return self.request(
            "GET",
            urljoin(CHATGPT_ORIGIN, "/backend-api/conversations"),
            params={
                "offset": 0,
                "limit": DEFAULT_PAGE_SIZE,
                "order": "updated",
                "is_archived": "false",
                "is_starred": "false",
            },
        ).json()

    def model_catalog(self, *, refresh: bool = False):
        cached = getattr(self, "_model_catalog", None)
        if cached is not None and not refresh and time.monotonic() - cached[0] < 300:
            return cached[1]
        raw = self.request(
            "GET",
            urljoin(CHATGPT_ORIGIN, "/backend-api/models"),
            params={
                "iim": "false",
                "is_gizmo": "false",
                "supports_model_picker_upgrade_presets": "true",
            },
        ).json()
        catalog = discovery.model_catalog(raw)
        self._model_catalog = (time.monotonic(), catalog)
        return catalog

    def list_projects(self, cursor: str | None = None) -> dict[str, Any]:
        params = {"conversations_per_gizmo": 0, "limit": 20, "owned_only": "false"}
        if cursor is not None:
            params["cursor"] = cursor
        raw = self.request(
            "GET", urljoin(CHATGPT_ORIGIN, "/backend-api/gizmos/snorlax/sidebar"), params=params
        ).json()
        items, next_cursor = discovery.page(raw)
        projects = [value for item in items if (value := discovery.project(item)) is not None]
        return {"items": projects, "cursor": next_cursor}

    def get_project(self, project_id: str) -> dict[str, Any]:
        raw = self.request(
            "GET", urljoin(CHATGPT_ORIGIN, f"/backend-api/gizmos/{quote(project_id, safe='')}")
        ).json()
        value = discovery.project(raw)
        if value is None:
            raise ChatGPTWebError("Requested gizmo is not a project")
        return value

    def list_project_chats(self, project_id: str, cursor: str | None = None) -> dict[str, Any]:
        raw = self.request(
            "GET",
            urljoin(
                CHATGPT_ORIGIN, f"/backend-api/gizmos/{quote(project_id, safe='')}/conversations"
            ),
            params={"cursor": cursor or "0", "limit": 30, "owned_only": "false"},
        ).json()
        items, next_cursor = discovery.page(raw)
        return {
            "items": [dict(item, project_id=project_id) for item in items],
            "cursor": next_cursor,
        }

    def list_page(self, offset: int = 0, limit: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid conversation page")
        return self.request(
            "GET",
            urljoin(CHATGPT_ORIGIN, "/backend-api/conversations"),
            params={
                "offset": offset,
                "limit": limit,
                "order": "updated",
                "is_archived": "false",
                "is_starred": "false",
            },
        ).json()

    def list_conversations(self, maximum: int | None) -> list[dict[str, Any]]:
        offset = 0
        conversations: list[dict[str, Any]] = []
        seen: set[str] = set()
        while True:
            response = self.request(
                "GET",
                urljoin(CHATGPT_ORIGIN, "/backend-api/conversations"),
                params={
                    "offset": offset,
                    "limit": DEFAULT_PAGE_SIZE,
                    "order": "updated",
                    "is_archived": "false",
                    "is_starred": "false",
                },
            ).json()
            items = response.get("items", [])
            if not isinstance(items, list):
                raise ChatGPTWebError("conversation list did not contain an items array")
            fresh = [
                item for item in items if isinstance(item, dict) and item.get("id") not in seen
            ]
            conversations.extend(
                dict(item, project_id=discovery.project_id(item)) for item in fresh
            )
            seen.update(item["id"] for item in fresh if item.get("id"))
            offset += len(items)
            if maximum is not None and len(conversations) >= maximum:
                return conversations[:maximum]
            # The private endpoint's `total` is a rolling window marker rather
            # than a stable collection size. A short page (or no new IDs) is
            # the reliable end condition.
            if len(items) < DEFAULT_PAGE_SIZE or not fresh:
                return conversations

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        url = urljoin(
            CHATGPT_ORIGIN,
            f"/backend-api/conversations/{conversation_id}",
        )
        result = self.request("GET", url).json()
        if not isinstance(result, dict):
            raise ChatGPTWebError("conversation response was not an object")
        return result

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        self.request(
            "PATCH",
            urljoin(CHATGPT_ORIGIN, f"/backend-api/conversation/{conversation_id}"),
            json={"title": title},
        )

    def move_conversation(self, conversation_id: str, project_id: str | None) -> dict[str, Any]:
        """Change native membership, then require the server to confirm it."""
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ValueError("conversation ID is required")
        if project_id is not None and (
            not isinstance(project_id, str) or not project_id.startswith("g-p-")
        ):
            raise ValueError("invalid project ID")
        self.request(
            "PATCH",
            urljoin(CHATGPT_ORIGIN, f"/backend-api/conversation/{quote(conversation_id, safe='')}"),
            json={"gizmo_id": project_id},
        )
        conversation = self.get_conversation(conversation_id)
        if discovery.project_id(conversation) != project_id:
            raise ChatGPTWebError(
                "Server did not confirm project membership; refresh this conversation"
            )
        return conversation

    def send_prompt(
        self,
        conversation_id: str | None,
        prompt: str,
        attachments: list[dict[str, Any]] | None = None,
        on_event: Any = None,
        model: str | None = None,
        effort: str | None = None,
        project_id: str | None = None,
        temporary: bool = False,
    ) -> tuple[str, dict[str, Any], set[str]]:
        if type(temporary) is not bool or (
            project_id is not None
            and (not isinstance(project_id, str) or not project_id.startswith("g-p-"))
        ):
            raise ChatGPTWebError("invalid conversation options")
        if temporary and project_id:
            raise ChatGPTWebError("temporary conversations cannot belong to a project")
        send_mode = os.environ.get("CHATGPT_WEB_SEND_MODE", "node")
        if attachments and send_mode != "node":
            raise ChatGPTWebError("image attachments require the direct node send mode")
        if temporary:
            send_mode = "node"
        previous_ids: set[str] = set()
        previous: dict[str, Any] | None = None
        if conversation_id:
            if temporary:
                previous = self.temporary_conversations.get(conversation_id)
                if previous is None:
                    raise ChatGPTWebError(
                        "temporary conversation expired locally; start a new temporary chat"
                    )
            else:
                previous = self.get_conversation(conversation_id)
            previous_messages = previous.get("messages", [])
            if isinstance(previous_messages, list):
                previous_ids = {
                    item["id"]
                    for item in previous_messages
                    if isinstance(item, dict) and item.get("id")
                }

        if project_id:
            if previous and discovery.project_id(previous) not in (None, project_id):
                raise ChatGPTWebError("conversation belongs to another project")
            previous = dict(previous or {}, project_id=project_id)
        if model or effort or send_mode != "chrome":
            catalog = self.model_catalog()
            model = (
                model
                or os.environ.get("CHATGPT_WEB_MODEL")
                or (previous.get("default_model_slug") if previous else None)
                or catalog.default_model
            )
            selected = next((item for item in catalog.models if item.id == model), None)
            if selected is None:
                raise ChatGPTWebError(
                    "Select a model from the current account catalog before sending"
                )
            if effort is not None and effort not in {item.id for item in selected.efforts}:
                raise ChatGPTWebError(f"Effort '{effort}' is not offered for model '{model}'")

        if send_mode != "chrome":
            resolved_id, submitted_message_id = self.send_prompt_direct(
                previous,
                prompt,
                attachments or [],
                on_event,
                model,
                effort,
                **({"temporary": True} if temporary else {}),
            )
            if temporary:
                conversation = {
                    "id": resolved_id,
                    "conversation_id": resolved_id,
                    "title": "Geçici sohbet",
                    "default_model_slug": model,
                    "current_node": self._last_stream_messages[-1].get("id")
                    if self._last_stream_messages
                    else None,
                    "messages": list((previous or {}).get("messages", []))
                    + [
                        {
                            "id": submitted_message_id,
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": [prompt]},
                        }
                    ]
                    + self._last_stream_messages,
                    "temporary": True,
                }
                if not conversation["current_node"]:
                    raise ChatGPTWebError("temporary response has no continuation cursor")
                size = sum(
                    len(text_from_parts(message_parts(item.get("content"))))
                    for item in conversation["messages"]
                )
                if size > 2_000_000:
                    raise ChatGPTWebError(
                        "temporary conversation reached the local memory limit; start a new one"
                    )
                self.temporary_conversations[resolved_id] = conversation
                self.temporary_conversations.move_to_end(resolved_id)
                while len(self.temporary_conversations) > 32:
                    self.temporary_conversations.popitem(last=False)
            else:
                conversation = self.get_conversation(resolved_id)
            messages = conversation.get("messages", [])
            attached = isinstance(messages, list) and any(
                isinstance(message, dict) and message.get("id") == submitted_message_id
                for message in messages
            )
            if not attached:
                raise ChatGPTWebError(
                    "ChatGPT did not attach the submitted message to the conversation"
                )
            return resolved_id, conversation, previous_ids

        helper = script_path(Script.SEND)
        environment = self.session.environment()
        environment["CHATGPT_WEB_CDP_URL"] = self.cdp_url.rstrip("/")
        process = None
        try:
            process = subprocess.Popen(
                ["node", str(helper)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=environment,
            )
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(
                json.dumps(
                    {
                        "conversation_id": conversation_id,
                        "prompt": prompt,
                        "model": model,
                        "effort": effort,
                        "project_id": project_id,
                    }
                )
            )
            process.stdin.close()
            resolved_id = None
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ChatGPTWebError("Chrome returned an invalid stream event") from error
                if event.get("type") == "completed":
                    resolved_id = event.get("conversation_id")
                elif on_event is not None:
                    on_event(event)
            return_code = process.wait(timeout=5)
        except (BrokenPipeError, ConnectionError, OSError):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            raise
        except subprocess.TimeoutExpired as error:
            if process is not None:
                process.kill()
            raise ChatGPTWebError("prompt submission timed out") from error
        if return_code != 0:
            assert process.stderr is not None
            detail = process.stderr.read().strip() or "prompt submission failed"
            raise ChatGPTWebError(detail)
        if not isinstance(resolved_id, str) or not resolved_id:
            raise ChatGPTWebError("ChatGPT did not return a conversation ID")

        conversation = self.get_conversation(resolved_id)
        return resolved_id, conversation, previous_ids

    def send_prompt_direct(
        self,
        conversation: dict[str, Any] | None,
        prompt: str,
        attachments: list[dict[str, Any]],
        on_event: Any = None,
        model: str | None = None,
        effort: str | None = None,
        temporary: bool = False,
    ) -> tuple[str, str]:
        conversation_id = None
        if conversation:
            candidate = conversation.get("conversation_id") or conversation.get("id")
            if isinstance(candidate, str):
                conversation_id = candidate
        mode = os.environ.get("CHATGPT_WEB_SEND_MODE", "node")
        if temporary and mode == "recipe":
            raise ChatGPTWebError(
                "temporary chat requires direct transport; browser recipe capture is disabled"
            )
        if mode == "recipe":
            method, url, headers, body = self.capture_send_recipe(conversation_id, prompt)
        else:
            try:
                sentinel_headers = self.create_sentinel_headers()
            except SentinelCompatibilityError as error:
                if temporary:
                    raise ChatGPTWebError(
                        "temporary chat preparation failed; no browser fallback was used"
                    ) from error
                if not self.session.is_ready():
                    raise ChatGPTWebError(
                        f"{error}; run siopechat login for compatibility fallback"
                    ) from error
                method, url, headers, body = self.capture_send_recipe(conversation_id, prompt)
            else:
                method = "POST"
                url = urljoin(CHATGPT_ORIGIN, "/backend-api/f/conversation")
                headers = self.headers.copy()
                headers.update(sentinel_headers)
                body = self.create_conversation_body(conversation, prompt, attachments)

        # Apply the provider-owned choices to this request; never retain an
        # effort preference captured from a different model.
        if model:
            body["model"] = model
            # A captured recipe can carry the old model's effort preference.
            body.pop("thinking_effort", None)
        if effort:
            body["thinking_effort"] = effort

        if conversation and discovery.project_id(conversation):
            body["gizmo_id"] = discovery.project_id(conversation)
        body["history_and_training_disabled"] = temporary
        headers.pop("x-openai-web-sse-compression", None)
        headers["x-openai-target-path"] = "/backend-api/f/conversation"
        headers["x-openai-target-route"] = "/backend-api/f/conversation"
        body["supported_encodings"] = []
        body["supports_buffering"] = False
        submitted_message_id = body.get("messages", [{}])[0].get("id")
        if not isinstance(submitted_message_id, str) or not submitted_message_id:
            raise ChatGPTWebError("conversation request has no user message ID")
        resolved_id: str | None = conversation_id
        rendered_by_channel = {}
        streamed_messages = OrderedDict()
        self._last_stream_messages = []
        with self.http.stream(
            method,
            url,
            headers=headers,
            json=body,
            timeout=httpx.Timeout(300.0),
        ) as response:
            if response.status_code == 429:
                retry_after = response.headers.get("retry-after")
                suffix = f"; retry after {retry_after} seconds" if retry_after else ""
                raise ChatGPTWebError(f"ChatGPT web rate limit reached{suffix}")
            if response.status_code >= 400:
                raise ChatGPTWebError(f"ChatGPT submission returned HTTP {response.status_code}")
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                raise ChatGPTWebError(
                    f"ChatGPT returned unexpected content type {content_type or '<missing>'}"
                )
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_id = event.get("conversation_id")
                if isinstance(event_id, str) and event_id:
                    resolved_id = event_id
                message = event.get("message")
                if not isinstance(message, dict):
                    continue
                author = message.get("author")
                if not isinstance(author, dict) or author.get("role") != "assistant":
                    continue
                identity = str(message.get("id") or message.get("channel") or "assistant")
                streamed_messages[identity] = message
                self._last_stream_messages = list(streamed_messages.values())
                content = message.get("content") or {}
                channel = message.get("channel") or (
                    "analysis"
                    if content.get("content_type")
                    in ("thoughts", "reasoning_recap", "reasoning_summary")
                    else "final"
                )
                if (message.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
                    continue
                if message.get("recipient") not in (None, "all"):
                    continue
                text = text_from_parts(message_parts(message.get("content")))
                rendered = rendered_by_channel.get(identity, "")
                if not text or text == rendered:
                    continue
                if on_event is not None:
                    on_event(
                        {
                            "type": "delta" if text.startswith(rendered) else "replace",
                            "text": text[len(rendered) :] if text.startswith(rendered) else text,
                            "channel": channel,
                            "message_id": identity,
                        }
                    )
                rendered_by_channel[identity] = text
        if not isinstance(resolved_id, str) or not resolved_id:
            raise ChatGPTWebError("ChatGPT did not return a conversation ID")
        return resolved_id, submitted_message_id

    def run_sentinel_node(
        self, arguments: list[str], payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        helper = script_path(Script.SIGN)
        environment = self.session.environment()
        try:
            result = subprocess.run(
                ["node", str(helper), *arguments],
                input=json.dumps(payload) if payload is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
                env=environment,
            )
        except FileNotFoundError as error:
            raise SentinelCompatibilityError(
                "Node.js 22 or newer is required for signing"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise SentinelCompatibilityError("Sentinel signing timed out") from error
        if result.returncode != 0:
            detail = result.stderr.strip() or "Sentinel signing failed"
            raise SentinelCompatibilityError(detail)
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise SentinelCompatibilityError("Sentinel signer returned invalid JSON") from error
        if not isinstance(value, dict):
            raise SentinelCompatibilityError("Sentinel signer returned an invalid result")
        return value

    def create_sentinel_headers(self) -> dict[str, str]:
        initial = self.run_sentinel_node(["--emit-initial"]).get("initial")
        if not isinstance(initial, str) or not initial:
            raise SentinelCompatibilityError("Sentinel signer did not generate an initial proof")
        requirements = self.request(
            "POST",
            urljoin(CHATGPT_ORIGIN, "/backend-api/sentinel/chat-requirements/prepare"),
            json={"p": initial},
        ).json()
        if not isinstance(requirements, dict):
            raise ChatGPTWebError("Sentinel prepare returned an invalid response")
        enforcement = self.run_sentinel_node(
            ["--requirements-stdin", "--emit-enforcement-json"], requirements
        )
        prepare_token = requirements.get("prepare_token")
        proof = enforcement.get("proof")
        turnstile = enforcement.get("turnstile")
        if not isinstance(prepare_token, str) or not isinstance(proof, str):
            raise SentinelCompatibilityError("Sentinel did not return required enforcement tokens")
        result = {
            "openai-sentinel-chat-requirements-prepare-token": prepare_token,
            "openai-sentinel-proof-token": proof,
        }
        if isinstance(turnstile, str) and turnstile:
            result["openai-sentinel-turnstile-token"] = turnstile
        return result

    def create_conversation_body(
        self,
        conversation: dict[str, Any] | None,
        prompt: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        parent = conversation.get("current_node") if conversation else None
        if not isinstance(parent, str) or not parent:
            parent = str(uuid.uuid4())
        now = datetime.now().astimezone()
        offset = now.utcoffset()
        offset_minutes = -int(offset.total_seconds() / 60) if offset else 0
        model = os.environ.get("CHATGPT_WEB_MODEL")
        if not model and conversation:
            candidate = conversation.get("default_model_slug")
            if isinstance(candidate, str) and candidate:
                model = candidate
        attachments = attachments or []
        content_parts: list[Any] = [prompt]
        attachment_metadata: list[dict[str, Any]] = []
        if attachments:
            content_parts = []
            for attachment in attachments:
                file_id = attachment.get("id")
                if not isinstance(file_id, str) or not file_id.startswith(("file-", "file_")):
                    raise ChatGPTWebError("invalid uploaded image ID")
                size = attachment.get("size_bytes")
                width = attachment.get("width")
                height = attachment.get("height")
                if not all(isinstance(value, int) and value > 0 for value in (size, width, height)):
                    raise ChatGPTWebError("invalid uploaded image dimensions")
                content_parts.append(
                    {
                        "content_type": "image_asset_pointer",
                        "asset_pointer": f"file-service://{file_id}",
                        "size_bytes": size,
                        "width": width,
                        "height": height,
                    }
                )
                attachment_metadata.append(
                    {
                        "id": file_id,
                        "name": attachment.get("name") or "clipboard.png",
                        "mime_type": attachment.get("mime_type") or "image/png",
                        "size": size,
                        "width": width,
                        "height": height,
                    }
                )
            content_parts.append(prompt)
        body = {
            "action": "next",
            "messages": [
                {
                    "id": str(uuid.uuid4()),
                    "author": {"role": "user"},
                    "create_time": time.time(),
                    "content": {
                        "content_type": "multimodal_text" if attachments else "text",
                        "parts": content_parts,
                    },
                    "metadata": {
                        "attachments": attachment_metadata,
                        "selected_sources": [],
                        "serialization_metadata": {"custom_symbol_offsets": []},
                        "submission_mode": "manual_send",
                    },
                }
            ],
            "parent_message_id": parent,
            "client_prepare_state": "sent",
            "timezone_offset_min": offset_minutes,
            "timezone": os.environ.get("TZ", "Europe/Lisbon"),
            "conversation_mode": {"kind": "primary_assistant"},
            "enable_message_followups": True,
            "system_hints": [],
            "supports_buffering": False,
            "supported_encodings": [],
            "paragen_cot_summary_display_override": "allow",
            "force_parallel_switch": "auto",
            "local_function_names": ["local.continue_in_work"],
        }
        if model:
            body["model"] = model
        if conversation:
            project_id = discovery.project_id(conversation)
            if project_id:
                body["gizmo_id"] = project_id
            conversation_id = conversation.get("conversation_id") or conversation.get("id")
            if isinstance(conversation_id, str) and conversation_id:
                body["conversation_id"] = conversation_id
        return body

    def upload_image(
        self,
        file_name: str,
        mime_type: str,
        data: bytes,
        width: int,
        height: int,
    ) -> dict[str, Any]:
        if mime_type != "image/png" or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ChatGPTWebError("only PNG clipboard images are currently supported")
        if not data or len(data) > 20 * 1024 * 1024:
            raise ChatGPTWebError("clipboard image must be smaller than 20 MiB")
        slot = self.request(
            "POST",
            urljoin(CHATGPT_ORIGIN, "/backend-api/files"),
            json={
                "file_name": file_name,
                "file_size": len(data),
                "use_case": "multimodal",
            },
        ).json()
        file_id = slot.get("file_id")
        upload_url = slot.get("upload_url")
        if not isinstance(file_id, str) or not isinstance(upload_url, str):
            raise ChatGPTWebError("ChatGPT returned an invalid image upload slot")
        upload = self.http.put(
            upload_url,
            content=data,
            headers={"content-type": mime_type, "x-ms-blob-type": "BlockBlob"},
            timeout=60,
        )
        upload.raise_for_status()
        finalized = self.request(
            "POST",
            urljoin(CHATGPT_ORIGIN, f"/backend-api/files/{file_id}/uploaded"),
            json={},
        ).json()
        if finalized.get("status") != "success":
            raise ChatGPTWebError("ChatGPT did not finalize the image upload")
        return {
            "id": file_id,
            "name": file_name,
            "mime_type": mime_type,
            "size_bytes": len(data),
            "width": width,
            "height": height,
        }

    def capture_send_recipe(
        self, conversation_id: str | None, prompt: str
    ) -> tuple[str, str, dict[str, str], dict[str, Any]]:
        helper = script_path(Script.CAPTURE)
        environment = self.session.environment()
        environment["CHATGPT_WEB_CDP_URL"] = self.cdp_url.rstrip("/")
        try:
            captured = subprocess.run(
                ["node", str(helper)],
                input=json.dumps({"conversation_id": conversation_id, "prompt": prompt}),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=45,
                check=False,
                env=environment,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            raise ChatGPTWebError("Chrome request preparation failed") from error
        if captured.returncode != 0:
            raise ChatGPTWebError(captured.stderr.strip() or "request preparation failed")
        try:
            recipe = json.loads(captured.stdout)
            url = recipe["url"]
            method = recipe["method"]
            body = json.loads(recipe["postData"])
            headers = {
                key.lower(): value
                for key, value in recipe["headers"].items()
                if key.lower() not in FORBIDDEN_REPLAY_HEADERS and not key.startswith(":")
            }
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ChatGPTWebError("Chrome returned an invalid send recipe") from error
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.netloc, parsed.path) != (
            "https",
            "chatgpt.com",
            "/backend-api/f/conversation",
        ) or method != "POST":
            raise ChatGPTWebError("Chrome returned an unexpected send destination")
        return method, url, headers, body
