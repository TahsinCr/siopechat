"""Owned-account SDK bootstrap: no global cookie discovery or guest fallback."""

import asyncio
import random
import uuid

from ..errors import AccountPaused, ProviderError
from ..session import WebSession, read_session, save_session


def owned_client_type(base, credential_file):
    class OwnedClient(base):
        async def init(
            self, timeout=45, auto_close=False, auto_refresh=True, verbose=False, **options
        ):
            from curl_cffi.requests import AsyncSession
            from gemini_webapi.constants import BROWSER_TYPE, AccountStatus, Endpoint, Headers
            from gemini_webapi.utils.get_access_token import _extract_payload

            async with self._lock:
                if self._running:
                    return
                self._siope_verified = False
                self._siope_save_lock = asyncio.Lock()
                values = read_session(credential_file)
                if not values.get("__Secure-1PSID"):
                    raise AccountPaused("Gemini oturumu eksik; hesabı doğrulayın")
                self.cookies = values
                self.verbose, self.impersonate = verbose, options.get("impersonate", BROWSER_TYPE)
                self.timeout, self.auto_close, self.auto_refresh = timeout, auto_close, auto_refresh
                self.refresh_interval = options.get("refresh_interval", 600)
                self.client = AsyncSession(
                    cookies=self.cookies,
                    impersonate=self.impersonate,
                    timeout=timeout,
                    proxy=self.proxy,
                    **self.kwargs,
                )
                try:
                    response = await self.client.get(Endpoint.INIT, headers=Headers.GEMINI.value)
                    WebSession.check(response)
                    payload = _extract_payload(response)
                    if not payload or not payload[0]:
                        raise AccountPaused("Gemini oturumu sona ermiş; hesabı doğrulayın")
                    self.access_token, self.build_label, self.session_id, language, push_id = (
                        payload
                    )
                    self.language, self.push_id = language or self.language, push_id or self.push_id
                    self._running, self._cookie_source = True, "SiopeChat account"
                    self._sessionid, self._reqid = (
                        str(uuid.uuid4()).upper(),
                        random.randint(10000, 99999),
                    )
                    await self._init_rpc()
                    if not self._check_account_status():
                        if self.account_status == AccountStatus.UNAUTHENTICATED:
                            raise AccountPaused(
                                "Gemini oturumu doğrulanamadı; misafir moduna geçilmedi"
                            )
                        raise ProviderError(
                            "Gemini hesabı şu anda kullanılamıyor; oturum kaydı korundu"
                        )
                    self._siope_verified = True
                    await self._persist_owned_cookies()
                    if auto_refresh:
                        self.refresh_task = asyncio.create_task(self._refresh_owned_session())
                except BaseException:
                    await self.close()
                    raise

        async def _persist_owned_cookies(self):
            if not getattr(self, "_siope_verified", False) or self.client is None:
                return
            async with self._siope_save_lock:
                values = self.client.cookies.get_dict()
                if values != getattr(self, "_siope_saved", None):
                    try:
                        await asyncio.to_thread(save_session, credential_file, values)
                        self._siope_saved = values
                    except OSError:
                        self._siope_session_warning = "Oturum kaydı güncellenemedi"

        async def _batch_execute(self, *args, **kwargs):
            kwargs.setdefault("current_retry", 0)
            response = await super()._batch_execute(*args, **kwargs)
            await self._persist_owned_cookies()
            return response

        async def _refresh_owned_session(self):
            from gemini_webapi.constants import Endpoint, Headers

            while self._running:
                await asyncio.sleep(max(60, self.refresh_interval))
                if not self._running or self.client is None:
                    return
                try:
                    response = await self.client.post(
                        Endpoint.ROTATE_COOKIES,
                        headers=Headers.ROTATE_COOKIES.value,
                        data='[000,"-0000000000000000000"]',
                    )
                    WebSession.check(response)
                    await self._persist_owned_cookies()
                except asyncio.CancelledError:
                    raise
                except (AccountPaused, ProviderError):
                    self._siope_session_warning = "Gemini oturumu yenilenemedi"
                    return
                except Exception:
                    self._siope_session_warning = "Gemini oturum yenileme bağlantısı kesildi"

        async def close(self, delay=0):
            self._running = False
            for name in ("close_task", "refresh_task", "activity_task"):
                task = getattr(self, name, None)
                if task is not None and task is not asyncio.current_task():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                setattr(self, name, None)
            client = self.client
            if client is not None:
                try:
                    await self._persist_owned_cookies()
                    self._cookies.update(client.cookies)
                finally:
                    self.client = None
                    await client.close()

    return OwnedClient
