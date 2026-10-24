"""Synthetic provider responses shared by offline tests."""

CATALOG = {
    "default_model_slug": "account-thinking",
    "models": [
        {
            "slug": "account-thinking",
            "title": "Account Thinking",
            "thinking_efforts": [
                {"thinking_effort": "standard", "full_label": "Standard", "is_default": True},
                {"thinking_effort": "extended", "title": "Extended"},
            ],
        },
        {"slug": "account-instant", "title": "Account Instant"},
        {"slug": "locked", "is_available": False},
    ],
}


def notebook():
    source = [None] * 22
    source[1], source[2], source[5] = 11, "Research notes", "notebooks/abcdef/sources/source1"
    detail = [None] * 15
    detail[0], detail[1], detail[10], detail[12] = (
        "My notebook",
        "Notebook description",
        [[source], []],
        [2, None, None, None, 3],
    )
    return ["notebooks/abcdef", detail, ["Use citations"]]
