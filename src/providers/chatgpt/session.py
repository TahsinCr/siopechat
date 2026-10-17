#!/usr/bin/env python3
"""Browser authentication, Chrome lifecycle and caller-owned session snapshots."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .constants import DEFAULT_CDP_URL, FORBIDDEN_REPLAY_HEADERS
from .errors import ChatGPTWebError
from .resources import Script, script_path


@dataclass(frozen=True)
class SessionPaths:
    session_file: Path
    cache: Path
    profile: Path


def _private(path: Path, mode: int) -> None:
    if os.name != "nt":
        os.chmod(path, mode)


def _detach() -> dict[str, int | bool]:
    return {"creationflags": 0x208} if os.name == "nt" else {"start_new_session": True}


def find_chrome_executable() -> str | None:
    if os.name == "nt":
        roots = (
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")),
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")),
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")),
        )
        candidates = [root / "Google" / "Chrome" / "Application" / "chrome.exe" for root in roots]
        candidates.extend(root / "Chromium" / "Application" / "chrome.exe" for root in roots)
        return next(
            (str(path) for path in candidates if path.is_file()), shutil.which("chrome.exe")
        )
    if sys.platform == "darwin":
        candidates = (
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        )
        return next(
            (str(path) for path in candidates if path.is_file()), shutil.which("google-chrome")
        )
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        executable = shutil.which(name)
        if executable:
            return executable
    return None


class BrowserSession:
    """Browser authentication with caller-supplied paths; no application state."""

    def __init__(self, paths: SessionPaths, cdp_url: str = DEFAULT_CDP_URL):
        self.paths = paths
        self.cdp_url = cdp_url

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["SIOPECHAT_CACHE_HOME"] = str(self.paths.cache)
        return environment

    def save(self, url: str, headers: dict[str, str]) -> None:
        path = self.paths.session_file
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private(path.parent, 0o700)
        temporary = path.with_suffix(f".tmp-{os.getpid()}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "url": url, "headers": headers}, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _private(path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def load(self) -> tuple[str, dict[str, str]]:
        path = self.paths.session_file
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ChatGPTWebError("saved ChatGPT session exceeds the size limit")
            value = json.loads(path.read_text(encoding="utf-8"))
            url = value["url"]
            headers = value["headers"]
        except FileNotFoundError as error:
            raise ChatGPTWebError("no saved session; run siopechat login") from error
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
            raise ChatGPTWebError("saved ChatGPT session is invalid") from error
        if not isinstance(url, str):
            raise ChatGPTWebError("saved ChatGPT session has an invalid origin")
        try:
            parsed = urlsplit(url)
        except ValueError as error:
            raise ChatGPTWebError("saved ChatGPT session has an invalid origin") from error
        if parsed.scheme != "https" or parsed.netloc != "chatgpt.com":
            raise ChatGPTWebError("saved ChatGPT session has an invalid origin")
        if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in headers.items()
        ):
            raise ChatGPTWebError("saved ChatGPT session has invalid headers")
        return url, {key.lower(): value for key, value in headers.items()}

    def capture_auth(self) -> tuple[str, dict[str, str]]:
        helper = script_path(Script.AUTH)
        environment = self.environment()
        environment["CHATGPT_WEB_CDP_URL"] = self.cdp_url.rstrip("/")
        try:
            result = subprocess.run(
                ["node", str(helper)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
                check=False,
                env=environment,
            )
        except FileNotFoundError as error:
            raise ChatGPTWebError(
                "Node.js 22 or newer is required for Chrome authentication"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise ChatGPTWebError("Chrome authentication capture timed out") from error

        if result.returncode != 0:
            detail = result.stderr.strip() or "Chrome authentication capture failed"
            raise ChatGPTWebError(detail)
        try:
            captured = json.loads(result.stdout)
            url = captured["url"]
            headers = {
                key.lower(): value
                for key, value in captured["headers"].items()
                if key.lower() not in FORBIDDEN_REPLAY_HEADERS
            }
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ChatGPTWebError("Chrome returned an invalid authentication snapshot") from error
        return url, headers

    def is_ready(self) -> bool:
        try:
            response = httpx.get(f"{self.cdp_url.rstrip('/')}/json/version", timeout=1.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def launch(self, url: str = "https://chatgpt.com/") -> dict[str, Any]:
        if url not in ("https://chatgpt.com/", "https://gemini.google.com/"):
            raise ValueError("unsupported login origin")
        if self.is_ready():
            return {"browser": "already_running", "cdp_url": self.cdp_url}
        parsed = urlsplit(self.cdp_url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ChatGPTWebError("automatic browser launch requires a local HTTP CDP URL")
        port = parsed.port or 9222
        browser = find_chrome_executable()
        if not browser:
            raise ChatGPTWebError("Chrome or Chromium was not found")
        profile = self.paths.profile
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private(profile, 0o700)
        subprocess.Popen(
            [
                browser,
                f"--user-data-dir={profile}",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}",
                "--no-first-run",
                "--no-default-browser-check",
                "--new-window",
                url,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **_detach(),
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.is_ready():
                return {
                    "browser": "started",
                    "cdp_url": self.cdp_url,
                    "profile": str(profile),
                    "login_required": True,
                }
            time.sleep(0.1)
        raise ChatGPTWebError("Chrome started but its debugger did not become ready")

    def open_login(self) -> dict[str, Any]:
        opened = webbrowser.open("https://chatgpt.com/", new=2)
        if not opened:
            raise ChatGPTWebError("default browser could not be opened")
        return {
            "browser": "default",
            "opened": True,
            "authenticated": False,
            "reason": "browser_login_does_not_import_the_application_session",
        }

    def cache_status(self) -> dict[str, Any] | None:
        manifest = self.paths.cache / "sentinel" / "current.json"
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        digest = value.get("sha256")
        implementation = value.get("implementation")
        checked_at = value.get("checked_at")
        if not isinstance(digest, str) or not isinstance(implementation, str):
            return None
        return {
            "implementation": implementation,
            "sha256": digest,
            "checked_at": checked_at if isinstance(checked_at, int) else None,
        }
