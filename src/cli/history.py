"""Background-only archive snapshots and transcript preparation for the terminal."""

import threading
from types import SimpleNamespace

from accounts import origin

from .markdown import MessageRenderer, index_transcript, transcript_fragments, visible_chat_messages
from .transcript import VirtualTranscript

_render_gate = threading.Semaphore(1)


def read_history(
    archive, account, remote, *, start=None, before=None, complete=False, until=None, cancelled=None
):
    """Read lean history batches; never combine different SQLite revisions."""
    if until is not None and (before is not None or start is None or until <= start):
        raise ValueError("invalid bounded history range")
    for attempt in range(2):
        cursor, boundary = start, before
        result, messages, version = None, [], None
        while True:
            if cancelled and cancelled():
                return None
            page = archive.conversation_window(
                account,
                remote,
                **({"before": boundary} if boundary is not None else {"start": cursor}),
            )
            if page is None:
                return None
            current_version = page.get("history_version")
            if result is not None and current_version != version:
                break
            window = page["history_window"]
            if result is None:
                result = {k: v for k, v in page.items() if k != "messages"}
                result["history_window"] = dict(window)
                version = current_version
            page_messages = visible_chat_messages(page.get("messages", ()))
            if until is not None and boundary is None:
                page_messages = [
                    message
                    for message in page_messages
                    if message.get("_archive_ordinal") is not None
                    and message["_archive_ordinal"] < until
                ]
            messages.extend(page_messages)
            extent = result["history_window"]
            extent.update(
                start=min(extent["start"], window["start"]), end=max(extent["end"], window["end"])
            )
            if until is not None and boundary is None:
                extent["end"] = min(extent["end"], until)
            if (
                not complete
                or (boundary is not None and window["start"] == 0)
                or (boundary is None and window["end"] >= window["total"])
                or (until is not None and window["end"] >= until)
            ):
                messages.sort(key=lambda m: m.get("_archive_ordinal", -1))
                return dict(result, messages=messages)
            next_cursor = window["start"] if boundary is not None else window["end"]
            previous = boundary if boundary is not None else cursor
            if previous is not None and next_cursor == previous:
                raise RuntimeError("Archive history did not advance")
            if boundary is not None:
                boundary = next_cursor
            else:
                cursor = next_cursor
        # One retry starts a fresh revision, without publishing a mixed snapshot.
    raise RuntimeError("Archive history changed during the read; retry synchronization")


def prepare_virtual_transcript(
    messages,
    width,
    labels,
    reasoning,
    cancelled=None,
    active_index=None,
    renderer=None,
    preserve_order=False,
    previous=None,
):
    """Build line metadata off-thread; retain only pages requested by the viewport."""
    with _render_gate:
        if cancelled and cancelled():
            raise InterruptedError("Superseded transcript")
        if renderer is None:
            renderer = MessageRenderer(labels)
        renderer.cache_byte_limit = 256 * 1024
        renderer.cache_limit = 512
        transcript = VirtualTranscript(
            messages,
            width,
            reasoning,
            renderer=renderer,
            cancelled=cancelled,
            active_index=active_index,
            preserve_order=preserve_order,
            previous=previous,
        )
        return transcript


def prepare_transcript(
    messages,
    width,
    labels,
    reasoning,
    cancelled=None,
    *,
    renderer=None,
    active_index=None,
    index_rows=True,
):
    with _render_gate:
        if cancelled and cancelled():
            raise InterruptedError("Superseded transcript")
        if renderer is None:
            renderer = MessageRenderer(labels)
        text, ranges = renderer.transcript(messages, width, reasoning, cancelled, active_index)
        ansi = transcript_fragments(text)
        return text, ranges, ansi, index_transcript(ansi, ranges) if index_rows else ()


def prepare_archive_snapshot(
    archive, accounts, tabs, known_chats, known_projects, default_account, revision
):
    values, deleted = {}, set()
    states = archive.states()
    state_map = {(x["account"], x["kind"], x["remote"]): x for x in states}
    for account in accounts:
        key = account["key"]
        for kind in ("chats", "containers"):
            values[key, kind, ""] = archive.get(key, kind, "")
    identities = [(x.get("id"), "conversation") for x in known_chats] + [
        (x, "container") for x in known_projects
    ]
    for (key, kind, _), page in list(values.items()):
        if page:
            identities.extend(
                (x.get("id"), "container" if kind == "containers" else "conversation")
                for x in page.get("items", [])
            )
    for tab, record in tabs:
        cid = record.get("conversation_id")
        if not cid:
            continue
        key, remote = origin(cid, record.get("account_key", default_account))
        identities.append((cid, "conversation"))
        state = state_map.get((key, "conversation", remote))
        if (
            record.get("busy")
            or not state
            or state["state"] != "ready"
            or state.get("version") == record.get("archive_version")
        ):
            continue
        window = record.get("history_window") or {}
        has_window = window.get("end", 0) > window.get("start", 0)
        value = read_history(
            archive,
            key,
            remote,
            start=window.get("start") if has_window else None,
            complete=has_window,
            until=window["end"] if has_window else None,
        )
        values[key, "conversation", remote] = value
    for cid, kind in identities:
        if cid:
            key, remote = origin(cid, default_account)
            if archive.deleted(key, kind, remote):
                deleted.add((key, kind, remote))
    return SimpleNamespace(
        revision=revision,
        tab_windows={id(tab): record.get("history_window") for tab, record in tabs},
        get=lambda *key: values.get(key),
        deleted=lambda *key: key in deleted,
        states=lambda: states,
    )
