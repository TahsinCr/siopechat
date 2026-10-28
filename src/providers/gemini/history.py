"""Native Gemini read contracts; parsing has no session or frontend dependency."""

import re

from ..messages import actions_for, sources_from
from ..models import Content, Conversation, Message, Resource
from .errors import GeminiCompatibilityError, GeminiError
from .media import _field, candidate_files, resolve_image_links

_CITATION_MARKER = re.compile(r"\[cite:\s*([\d,\s]+)\]")
_CITATION_NUMBER = re.compile(r"\d+")


def nested(value, *path, default=None):
    try:
        for key in path:
            value = value[key]
        return value
    except (IndexError, KeyError, TypeError):
        return default


def timestamp(value):
    if isinstance(value, list) and len(value) == 2 and all(type(x) in (int, float) for x in value):
        return value[0] + value[1] / 1e9
    return None


def candidate_metadata(candidate, activity=None):
    """Extract published citations separately from the candidate's thought summary."""
    groups = _field(nested(candidate, 12), 43, [])
    citations = {}
    for group in groups if isinstance(groups, list) else ():
        marker = nested(group, 0, 0)
        entries = nested(group, 1)
        if not isinstance(marker, str) or not isinstance(entries, list):
            continue
        match = _CITATION_MARKER.search(marker)
        if match is None:
            continue
        for number, entry in zip(_CITATION_NUMBER.findall(match.group(1)), entries):
            citation_id = int(number)
            if citation_id in citations:
                continue
            url = nested(entry, 3, 0, 1)
            if isinstance(url, str):
                citations[citation_id] = {
                    "url": url,
                    "title": nested(entry, 3, 0, 2),
                }
    ordered = [citations[key] for key in sorted(citations)]
    _, activity_sources = activity if activity is not None else published_activity(candidate)
    return {"sources": sources_from({"sources": ordered + list(activity_sources)})}


def published_activity(candidate):
    """Return provider-published search steps and sources from Gemini history."""
    activities = nested(candidate, 37, 1, default=[])
    if not isinstance(activities, list):
        return (), ()
    steps, seen_steps, sources = [], set(), []
    for activity in activities[:64]:
        if not isinstance(activity, list):
            continue
        for index in (1, 5):
            label = nested(activity, index)
            if isinstance(label, str):
                label = " ".join(label.split())
                if label and label not in seen_steps:
                    seen_steps.add(label)
                    steps.append(label[:500])
        results = nested(activity, 7, default=[])
        if not isinstance(results, list):
            continue
        for result in results[:128]:
            url = nested(result, 1)
            title = nested(result, 2)
            if isinstance(url, str):
                sources.append({"url": url, "title": title})
    return tuple(steps), sources_from({"sources": sources})


def published_thoughts(candidate, steps=None, previous=""):
    """Combine Gemini's public thought summary with visible search activity."""
    if steps is None:
        steps, _ = published_activity(candidate)
    summary = nested(candidate, 37, 0, 0)
    parts = [summary.strip()] if isinstance(summary, str) and summary.strip() else []
    parts.extend(step for step in steps if step not in parts)
    return "\n".join(parts) or previous


def conversations(pages, origin):
    result = {}
    for page in pages:
        if page == []:
            continue
        rows = nested(page, 2)
        if not isinstance(rows, list):
            raise GeminiCompatibilityError(
                "Gemini sohbet listesi sözleşmesi değişti; eşitleme tamamlanmadı"
            )
        for row in rows:
            cid, title = nested(row, 0), nested(row, 1)
            if not isinstance(cid, str) or not cid.startswith("c_"):
                continue
            result[cid] = Conversation(
                origin(cid),
                title or "Gemini sohbeti",
                project_id=(
                    nested(row, 7)
                    if isinstance(nested(row, 7), str) and nested(row, 7).startswith("notebooks/")
                    else None
                ),
                metadata={
                    "native": row,
                    "updated_at": timestamp(nested(row, 5)),
                    "project_membership_known": len(row) > 7,
                },
            )
    return tuple(result.values())


def history(data, origin, cid):
    turns = nested(data, 0)
    if not isinstance(turns, list):
        raise GeminiError("Gemini geçmişi henüz okunamıyor; kayıtlı geçmiş korunuyor")
    messages = []
    for turn_index, turn in enumerate(reversed(turns)):
        rid = nested(turn, 0, 1)
        if not isinstance(rid, str):
            raise GeminiCompatibilityError("Gemini geçmişinde yanıt kimliği eksik")
        user = nested(turn, 2, 0, 0)
        if isinstance(user, str) and user:
            messages.append(
                Message(
                    rid + ":user",
                    "user",
                    (Content("text", user),),
                    metadata={"turn_id": rid},
                    actions=actions_for("user"),
                )
            )
        candidates = nested(turn, 3, 0, default=[])
        if not candidates:
            continue
        candidate = candidates[0]
        text = nested(candidate, 1, 0, default="")
        # Published thinking summaries only; never infer hidden reasoning.
        activity = published_activity(candidate)
        steps = activity[0]
        thoughts = published_thoughts(candidate, steps)
        if thoughts:
            messages.append(
                Message(
                    rid + ":model:thoughts",
                    "assistant",
                    (Content("text", thoughts),),
                    channel="analysis",
                    metadata={"turn_id": rid, "published_activity": bool(steps)},
                )
            )
        images = candidate_files(candidate)
        text = resolve_image_links(text if isinstance(text, str) else "", images)
        if text or images:
            messages.append(
                Message(
                    rid + ":model",
                    "assistant",
                    (Content("text", text),)
                    + tuple(
                        Content(
                            "resource",
                            resource=Resource(
                                image["alias"], image["name"], image.get("media_type"), image["url"]
                            ),
                        )
                        for image in images
                    ),
                    metadata={
                        "native_candidate": candidate,
                        "turn_id": rid,
                        "candidate_id": nested(candidate, 0),
                        **candidate_metadata(candidate, activity),
                        "files": tuple(
                            {
                                "id": image["alias"],
                                "name": image["name"],
                                "remote_url": image["url"],
                                "media_type": image.get("media_type"),
                            }
                            for image in images
                        ),
                    },
                    actions=actions_for(
                        "assistant", latest=turn_index == len(turns) - 1, branch=True
                    ),
                )
            )
    return Conversation(
        origin(cid),
        "Gemini sohbeti",
        tuple(messages),
        metadata={"history_partial": len(turns) >= 1000, "title_unavailable": True},
    )
