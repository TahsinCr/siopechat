"""Native Gemini read contracts; parsing has no session or frontend dependency."""

from ..errors import ProviderError
from ..models import Content, Conversation, Message, Resource
from .media import candidate_images, render_images


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


def conversations(pages, origin):
    result = {}
    for page in pages:
        if page == []:
            continue
        rows = nested(page, 2)
        if not isinstance(rows, list):
            raise ProviderError("Gemini sohbet listesi sözleşmesi değişti; eşitleme tamamlanmadı")
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
        raise ProviderError("Gemini geçmişi henüz okunamıyor; kayıtlı geçmiş korunuyor")
    messages = []
    for turn in reversed(turns):
        rid = nested(turn, 0, 1)
        if not isinstance(rid, str):
            raise ProviderError("Gemini geçmişinde yanıt kimliği eksik")
        user = nested(turn, 2, 0, 0)
        if isinstance(user, str) and user:
            messages.append(Message(rid + ":user", "user", (Content("text", user),)))
        candidates = nested(turn, 3, 0, default=[])
        if not candidates:
            continue
        candidate = candidates[0]
        text = nested(candidate, 1, 0, default="")
        # Published thinking summaries only; never infer hidden reasoning.
        thoughts = nested(candidate, 37, 0, 0)
        if isinstance(thoughts, str) and thoughts:
            messages.append(
                Message(
                    rid + ":model:thoughts",
                    "assistant",
                    (Content("text", thoughts),),
                    channel="analysis",
                )
            )
        images = candidate_images(candidate)
        text = render_images(text if isinstance(text, str) else "", images)
        if text:
            messages.append(
                Message(
                    rid + ":model",
                    "assistant",
                    (Content("text", text),)
                    + tuple(
                        Content(
                            "image",
                            resource=Resource(
                                image["alias"], image["name"], image.get("media_type"), image["url"]
                            ),
                        )
                        for image in images
                    ),
                    metadata={"native_candidate": candidate},
                )
            )
    return Conversation(
        origin(cid),
        "Gemini sohbeti",
        tuple(messages),
        metadata={"history_partial": len(turns) >= 1000, "title_unavailable": True},
    )
