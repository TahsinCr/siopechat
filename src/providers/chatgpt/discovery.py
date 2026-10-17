"""Web response normalization, isolated from transport and application state."""

from typing import Any

from ..models import Choice, FeatureState, Model, ModelCatalog
from .errors import ChatGPTWebError


def model_catalog(raw: Any) -> ModelCatalog:
    if not isinstance(raw, dict) or not isinstance(raw.get("models"), list):
        raise ChatGPTWebError("Model catalog response changed: expected models list")
    models: dict[str, Model] = {}
    categories = raw.get("categories")
    if categories is not None and not isinstance(categories, list):
        raise ChatGPTWebError("Model picker response changed: expected categories list")
    picker: dict[str, dict[str, Any]] = {}
    for category in categories or []:
        if not isinstance(category, dict):
            raise ChatGPTWebError("Model picker category changed")
        supported = category.get("supported_models", [])
        if not isinstance(supported, list) or any(not isinstance(slug, str) for slug in supported):
            raise ChatGPTWebError("Model picker supported_models changed")
        for slug in supported + [category.get("default_model")]:
            if isinstance(slug, str) and slug:
                picker.setdefault(slug, category)
    for entry in raw["models"]:
        if not isinstance(entry, dict):
            raise ChatGPTWebError("Model catalog response contains an invalid model")
        slug = entry.get("slug") or entry.get("id")
        if not isinstance(slug, str) or not slug:
            raise ChatGPTWebError("Model catalog response contains a model without an ID")
        if (
            any(entry.get(key) is False for key in ("enabled", "is_enabled", "is_available"))
            or entry.get("disabled") is True
        ):
            continue
        efforts: dict[str, Choice] = {}
        values = entry.get("thinking_efforts", [])
        if not isinstance(values, list):
            raise ChatGPTWebError(f"Effort catalog response changed for {slug}")
        default_effort = entry.get("default_thinking_effort") or entry.get("default_effort")
        for value in values:
            item = {"thinking_effort": value} if isinstance(value, str) else value
            if not isinstance(item, dict):
                raise ChatGPTWebError(f"Invalid effort choice for {slug}")
            effort = item.get("thinking_effort") or item.get("id") or item.get("effort")
            if not isinstance(effort, str) or not effort:
                raise ChatGPTWebError(f"Effort choice has no ID for {slug}")
            title = (
                item.get("full_label")
                or item.get("title")
                or item.get("short_label")
                or item.get("name")
            )
            efforts[effort] = Choice(effort, title if isinstance(title, str) and title else effort)
            if item.get("is_default") is True or item.get("default") is True:
                default_effort = effort
        visible = not slug.casefold().endswith("-wm")
        visible = (
            visible
            and entry.get("is_hidden") is not True
            and entry.get("show_in_model_picker") is not False
        )
        if categories is not None:
            visible = visible and (
                slug in picker or (not categories and slug == raw.get("default_model_slug"))
            )
        category = picker.get(slug, {})
        version = category.get("model_version")
        lane = category.get("model_lane")
        label = {
            "auto": "Auto",
            "instant": "Instant",
            "thinking": "Thinking",
            "thinking_mini": "Thinking mini",
        }.get(lane, category.get("human_category_name"))
        chat_title = (
            f"GPT-{version} {label}"
            if isinstance(version, str) and isinstance(label, str)
            else None
        )
        models[slug] = Model(
            slug,
            str(entry.get("title") or slug),
            tuple(efforts.values()),
            default_effort if default_effort in efforts else None,
            dict(entry),
            visible,
            chat_title,
        )
    default = raw.get("default_model_slug") or raw.get("default_model")
    if not isinstance(default, str) or default not in models:
        default = next(
            (m.id for m in models.values() if m.metadata.get("is_default") is True), None
        )
    return ModelCatalog(FeatureState.AVAILABLE, tuple(models.values()), default)


def page(raw: Any) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise ChatGPTWebError("Project response changed: expected items list")
    if any(not isinstance(item, dict) for item in raw["items"]):
        raise ChatGPTWebError("Project response contains an invalid item")
    cursor = raw.get("cursor") or raw.get("next_cursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise ChatGPTWebError("Project response contains an invalid cursor")
    return raw["items"], cursor


def project(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        raise ChatGPTWebError("Project response changed: expected an object")
    envelope = raw
    gizmo = raw
    for _ in range(3):
        nested = gizmo.get("gizmo")
        if not isinstance(nested, dict):
            break
        envelope, gizmo = gizmo, nested
    project_id = gizmo.get("id")
    if not isinstance(project_id, str) or not project_id:
        raise ChatGPTWebError("Project response contains a project without an ID")
    if not project_id.startswith("g-p-") and gizmo.get("gizmo_type") != "snorlax":
        return None
    display = gizmo.get("display") or {}
    if not isinstance(display, dict):
        raise ChatGPTWebError("Project display response changed")
    files = envelope.get("files", gizmo.get("files", []))
    if not isinstance(files, list) or any(not isinstance(f, dict) for f in files):
        raise ChatGPTWebError("Project resource response changed")
    return {
        "id": project_id,
        "name": str(display.get("name") or gizmo.get("name") or project_id),
        "description": display.get("description") or "",
        "instructions": gizmo.get("instructions") or "",
        "resources": files,
        "metadata": dict(raw),
    }


def project_id(raw: dict[str, Any]) -> str | None:
    value = raw.get("project_id") or raw.get("gizmo_id") or raw.get("conversation_template_id")
    return value if isinstance(value, str) and value.startswith("g-p-") else None
