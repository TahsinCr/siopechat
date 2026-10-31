"""Bounded Gemini generation protocol and streaming disk uploads."""

import asyncio
import codecs
import json
import mimetypes
import re
import uuid
from pathlib import Path

from ..api import MessageAction
from ..session import WebSession
from .errors import GeminiCompatibilityError, GeminiError, GeminiStreamError
from .media import candidate_files
from .session import BASE, HEADERS, nested


class Frames:
    """Decode Gemini's length-prefixed JSPB stream incrementally."""

    _MAX_FRAME_UNITS = 8 * 1024 * 1024
    _LENGTH_MARKER = re.compile(r"(\d+)\n")
    _ANTI_XSSI = ")]}'"

    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.pending = ""
        self.expected_units = None
        self.payload_start = 0
        self.scanned_chars = 0
        self.scanned_units = 0
        self.prefix_checked = False

    def feed(self, data, final=False):
        try:
            self.pending += self.decoder.decode(data, final=final)
        except UnicodeDecodeError as error:
            raise GeminiStreamError("Gemini akışı geçersiz UTF-8 içeriyor") from error

        self._strip_prefix()
        while self.expected_units is not None or self._read_length_marker():
            self._scan_payload()
            if self.scanned_units < self.expected_units:
                break

            end = self.payload_start + self.scanned_chars
            payload, self.pending = self.pending[self.payload_start : end], self.pending[end:]
            self._reset_frame()
            try:
                rows = json.loads(payload)
            except ValueError as error:
                raise GeminiStreamError("Gemini akış biçimi değişti") from error
            if isinstance(rows, list):
                yield from rows
            else:
                yield rows

        if len(self.pending) > self._MAX_FRAME_UNITS + 16:
            raise GeminiStreamError("Gemini akış çerçevesi boyut sınırını aştı")
        if final and self.pending.strip():
            raise GeminiStreamError("Gemini akışı tamamlanmamış bir çerçeveyle kesildi")

    def _strip_prefix(self):
        if self.prefix_checked:
            return
        if len(self.pending) < len(self._ANTI_XSSI) and self._ANTI_XSSI.startswith(self.pending):
            return
        if self.pending.startswith(self._ANTI_XSSI):
            self.pending = self.pending[len(self._ANTI_XSSI) :].lstrip()
        self.prefix_checked = True

    def _read_length_marker(self):
        if not self.prefix_checked:
            return False
        self.pending = self.pending.lstrip()
        if not self.pending:
            return False
        match = self._LENGTH_MARKER.match(self.pending)
        if match is None:
            if self.pending.isdecimal() and len(self.pending) <= 10:
                return False
            if self.pending[0].isdigit() and "\n" not in self.pending:
                raise GeminiStreamError("Gemini akış çerçevesinin uzunluğu geçersiz")
            raise GeminiStreamError("Gemini akış çerçevesinin uzunluk alanı geçersiz")

        length = int(match.group(1))
        if not 1 <= length <= self._MAX_FRAME_UNITS:
            raise GeminiStreamError("Gemini akış çerçevesi boyut sınırını aştı")
        self.expected_units = length
        # Google counts the newline after the length marker in the UTF-16 size.
        self.payload_start = len(match.group(1))
        self.scanned_chars = 0
        self.scanned_units = 0
        return True

    def _scan_payload(self):
        index = self.payload_start + self.scanned_chars
        while self.scanned_units < self.expected_units and index < len(self.pending):
            units = 2 if ord(self.pending[index]) > 0xFFFF else 1
            if self.scanned_units + units > self.expected_units:
                raise GeminiStreamError("Gemini akış çerçevesinin UTF-16 uzunluğu geçersiz")
            self.scanned_chars += 1
            self.scanned_units += units
            index += 1

    def _reset_frame(self):
        self.expected_units = None
        self.payload_start = 0
        self.scanned_chars = 0
        self.scanned_units = 0


async def upload_files(session, paths):
    from curl_cffi import CurlMime

    files = []
    for value in paths:
        path = Path(value)
        mime = CurlMime()
        try:
            mime.addpart(
                name="file",
                content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                filename=path.name,
                local_path=str(path),
            )
            response = await session.http.post(
                "https://content-push.googleapis.com/upload",
                headers={
                    "Origin": "https://gemini.google.com",
                    "Referer": "https://gemini.google.com/",
                    "X-Tenant-Id": "bard-storage",
                    "Push-ID": session.push_id,
                },
                multipart=mime,
                allow_redirects=False,
            )
            WebSession.check(response)
            files.append([[response.text], path.name])
        finally:
            mime.close()
    return files


def generation_request(session, request, metadata, files, notebook=None):
    selected = (
        next((m for m in session.models if request.model in (m.id, m.metadata["model_id"])), None)
        if request.model
        else next(iter(session.models), None)
    )
    if selected is None:
        raise ValueError("Model bu Gemini hesabında kullanılamıyor")
    effort = request.effort or selected.default_effort
    if effort is not None and effort not in {choice.id for choice in selected.efforts}:
        raise ValueError("Efor bu Gemini modelinde kullanılamıyor")
    thinking = selected.metadata["effort_values"].get(effort)
    body = [None] * 81
    body[0], body[1], body[2] = (
        [request.prompt, 0, None, files or None, None, None, 0],
        [session.language],
        metadata,
    )
    for index, value in {
        6: [1],
        7: 1,
        10: 1,
        11: 0,
        17: [[0]],
        18: 0,
        27: 1,
        30: [4],
        41: [1],
        53: 0,
        61: [],
        68: 1,
        79: 1,
        80: thinking,
    }.items():
        body[index] = value
    if request.action is not None:
        body.extend([None] * 18)
        retry = request.action == MessageAction.RETRY
        body[6], body[17], body[18], body[32] = [0], None, 2 if retry else 0, 1
        body[55], body[61], body[72] = [], [None, None, None, 0], 7 if retry else 2
        body[91], body[96], body[98] = 1, 0, 1
    if request.temporary:
        body[45] = 1
    if notebook:
        body[19] = notebook[0]
        body[40] = [None] * 14
        body[40][13] = notebook[1]
    identifier = str(uuid.uuid4()).upper()
    body[59] = identifier
    headers = {**HEADERS, "x-goog-ext-525005358-jspb": json.dumps([identifier, 1])}
    info = selected.metadata
    selector = [
        1,
        None,
        None,
        None,
        info["model_id"],
        None,
        None,
        int(request.temporary),
        [4, 5, 6, 8],
        None,
        None,
    ]
    selector.extend([None] * (info["capacity_slot"] - 1 - len(selector)))
    selector.extend([info["capacity"], None, None, info["number"], thinking, session.session_id])
    headers.update(
        {
            "x-goog-ext-525001261-jspb": json.dumps(selector),
            "x-goog-ext-73010989-jspb": "[0]",
            "x-goog-ext-73010990-jspb": "[0,0,0]",
        }
    )
    body[79] = info["number"]
    return headers, {"at": session.token, "f.req": json.dumps([None, json.dumps(body)])}


async def generate(
    session, request, metadata, notebook=None, *, files=None, request_timeout=(15, 130)
):
    from curl_cffi.const import CurlECode
    from curl_cffi.requests.exceptions import RequestException

    from .history import candidate_metadata, published_activity, published_thoughts

    if files is None:
        files = await upload_files(session, [item["path"] for item in request.attachments])
    headers, data = generation_request(session, request, metadata, files, notebook)
    frames = Frames()
    completed = False
    last_text = last_thoughts = ""
    try:
        response = await session.http.request(
            "POST",
            BASE + "assistant.lamda.BardFrontendService/StreamGenerate",
            params=session.params(),
            headers=headers,
            data=data,
            allow_redirects=False,
            timeout=request_timeout,
            stream=True,
        )
        try:
            WebSession.check(response)

            async def parts():
                async for chunk in response.aiter_content():
                    for frame in frames.feed(chunk):
                        yield frame
                for frame in frames.feed(b"", final=True):
                    yield frame

            async for frame in parts():
                if nested(frame, 5, 2, 0, 1, 0):
                    raise GeminiError(
                        "Gemini isteği reddetti; kota veya model uygunluğunu kontrol edin"
                    )
                payload = nested(frame, 2)
                if not isinstance(payload, str):
                    continue
                try:
                    body = json.loads(payload)
                except ValueError as error:
                    raise GeminiCompatibilityError("Gemini yanıt gövdesi değişti") from error
                returned = nested(body, 1)
                if isinstance(returned, list) and returned:
                    metadata = returned
                    if nested(metadata, 0) and not nested(body, 4):
                        yield {
                            "cid": metadata[0],
                            "metadata": metadata,
                            "context_only": True,
                        }
                context = nested(body, 25)
                if isinstance(context, str):
                    completed = True
                for candidate in nested(body, 4, default=[]):
                    if not nested(candidate, 0):
                        continue
                    cid = nested(metadata, 0) or request.conversation_id
                    current = list(metadata)
                    current.extend([None] * max(0, 3 - len(current)))
                    current[2] = candidate[0]
                    response_complete = nested(candidate, 8, 0) == 2
                    completed = completed or response_complete
                    text = nested(candidate, 1, 0)
                    if isinstance(text, str) and text.startswith(
                        (
                            "http://googleusercontent.com/card_content/",
                            "https://googleusercontent.com/card_content/",
                        )
                    ):
                        text = nested(candidate, 22, 0) or text
                    last_text = text or last_text
                    activity = published_activity(candidate)
                    last_thoughts = published_thoughts(candidate, activity[0], last_thoughts)
                    yield {
                        "cid": cid,
                        "metadata": current,
                        "text": last_text,
                        "thoughts": last_thoughts,
                        "files": candidate_files(candidate),
                        "sources": candidate_metadata(candidate, activity)["sources"],
                        "response_complete": response_complete,
                    }
                if isinstance(context, str):
                    metadata = [None] * 9 + [context]
                    yield {
                        "cid": request.conversation_id,
                        "metadata": metadata,
                        "context_only": True,
                    }
        finally:
            # curl_cffi's aclose waits for the transfer; cancel it first so a
            # stalled connection cannot hold the provider gate after cancellation.
            response.astream_task.cancel()
            await asyncio.gather(response.astream_task, return_exceptions=True)
    except RequestException as error:
        if error.code != CurlECode.OPERATION_TIMEDOUT:
            raise
        raise GeminiStreamError(
            "Gemini isteği ağ zaman aşımına uğradı; gönderim otomatik tekrarlanmadı "
            "ve sohbet geçmişi korundu."
        ) from None
    if not completed:
        raise GeminiStreamError("Gemini akışı tamamlanmadan kesildi; yeniden gönderilmedi")
    await session.persist()
