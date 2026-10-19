"""Account-entitled model selector, separate from global model definitions."""

from ..errors import ProviderError


def selector_catalog(data):
    config = next((x for x in data.get("model_selector_config", []) if x.get("id") == "chat"), None)
    state = next((x for x in data.get("model_selector_state", []) if x.get("id") == "chat"), {})
    if not config or not isinstance(config.get("models"), list):
        raise ProviderError("Claude hesap model seçim sözleşmesi değişmiş")
    states = {x["id"]: x.get("thinking", {}) for x in state.get("thinking_by_model", [])}
    if state.get("model"):
        states[state["model"]] = state.get("thinking", {})
    models = []
    for entry in config["models"]:
        if (
            not isinstance(entry.get("id"), str)
            or entry.get("disabled")
            or entry.get("disabled_reason")
            or entry.get("inactive")
            or entry.get("is_available") is False
        ):
            continue
        thinking = entry.get("thinking") or {}
        efforts = [x for x in thinking.get("effort_options", []) if not x.get("disabled")]
        modes = [x for x in thinking.get("mode_options", []) if not x.get("disabled")]
        selections, choices = {}, []
        selected = states.get(entry["id"], {})
        effort_ids = {x["id"] for x in efforts}
        mode_ids = {x["id"] for x in modes}
        default_effort = selected.get("effort")
        if default_effort not in effort_ids:
            default_effort = next(
                (x["id"] for x in efforts if x.get("recommended")),
                efforts[0]["id"] if efforts else None,
            )
        default_mode = selected.get("mode")
        if default_mode not in mode_ids:
            default_mode = modes[0]["id"] if modes else None
        for effort in efforts or [None]:
            for mode in modes or [None]:
                if effort is None and mode is None:
                    continue
                eid, mid = effort["id"] if effort else None, mode["id"] if mode else None
                identity = eid if eid and len(modes) <= 1 else f"{eid}:{mid}" if eid else mid
                title = effort.get("name", eid) if effort else mode.get("name", mid)
                if effort and len(modes) > 1:
                    title += " · " + mode.get("name", mid)
                payload = {}
                if eid:
                    payload["effort"] = eid
                if mid:
                    payload["thinking_mode"] = mid
                selections[identity] = payload
                choices.append(
                    {
                        "id": identity,
                        "title": title,
                        "selected": (eid == default_effort or not effort)
                        and (mid == default_mode or not mode),
                    }
                )
        models.append(
            {
                "id": entry["id"],
                "title": entry.get("name") or entry["id"],
                "efforts": choices,
                "selections": selections,
                "native": entry,
            }
        )
    if not models:
        raise ProviderError("Claude bu hesap için kullanılabilir sohbet modeli sunmadı")
    preferred = state.get("model")
    return {
        "models": models,
        "default_model": preferred
        if any(x["id"] == preferred for x in models)
        else models[0]["id"],
        "source": "model_selector/chat",
    }
