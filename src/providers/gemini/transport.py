"""Small, scoped compatibility layer for native Gemini notebook requests."""

import inspect
import json
import mimetypes
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from ..errors import ProviderError

NOTEBOOK_ID_INDEX = 19
NOTEBOOK_CONTEXT_INDEX = 40
CONTEXT_NOTEBOOK_INDEX = 13


def notebook_payload(data, remote, notebook_context):
    """Validate and copy the SDK envelope before binding native notebook context."""
    try:
        data = dict(data)
        outer = json.loads(data["f.req"])
        if not isinstance(outer, list) or len(outer) < 2 or not isinstance(outer[1], str):
            raise ValueError("changed outer envelope")
        inner = json.loads(outer[1])
        if not isinstance(inner, list) or len(inner) <= NOTEBOOK_CONTEXT_INDEX:
            raise ValueError("changed generation envelope")
        if inner[NOTEBOOK_CONTEXT_INDEX] is not None and not isinstance(
            inner[NOTEBOOK_CONTEXT_INDEX], list
        ):
            raise ValueError("changed notebook context")
        if not isinstance(remote, str) or not remote or not isinstance(notebook_context, list):
            raise ValueError("invalid notebook binding")
        inner[NOTEBOOK_ID_INDEX] = remote
        context = list(inner[NOTEBOOK_CONTEXT_INDEX] or [])
        context.extend([None] * max(0, CONTEXT_NOTEBOOK_INDEX + 1 - len(context)))
        context[CONTEXT_NOTEBOOK_INDEX] = notebook_context
        inner[NOTEBOOK_CONTEXT_INDEX] = context
        outer[1] = json.dumps(inner)
        data["f.req"] = json.dumps(outer)
        return data
    except (KeyError, TypeError, ValueError, IndexError):
        raise ProviderError("Gemini defter gönderim sözleşmesi değişmiş") from None


class NotebookTransport:
    def __init__(self, transport, remote, context):
        self.transport, self.remote, self.context = transport, remote, context

    def __getattr__(self, name):
        return getattr(self.transport, name)

    def stream(self, method, url, **options):
        target = urlparse(str(getattr(url, "value", url)))
        if target.hostname == "gemini.google.com" and target.path.endswith(
            "/assistant.lamda.BardFrontendService/StreamGenerate"
        ):
            options["data"] = notebook_payload(options.get("data"), self.remote, self.context)
        return self.transport.stream(method, url, **options)


def notebook_client_type(base):
    """Keep context across SDK reconnects without replacing its owned session."""

    class NotebookClient(base):
        @property
        def _live_client(self):
            transport = super()._live_client
            context = getattr(self, "_siope_notebook", None)
            return NotebookTransport(transport, *context) if context else transport

    return NotebookClient


@contextmanager
def notebook_transport(client, remote, context):
    original = getattr(client, "_siope_notebook", None)
    client._siope_notebook = (remote, context)
    try:
        yield
    finally:
        client._siope_notebook = original


async def stream_local_files(
    client, chat, prompt, paths, *, model=None, temporary=False, extended_thinking=False
):
    """Upload paths one at a time from disk, then stream through the SDK's request builder.

    gemini-webapi 2.1.x reads every selected path into memory and uploads them with
    asyncio.gather. CurlMime's local_path keeps one file at a time on disk instead.
    """
    paths = [Path(path) for path in paths]
    if not paths:
        async for chunk in chat.send_message_stream(
            prompt,
            temporary=temporary,
            extended_thinking=extended_thinking,
            current_retry=0,
        ):
            yield chunk
        return

    if not (
        hasattr(client, "_live_client")
        and inspect.iscoroutinefunction(getattr(type(client), "_sync_activity", None))
        and inspect.isasyncgenfunction(getattr(type(client), "_generate", None))
    ):
        # Test doubles and compatible client wrappers can retain their own upload method.
        async for chunk in chat.send_message_stream(
            prompt,
            files=[str(path) for path in paths],
            temporary=temporary,
            extended_thinking=extended_thinking,
            current_retry=0,
        ):
            yield chunk
        return

    from curl_cffi import CurlMime
    from gemini_webapi.constants import Endpoint, Headers
    from gemini_webapi.utils.upload_file import parse_file_name

    if getattr(client, "auto_close", False) is True:
        await client.reset_close_task()
    client._check_account_status(raise_error=True)
    await client._sync_activity()
    file_data = []
    for path in paths:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        mime_part = CurlMime()
        try:
            mime_part.addpart(
                name="file",
                content_type=mime,
                filename=path.name,
                local_path=str(path),
            )
            response = await client._live_client.post(
                url=Endpoint.UPLOAD,
                headers={
                    **Headers.REFERER.value,
                    **Headers.UPLOAD.value,
                    "Push-ID": client.push_id,
                },
                multipart=mime_part,
                allow_redirects=True,
            )
            response.raise_for_status()
            file_data.append([[response.text], parse_file_name(path)])
        finally:
            mime_part.close()

    await client._sync_activity()
    output = None
    async for chunk in client._generate(
        prompt=prompt,
        req_file_data=file_data,
        model=model,
        gem=None,
        chat=chat,
        temporary=temporary,
        session_state={"last_texts": {}, "last_thoughts": {}},
        deep_research=False,
        extended_thinking=extended_thinking,
        current_retry=0,
    ):
        output = chunk
        yield chunk
    if output is not None and hasattr(output, "metadata") and hasattr(chat, "metadata"):
        output.metadata = chat.metadata
        chat.last_output = output
