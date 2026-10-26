"""Monotonic SQL history hydration; scrolling never replaces loaded messages."""

from accounts import origin
from providers.history import merge_conversation

from .history import read_history
from .markdown import visible_chat_messages

MAX_LOADED_MESSAGES = 240


def visible_window(value, local=()):
    messages = visible_chat_messages(value.get("messages", ()))
    pending = [
        item
        for item in local
        if "_archive_ordinal" not in item
        and (item.get("local_pending") or item.get("role") == "system")
    ]
    return visible_chat_messages(
        merge_conversation({"messages": messages}, {"messages": pending})["messages"]
    )


class HistoryPager:
    """One contiguous loaded range per tab, extended at its edges in background."""

    def __init__(self, view):
        self.view = view

    def anchor(self, tab):
        view = self.view
        layout = getattr(view, "_virtual_transcript", None)
        if tab is not view.tabs[view.active_tab] or layout is None:
            return None
        scroll = min(max(0, view.transcript_window.vertical_scroll), max(0, layout.line_count - 2))
        ordinals = getattr(layout, "ordinals", ())
        identifiers = getattr(layout, "message_ids", ())
        for start, end, index in layout.ranges:
            if start <= scroll < end:
                ordinal = (
                    ordinals[index]
                    if index < len(ordinals)
                    else (
                        view.messages[index].get("_archive_ordinal")
                        if index < len(view.messages)
                        else None
                    )
                )
                if ordinal is not None:
                    result = {"ordinal": ordinal, "offset": scroll - start}
                    if index < len(identifiers) and identifiers[index]:
                        result["message_id"] = identifiers[index]
                    return result
        return None

    @staticmethod
    def position(layout, ordinals, anchor, height, fallback):
        if anchor:
            if anchor.get("edge") == "bottom":
                return max(0, layout.line_count - height)
            if anchor.get("edge") == "top":
                return 0
            identifiers = getattr(layout, "message_ids", ())
            target = anchor.get("message_id")
            match = next(
                (i for i, value in enumerate(identifiers) if target and value == target), None
            )
            for start, end, index in layout.ranges:
                if (match is not None and index == match) or (
                    match is None and ordinals[index] == anchor.get("ordinal")
                ):
                    return start + min(anchor["offset"], max(0, end - start - 1))
        return min(max(0, fallback), max(0, layout.line_count - height))

    def prefetch(self, layout, scroll, height):
        view = self.view
        tab = view.tabs[view.active_tab]
        window = tab.get("history_window")
        direction = view.transcript_window.scroll_direction
        if not window or not direction or tab.get("busy") or view.transcript_control.dragging:
            return
        requested = True
        if direction < 0 and window["start"] and not view.follow_output and scroll < 3 * height:
            requested = self.request("older")
        elif (
            direction > 0
            and window["end"] < window["total"]
            and scroll + 4 * height >= layout.line_count
        ):
            requested = self.request("newer")
        if requested:
            view.transcript_window.scroll_direction = 0

    def request(self, direction, *, refresh=False):
        if direction not in ("older", "newer", "top", "bottom"):
            raise ValueError("Unknown history direction")
        view = self.view
        tab = view.tabs[view.active_tab]
        window = tab.get("history_window")
        archive = view.archive_service()
        if not window or archive is None or tab.get("busy"):
            return False
        if tab.get("history_read"):
            if direction in ("top", "bottom"):
                tab["history_pending_edge"] = direction
                return True
            return False
        backwards = direction in ("older", "top")
        if not refresh and (
            window["start"] == 0 if backwards else window["end"] >= window["total"]
        ):
            return False
        cid = tab["conversation_id"]
        key, remote = origin(cid, tab.get("account_key", view.account_key))
        token = object()
        tab["history_read"] = token
        requested_anchor = self.anchor(tab)
        revision = getattr(view, "_manual_scroll_revision", 0)

        def cancelled():
            return tab.get("history_read") is not token

        def read():
            if refresh:
                options = (
                    {"start": window["start"], "complete": True, "until": window["end"]}
                    if window["end"] > window["start"]
                    else {"start": None}
                )
            elif direction == "top":
                options = {"start": 0}
            elif direction == "bottom":
                options = {"start": None}
            elif backwards:
                options = {"before": window["start"]}
            else:
                options = {"start": window["end"]}
            result = read_history(archive, key, remote, cancelled=cancelled, **options)
            old_version = tab.get("history_version")
            if result and old_version and old_version != result.get("history_version"):
                # Re-read only the affected page or resident range. A refresh must
                # not turn a bounded viewport into a full-history materialization.
                if direction == "top":
                    result = read_history(archive, key, remote, start=0, cancelled=cancelled)
                elif direction == "bottom":
                    result = read_history(archive, key, remote, cancelled=cancelled)
                else:
                    start = min(window["start"], result["history_window"]["start"])
                    end = max(window["end"], result["history_window"]["end"])
                    result = read_history(
                        archive,
                        key,
                        remote,
                        start=start,
                        complete=True,
                        until=end,
                        cancelled=cancelled,
                    )
                if result is not None:
                    result["_replace_loaded"] = True
            elif result and direction in ("top", "bottom"):
                result["_replace_loaded"] = True
            return result

        def finished(value):
            if cancelled():
                return
            tab.pop("history_read", None)
            pending = tab.pop("history_pending_edge", None)
            if (
                value is None
                or tab.get("history_window") is not window
                or tab.get("busy")
                or not any(x is tab for x in view.tabs)
            ):
                view.app.invalidate()
                return
            active = tab is view.tabs[view.active_tab]
            anchor = self.anchor(tab) if active else requested_anchor
            edge = pending or (
                direction
                if direction in ("top", "bottom")
                and getattr(view, "_manual_scroll_revision", 0) == revision
                else None
            )
            incoming = value["messages"]
            current = (
                [] if value.pop("_replace_loaded", False) or refresh else tab.get("messages", ())
            )
            by_id = {
                m.get("id") or ("ordinal", m.get("_archive_ordinal")): m
                for m in (*current, *incoming)
                if m.get("_archive_ordinal") is not None
            }
            messages = sorted(by_id.values(), key=lambda m: m["_archive_ordinal"])
            messages = visible_window({"messages": messages}, tab.get("messages", ()))
            extent = dict(value["history_window"])
            if current:
                extent.update(
                    start=min(window["start"], extent["start"]),
                    end=max(window["end"], extent["end"]),
                )
            archived = [m for m in messages if m.get("_archive_ordinal") is not None]
            pending_messages = [m for m in messages if m.get("_archive_ordinal") is None]
            if len(archived) > MAX_LOADED_MESSAGES:
                if direction == "older":
                    archived = archived[:MAX_LOADED_MESSAGES]
                    extent["end"] = min(extent["end"], archived[-1]["_archive_ordinal"] + 1)
                else:
                    archived = archived[-MAX_LOADED_MESSAGES:]
                    extent["start"] = max(extent["start"], archived[0]["_archive_ordinal"])
                messages = archived + pending_messages
            tab.update(
                messages=messages,
                history_window=extent,
                history_version=value.get("history_version"),
            )
            if active:
                view.messages = messages
                tab["history_anchor"] = {"edge": edge} if edge else anchor
                view.transcript_control.clear_selection()
                if edge:
                    view.follow_output = edge == "bottom"
            view.app.invalidate()
            if pending and active:
                (view.scroll_top if pending == "top" else view.scroll_bottom)()

        def failed(reason):
            if not cancelled():
                tab.pop("history_read", None)
                tab.pop("history_pending_edge", None)
                view.status = view.tr("Arşiv okunamadı: ") + str(reason)
                view.app.invalidate()

        view._read_in_background(read, finished, failed, session_bound=False)
        return True
