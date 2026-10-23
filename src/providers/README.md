# SiopeChat providers

An independent Python library for the ChatGPT, Gemini and Claude **web chat** services.
These unofficial clients use a saved web session. They do not use paid API keys,
Codex or Claude Code subscriptions. The services can change their private protocols;
available models and features depend on the authenticated account.

This directory is a package root. It can be copied into its own repository or used
as a submodule without the SiopeChat terminal, database, settings or extensions.

```sh
python -m pip install .
```

Python 3.10+ supports ChatGPT and Claude. The Gemini transport requires Python
3.11+. ChatGPT request signing also requires Node.js 22+ on PATH. Importing the
library or configuring an account does not open a browser or access the network.

## Using an account

```python
import asyncio
from providers import ProviderClient, SendRequest, TextDelta, ReplaceText


async def main():
    async with ProviderClient() as client:
        account = client.connect(
            "claude", "personal", credential_file="/private/claude-session.json"
        )
        await account.authenticate()
        catalog = await account.models()
        model = catalog.default_chat_model
        request = SendRequest(
            provider_id="claude",
            account_id="personal",
            chat_id="my-local-draft",
            prompt="Hello",
            model=model.id if model else None,
        )
        async for event in client.stream(request):
            if isinstance(event, (TextDelta, ReplaceText)):
                print(event.channel, event.text)


asyncio.run(main())
```

The caller supplies credential paths and imports a valid session through its own
login flow or the profile helpers in `providers.profiles`. No application config is
read. For ChatGPT, `create_provider` also accepts an existing `BrowserSession`, or
explicit `cache_directory` and `profile_directory` paths. Each account has its own
adapter, connections and concurrency controls.

`client.account(provider_id, account_id)` returns the complete `Backend` contract:
session status and authentication, model catalogs, paginated chats and projects,
chat history, rename/delete, project editing and sources, file uploads and download
resolution. Use `describe()` and `project_options()` to discover supported actions.
Unsupported actions raise `FeatureUnavailable`; an empty list does not indicate
unsupported functionality.

To continue a chat, set `SendRequest.conversation_id` to the chat's
`origin.remote_id`. For a project, also set `project_id` to its remote ID. Local
`chat_id` is a caller-owned stable identity. Keep the provider and account identity
from `Origin` with all stored records; remote IDs alone are not globally unique.

Streams yield `TextDelta`, `ReplaceText` and `ConversationBound`. Replacement events
replace the corresponding channel's accumulated text. Published thinking summaries
use the analysis channel. Cancellation closes the transport; requests are never
automatically resent after an ambiguous send failure. Closing a client cancels its
active streams and closes all accounts, while keeping credential files intact.

Applications own persistence and synchronization. `get_chat()` returns the history
available from the provider; the library does not invent remote message pagination
where a web transport has none. The local SQL paging used by SiopeChat belongs to
its archive extension.

Offline API tests run after installation with:

```sh
python -m unittest discover -s tests
```

Gemini uses the bundled native HTTPS implementation. All three backends accept caller-owned credential storage through `providers.session.CredentialStore` (`load`, `save`, `exists`, `delete`); file paths remain supported. When creating ChatGPT with a credential store, supply `cache_directory` for its request-signing resources. Database implementations belong to the host application.
