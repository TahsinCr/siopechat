import json
from typing import Any, Iterable

from .discovery import project_id


def message_parts(content: Any) -> list[Any]:
    if not isinstance(content, dict):
        return []
    if content.get("content_type") == "thoughts":
        thoughts = content.get("thoughts") or []
        return [
            item.get("summary") or item.get("content", "")
            for item in thoughts
            if isinstance(item, dict)
        ]
    if content.get("content_type") in ("reasoning_recap", "reasoning_summary"):
        return [content.get("content") or content.get("text") or ""]
    parts = content.get("parts", [])
    return parts if isinstance(parts, list) else []


def text_from_parts(parts: Iterable[Any]) -> str:
    rendered: list[str] = []
    for part in parts:
        if isinstance(part, str):
            rendered.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                rendered.append(text)
            else:
                rendered.append(json.dumps(part, ensure_ascii=False, sort_keys=True))
        elif part is not None:
            rendered.append(str(part))
    return "\n".join(rendered)


def normalize_message(message: dict[str, Any]) -> dict[str, Any]:
    author = message.get("author")
    author = author if isinstance(author, dict) else {}
    content = message.get("content")
    content = content if isinstance(content, dict) else {}
    metadata = message.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    parts = message_parts(content)
    return {
        "id": message.get("id"),
        "role": author.get("role"),
        "name": author.get("name"),
        "channel": message.get("channel"),
        "recipient": message.get("recipient"),
        "status": message.get("status"),
        "create_time": message.get("create_time"),
        "update_time": message.get("update_time"),
        "content_type": content.get("content_type"),
        "parts": parts,
        "text": text_from_parts(parts),
        "hidden": bool(metadata.get("is_visually_hidden_from_conversation")),
    }


def normalize_conversation(conversation: dict[str, Any]) -> dict[str, Any]:
    raw_messages = conversation.get("messages", [])
    messages = raw_messages if isinstance(raw_messages, list) else []
    return {
        "id": conversation.get("conversation_id") or conversation.get("id"),
        "title": conversation.get("title"),
        "create_time": conversation.get("create_time"),
        "update_time": conversation.get("update_time"),
        "current_node": conversation.get("current_node"),
        "model": conversation.get("default_model_slug"),
        "project_id": project_id(conversation),
        "project_name": conversation.get("project_name"),
        "project": conversation.get("project"),
        "messages": [normalize_message(item) for item in messages if isinstance(item, dict)],
        "page_info": conversation.get("page_info"),
        "temporary": bool(conversation.get("temporary")),
    }


def new_messages(conversation: dict[str, Any], previous_ids: set[str]) -> list[dict[str, Any]]:
    raw = conversation.get("messages", [])
    if not isinstance(raw, list):
        return []
    return [
        normalize_message(item)
        for item in raw
        if isinstance(item, dict) and item.get("id") not in previous_ids
    ]
