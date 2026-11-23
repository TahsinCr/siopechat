"""AccountsSessions account operations built on webaiprovider v2."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from webaiprovider import (
    AccountPaused,
    ChatGPTWebAIProvider,
    MemoryCredentialStore,
    ProfileError,
    WebAIAccount,
    WebAIProvider,
    WebAIProviderType,
)
from webaiprovider.accounts import (
    import_cookies,
    read_session,
    save_session,
    select_cookies,
    session_exists,
)
from webaiprovider.accounts.cdp import DevToolsError, is_devtools_available, read_provider_cookies
from webaiprovider.accounts.status import SessionState
from webaiprovider.providers.chatgpt.auth import BrowserSession

from .shared import PROVIDERS


def _find_chromium_executable() -> str | None:
    if sys.platform == "win32":
        candidates = (
            "chrome",
            "msedge",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        )
    elif sys.platform == "darwin":
        candidates = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "google-chrome",
            "chromium",
        )
    else:
        candidates = (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "brave-browser",
            "microsoft-edge",
        )
    for item in candidates:
        path = Path(item)
        if path.is_absolute() and path.is_file():
            return str(path)
        resolved = shutil.which(item)
        if resolved:
            return resolved
    return None


def _open_cdp_tab(cdp_url: str, url: str) -> None:
    parts = urlsplit(cdp_url)
    base = f"{parts.scheme or 'http'}://{parts.netloc}"
    endpoint = f"{base}/json/new?{urllib.parse.quote(url, safe='')}"
    request = urllib.request.Request(endpoint, method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            response.read()
        return
    except urllib.error.HTTPError:
        request = urllib.request.Request(endpoint, method="GET")
    except (OSError, ValueError) as error:
        raise DevToolsError("Bağlı tarayıcıda giriş sayfası açılamadı") from error
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            response.read()
    except (OSError, ValueError) as error:
        raise DevToolsError("Bağlı tarayıcıda giriş sayfası açılamadı") from error


def _launch_or_open_cdp_browser(cdp_url: str, profile_dir: Path, url: str) -> dict:
    if is_devtools_available(cdp_url, timeout=0.25):
        _open_cdp_tab(cdp_url, url)
        return {"browser": "already_running", "cdp_url": cdp_url, "opened": True}

    executable = _find_chromium_executable()
    if executable is None:
        raise RuntimeError(
            "Chrome veya Chromium bulunamadı; --remote-debugging-port ile başlatıp bağlanın"
        )
    parts = urlsplit(cdp_url)
    port = parts.port or 9222
    profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = [
        executable,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    options = {"creationflags": 0x208} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **options,
    )
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if is_devtools_available(cdp_url, timeout=0.2):
            return {"browser": "launched", "cdp_url": cdp_url, "opened": True}
        time.sleep(0.15)
    return {"browser": "launched", "cdp_url": cdp_url, "opened": True}


class AccountsSessions:
    def browser_connected(self, key: str) -> bool:
        """Fast local port probe so UI rendering never blocks on HTTP timeouts."""
        endpoint = urlsplit(self._cdp_url_for(key))
        try:
            with socket.create_connection((endpoint.hostname, endpoint.port or 80), timeout=0.03):
                return True
        except OSError:
            return False

    def _remember_verification(self, key: str, *, resume: bool = False) -> None:
        del resume
        self._verified.add(key)
        self._verification_required.discard(key)
        self._paused_accounts.discard(tuple(key.split("/", 1)))
        try:
            self.repository.put(
                key, "verification", {"account_key": key, "verified_at": time.time()}
            )
        except OSError:
            pass

    def _forget_verification(self, key: str) -> None:
        self._verified.discard(key)
        self._verification_required.add(key)
        self._paused_accounts.add(tuple(key.split("/", 1)))
        try:
            self.repository.put(
                key, "verification", {"account_key": key, "requires_verification": True}
            )
        except OSError:
            pass
        prov = self._providers.get(key)
        if prov is not None and prov.opened:
            try:
                prov.close()
            except Exception:
                pass

    def status(self, key: str):
        if not key:
            return {
                "saved_snapshot": False,
                "authenticated": False,
                "enabled": False,
                "account_enabled": False,
                "provider_enabled": False,
                "account": "",
                "provider": "",
                "transport": "http",
            }
        item = self.item(key)
        store = self.credentials(key)
        saved = session_exists(store)
        try:
            marker = self.repository.get(key, "verification")
        except (OSError, ValueError, AttributeError):
            marker = None
        requires_verification = key in self._verification_required or bool(
            marker and marker.get("requires_verification") is True
        )
        prov = self._providers.get(key)
        verified = not requires_verification and (
            key in self._verified
            or bool(prov is not None and prov.opened)
            or bool(marker and marker.get("account_key") == key)
        )
        return {
            "saved_snapshot": saved,
            "authenticated": saved and verified,
            "enabled": item["enabled"],
            "account_enabled": item["account_enabled"],
            "provider_enabled": item["provider_enabled"],
            "transport": "http",
            "account": item["label"],
            "provider": key.split("/")[0],
        }

    def _verify_profile(self, key: str):
        with self._lock:
            if self._sending.get(key, 0) or key in self._session_changes:
                raise RuntimeError("Önce devam eden işlemi tamamlayın veya durdurun")
            self._session_changes.add(key)
        try:
            item = self.item(key)
            provider_id = item["provider"]
            family, cookies = import_cookies(provider_id)

            temp_store = MemoryCredentialStore()
            if provider_id == "chatgpt":
                session = BrowserSession(temp_store, cdp_url=self._cdp_url_for(key))
                session.import_cookies(cookies)
                candidate_account = WebAIAccount(
                    id=item["id"],
                    provider=provider_id,
                    credentials=temp_store,
                    name=item["label"],
                )
                candidate = ChatGPTWebAIProvider(candidate_account, session=session)
            else:
                save_session(temp_store, cookies)
                candidate_account = WebAIAccount(
                    id=item["id"],
                    provider=provider_id,
                    credentials=temp_store,
                    name=item["label"],
                )
                candidate = WebAIProvider(
                    candidate_account, type=WebAIProviderType.parse(provider_id)
                )

            try:
                state = (
                    candidate.authenticate(capture_browser=False)
                    if provider_id == "chatgpt"
                    else candidate.authenticate()
                )
                if state.state != SessionState.AUTHENTICATED:
                    raise RuntimeError("Oturum doğrulanamadı")
            finally:
                candidate.close()

            value = read_session(temp_store)
            self._discard_provider(key)
            with self._catalog_io_lock:
                save_session(self.credentials(key), value)
                self._accounts.pop(key, None)
                self._catalog_versions[key] = self._catalog_versions.get(key, 0) + 1
                self._catalogs.pop(key, None)
                self.repository.delete(key, "catalog")
            self._remember_verification(key, resume=True)
            return dict(self.status(key), imported_family=family)
        finally:
            with self._lock:
                self._session_changes.discard(key)

    def session_action(self, key: str, action: str):
        item = self.item(key)
        if action == "status":
            return self.status(key)
        if action == "remove":
            return self.remove(key)
        if action == "verify_profile":
            return self._verify_profile(key)
        if action == "toggle_enabled":
            self.set_account_enabled(key, not item["account_enabled"])
            return self.status(key)
        if action == "default_browser":
            options = {"creationflags": 0x208} if os.name == "nt" else {"start_new_session": True}
            result = subprocess.run(
                [sys.executable, "-m", "webbrowser", "-t", PROVIDERS[item["provider"]][1]],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
                **options,
            )
            if result.returncode:
                raise RuntimeError("Tarayıcı açılamadı")
            return {"opened": True}
        if action == "connect_browser":
            return _launch_or_open_cdp_browser(
                self._cdp_url_for(key),
                self.directory(key) / "chrome-profile",
                PROVIDERS[item["provider"]][1],
            )
        if action == "logout":
            with self._catalog_io_lock:
                self._catalog_versions[key] = self._catalog_versions.get(key, 0) + 1
                self._catalogs.pop(key, None)
            prov = self.provider(key)
            try:
                status = prov.forget_session()
            finally:
                self._discard_provider(key)
            self._forget_verification(key)
            return dict(self.status(key), logged_out=True, session_state=status.state.value)
        if action == "verify":
            provider_id = item["provider"]
            if provider_id in ("gemini", "claude", "grok"):
                cdp_url = self._cdp_url_for(key)
                provider_url = PROVIDERS[provider_id][1]
                try:
                    browser_cookies = read_provider_cookies(cdp_url, provider_url)
                except DevToolsError as error:
                    raise RuntimeError(
                        "Hesabın açık giriş tarayıcısı bulunamadı; önce giriş yapın"
                    ) from error
                try:
                    cookies = select_cookies(browser_cookies, provider_id)
                except ProfileError as error:
                    raise RuntimeError(str(error)) from error
                self._discard_provider(key)
                save_session(self.credentials(key), cookies)
            prov = self.provider(key)
            try:
                state = (
                    prov.authenticate(capture_browser=True)
                    if provider_id == "chatgpt"
                    else prov.authenticate()
                )
            except AccountPaused:
                self._forget_verification(key)
                raise
            if state.state != SessionState.AUTHENTICATED:
                raise RuntimeError("Oturum doğrulanamadı")
            self._remember_verification(key, resume=True)
            return self.status(key)
        raise ValueError("unknown account operation")
