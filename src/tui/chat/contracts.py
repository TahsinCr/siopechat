"""Public callable and transcript types shared by chat components."""

from typing import Any, Callable

Message = dict[str, str]
LoadConversation = Callable[[str], dict[str, Any]]
ListConversations = Callable[[], list[dict[str, Any]]]
SendPrompt = Callable[
    [
        str | None,
        str,
        list[dict[str, Any]],
        Callable[[dict[str, Any]], None],
        Callable[[Any], None],
        str,
        str,
    ],
    dict[str, Any],
]
UploadImage = Callable[[bytes, str, str, int, int], dict[str, Any]]
RenameConversation = Callable[[str, str], None]
SetActiveConversation = Callable[[str | None], None]
