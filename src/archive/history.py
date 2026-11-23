"""Bounded alignment of provider history and locally accepted, unsynchronized turns."""

from __future__ import annotations

from difflib import SequenceMatcher

MERGE_WINDOW = 512


def signature(message: dict) -> tuple[str | None, str, str]:
    role = message.get("role")
    channel = message.get("channel") or "final"
    if role == "reasoning":
        role, channel = "assistant", "analysis"
    return role, channel, (message.get("text") or "").replace("\r\n", "\n").strip()


def merge_conversation(remote: dict, local: dict | None) -> dict:
    """Provider history supplies the baseline; unmatched local turns remain pending."""
    if not local:
        return remote
    before, after = remote.get("messages", []), local.get("messages", [])
    if not after:
        return remote
    if not before:
        return dict(remote, messages=after)
    rstart = max(0, len(before) - MERGE_WINDOW)
    lstart = max(0, len(after) - MERGE_WINDOW)
    common_ids = {m.get("id") for m in before if m.get("id")} & {
        m.get("id") for m in after if m.get("id") and not m.get("local_pending")
    }

    def key(message: dict) -> tuple[str, ...]:
        return ("id", message["id"]) if message.get("id") in common_ids else signature(message)

    blocks = SequenceMatcher(
        None,
        [key(m) for m in before[rstart:]],
        [key(m) for m in after[lstart:]],
        autojunk=False,
    ).get_matching_blocks()
    prefix = []
    if remote.get("history_complete") is False and before[0].get("id"):
        start = next((i for i, m in enumerate(after) if m.get("id") == before[0]["id"]), 0)
        prefix = after[:start]
    merged, ri, li = prefix + list(before[:rstart]), rstart, lstart
    if blocks[0].size and blocks[0].a == 0 and rstart == 0:
        merged = list(after[: lstart + blocks[0].b])
        li = lstart + blocks[0].b
    for block in blocks:
        r, local_index = rstart + block.a, lstart + block.b
        pending = after[li:local_index]
        remote_gap = before[ri:r]
        remote_gap = [
            m
            for m in remote_gap
            if not any(
                x.get("local_pending")
                and signature(m)[:2] == signature(x)[:2]
                and signature(m)[2]
                and signature(x)[2].startswith(signature(m)[2])
                for x in pending
            )
        ]
        merged.extend(remote_gap)
        merged.extend(
            m
            for m in after[li:local_index]
            if m.get("local_pending")
            or (not m.get("id") and "content" not in m)
            or signature(m)[1] == "analysis"
        )
        for i in range(block.size):
            local_message, remote_message = after[local_index + i], before[r + i]
            message = dict(local_message) | remote_message
            if signature(local_message) == signature(remote_message):
                local_metadata = local_message.get("metadata") or {}
                remote_metadata = remote_message.get("metadata") or {}
                metadata = dict(remote_metadata)
                for name in ("model", "effort", "sources", "files"):
                    if name not in metadata and name in local_metadata:
                        metadata[name] = local_metadata[name]
                if metadata:
                    message["metadata"] = metadata
            message.pop("local_pending", None)
            merged.append(message)
        ri, li = r + block.size, local_index + block.size
    result = dict(local) | remote
    result["messages"] = merged
    result["project_id"] = remote.get("project_id") or local.get("project_id")
    return result
