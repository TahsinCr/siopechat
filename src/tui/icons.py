"""Canonical, single-cell text glyphs used by the terminal interface."""


class Icon:
    """Use monochrome text symbols to avoid emoji presentation and width drift."""

    # Message roles and actions
    USER = "●"
    ASSISTANT = "◆"
    THINKING = "◇"
    COPY = "⧉"
    EDIT = "✎"
    RETRY = "↻"
    BRANCH = "⎇"
    SOURCES = "↗"
    DELETE = "⌫"
    ATTACHMENT = "▤"

    # Navigation and controls
    ADD = "+"
    MINUS = "-"
    DROPDOWN = "▾"
    COLLAPSED = "▸"
    CLOSE = "×"
    CLOSE_ALL = "⊗"
    SEARCH = "⌕"
    FILTER = "≡"
    MENU = "≣"
    SPLIT = "◫"
    SETTINGS = "⚙"
    POWER = "⏻"
    STOP = "■"
    UP = "↑"
    DOWN = "↓"
    LEFT = "←"
    RIGHT = "→"
    BACK = "‹"
    FORWARD = "›"
    NEXT = "↳"
    INFO = "ⓘ"

    # State markers and activity
    RUNNING = "●"
    COMPLETE = "✓"
    IDLE = "◌"
    DISABLED = "○"
    PROVIDER = "◈"
    SPINNER = "|/-\\"


ROLE_MARKERS = {
    "user": Icon.USER,
    "assistant": Icon.ASSISTANT,
    "reasoning": Icon.THINKING,
}
