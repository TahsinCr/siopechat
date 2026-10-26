"""Shared message actions and published source metadata, independent of any UI."""

from urllib.parse import urlsplit

from .api import MessageAction
from .errors import ProviderError


def target_message(messages, message_id, role):
    """Resolve a provider identity only on the currently selected conversation path."""
    for index, message in enumerate(messages):
        if message.id == message_id:
            if message.role != role or message.channel == "analysis":
                break
            return index, message
    raise ProviderError("The selected message is no longer on the active conversation path")


def actions_for(role, *, final=True, latest=True, branch=False):
    if not final:
        return ()
    if role == "user":
        return (MessageAction.EDIT.value,)
    if role != "assistant":
        return ()
    return (
        *((MessageAction.RETRY.value,) if latest else ()),
        *((MessageAction.BRANCH.value,) if branch else ()),
    )


def sources_from(*roots):
    """Read explicit published citations; never guess sources from answer text."""
    sources, seen = [], set()

    def add(value):
        if not isinstance(value, dict) or len(sources) >= 128:
            return
        source = value.get("web") or value.get("source") or value
        if not isinstance(source, dict):
            return
        url = source.get("url") or source.get("uri") or source.get("href")
        file_id = source.get("file_id")
        if not url and isinstance(file_id, str) and file_id and len(file_id) <= 256:
            identity = ("file", file_id)
            if identity not in seen and all(ord(c) >= 32 and ord(c) != 127 for c in file_id):
                seen.add(identity)
                label = (
                    source.get("title") or source.get("filename") or source.get("name") or file_id
                )
                sources.append(
                    {
                        "title": " ".join(
                            "".join(c for c in str(label) if ord(c) >= 32 and ord(c) != 127).split()
                        )[:200],
                        "file_id": file_id,
                    }
                )
            return
        if not isinstance(url, str) or len(url) > 8192 or url in seen:
            return
        if any(ord(char) < 32 or ord(char) == 127 for char in url):
            return
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return
            if parsed.username or parsed.password:
                return
        except ValueError:
            return
        seen.add(url)
        label = source.get("title") or source.get("name") or value.get("title") or url
        sources.append(
            {
                "title": " ".join(
                    "".join(c for c in str(label) if ord(c) >= 32 and ord(c) != 127).split()
                )[:200],
                "url": url,
            }
        )

    def visit(value, depth=0):
        if not isinstance(value, dict) or depth > 4:
            return
        for key in ("citations", "sources", "content_references", "items", "grouped_webpages"):
            entries = value.get(key)
            if isinstance(entries, (list, tuple)):
                for item in entries[:128]:
                    add(item)
                    visit(item, depth + 1)
        for key in ("metadata", "native", "grounding_metadata", "groundingMetadata"):
            visit(value.get(key), depth + 1)
        for key in ("grounding_chunks", "groundingChunks"):
            entries = value.get(key)
            if isinstance(entries, (list, tuple)):
                for item in entries[:128]:
                    add(item)

    for root in roots:
        visit(root)
    return tuple(sources)
