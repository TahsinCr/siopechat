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
from .constants import CHATGPT_ORIGIN, DEFAULT_PAGE_SIZE
from .discovery import (
    model_catalog as parse_model_catalog,
)
from .discovery import (
    page as parse_page,
)
from .discovery import (
    project as parse_project,
)
from .discovery import (
    project_id as extract_project_id,
)
from .errors import ChatGPTWebError, SentinelCompatibilityError
from .formats import message_parts, text_from_parts
from .resources import Script, script_path
from .session import BrowserSession


def system_timezone():
    """Use an actual IANA identifier, omitting the optional field if unknown."""
    from pathlib import Path
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    candidates = [os.environ.get("TZ", "").removeprefix(":")]
    try:
        target = str(Path("/etc/localtime").resolve())
        if "/zoneinfo/" in target:
            candidates.append(target.split("/zoneinfo/", 1)[1])
        path = Path("/etc/timezone")
        if path.is_file():
            candidates.append(path.read_text().strip())
    except OSError:
        pass
    for value in candidates:
        try:
            if value:
                return ZoneInfo(value).key
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return None


def _sse_data(lines):
    """Yield Server-Sent Events data fields, including valid multiline payloads."""
    data = []
    for line in lines:
        if line == "":
            if data:
                yield "\n".join(data)
                data.clear()
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field == "data":
            if separator and value.startswith(" "):
                value = value[1:]
            if data:
                previous = "\n".join(data)
                try:
                    json.loads(previous)
                except json.JSONDecodeError:
                    pass
                else:
                    # Preserve providers that emit one complete JSON event per
                    # data line without the blank separator required by SSE.
                    yield previous
                    data.clear()
            data.append(value)
    if data:
        yield "\n".join(data)


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
            limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=30.0),
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

    def refresh_auth(self, prefer_browser=False):
        if prefer_browser:
            self.list_url, self.headers = self.session.capture_auth()
            self.session.save(self.list_url, self.headers)
            return
        try:
            self.list_url, self.headers = self.session.load()
        except ChatGPTWebError:
            raise ChatGPTWebError("Kayıtlı oturum yok; Hesaplar bölümünden doğrulayın") from None

    def refresh_session(self) -> None:
        headers = self.headers.copy()
        headers.pop("authorization", None)
        headers["accept-encoding"] = "gzip, deflate"
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

    def request(
        self, method: str, url: str, *, expect_json: bool = True, **kwargs: Any
    ) -> httpx.Response:
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
            if (
                response.status_code not in (401, 403)
                or attempt == 1
                or method.upper() not in ("GET", "HEAD")
            ):
                break
            try:
                self.refresh_session()
            except ChatGPTWebError:
                raise ChatGPTWebError(
                    "Oturum yenilenemedi; Hesaplar bölümünden tekrar doğrulayın"
                ) from None
        if response.status_code == 429:
            try:
                delay = float(response.headers.get("retry-after", ""))
            except ValueError:
                delay = None
            raise RateLimited(delay)
        if response.status_code == 404:
            from ..errors import RemoteNotFound

            raise RemoteNotFound("ChatGPT kaydı artık mevcut değil")
        if response.status_code >= 400:
            raise ChatGPTWebError(
                f"ChatGPT returned HTTP {response.status_code} for {method} {response.url.path}"
            )
        content_type = response.headers.get("content-type", "")
        if (
            not expect_json
            and response.status_code in (200, 201, 202, 204)
            and not response.content
        ):
            return response
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
        catalog = parse_model_catalog(raw)
        self._model_catalog = (time.monotonic(), catalog)
        return catalog

    def list_projects(self, cursor: str | None = None) -> dict[str, Any]:
        params = {"conversations_per_gizmo": 0, "limit": 20, "owned_only": "false"}
        if cursor is not None:
            params["cursor"] = cursor
        raw = self.request(
            "GET", urljoin(CHATGPT_ORIGIN, "/backend-api/gizmos/snorlax/sidebar"), params=params
        ).json()
        items, next_cursor = parse_page(raw)
        projects = [value for item in items if (value := parse_project(item)) is not None]
        return {"items": projects, "cursor": next_cursor}

    def create_project(self, name, instructions="", *, visibility="private", memory="default"):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("project name must contain 1–80 characters")
        if visibility != "private" or memory not in ("default", "project_only"):
            raise ValueError("unsupported project privacy or memory option")
        raw = self.request(
            "POST",
            urljoin(CHATGPT_ORIGIN, "/backend-api/projects"),
            json={
                "name": name.strip(),
                "instructions": instructions,
                "memory_scope": "project_v2" if memory == "project_only" else "global",
            },
        ).json()
        if raw.get("error") or not isinstance(raw.get("resource"), dict):
            raise ChatGPTWebError("Project creation response changed; request was not replayed")
        project = parse_project(raw["resource"])
        if project is None:
            raise ChatGPTWebError("Project creation returned no project")
        return self.get_project(project["id"])

    def get_project(self, project_id: str) -> dict[str, Any]:
        raw = self.request(
            "GET", urljoin(CHATGPT_ORIGIN, f"/backend-api/gizmos/{quote(project_id, safe='')}")
        ).json()
        value = parse_project(raw)
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
        items, next_cursor = parse_page(raw)
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
            conversations.extend(dict(item, project_id=extract_project_id(item)) for item in fresh)
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
            expect_json=False,
        )

    def delete_conversation(self, conversation_id: str) -> None:
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ValueError("conversation ID is required")
        response = self.request(
            "PATCH",
            urljoin(CHATGPT_ORIGIN, f"/backend-api/conversation/{quote(conversation_id, safe='')}"),
            json={"is_visible": False},
            expect_json=False,
        )
        if response.content and response.json().get("success") is False:
            raise ChatGPTWebError("ChatGPT sohbeti silmeyi reddetti")

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
        if extract_project_id(conversation) != project_id:
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
        action=None,
        message_id: str | None = None,
        branch=None,
    ) -> tuple[str, dict[str, Any], set[str]]:
        if type(temporary) is not bool or (
            project_id is not None
            and (not isinstance(project_id, str) or not project_id.startswith("g-p-"))
        ):
            raise ChatGPTWebError("invalid conversation options")
        if temporary and project_id:
            raise ChatGPTWebError("temporary conversations cannot belong to a project")
        previous_ids: set[str] = set()
        previous: dict[str, Any] | None = None
        if branch is not None:
            previous = {"current_node": branch.message_id}
        elif conversation_id:
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
            if previous and extract_project_id(previous) not in (None, project_id):
                raise ChatGPTWebError("conversation belongs to another project")
            previous = dict(previous or {}, project_id=project_id)
        catalog = self.model_catalog()
        model = (
            model
            or (previous.get("default_model_slug") if previous else None)
            or catalog.default_model
        )
        selected = next((item for item in catalog.models if item.id == model), None)
        if selected is None:
            raise ChatGPTWebError("Select a model from the current account catalog before sending")
        if effort is not None and effort not in {item.id for item in selected.efforts}:
            raise ChatGPTWebError(f"Effort '{effort}' is not offered for model '{model}'")

        resolved_id, submitted_message_id = self.send_prompt_direct(
            previous,
            prompt,
            attachments or [],
            on_event,
            model,
            effort,
            **({"temporary": True} if temporary else {}),
            **({"action": action, "message_id": message_id} if action else {}),
            **({"branch": branch} if branch else {}),
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
        if branch is not None:
            for item in messages:
                if item.get("id") == submitted_message_id:
                    break
                if item.get("id"):
                    previous_ids.add(item["id"])
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
        action=None,
        message_id: str | None = None,
        branch=None,
    ) -> tuple[str, str]:
        conversation_id = None
        if conversation:
            candidate = conversation.get("conversation_id") or conversation.get("id")
            if isinstance(candidate, str):
                conversation_id = candidate
        method = "POST"
        url = urljoin(CHATGPT_ORIGIN, "/backend-api/f/conversation")
        headers = self.headers.copy()
        body = self.create_conversation_body(conversation, prompt, attachments)
        if branch is not None:
            body.pop("conversation_id", None)
            conversation_id = None
            body.update(
                branching_from_conversation_id=branch.conversation_id,
                branching_from_message_id=branch.message_id,
                parent_message_id=branch.message_id,
            )
        submitted_message_id = body["messages"][0]["id"]
        if action:
            from ..api import MessageAction

            items = (conversation or {}).get("messages", [])
            target_index = next(
                (i for i, item in enumerate(items) if item.get("id") == message_id), None
            )
            if target_index is None or (conversation or {}).get("is_read_only"):
                raise ChatGPTWebError("The selected message is no longer editable")
            target = items[target_index]
            expected_role = "user" if action == MessageAction.EDIT else "assistant"
            if target.get("channel") == "analysis" or target.get("recipient") not in (None, "all"):
                raise ChatGPTWebError("Only visible messages can be changed")
            if (target.get("author") or {}).get("role") != expected_role:
                raise ChatGPTWebError("The message role does not match the requested action")
            if action == MessageAction.RETRY:
                user = next(
                    (
                        item
                        for item in reversed(items[:target_index])
                        if (item.get("author") or {}).get("role") == "user"
                    ),
                    None,
                )
                if user is None:
                    raise ChatGPTWebError("The original prompt is unavailable")
                body.update(action="variant", messages=[], parent_message_id=user["id"])
                submitted_message_id = user["id"]
            elif action == MessageAction.EDIT:
                if target_index == 0:
                    raise ChatGPTWebError("The original message parent is unavailable")
                body["parent_message_id"] = items[target_index - 1]["id"]
                original = target.get("content") or {}
                resource_parts = [
                    part for part in original.get("parts", []) if not isinstance(part, str)
                ]
                body["messages"][0]["content"] = {
                    "content_type": original.get("content_type") or "text",
                    "parts": resource_parts + [prompt],
                }
                body["messages"][0]["metadata"]["attachments"] = (target.get("metadata") or {}).get(
                    "attachments", []
                )
            else:
                raise ChatGPTWebError("Unsupported message action")
        # Apply the provider-owned choices to this request; never retain an
        # effort preference captured from a different model.
        if model:
            body["model"] = model
            # A captured recipe can carry the old model's effort preference.
            body.pop("thinking_effort", None)
        if effort:
            body["thinking_effort"] = effort

        if conversation and extract_project_id(conversation):
            body["gizmo_id"] = extract_project_id(conversation)
        body["history_and_training_disabled"] = temporary
        headers.pop("x-openai-web-sse-compression", None)
        headers["x-openai-target-path"] = "/backend-api/f/conversation"
        headers["x-openai-target-route"] = "/backend-api/f/conversation"
        body["supported_encodings"] = []
        body["supports_buffering"] = False
        if not isinstance(submitted_message_id, str) or not submitted_message_id:
            raise ChatGPTWebError("conversation request has no user message ID")
        headers.update(self.create_sentinel_headers())
        resolved_id: str | None = conversation_id
        rendered_by_channel = {}
        terminal_seen = False
        final_seen = False
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
            for payload in _sse_data(response.iter_lines()):
                if payload == "[DONE]":
                    terminal_seen = True
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    raise ChatGPTWebError(
                        "ChatGPT stream protocol changed; request not retried"
                    ) from None
                if not isinstance(event, dict):
                    continue
                if event.get("error"):
                    raise ChatGPTWebError("ChatGPT stream failed; request not retried")
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
                if channel != "analysis":
                    final_seen = True
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
        if not terminal_seen or not final_seen:
            raise ChatGPTWebError(
                "ChatGPT response incomplete; remote outcome unknown; request not retried"
            )
        if not isinstance(resolved_id, str) or not resolved_id:
            raise ChatGPTWebError("ChatGPT did not return a conversation ID")
        return resolved_id, submitted_message_id

    def run_sentinel_node(
        self, arguments: list[str], payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        helper = script_path(Script.SIGN)
        environment = {
            key: value
            for key, value in self.session.environment().items()
            if key
            in {
                "PATH",
                "HOME",
                "USERPROFILE",
                "SYSTEMROOT",
                "WINDIR",
                "TEMP",
                "TMP",
                "TMPDIR",
                "SIOPECHAT_CACHE_HOME",
                "CHATGPT_WEB_SENTINEL_CACHE_TTL_MS",
            }
        }
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
            raise SentinelCompatibilityError("ChatGPT web verification could not be prepared")
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
        zone = system_timezone()
        now = datetime.now().astimezone()
        offset = now.utcoffset()
        offset_minutes = -int(offset.total_seconds() / 60) if offset else 0
        model = None
        if conversation:
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
                if attachment.get("mime_type") != "image/png" and "width" not in attachment:
                    if type(size) is not int or size <= 0:
                        raise ChatGPTWebError("invalid file size")
                    attachment_metadata.append(
                        {
                            "id": file_id,
                            "name": attachment.get("name"),
                            "mime_type": attachment.get("mime_type"),
                            "size": size,
                        }
                    )
                    continue
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
                        "content_type": "multimodal_text"
                        if any(isinstance(part, dict) for part in content_parts)
                        else "text",
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
            **({"timezone": zone} if zone else {}),
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
            project_id = extract_project_id(conversation)
            if project_id:
                body["gizmo_id"] = project_id
            conversation_id = conversation.get("conversation_id") or conversation.get("id")
            if isinstance(conversation_id, str) and conversation_id:
                body["conversation_id"] = conversation_id
        return body

    def upload_file(
        self, file_name: str, mime_type: str, data, *, project_id=None, cancel_event=None
    ) -> dict[str, Any]:
        import os
        import time
        from pathlib import Path

        path = data if isinstance(data, Path) else None
        stream = path.open("rb") if path is not None else None
        try:
            before = os.fstat(stream.fileno()) if stream is not None else None
            size = before.st_size if before is not None else len(data)
            max_bytes = 512 * 1024 * 1024
            if mime_type.startswith("image/"):
                max_bytes = min(max_bytes, 20 * 1024 * 1024)
            if Path(file_name).suffix.lower() in {".csv", ".tsv", ".xls", ".xlsx"}:
                max_bytes = min(max_bytes, 50 * 1024 * 1024)
            if not 0 < size <= max_bytes:
                raise ChatGPTWebError("file exceeds the ChatGPT web upload size limit")

            deadline = time.monotonic() + max(60, min(3600, size / (512 * 1024)))

            def ensure_active():
                if cancel_event is not None and cancel_event.is_set():
                    raise ChatGPTWebError("file upload cancelled")
                if time.monotonic() >= deadline:
                    raise ChatGPTWebError("file upload timed out")

            ensure_active()
            if mime_type == "image/png" and project_id is None:
                if stream is not None:
                    data = stream.read()
                ensure_active()
                if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 24:
                    raise ChatGPTWebError("invalid PNG file")
                return self.upload_image(
                    file_name,
                    mime_type,
                    data,
                    int.from_bytes(data[16:20], "big"),
                    int.from_bytes(data[20:24], "big"),
                )

            ensure_active()
            slot = self.request(
                "POST",
                urljoin(CHATGPT_ORIGIN, "/backend-api/files"),
                json={
                    "file_name": file_name,
                    "file_size": size,
                    "use_case": "gizmo" if project_id else "ace_upload",
                    **({"gizmo_id": project_id, "mime_type": mime_type} if project_id else {}),
                },
            ).json()
            ensure_active()
            file_id, upload_url = slot.get("file_id"), slot.get("upload_url")
            if not isinstance(file_id, str) or not isinstance(upload_url, str):
                raise ChatGPTWebError("invalid file upload slot")
            headers = {"content-type": mime_type, "x-ms-blob-type": "BlockBlob"}
            if stream is not None:
                headers["content-length"] = str(size)

                def chunks():
                    while True:
                        ensure_active()
                        block = stream.read(256 * 1024)
                        if not block:
                            return
                        yield block

                content = chunks()
            else:
                content = data
            import httpx

            timeout = httpx.Timeout(connect=30, read=15, write=15, pool=30)
            response = self.http.put(upload_url, content=content, headers=headers, timeout=timeout)
            response.raise_for_status()
            ensure_active()
            if stream is not None:
                after = os.fstat(stream.fileno())

                def signature(value):
                    return (
                        value.st_dev,
                        value.st_ino,
                        value.st_size,
                        value.st_mtime_ns,
                        value.st_ctime_ns,
                    )

                if signature(before) != signature(after):
                    raise ChatGPTWebError("file changed while it was being uploaded")
            result = self.request(
                "POST",
                urljoin(CHATGPT_ORIGIN, f"/backend-api/files/{quote(file_id, safe='')}/uploaded"),
                json={
                    "gizmo_id": project_id,
                    "use_case": "gizmo",
                    "entry_surface": "project_sources",
                    "file_name": file_name,
                    "mime_type": mime_type,
                    "index_for_retrieval": True,
                }
                if project_id
                else {},
            ).json()
            if result.get("status") != "success":
                raise ChatGPTWebError("provider did not finalize the file upload")
            return {"id": file_id, "name": file_name, "mime_type": mime_type, "size_bytes": size}
        finally:
            if stream is not None:
                stream.close()

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
