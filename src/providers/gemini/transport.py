"""Small, scoped compatibility layer for native Gemini notebook requests."""

import json
from contextlib import contextmanager
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
