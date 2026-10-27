"""Account-owned Gemini HTTPS session. No browser or third-party Gemini runtime."""

import asyncio
import json
import random
import re
import uuid

from ..errors import AccountPaused, ProviderError, RateLimited
from ..session import WebSession, read_session, save_session_async

ORIGIN = "https://gemini.google.com"
BASE = ORIGIN + "/_/BardChatUi/data/"
HEADERS = {"Origin": ORIGIN, "Referer": ORIGIN + "/", "X-Same-Domain": "1"}
GRAPHQL_PATH = "/_/BardChatUi/"
GRAPHQL_SCHEMA = "GEMINI_WEB_GRAPHQL"
RENAME_CONVERSATION_SIGNATURE = "2/AK6lUKXaLqlYDDOn9zA0qKcCFN+eXBsWI9SXBtcRr1o="


def nested(value, *path, default=None):
    for index in path:
        if not isinstance(value, list) or not isinstance(index, int) or not 0 <= index < len(value):
            return default
        value = value[index]
    return default if value is None else value


def rpc_parts(text, rpc):
    for line in text.splitlines():
        if not line.startswith("[["):
            continue
        for row in json.loads(line):
            if nested(row, 0) == "wrb.fr" and nested(row, 1) == rpc:
                body = nested(row, 2)
                if body is not None:
                    yield json.loads(body)


class GeminiSession:
    def __init__(self, credential_file, *, http_factory=None):
        self.credential_file = credential_file
        self.http_factory = http_factory
        self.http = None
        self.refresh_task = None
        self.language = "en"
        self.push_id = ""
        self.build = self.sid = self.token = ""
        self.rpc_path_prefix = ""
        self.request_id = random.randint(10000, 99999)
        self.session_id = str(uuid.uuid4()).upper()
        self.models = ()
        self._saved = None
        self.verified = False
        self.persistence_warning = None
        self._save_lock = asyncio.Lock()

    async def open(self):
        from curl_cffi.requests import AsyncSession

        cookies = read_session(self.credential_file)
        if not cookies.get("__Secure-1PSID"):
            raise AccountPaused("Gemini oturum çerezleri eksik")
        self.http = (self.http_factory or AsyncSession)(
            cookies=cookies, impersonate="chrome145", timeout=45
        )
        try:
            response = await self.http.get(ORIGIN + "/app", headers=HEADERS, allow_redirects=False)
            WebSession.check(response)
            fields = {}
            for name in ("SNlM0e", "cfb2h", "FdrFJe", "TuX5cc", "qKIAYe", "eptZe"):
                match = re.search(r'"' + name + r'"\s*:\s*"([^"\\]*)"', response.text)
                fields[name] = match.group(1) if match else ""
            self.token, self.build, self.sid = (fields[k] for k in ("SNlM0e", "cfb2h", "FdrFJe"))
            self.language = fields["TuX5cc"] or "en"
            self.push_id = fields["qKIAYe"]
            self.rpc_path_prefix = fields["eptZe"]
            if not self.token:
                raise AccountPaused("Gemini oturumu geçersiz; hesabı doğrulayın")
            self.verified = True
            await self.persist()
            self.refresh_task = asyncio.create_task(self.refresh(), name="gemini-session-refresh")
        except BaseException:
            await self.close()
            raise
        return self

    def params(self):
        current = self.request_id
        self.request_id += 100000
        return {
            "hl": self.language,
            "_reqid": current,
            "rt": "c",
            "bl": self.build,
            "f.sid": self.sid,
        }

    async def batch(self, payloads, source_path="/app"):
        params = self.params()
        params.update({"rpcids": ",".join(p.rpcid for p in payloads), "source-path": source_path})
        selector = [
            1,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            [4, 5, 6, 8],
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            self.session_id,
        ]
        response = await self.http.post(
            BASE + "batchexecute",
            params=params,
            headers={
                **HEADERS,
                "x-goog-ext-525001261-jspb": json.dumps(selector),
                "x-goog-ext-73010989-jspb": "[0]",
            },
            data={"at": self.token, "f.req": json.dumps([[p.serialize() for p in payloads]])},
            allow_redirects=False,
        )
        WebSession.check(response)
        await self.persist()
        return response

    def graphql_endpoints(self):
        prefix = self.rpc_path_prefix
        marker = GRAPHQL_PATH
        if not prefix.startswith("/") or marker not in prefix:
            raise ProviderError("Gemini sohbet adı değiştirme uç noktası kullanılamıyor")
        base = ORIGIN + prefix[: prefix.index(marker)] + marker + "graphql"
        return (
            f"{base}/schemas/{GRAPHQL_SCHEMA}/executeQuery",
            f"{base}/xsrfToken",
        )

    async def rename_conversation(self, conversation_id, title):
        if not isinstance(title, str):
            raise ValueError("Gemini chat title must be text")
        title = title.strip()
        if not conversation_id or not 1 <= len(title) <= 100:
            raise ValueError("Gemini chat title must contain 1–100 characters")
        if self.http is None or not self.token:
            raise AccountPaused("Gemini oturumu hazır değil; hesabı yeniden doğrulayın")

        endpoint, xsrf_endpoint = self.graphql_endpoints()
        headers = {
            **HEADERS,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Framework-XSRF-Token": self.token,
        }
        body = json.dumps(
            {
                "querySignature": RENAME_CONVERSATION_SIGNATURE,
                "variables": {"conversationId": conversation_id, "title": title},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        response = await self.http.post(
            endpoint,
            headers=headers,
            data=body,
            allow_redirects=False,
        )
        if response.status_code == 400 and self._has_xsrf_error(response):
            token_response = await self.http.get(
                xsrf_endpoint,
                headers={**HEADERS, "Accept": "text/plain"},
                allow_redirects=False,
            )
            WebSession.check(token_response)
            token = token_response.text.strip()
            if not token:
                raise ProviderError("Gemini oturum doğrulama belirteci yenilenemedi")
            self.token = token
            response = await self.http.post(
                endpoint,
                headers={**headers, "X-Framework-XSRF-Token": token},
                data=body,
                allow_redirects=False,
            )
        WebSession.check(response)
        result = self._decode_graphql(response.text)
        errors = result.get("errors")
        if errors:
            message = errors[0].get("message") if isinstance(errors[0], dict) else None
            raise ProviderError(message or "Gemini sohbeti yeniden adlandıramadı")
        data = result.get("data")
        update = data.get("updateConversation") if isinstance(data, dict) else None
        conversation = update.get("clientConversation") if isinstance(update, dict) else None
        if (
            not isinstance(conversation, dict)
            or conversation.get("conversationId") != conversation_id
            or conversation.get("title") != title
        ):
            raise ProviderError("Gemini sohbet adı sunucuda doğrulanamadı")
        await self.persist()

    @staticmethod
    def _has_xsrf_error(response):
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return False
        if not isinstance(payload, list):
            return False
        return any(
            isinstance(item, dict)
            and isinstance(item.get("error"), dict)
            and isinstance(item["error"].get("details"), list)
            and any(
                isinstance(detail, dict)
                and detail.get("@type") == "type.googleapis.com/fava.request.XsrfData"
                for detail in item["error"]["details"]
            )
            for item in payload
        )

    @staticmethod
    def _decode_graphql(text):
        payload = text.lstrip()
        if payload.startswith(")]}'"):
            payload = payload[4:].lstrip("\r\n")
        try:
            results = json.loads(payload)
        except (ValueError, TypeError) as error:
            raise ProviderError("Gemini sohbet adı yanıtı çözümlenemedi") from error
        if not isinstance(results, list):
            raise ProviderError("Gemini sohbet adı yanıt biçimi değişti")
        for result in results:
            if isinstance(result, dict) and isinstance(result.get("data"), dict):
                return result
            if isinstance(result, dict) and result.get("errors"):
                return result
        raise ProviderError("Gemini sohbet adı yanıtı eksik")

    async def discover_models(self):
        from ..models import Choice, Model
        from .rpc import RPCPayload

        response = await self.batch([RPCPayload("otAQ7b", "[]")])
        parts = list(rpc_parts(response.text, "otAQ7b"))
        if len(parts) != 1 or nested(parts[0], 14) == 1016:
            raise AccountPaused("Gemini hesabı doğrulanamadı; misafir moduna geçilmedi")
        body = parts[0]
        rows = nested(body, 15)
        if not isinstance(rows, list):
            raise ProviderError("Gemini model kataloğu sözleşmesi değişti")
        tiers, flags = nested(body, 16, default=[]), nested(body, 17, default=[])
        capacity, slot = (
            (1, 13)
            if 21 in tiers
            else (2, 13)
            if 22 in tiers
            else (4, 12)
            if 115 in flags
            else (3, 12)
            if 16 in tiers or 106 in flags
            else (2, 12)
            if 8 in tiers or 19 in flags
            else (1, 12)
        )
        models = []
        for row in rows:
            remote = nested(row, 0)
            if not isinstance(remote, str):
                continue
            category = nested(row, 1) or nested(row, 10) or remote
            title = nested(row, 11) or nested(row, 19) or category
            number = nested(row, 17)
            if not isinstance(number, int):
                number = nested(row, 9, default=1)
            models.append(
                Model(
                    "gemini-" + str(category).lower().replace(" ", "-"),
                    str(title),
                    (Choice("standard", "Standart"), Choice("extended", "Genişletilmiş")),
                    metadata={
                        "model_id": remote,
                        "capacity": capacity,
                        "capacity_slot": slot,
                        "number": number,
                        "description": nested(row, 12) or nested(row, 2),
                    },
                )
            )
        if not models:
            raise ProviderError("Gemini kullanılabilir model döndürmedi")
        return tuple(models)

    async def persist(self):
        async with self._save_lock:
            if self.http is None or not self.verified:
                return
            cookies = self.http.cookies.get_dict()
            if cookies != self._saved:
                try:
                    await save_session_async(self.credential_file, cookies)
                except (OSError, ValueError):
                    self.persistence_warning = "Gemini oturum kaydı güncellenemedi"
                    return
                self._saved = cookies
                self.persistence_warning = None

    async def refresh(self):
        interval = 600
        failures = 0
        while True:
            await asyncio.sleep(interval)
            try:
                response = await self.http.post(
                    "https://accounts.google.com/RotateCookies",
                    headers={
                        "Origin": "https://accounts.google.com",
                        "Content-Type": "application/json",
                    },
                    data='[000,"-0000000000000000000"]',
                    allow_redirects=False,
                )
                if 500 <= response.status_code < 600:
                    failures += 1
                    interval = min(15 * (2 ** min(failures - 1, 6)), 900)
                    continue
                WebSession.check(response)
                await self.persist()
                failures = 0
                interval = 600
            except asyncio.CancelledError:
                raise
            except AccountPaused:
                return
            except RateLimited as error:
                failures += 1
                interval = min(
                    max(error.retry_after or 15 * (2 ** min(failures - 1, 6)), 1),
                    900,
                )
            except OSError:
                failures += 1
                interval = min(15 * (2 ** min(failures - 1, 6)), 900)
            except ProviderError:
                return
            except Exception:
                return

    async def close(self):
        task, self.refresh_task = self.refresh_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        http = self.http
        if http is not None:
            try:
                await self.persist()
            finally:
                self.http = None
                self.verified = False
                await http.close()
