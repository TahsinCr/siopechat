"""Shared message actions and published source metadata, independent of any UI."""

import re
from urllib.parse import unquote, urlsplit

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
    remaining_nodes = 512

    def add(value):
        if not isinstance(value, dict) or len(sources) >= 128:
            return
        source = value.get("web") or value.get("source") or value.get("url_citation") or value
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
        nonlocal remaining_nodes
        if depth > 4 or remaining_nodes <= 0:
            return
        remaining_nodes -= 1
        if isinstance(value, (list, tuple)):
            for item in value[:128]:
                visit(item, depth + 1)
            return
        if not isinstance(value, dict):
            return
        for key in (
            "citations",
            "sources",
            "content_references",
            "annotations",
            "items",
            "grouped_webpages",
        ):
            entries = value.get(key)
            if isinstance(entries, (list, tuple)):
                for item in entries[:128]:
                    add(item)
                    visit(item, depth + 1)
        for key in (
            "metadata",
            "native",
            "content",
            "candidate",
            "grounding_metadata",
            "groundingMetadata",
        ):
            visit(value.get(key), depth + 1)
        for key in ("grounding_chunks", "groundingChunks"):
            entries = value.get(key)
            if isinstance(entries, (list, tuple)):
                for item in entries[:128]:
                    add(item)

    for root in roots:
        visit(root)
    return tuple(sources)


def files_from(*roots):
    """Collect explicit downloadable resources without interpreting ordinary web links."""
    files, seen = [], set()
    remaining = 2048

    def add(value, owner=None):
        if not isinstance(value, dict) or len(files) >= 128:
            return
        native = (
            value.get("metadata", {}).get("native", {})
            if isinstance(value.get("metadata"), dict)
            else {}
        )
        value = native | value if isinstance(native, dict) else value
        target = next(
            (
                value.get(key)
                for key in (
                    "download_url",
                    "remote_url",
                    "file_path",
                    "path",
                    "url",
                    "asset_pointer",
                )
                if isinstance(value.get(key), str) and value[key]
            ),
            None,
        )
        identity = value.get("id")
        if isinstance(identity, str) and identity.startswith("gemini-media:"):
            target = identity
        if isinstance(identity, str) and identity.startswith(
            (
                "http://googleusercontent.com/image_generation_content/",
                "https://googleusercontent.com/image_generation_content/",
            )
        ):
            target = identity
        if (
            not target
            and isinstance(identity, str)
            and identity.startswith(("sandbox:/mnt/", "/mnt/user-data/"))
        ):
            target = identity
        if (
            not target
            or len(target) > 8192
            or target in seen
            or any(ord(c) < 32 or ord(c) == 127 for c in target)
        ):
            return
        try:
            parsed = urlsplit(target)
            if parsed.scheme in ("http", "https"):
                if not parsed.hostname or parsed.username or parsed.password:
                    return
            elif parsed.scheme == "file-service":
                if not re.fullmatch(r"file[-_][A-Za-z0-9_-]+", parsed.netloc) or parsed.path:
                    return
            elif parsed.scheme == "gemini-media":
                if not parsed.path or parsed.netloc or len(target) > 512:
                    return
            elif (
                parsed.scheme not in ("", "sandbox", "computer")
                or not parsed.path.startswith(("/mnt/data/", "/mnt/user-data/"))
                or parsed.netloc
            ):
                return
        except ValueError:
            return
        seen.add(target)
        name = (
            value.get("name")
            or value.get("file_name")
            or value.get("filename")
            or unquote(parsed.path).rsplit("/", 1)[-1]
            or parsed.netloc
            or "File"
        )
        item = {
            "url": target,
            "name": " ".join(
                "".join(c for c in str(name) if ord(c) >= 32 and ord(c) != 127).split()
            )[:200],
        }
        message_id = value.get("message_id") or owner
        if isinstance(message_id, str) and 0 < len(message_id) <= 256:
            item["message_id"] = message_id
        media = value.get("media_type") or value.get("mime_type") or value.get("content_type")
        if isinstance(media, str) and re.fullmatch(r"[\w!#$&^.+-]+/[\w!#$&^.+-]+", media):
            item["media_type"] = media[:120]
        size = value.get("size_bytes", value.get("size"))
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            item["size_bytes"] = size
        files.append(item)

    def visit(value, depth=0, resource=False, owner=None):
        nonlocal remaining
        if depth > 7 or remaining <= 0 or len(files) >= 128:
            return
        remaining -= 1
        if isinstance(value, (list, tuple)):
            for item in value[:128]:
                visit(item, depth + 1, resource, owner)
        elif isinstance(value, str):
            for match in re.finditer(
                r"\[([^\]\n]{1,200})\]\(((?:sandbox:|computer:)?/mnt/(?:data|user-data)/[^\s)]+)\)",
                value,
            ):
                add({"name": match[1], "url": match[2]}, owner)
        elif isinstance(value, dict):
            if resource or any(
                value.get(key) for key in ("file_path", "download_url", "asset_pointer")
            ):
                add(value, owner)
            visit(value.get("text"), depth + 1, owner=owner)
            for key in (
                "files",
                "resources",
                "attachments",
                "artifacts",
                "resource",
                "file",
                "output_files",
                "files_metadata",
            ):
                visit(value.get(key), depth + 1, True, owner)
            for key in ("metadata", "native", "native_message", "content", "parts", "value"):
                visit(value.get(key), depth + 1, owner=owner)

    for root in roots:
        owner = (
            root.get("id")
            if isinstance(root, dict) and (root.get("role") or root.get("author"))
            else None
        )
        visit(root, owner=owner)
    return tuple(files)
