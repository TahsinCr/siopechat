"""An explicitly configured account for existing provider integration scenarios."""

from bootstrap import create_application


def configured_application(*args, **kwargs):
    api = create_application(*args, **kwargs)
    if not api.settings.snapshot()["accounts"]["items"]:
        api.settings.update(
            "accounts",
            {
                "active": "chatgpt/default",
                "items": [
                    {"provider": "chatgpt", "id": "default", "label": "ChatGPT", "port": 9222}
                ],
            },
        )
    return api
