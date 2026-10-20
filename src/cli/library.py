"""Library rows, filtering and ordering for projects, conversations and workspaces."""

from datetime import datetime
from typing import Any

from accounts import origin

SORT_OPTIONS = [
    ("time_desc", "Zaman · en yeni önce"),
    ("time_asc", "Zaman · en eski önce"),
    ("name_asc", "İsim · A–Z"),
    ("name_desc", "İsim · Z–A"),
]


def modified_time(record):
    """Read declared timestamps only; unknown dates remain unknown."""
    metadata = record.get("metadata") or {}
    native = metadata.get("native") or {}
    sources = (record, metadata, native) if isinstance(native, dict) else (record, metadata)
    for source in sources:
        for key in ("updated_at", "update_time", "modified_at", "created_at", "create_time"):
            value = source.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                except (ValueError, OverflowError):
                    pass
    return None


def sort_records(records, mode="time_desc"):
    values = sorted(
        records,
        key=lambda x: (str(x.get("title") or x.get("name") or "").casefold(), str(x.get("id", ""))),
    )
    if mode.startswith("name_"):
        return list(reversed(values)) if mode == "name_desc" else values
    dated, undated = [], []
    for value in values:
        stamp = modified_time(value)
        (undated if stamp is None else dated).append(value)
    return sorted(dated, key=modified_time, reverse=mode != "time_asc") + undated


def build_sidebar_rows(view) -> None:
    query = view.sidebar_search.text.strip().casefold()
    active_keys = (
        {
            item["key"]
            for item in view.account_service.items()
            if item.get("enabled", True)
            and (view.account_filter is None or item["key"] in view.account_filter)
        }
        if view.account_service and hasattr(view.account_service, "items")
        else None
    )
    live_items = {item.get("id"): dict(item) for item in view.sidebar_items}
    for tab in view.tabs + view.background_tabs:
        if not tab.get("busy") and not any(tab is item for item in view.background_tabs):
            continue
        key = tab.get("account_key", view.account_key)
        identifier = tab.get("conversation_id") or "background:" + str(id(tab))
        item = live_items.setdefault(identifier, {"id": identifier})
        item.update(
            title=tab.get("title") or view.tr("New conversation"),
            provider_id=key.split("/")[0],
            project_id=(tab.get("project") or {}).get("id"),
            live_tab=tab,
        )
    conversations = [
        item
        for item in live_items.values()
        if item.get("kind") != "workspace"
        and (
            active_keys is None
            or (
                item["live_tab"].get("account_key", view.account_key)
                if item.get("live_tab") is not None
                else origin(item.get("id"), view.account_key)[0]
            )
            in active_keys
        )
        and (
            view.provider_filter is None
            or item.get("provider_id", "chatgpt") in view.provider_filter
        )
    ]
    workspaces = [item for item in view.sidebar_items if item.get("kind") == "workspace"]
    projects: dict[str, list[dict[str, Any]]] = {
        key: []
        for key, project in view.project_items.items()
        if (active_keys is None or origin(key, view.account_key)[0] in active_keys)
        and (
            view.provider_filter is None
            or project.get("provider_id", origin(key)[0].split("/")[0]) in view.provider_filter
        )
    }
    unassigned: list[dict[str, Any]] = []
    for item in conversations:
        project = item.get("project")
        project_id = item.get("project_id")
        if isinstance(project, dict):
            project_id = project.get("id") or project_id
        if isinstance(project_id, str) and project_id:
            projects.setdefault(project_id, []).append(item)
        else:
            unassigned.append(item)

    def matches(item: dict[str, Any]) -> bool:
        title = str(item.get("title") or item.get("name") or "").casefold()
        return not query or query in title or query in str(item.get("id", "")).casefold()

    rows: list[dict[str, Any]] = []
    rows.append(
        {
            "kind": "section",
            "id": "projects",
            "title": view.tr("Projeler"),
            "count": len(projects),
            "collapsed": "section:projects" in view.sidebar_collapsed,
        }
    )
    if "section:projects" not in view.sidebar_collapsed:
        project_order = sort_records(
            [dict(view.project_items.get(pid, {}), id=pid) for pid in projects],
            view.library_sort,
        )
        for project_record in project_order:
            project_id = project_record["id"]
            items = sort_records(projects[project_id], view.library_sort)
            project_meta = items[0].get("project") if items else {}
            project_meta = project_meta if isinstance(project_meta, dict) else {}
            remote = view.project_items.get(project_id, {})
            project_name = str(
                remote.get("name")
                or (items[0].get("project_name") if items else None)
                or project_meta.get("name")
                or view.tr("Proje")
            )
            visible_items = [item for item in items if matches(item)]
            if query and query in project_name.casefold():
                visible_items = items
            if query and query not in project_name.casefold() and not visible_items:
                continue
            collapsed = f"project:{project_id}" in view.sidebar_collapsed
            rows.append(
                {
                    "kind": "project",
                    "id": project_id,
                    "title": project_name,
                    "provider_id": remote.get("provider_id") or origin(project_id)[0].split("/")[0],
                    "count": len(items),
                    "collapsed": collapsed,
                }
            )
            if not collapsed:
                if project_id in view.project_chats_loading:
                    rows.append({"kind": "message", "title": view.tr("Sohbetler yükleniyor…")})
                if project_id in view.project_chat_errors:
                    rows.append({"kind": "message", "title": view.project_chat_errors[project_id]})
                rows.extend(view._chat_row(item, nested=True) for item in visible_items)
                if view.project_chat_cursors.get(project_id):
                    rows.append(
                        {
                            "kind": "project_more",
                            "id": project_id,
                            "title": view.tr("Daha fazla sohbet…"),
                        }
                    )
        if not projects:
            rows.append(
                {
                    "kind": "message",
                    "title": view.tr("Projeler yükleniyor…")
                    if view.projects_loading
                    else view.project_error or view.tr("Henüz proje yok"),
                }
            )
        elif view.project_error:
            rows.append(
                {"kind": "message", "title": view.tr("Hata: {0}").format(view.project_error)}
            )
        if view.project_cursor:
            rows.append({"kind": "projects_more", "title": view.tr("Daha fazla proje…")})

    visible_chats = sort_records([item for item in unassigned if matches(item)], view.library_sort)
    rows.append(
        {
            "kind": "section",
            "id": "chats",
            "title": view.tr("Sohbetler"),
            "count": len(unassigned),
            "collapsed": "section:chats" in view.sidebar_collapsed,
        }
    )
    if "section:chats" not in view.sidebar_collapsed:
        if view.sidebar_error:
            rows.append(
                {
                    "kind": "message",
                    "title": view.tr("Hata: {0}").format(view.sidebar_error[:80]),
                }
            )
        rows.extend(view._chat_row(item) for item in visible_chats)
        if view.history_cursor:
            rows.append(
                {
                    "kind": "history_more",
                    "title": view.tr("Yükleniyor…")
                    if view.history_loading
                    else view.tr("Daha eski sohbetleri yükle…"),
                }
            )
        if not visible_chats and unassigned:
            rows.append({"kind": "message", "title": view.tr("Aramayla eşleşen sohbet yok")})

    layout_start = len(rows)
    visible_workspaces = [item for item in workspaces if matches(item)]
    rows.append(
        {
            "kind": "section",
            "id": "layouts",
            "title": view.tr("Düzenler"),
            "count": len(workspaces),
            "collapsed": "section:layouts" in view.sidebar_collapsed,
        }
    )
    if "section:layouts" not in view.sidebar_collapsed:
        rows.extend(
            {
                "kind": "workspace",
                "id": item.get("id"),
                "title": str(item.get("name") or view.tr("Adsız çalışma alanı")),
            }
            for item in visible_workspaces
        )
        if view.workspace_error:
            rows.append({"kind": "message", "title": view.tr("Düzen eklentisi kullanılamıyor")})
        elif not workspaces:
            rows.append({"kind": "message", "title": view.tr("Henüz düzen yok")})
    if not getattr(view, "layout_enabled", True):
        rows = rows[:layout_start]
    if view.account_service and not projects and view.provider_id == "gemini":
        rows = [
            row for row in rows if not (row["kind"] == "section" and row.get("id") == "projects")
        ]
    view.sidebar_rows = rows
    view.sidebar_index = min(max(0, view.sidebar_index), max(0, len(rows) - 1))
