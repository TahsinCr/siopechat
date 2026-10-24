"""Bounded alignment of provider history and locally accepted, unsynchronized turns."""

from difflib import SequenceMatcher

MERGE_WINDOW = 512


def signature(message):
    role = message.get("role")
    channel = message.get("channel") or "final"
    if role == "reasoning":
        role, channel = "assistant", "analysis"
    return role, channel, (message.get("text") or "").replace("\r\n", "\n").strip()


def merge_conversation(remote, local):
    """Provider history supplies the baseline; unmatched local turns remain pending.

    Align only bounded tails. Earlier provider history is kept verbatim. Confirmed
    provider IDs/content replace provisional copies while local resource paths and
    published thinking summaries are retained. No timestamps are invented.
    """
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

    def key(m):
        return ("id", m["id"]) if m.get("id") in common_ids else signature(m)

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
        # A read racing generation may contain a prefix of a locally completed
        # answer. Keep the complete answer once rather than appending both.
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
        # Only unconfirmed sends and thoughts missing from the provider can be
        # added independently. An edited/deleted remote branch is not resurrected.
        merged.extend(
            m
            for m in after[li:local_index]
            if m.get("local_pending")
            or (not m.get("id") and "content" not in m)
            or signature(m)[1] == "analysis"
        )
        for i in range(block.size):
            message = dict(after[local_index + i]) | before[r + i]
            message.pop("local_pending", None)
            merged.append(message)
        ri, li = r + block.size, local_index + block.size
    result = dict(local) | remote
    result["messages"] = merged
    result["project_id"] = remote.get("project_id") or local.get("project_id")
    return result
