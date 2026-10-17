"""Small, scoped compatibility layer for native Gemini notebook requests."""

import json
from contextlib import contextmanager
from urllib.parse import urlparse

from ..errors import ProviderError


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
            try:
                data = dict(options["data"])
                outer = json.loads(data["f.req"])
                inner = json.loads(outer[1])
                if not isinstance(inner, list) or len(inner) < 41:
                    raise ValueError("changed generation envelope")
                # Native frontend yyd: project id is field 20; notebook context
                # is field 41's field 14. The id alone is interpreted as a Gem.
                inner[19] = self.remote
                context = list(inner[40] or [])
                context.extend([None] * max(0, 14 - len(context)))
                context[13] = self.context
                inner[40] = context
                outer[1] = json.dumps(inner)
                data["f.req"] = json.dumps(outer)
                options["data"] = data
            except (KeyError, TypeError, ValueError, IndexError):
                raise ProviderError("Gemini defter gönderim sözleşmesi değişmiş") from None
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
