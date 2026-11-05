"""AccountsSessions account operations."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from webai_provider.cdp import DevToolsError, open_provider_page, read_provider_cookies
from webai_provider.errors import AccountPaused
from webai_provider.models import SessionState
from webai_provider.profiles import import_cookies
from webai_provider.providers.chatgpt import BrowserSession, ChatGPTProvider
from webai_provider.registry import PROVIDERS, create_provider
from webai_provider.session import read_session, save_session, session_exists

from utils.paths import ensure_runtime_dir


class AccountsSessions:
    def browser_connected(self, key):
        # A short local socket probe cannot block UI binding on an HTTP timeout.
        import socket
        from urllib.parse import urlsplit

        endpoint = urlsplit(self.session(key).cdp_url)
        try:
            with socket.create_connection((endpoint.hostname, endpoint.port or 80), timeout=0.03):
                return True
        except OSError:
            return False

    def _remember_verification(self, key, *, resume=False):
        self._verified.add(key)
        self._verification_required.discard(key)
        if resume and self._loop is not None and self._loop.is_running():

            def resume_job_account():
                try:
                    self.api.tasks.resume_account(*key.split("/", 1))
                except AccountPaused:
                    pass  # Verification must not bypass an active server cooldown.

            self._loop.call_soon_threadsafe(resume_job_account)
        try:
            self.repository.put(
                key, "verification", {"account_key": key, "verified_at": time.time()}
            )
        except OSError:
            # Persistence failure cannot invalidate an authenticated provider reply.
            pass

    def _forget_verification(self, key):
        self._verified.discard(key)
        self._verification_required.add(key)
        self.repository.put(
            key, "verification", {"account_key": key, "requires_verification": True}
        )
        transport = getattr(self._providers.get(key), "transport", None)
        if transport is not None:
            transport.authenticated = False

    def status(self, key):
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
        path = self.credentials(key)
        saved = session_exists(path)
        try:
            marker = self.repository.get(key, "verification")
        except (OSError, ValueError, AttributeError):
            marker = None
        requires_verification = key in self._verification_required or bool(
            marker and marker.get("requires_verification") is True
        )
        verified = not requires_verification and (
            key in self._verified
            or getattr(getattr(self._providers.get(key), "transport", None), "authenticated", False)
            is True
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

    def _verify_profile(self, key):
        # The worker resolves/decrypts a single default profile off the UI loop.
        with self._lock:
            if self._sending.get(key, 0) or key in self._session_changes:
                raise RuntimeError("Önce devam eden işlemi tamamlayın veya durdurun")
            self._session_changes.add(key)
        try:
            family, cookies = import_cookies(self.item(key)["provider"])

            async def verify_candidate():
                # Validate independently before replacing any working session.
                with tempfile.TemporaryDirectory(
                    prefix="siope-profile-", dir=ensure_runtime_dir(self.runtime_root)
                ) as folder:
                    path = Path(folder) / "session.json"
                    kind = self.item(key)["provider"]
                    if kind == "chatgpt":
                        session = BrowserSession(
                            replace(self.session(key).paths, session_file=path)
                        )
                        await asyncio.to_thread(session.import_cookies, cookies)
                        candidate = ChatGPTProvider(session, self.item(key)["id"])
                    else:
                        save_session(path, cookies)
                        candidate = create_provider(
                            kind,
                            self.item(key)["id"],
                            credential_file=path,
                        )
                    try:
                        state = await (
                            candidate.authenticate(capture_browser=False)
                            if kind == "chatgpt"
                            else candidate.authenticate()
                        )
                        if state.state != SessionState.AUTHENTICATED:
                            raise RuntimeError("Oturum doğrulanamadı")
                    finally:
                        await candidate.close()
                    value = read_session(path)
                    task = self._catalog_tasks.get(key)
                    if task:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    previous = self._providers.get(key)
                    if previous:
                        await previous.close()
                    # No background catalog may persist the old account snapshot.
                    with self._catalog_io_lock:
                        save_session(self.credentials(key), value)
                        self._providers.pop(key, None)
                        self._catalog_versions[key] = self._catalog_versions.get(key, 0) + 1
                        self._catalogs.pop(key, None)
                        self.repository.delete(key, "catalog")
                    self._remember_verification(key, resume=True)

            self.call(verify_candidate(), timeout=90)
            return dict(self.status(key), imported_family=family)
        finally:
            with self._lock:
                self._session_changes.discard(key)

    def session_action(self, key, action):
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
            # Browser stderr can contain Chromium diagnostics. Launch the
            # stdlib browser handler in a quiet child so it cannot corrupt the
            # terminal UI's alternate screen.
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
            session = self.session(key)
            url = PROVIDERS[item["provider"]][1]
            result = session.launch(url)
            if result.get("browser") == "already_running":
                try:
                    open_provider_page(session.cdp_url, url)
                except DevToolsError as error:
                    raise RuntimeError("Bağlı tarayıcıda giriş sayfası açılamadı") from error
                result["opened"] = True
            return result
        provider = self.provider(key)
        if action == "logout":
            with self._catalog_io_lock:
                self._catalog_versions[key] = self._catalog_versions.get(key, 0) + 1
                self._catalogs.pop(key, None)

            async def forget():
                task = self._catalog_tasks.get(key)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await provider.forget_session()

            self.call(forget())
            self._forget_verification(key)
            return dict(self.status(key), logged_out=True)
        if action == "verify":
            if item["provider"] in ("gemini", "claude", "grok"):
                session = self.session(key)
                provider_id = item["provider"]
                provider_url = PROVIDERS[provider_id][1]
                try:
                    browser_cookies = read_provider_cookies(session.cdp_url, provider_url)
                except DevToolsError as error:
                    raise RuntimeError(
                        "Hesabın açık giriş tarayıcısı bulunamadı; önce giriş yapın"
                    ) from error
                if provider_id == "grok":
                    from webai_provider.profiles import ProfileError, select_cookies

                    try:
                        cookies = select_cookies(browser_cookies, provider_id)
                    except ProfileError as error:
                        raise RuntimeError(str(error)) from error
                    required = None
                else:
                    host = urlsplit(provider_url).hostname or ""
                    allowed_claude_cookies = {
                        "sessionKey",
                        "lastActiveOrg",
                        "anthropic-device-id",
                        "__cf_bm",
                        "cf_clearance",
                    }
                    cookies = {}
                    for cookie in browser_cookies:
                        name = cookie.get("name")
                        value = cookie.get("value")
                        if not isinstance(name, str) or not name or not isinstance(value, str):
                            continue
                        domain = str(cookie.get("domain", "")).lstrip(".").lower()
                        applies_to_provider = host == domain or host.endswith(f".{domain}")
                        if applies_to_provider and (
                            provider_id == "gemini" or name in allowed_claude_cookies
                        ):
                            cookies[name] = value
                    required = "__Secure-1PSID" if provider_id == "gemini" else "sessionKey"
                if required and not cookies.get(required):
                    raise RuntimeError("Hesaba giriş tamamlanmamış")
                directory = self.directory(key)
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.call(provider.close())
                save_session(self.credentials(key), cookies)
            try:
                status = self.call(provider.authenticate())
            except AccountPaused:
                self._forget_verification(key)
                raise
            if status.state != SessionState.AUTHENTICATED:
                raise RuntimeError("Oturum doğrulanamadı")
            self._remember_verification(key, resume=True)
            return self.status(key)
        raise ValueError("unknown account operation")
