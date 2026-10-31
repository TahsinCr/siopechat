"""Application service for local IPC, sessions and conversation snapshots."""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from providers.chatgpt import BrowserSession, ChatGPTClient, SessionPaths
from providers.chatgpt.constants import DEFAULT_CDP_URL
from providers.chatgpt.errors import ChatGPTWebError
from providers.chatgpt.formats import normalize_conversation
from providers.errors import ProviderError
from utils.files import atomic_json_write
from utils.paths import application_root, runtime_dir, state_dir, user_data_dir
from utils.process import acquire_exclusive, detached_process_options, set_private_permissions

MAX_DAEMON_MESSAGE = 64 * 1024 * 1024


IPC_PROTOCOL = 6


class BackendService:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else application_root()
        self._daemon_start_lock = threading.Lock()

    def auth_store_path(self) -> Path:
        return (
            state_dir()
            if self.root.resolve() == application_root().resolve()
            else self.root / "data" / "session"
        ) / "session.json"

    def create_session(self, cdp_url: str = DEFAULT_CDP_URL) -> BrowserSession:
        from account_repository import AccountRepository
        from providers.session import read_session

        store = AccountRepository(self.root / "data" / "accounts.sqlite3").credentials(
            "chatgpt/default"
        )
        legacy = self.auth_store_path()
        if not store.exists() and legacy.is_file():
            store.save(read_session(legacy))
            legacy.unlink(missing_ok=True)
        return BrowserSession(
            SessionPaths(
                store,
                (
                    user_data_dir()
                    if self.root.resolve() == application_root().resolve()
                    else self.root / "data" / "session"
                )
                / "chrome-profile",
            ),
            cdp_url,
        )

    def create_client(self, cdp_url: str = DEFAULT_CDP_URL) -> ChatGPTClient:
        return ChatGPTClient(self.create_session(cdp_url))

    def conversation_cache_dir(self) -> Path:
        return (self.root / "data") / "conversations"

    def conversation_cache_path(self, conversation_id: str) -> Path:
        if not re.fullmatch("[A-Za-z0-9_-]+", conversation_id):
            raise ChatGPTWebError("invalid conversation ID")
        return self.conversation_cache_dir() / f"{conversation_id}.json"

    def active_conversation_path(self) -> Path:
        return self.conversation_cache_dir().parent / "active-conversation.json"

    def atomic_json_write(self, path: Path, value: dict[str, Any]) -> None:
        atomic_json_write(path, value, separators=(",", ":"), directory_mode=0o700)

    def set_active_conversation(self, conversation_id: str | None) -> None:
        if conversation_id is None:
            self.active_conversation_path().unlink(missing_ok=True)
            return
        self.conversation_cache_path(conversation_id)
        self.atomic_json_write(
            self.active_conversation_path(), {"conversation_id": conversation_id}
        )

    def cache_conversation(
        self, conversation: dict[str, Any], make_active: bool = True
    ) -> dict[str, Any]:
        raw_messages = conversation.get("messages")
        already_normalized = isinstance(raw_messages, list) and (
            not raw_messages
            or (
                isinstance(raw_messages[0], dict)
                and "role" in raw_messages[0]
                and ("author" not in raw_messages[0])
            )
        )
        normalized = (
            dict(conversation) if already_normalized else normalize_conversation(conversation)
        )
        if conversation.get("temporary"):
            return normalized
        conversation_id = normalized.get("id")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ChatGPTWebError("conversation has no ID")
        normalized["cached_at"] = time.time()
        self.atomic_json_write(self.conversation_cache_path(conversation_id), normalized)
        if make_active:
            self.set_active_conversation(conversation_id)
        return normalized

    def load_cached_conversation(self, conversation_id: str | None = None) -> dict[str, Any] | None:
        if conversation_id is None:
            conversation_id = self.active_conversation_id()
            if conversation_id is None:
                return None
        if self.conversation_cache_path(conversation_id).with_suffix(".deleted").is_file():
            return None
        try:
            value = json.loads(
                self.conversation_cache_path(conversation_id).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def active_conversation_id(self):
        """Read only the small active pointer, never its conversation body."""
        try:
            value = json.loads(self.active_conversation_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(value, dict):
            candidate = value.get("conversation_id")
            return candidate if isinstance(candidate, str) and candidate else None
        return None

    def mark_conversation_deleted(self, conversation_id):
        path = self.conversation_cache_path(conversation_id)
        self.atomic_json_write(path.with_suffix(".deleted"), {"deleted": True})
        path.unlink(missing_ok=True)
        try:
            active = json.loads(self.active_conversation_path().read_text())
            if active.get("conversation_id") == conversation_id:
                self.set_active_conversation(None)
        except (OSError, ValueError, AttributeError):
            pass

    def rename_cached_conversation(self, conversation_id: str, title: str) -> None:
        cached = self.load_cached_conversation(conversation_id)
        if cached is None:
            return
        cached["title"] = title
        self.atomic_json_write(self.conversation_cache_path(conversation_id), cached)

    def default_socket_path(self) -> Path:
        return runtime_dir() / "daemon.sock"

    def receive_json(
        self, connection: socket.socket, maximum: int | None = MAX_DAEMON_MESSAGE
    ) -> dict[str, Any]:
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if maximum is not None and size > maximum:
                raise ChatGPTWebError("daemon message exceeded the size limit")
        try:
            value = json.loads(b"".join(chunks))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ChatGPTWebError("daemon received invalid JSON") from error
        if not isinstance(value, dict):
            raise ChatGPTWebError("daemon message was not an object")
        return value

    def send_json(self, connection: socket.socket, value: dict[str, Any]) -> None:
        connection.sendall(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )

    def send_json_line(self, connection: socket.socket, value: dict[str, Any]) -> None:
        connection.sendall(
            (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        )

    def daemon_request(self, socket_path: Path, request: dict[str, Any]) -> dict[str, Any]:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(120)
        try:
            connection.connect(str(socket_path))
            self.send_json(connection, request)
            connection.shutdown(socket.SHUT_WR)
            response = self.receive_json(connection, maximum=64 * 1024 * 1024)
        finally:
            connection.close()
        if not response.get("ok"):
            raise ChatGPTWebError(response.get("error", "daemon request failed"))
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise ChatGPTWebError("daemon returned an invalid result")
        return result

    def daemon_stream_request(
        self, socket_path: Path, request: dict[str, Any], on_event: Any, on_connection: Any = None
    ) -> dict[str, Any]:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(330)
        result: dict[str, Any] | None = None
        try:
            connection.connect(str(socket_path))
            if on_connection is not None:
                on_connection(connection)
            self.send_json(connection, request)
            connection.shutdown(socket.SHUT_WR)
            with connection.makefile("rb") as stream:
                while line := stream.readline(64 * 1024 * 1024 + 1):
                    if len(line) > 64 * 1024 * 1024:
                        raise ChatGPTWebError("daemon stream exceeded the size limit")
                    try:
                        message = json.loads(line)
                    except (ValueError, UnicodeDecodeError) as error:
                        raise ChatGPTWebError("daemon stream returned invalid JSON") from error
                    if not isinstance(message, dict):
                        raise ChatGPTWebError("daemon stream returned an invalid message")
                    if not message.get("ok"):
                        raise ChatGPTWebError(message.get("error", "daemon request failed"))
                    if "event" in message:
                        on_event(message["event"])
                    elif "result" in message:
                        result = message["result"]
        finally:
            if on_connection is not None:
                on_connection(None)
            connection.close()
        if not isinstance(result, dict):
            raise ChatGPTWebError("daemon stream ended without a result")
        return result

    def daemon_is_ready(self, socket_path: Path) -> bool:
        try:
            return self.daemon_request(socket_path, {"command": "ping"}).get("ready") is True
        except (ProviderError, OSError):
            return False

    def start_daemon(self, socket_path: Path, cdp_url: str) -> None:
        socket_path.parent.mkdir(mode=448, parents=True, exist_ok=True)
        set_private_permissions(socket_path.parent, 448)
        command = [
            sys.executable,
            "-m",
            "bootstrap",
            "--cdp-url",
            cdp_url,
            "daemon",
            "--socket",
            str(socket_path),
        ]
        environment = os.environ.copy()
        package_root = str(Path(__file__).resolve().parent)
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, (package_root, environment.get("PYTHONPATH")))
        )
        process = subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **detached_process_options(),
        )
        deadline = time.monotonic() + 22
        while time.monotonic() < deadline:
            if self.daemon_is_ready(socket_path):
                return
            if process.poll() is not None:
                raise ChatGPTWebError(
                    f"daemon exited during startup (code {process.returncode}); run './siopechat daemon' in the foreground to see the cause"
                )
            time.sleep(0.1)
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        raise ChatGPTWebError("timed out starting the SiopeChat daemon")

    def ensure_daemon(self, socket_path: Path, cdp_url: str) -> None:
        with self._daemon_start_lock:
            try:
                state = self.daemon_request(socket_path, {"command": "ping"})
            except (ProviderError, OSError):
                state = {}
            if state.get("ready") is True:
                if state.get("protocol") == IPC_PROTOCOL:
                    return
                # Older persistent daemons cannot serve the discovery commands.
                self.daemon_request(socket_path, {"command": "stop"})
                deadline = time.monotonic() + 3
                while self.daemon_is_ready(socket_path):
                    if time.monotonic() >= deadline:
                        raise ChatGPTWebError("Old daemon is still stopping; retry shortly")
                    time.sleep(0.05)
            self.start_daemon(socket_path, cdp_url)

    def forget_session(self, socket_path: Path) -> dict[str, Any]:
        with self._daemon_start_lock:
            if self.daemon_is_ready(socket_path):
                self.daemon_request(socket_path, {"command": "stop"})
                deadline = time.monotonic() + 5
                while self.daemon_is_ready(socket_path):
                    if time.monotonic() >= deadline:
                        raise ChatGPTWebError(
                            "daemon is still stopping; session has not been removed"
                        )
                    time.sleep(0.05)
            self.auth_store_path().unlink(missing_ok=True)
        return {"logged_out": True, "browser_session_retained": True}

    @staticmethod
    def send_options(request):
        project_id = request.get("project_id")
        temporary = request.get("temporary", False)
        if type(temporary) is not bool or (
            project_id is not None
            and (not isinstance(project_id, str) or not project_id.startswith("g-p-"))
        ):
            raise ChatGPTWebError("invalid conversation options")
        if temporary and project_id:
            raise ChatGPTWebError("temporary conversations cannot belong to projects")
        return {
            **({"project_id": project_id} if project_id else {}),
            **({"temporary": True} if temporary else {}),
        }

    def handle_daemon_command(
        self, client: ChatGPTClient, request: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        command = request.get("command")
        if command == "forget_temporary":
            conversation_id = request.get("conversation_id")
            if not isinstance(conversation_id, str) or not conversation_id:
                raise ChatGPTWebError("invalid temporary conversation ID")
            return (
                {
                    "forgotten": client.temporary_conversations.pop(conversation_id, None)
                    is not None
                },
                False,
            )
        if command == "ping":
            return ({"ready": True, "protocol": IPC_PROTOCOL}, False)
        if command == "auth":
            if request.get("refresh"):
                client.refresh_auth(prefer_browser=True)
            response = client.verify()
            items = response.get("items", [])
            return (
                {
                    "authenticated": True,
                    "first_page_count": len(items) if isinstance(items, list) else None,
                },
                False,
            )
        if command == "status":
            return (
                {
                    "daemon": "running",
                    "session_saved": self.auth_store_path().is_file(),
                    "chrome": "running" if client.session.is_ready() else "stopped",
                },
                False,
            )
        if command == "list":
            maximum = request.get("maximum")
            if maximum is not None and (not isinstance(maximum, int) or maximum < 1):
                raise ChatGPTWebError("invalid conversation limit")
            return ({"items": client.list_conversations(maximum)}, False)
        if command == "history_page":
            offset = request.get("offset", 0)
            limit = request.get("limit", 50)
            if (
                type(offset) is not int
                or offset < 0
                or type(limit) is not int
                or not 1 <= limit <= 100
            ):
                raise ChatGPTWebError("invalid history page")
            raw = client.list_page(offset, limit)
            items = raw.get("items")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ChatGPTWebError("invalid history page response")
            from providers.chatgpt.discovery import project_id

            total = raw.get("total")
            next_offset = offset + limit if type(total) is int else offset + len(items)
            has_more = next_offset < total if type(total) is int else len(items) == limit
            return (
                {
                    "items": [dict(item, project_id=project_id(item)) for item in items],
                    "cursor": str(next_offset) if has_more else None,
                },
                False,
            )
        if command == "models":
            return (
                {"catalog": asdict(client.model_catalog(refresh=bool(request.get("refresh"))))},
                False,
            )
        if command == "projects":
            cursor = request.get("cursor")
            if cursor is not None and (not isinstance(cursor, str) or not cursor):
                raise ChatGPTWebError("invalid project cursor")
            return (client.list_projects(cursor), False)
        if command in ("project", "project_chats"):
            project_id = request.get("project_id")
            if not isinstance(project_id, str) or not project_id.startswith("g-p-"):
                raise ChatGPTWebError("project ID is required")
            if command == "project":
                return ({"project": client.get_project(project_id)}, False)
            cursor = request.get("cursor")
            if cursor is not None and (not isinstance(cursor, str) or not cursor):
                raise ChatGPTWebError("invalid project chat cursor")
            return (client.list_project_chats(project_id, cursor), False)
        if command == "show":
            conversation_id = request.get("conversation_id")
            if not isinstance(conversation_id, str) or not conversation_id:
                raise ChatGPTWebError("conversation ID is required")
            return ({"conversation": client.get_conversation(conversation_id)}, False)
        if command == "show_normalized":
            conversation_id = request.get("conversation_id")
            if not isinstance(conversation_id, str) or not conversation_id:
                raise ChatGPTWebError("conversation ID is required")
            conversation = client.get_conversation(conversation_id)
            return ({"conversation": self.cache_conversation(conversation)}, False)
        if command == "move_chat":
            conversation_id = request.get("conversation_id")
            project_id = request.get("project_id")
            if not isinstance(conversation_id, str) or not conversation_id:
                raise ChatGPTWebError("conversation ID is required")
            if project_id is not None and (
                not isinstance(project_id, str) or not project_id.startswith("g-p-")
            ):
                raise ChatGPTWebError("invalid project ID")
            conversation = client.move_conversation(conversation_id, project_id)
            return (
                {
                    "conversation": self.cache_conversation(
                        normalize_conversation(conversation), make_active=False
                    )
                },
                False,
            )
        if command == "rename":
            conversation_id = request.get("conversation_id")
            title = request.get("title")
            if not isinstance(conversation_id, str) or not conversation_id:
                raise ChatGPTWebError("conversation ID is required")
            if not isinstance(title, str) or not title.strip():
                raise ChatGPTWebError("conversation title is required")
            title = title.strip()
            if len(title) > 256:
                raise ChatGPTWebError("conversation title must be 256 characters or fewer")
            client.rename_conversation(conversation_id, title)
            self.rename_cached_conversation(conversation_id, title)
            return ({"conversation_id": conversation_id, "title": title}, False)
        if command == "send":
            conversation_id = request.get("conversation_id")
            prompt = request.get("prompt")
            attachments = request.get("attachments", [])
            if conversation_id is not None and (not isinstance(conversation_id, str)):
                raise ChatGPTWebError("invalid conversation ID")
            if not isinstance(prompt, str) or (not prompt.strip() and (not attachments)):
                raise ChatGPTWebError("prompt or attachment is required")
            if not isinstance(attachments, list):
                raise ChatGPTWebError("invalid attachment list")
            resolved_id, conversation, previous_ids = client.send_prompt(
                conversation_id, prompt, attachments, **self.send_options(request)
            )
            if request.get("temporary"):
                conversation = dict(conversation, temporary=True)
            self.cache_conversation(conversation)
            return (
                {
                    "conversation_id": resolved_id,
                    "conversation": conversation,
                    "previous_ids": sorted(previous_ids),
                },
                False,
            )
        if command == "upload_image":
            encoded = request.get("data")
            file_name = request.get("file_name")
            mime_type = request.get("mime_type")
            width = request.get("width")
            height = request.get("height")
            if not isinstance(encoded, str) or not isinstance(file_name, str):
                raise ChatGPTWebError("invalid image upload request")
            if (
                mime_type != "image/png"
                or not isinstance(width, int)
                or (not isinstance(height, int))
            ):
                raise ChatGPTWebError("invalid image upload metadata")
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError as error:
                raise ChatGPTWebError("invalid image upload data") from error
            return (
                {"attachment": client.upload_image(file_name, mime_type, data, width, height)},
                False,
            )
        if command == "stop":
            return ({"stopping": True}, True)
        raise ChatGPTWebError(f"unknown daemon command: {command}")

    def _daemon_response(self, client, connection, request):
        if request.get("command") == "send_stream":
            conversation_id = request.get("conversation_id")
            prompt = request.get("prompt")
            attachments = request.get("attachments", [])
            model = request.get("model")
            effort = request.get("effort")
            if conversation_id is not None and (not isinstance(conversation_id, str)):
                raise ChatGPTWebError("invalid conversation ID")
            if not isinstance(prompt, str) or (not prompt.strip() and (not attachments)):
                raise ChatGPTWebError("prompt or attachment is required")
            if not isinstance(attachments, list):
                raise ChatGPTWebError("invalid attachment list")
            if model is not None and (not isinstance(model, str) or not model or len(model) > 128):
                raise ChatGPTWebError("invalid model identifier")
            if effort is not None and (not isinstance(effort, str) or not effort):
                raise ChatGPTWebError("invalid effort choice")
            resolved_id, conversation, previous_ids = client.send_prompt(
                conversation_id,
                prompt,
                attachments,
                lambda event: self.send_json_line(connection, {"ok": True, "event": event}),
                model,
                effort,
                **self.send_options(request),
            )
            if request.get("temporary"):
                conversation = dict(conversation, temporary=True)
            self.cache_conversation(conversation)
            self.send_json_line(
                connection,
                {
                    "ok": True,
                    "result": {
                        "conversation_id": resolved_id,
                        "conversation": conversation,
                        "previous_ids": sorted(previous_ids),
                    },
                },
            )
            return False
        result, stop_after_response = self.handle_daemon_command(client, request)
        self.send_json(connection, {"ok": True, "result": result})
        return stop_after_response

    def run_daemon(self, socket_path: Path, cdp_url: str) -> int:
        socket_path.parent.mkdir(mode=448, parents=True, exist_ok=True)
        set_private_permissions(socket_path.parent, 448)
        lock_path = socket_path.with_suffix(".lock")
        lock = open(lock_path, "a+", encoding="utf-8")
        try:
            acquire_exclusive(lock)
        except BlockingIOError:
            lock.close()
            return 0
        if socket_path.exists():
            socket_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stopping = False

        def request_stop(_signum: int, _frame: object) -> None:
            nonlocal stopping
            stopping = True
            server.close()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        from concurrent.futures import ThreadPoolExecutor
        from contextlib import nullcontext

        capacity = threading.BoundedSemaphore(8)
        mutations = threading.Lock()
        from collections import OrderedDict

        temporary_conversations = OrderedDict()

        def handle(connection):
            nonlocal stopping
            try:
                with connection:
                    connection.settimeout(10)
                    try:
                        request = self.receive_json(connection)
                        if not isinstance(request, dict) or not isinstance(
                            request.get("command"), str
                        ):
                            raise ValueError("invalid request")
                        command = request["command"]
                        if command in ("ping", "stop"):
                            result, stop = self.handle_daemon_command(None, request)
                            self.send_json(connection, {"ok": True, "result": result})
                            if stop:
                                stopping = True
                            return
                        gate = (
                            mutations
                            if command
                            not in {
                                "list",
                                "history_page",
                                "projects",
                                "project",
                                "project_chats",
                                "load",
                                "models",
                                "status",
                            }
                            else nullcontext()
                        )
                        with gate, self.create_client(cdp_url) as client:
                            client.temporary_conversations = temporary_conversations
                            if self._daemon_response(client, connection, request):
                                stopping = True
                    except Exception:
                        try:
                            self.send_json_line(
                                connection, {"ok": False, "error": "daemon_request_failed"}
                            )
                        except OSError:
                            pass
            finally:
                capacity.release()

        try:
            server.bind(str(socket_path))
            set_private_permissions(socket_path, 384)
            server.listen(8)
            server.settimeout(0.5)
            with ThreadPoolExecutor(max_workers=8, thread_name_prefix="siopechat-ipc") as workers:
                while not stopping:
                    try:
                        connection, _ = server.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        if stopping:
                            break
                        raise
                    if not capacity.acquire(blocking=False):
                        connection.close()
                        continue
                    workers.submit(handle, connection)
        finally:
            server.close()
            if socket_path.exists():
                socket_path.unlink()
            lock.close()
        return 0
