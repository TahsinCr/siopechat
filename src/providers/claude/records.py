"""Lossless Claude records mapped to the shared provider contract."""

from ..errors import ProviderError
from ..models import Content, Conversation, Message, Origin, Project, Resource


def conversation(value, account_id):
    if not isinstance(value, dict) or not value.get("uuid"):
        raise ProviderError("Claude conversation contract changed")
    messages = []
    turns = value.get("chat_messages", [])
    leaf = value.get("current_leaf_message_uuid")
    by_id = {x.get("uuid"): x for x in turns}
    if leaf in by_id and any("parent_message_uuid" in x for x in turns):
        branch, seen = [], set()
        while leaf in by_id and leaf not in seen:
            seen.add(leaf)
            item = by_id[leaf]
            branch.append(item)
            leaf = item.get("parent_message_uuid")
        turns = list(reversed(branch))
    for item in turns:
        role = "user" if item.get("sender") == "human" else "assistant"
        content = item.get("content") or (
            [{"type": "text", "text": item["text"]}] if item.get("text") else []
        )
        for index, block in enumerate(content):
            kind = block.get("type")
            if kind in ("text", "thinking"):
                text = (
                    block.get("text")
                    if kind == "text"
                    else block.get("thinking") or block.get("text")
                )
                if text:
                    messages.append(
                        Message(
                            str(item["uuid"]) + ":" + str(index),
                            role,
                            (Content("text", text),),
                            "analysis" if kind == "thinking" else "final",
                            {
                                "native": block,
                                "native_message": {k: v for k, v in item.items() if k != "content"},
                            },
                        )
                    )
            else:
                # Keep unsupported tool/media blocks available to future consumers.
                messages.append(
                    Message(
                        str(item["uuid"]) + ":" + str(index),
                        role,
                        (Content(kind or "unknown", metadata={"native": block}),),
                        metadata={"native": block},
                    )
                )
        for resource_index, resource in enumerate(
            (item.get("attachments") or []) + (item.get("files") or [])
        ):
            messages.append(
                Message(
                    str(item["uuid"]) + ":resource:" + str(resource_index),
                    role,
                    (
                        Content(
                            "resource",
                            resource=Resource(
                                str(resource.get("uuid") or resource.get("id") or ""),
                                resource.get("file_name") or resource.get("name") or "File",
                                metadata={"native": resource},
                            ),
                        ),
                    ),
                )
            )
    return Conversation(
        Origin("claude", account_id, value["uuid"]),
        value.get("name") or "Claude sohbeti",
        tuple(messages),
        value.get("project_uuid"),
        {"native": {k: v for k, v in value.items() if k != "chat_messages"}},
    )


def project(value, account_id):
    if not isinstance(value, dict) or not value.get("uuid"):
        raise ProviderError("Claude project contract changed")
    resources = tuple(
        Resource(
            str(x.get("uuid") or x.get("id") or ""),
            x.get("file_name") or x.get("name") or "Resource",
            metadata={"native": x},
        )
        for x in (value.get("docs") or []) + (value.get("files") or [])
    )
    return Project(
        Origin("claude", account_id, value["uuid"]),
        value.get("name") or "Claude projesi",
        value.get("prompt_template") or value.get("instructions"),
        resources,
        {"kind": "project", "native": value},
    )
