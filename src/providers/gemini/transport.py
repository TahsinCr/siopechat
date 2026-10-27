"""Bounded Gemini generation protocol and streaming disk uploads."""

import codecs
import json
import mimetypes
import uuid
from pathlib import Path

from ..api import MessageAction
from ..errors import ProviderError
from ..session import WebSession
from .media import candidate_files
from .session import BASE, HEADERS, nested


class Frames:
    """Decode newline-framed JSPB without retaining the full response."""

    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.pending = ""

    def feed(self, data, final=False):
        try:
            self.pending += self.decoder.decode(data, final=final)
        except UnicodeDecodeError as error:
            raise ProviderError("Gemini akışı geçersiz UTF-8 içeriyor") from error
        if len(self.pending) > 8 * 1024 * 1024:
            raise ProviderError("Gemini akış çerçevesi boyut sınırını aştı")
        lines = self.pending.split("\n")
        self.pending = "" if final else lines.pop()
        for line in lines:
            if not line.startswith("[["):
                continue
            try:
                rows = json.loads(line)
            except ValueError as error:
                raise ProviderError("Gemini akış biçimi değişti") from error
            yield from rows


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
    selected = next(
        (m for m in session.models if request.model in (m.id, m.metadata["model_id"])), None
    )
    if request.model and selected is None:
        raise ValueError("Model bu Gemini hesabında kullanılamıyor")
    extended = request.effort == "extended"
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
        80: 2 if extended else 1,
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
    if selected:
        info = selected.metadata
        selector = [1, None, None, None, info["model_id"], None, None, 0, [4, 5, 6, 8], None, None]
        selector.extend([None] * (info["capacity_slot"] - 1 - len(selector)))
        selector.extend(
            [info["capacity"], None, None, info["number"], 2 if extended else 1, session.session_id]
        )
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
        async with session.http.stream(
            "POST",
            BASE + "assistant.lamda.BardFrontendService/StreamGenerate",
            params=session.params(),
            headers=headers,
            data=data,
            allow_redirects=False,
            timeout=request_timeout,
        ) as response:
            WebSession.check(response)

            async def parts():
                async for chunk in response.aiter_content():
                    for frame in frames.feed(chunk):
                        yield frame
                for frame in frames.feed(b"", final=True):
                    yield frame

            async for frame in parts():
                if nested(frame, 5, 2, 0, 1, 0):
                    raise ProviderError(
                        "Gemini isteği reddetti; kota veya model uygunluğunu kontrol edin"
                    )
                payload = nested(frame, 2)
                if not isinstance(payload, str):
                    continue
                try:
                    body = json.loads(payload)
                except ValueError as error:
                    raise ProviderError("Gemini yanıt gövdesi değişti") from error
                returned = nested(body, 1)
                if isinstance(returned, list) and returned:
                    metadata = returned
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
                    completed = completed or nested(candidate, 8, 0) == 2
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
                    }
                if isinstance(context, str):
                    metadata = [None] * 9 + [context]
                    yield {
                        "cid": request.conversation_id,
                        "metadata": metadata,
                        "context_only": True,
                    }
    except RequestException as error:
        if error.code != CurlECode.OPERATION_TIMEDOUT:
            raise
        raise ProviderError(
            "Gemini isteği ağ zaman aşımına uğradı; gönderim otomatik tekrarlanmadı "
            "ve sohbet geçmişi korundu."
        ) from None
    if not completed:
        raise ProviderError("Gemini akışı tamamlanmadan kesildi; yeniden gönderilmedi")
    await session.persist()
