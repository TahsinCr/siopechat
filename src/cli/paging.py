"""Bounded SQL history navigation; all disk reads run outside terminal painting."""

from accounts import origin
from providers.history import merge_conversation

from .markdown import visible_chat_message


def visible_window(value, local=()):
    messages = [m for item in value.get("messages", ()) if (m := visible_chat_message(item))]
    pending = [
        item
        for item in local
        if "_archive_ordinal" not in item
        and (item.get("local_pending") or item.get("role") == "system")
    ]
    return merge_conversation({"messages": messages}, {"messages": pending})["messages"]


class HistoryPager:
    """Keep one overlapping message window per tab and preserve its visual anchor."""

    def __init__(self, view):
        self.view = view

    def anchor(self, tab):
        view = self.view
        layout = getattr(view, "_virtual_transcript", None)
        if tab is not view.tabs[view.active_tab] or layout is None:
            return None
        scroll = view.transcript_window.vertical_scroll
        for start, end, index in layout.ranges:
            if start <= scroll < end and index < len(view.messages):
                ordinal = view.messages[index].get("_archive_ordinal")
                if ordinal is not None:
                    return {"ordinal": ordinal, "offset": scroll - start}
        return None

    @staticmethod
    def position(layout, ordinals, anchor, height, fallback):
        if anchor:
            if anchor.get("edge") == "bottom":
                return max(0, layout.line_count - height)
            if anchor.get("edge") == "top":
                return 0
            for start, end, index in layout.ranges:
                if ordinals[index] == anchor.get("ordinal"):
                    return start + min(anchor["offset"], max(0, end - start - 1))
        return min(max(0, fallback), max(0, layout.line_count - height))

    def prefetch(self, layout, scroll, height):
        view = self.view
        tab = view.tabs[view.active_tab]
        window = tab.get("history_window")
        if (
            not window
            or tab.get("busy")
            or view.transcript_control.selected_text
            or view.transcript_control.dragging
        ):
            return
        if window["end"] < window["total"] and scroll + 3 * height >= layout.line_count:
            self.request("newer")
        elif window["start"] and not view.follow_output and scroll < 2 * height:
            self.request("older")

    def request(self, direction, *, refresh=False):
        view = self.view
        tab = view.tabs[view.active_tab]
        window = tab.get("history_window")
        archive = view.archive_service()
        if not window or archive is None or tab.get("busy") or tab.get("history_read"):
            return False
        if not refresh and (
            (direction == "top" and window["start"] == 0)
            or (direction == "bottom" and window["end"] == window["total"])
        ):
            return False
        count = max(1, window["end"] - window["start"])
        if direction == "older":
            if not window["start"]:
                return False
            start = max(0, window["start"] - max(1, count // 2))
        elif direction == "newer":
            if window["end"] >= window["total"]:
                return False
            start = window["start"] + max(1, count // 2)
        else:
            start = 0 if direction == "top" else None
        anchor = {"edge": direction} if direction in ("top", "bottom") else self.anchor(tab)
        request_scroll = view.transcript_window.vertical_scroll
        if direction in ("top", "bottom"):
            view.transcript_control.clear_selection()
        cid = tab["conversation_id"]
        key, remote = origin(cid, tab.get("account_key", view.account_key))
        token = object()
        tab["history_read"] = token

        def finished(value):
            if tab.get("history_read") is not token:
                return
            tab.pop("history_read", None)
            if (
                value is None
                or tab.get("conversation_id") != cid
                or tab.get("busy")
                or tab.get("history_window") is not window
                or not any(item is tab for item in view.tabs)
                or (
                    tab is view.tabs[view.active_tab]
                    and (view.transcript_control.selected_text or view.transcript_control.dragging)
                )
            ):
                return
            active = tab is view.tabs[view.active_tab]
            moved_while_loading = (
                active and view.transcript_window.vertical_scroll != request_scroll
            )
            current_anchor = view.history_pager.anchor(tab) if moved_while_loading else anchor
            messages = visible_window(value, tab.get("messages", ()))
            tab.update(messages=messages, history_window=value["history_window"])
            if active:
                view.messages = messages
                # A late history page must preserve where the user is now, not
                # jump back to the viewport that originally triggered the read.
                tab["history_anchor"] = current_anchor
                view.transcript_control.clear_selection()
                if not moved_while_loading:
                    view.follow_output = direction == "bottom"
            view.app.invalidate()

        def failed(reason):
            if tab.get("history_read") is token:
                tab.pop("history_read", None)
                view.status = view.tr("Arşiv okunamadı: ") + str(reason)
                view.app.invalidate()

        view._read_in_background(
            lambda: archive.conversation_window(key, remote, start=start),
            finished,
            failed,
            session_bound=False,
        )
        return True
